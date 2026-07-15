package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"

	_ "github.com/lib/pq"
	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
	"github.com/sudebaker/journalist-mcp/internal/config"
	"github.com/sudebaker/journalist-mcp/internal/executor"
	"github.com/sudebaker/journalist-mcp/internal/health"
	"github.com/sudebaker/journalist-mcp/internal/prompts"
	"github.com/sudebaker/journalist-mcp/internal/session"
	"github.com/sudebaker/journalist-mcp/internal/tracing"
	"github.com/sudebaker/journalist-mcp/internal/transport"
)

const (
	Version       = "0.1.0"
	maxArgsSize   = 1 << 20
	maxArgsSizeMB = 1
)

func main() {
	configPath := flag.String("config", "configs/config.yaml", "Path to configuration file")
	debug := flag.Bool("debug", false, "Enable debug logging")
	flag.Parse()

	zerolog.TimeFieldFormat = zerolog.TimeFormatUnix
	if *debug {
		zerolog.SetGlobalLevel(zerolog.DebugLevel)
		log.Logger = log.Output(zerolog.ConsoleWriter{Out: os.Stderr})
	} else {
		zerolog.SetGlobalLevel(zerolog.InfoLevel)
	}

	log.Info().
		Str("version", Version).
		Str("config", *configPath).
		Msg("Starting journalist-mcp")

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to load configuration")
	}

	if err := config.Validate(cfg); err != nil {
		log.Fatal().Err(err).Msg("Configuration validation failed")
	}

	deps := health.BuildDependencies(cfg)

	var db *sql.DB
	databaseURL := os.Getenv("DATABASE_URL")
	if databaseURL != "" {
		if !containsParam(databaseURL, "sslmode") {
			separator := "?"
			if containsChar(databaseURL, '?') {
				separator = "&"
			}
			databaseURL = databaseURL + separator + "sslmode=disable"
		}
		var dbErr error
		db, dbErr = sql.Open("postgres", databaseURL)
		if dbErr != nil {
			log.Error().Err(dbErr).Msg("Failed to open PostgreSQL connection for health checks")
			db = nil
		} else {
			db.SetMaxOpenConns(2)
			db.SetMaxIdleConns(1)
			log.Info().Msg("PostgreSQL health check connection initialized")
		}
	}

	if os.Getenv("REDIS_URL") == "" {
		filtered := make([]health.DependencyCheck, 0, len(deps))
		for _, d := range deps {
			if d.Name != "redis" {
				filtered = append(filtered, d)
			}
		}
		deps = filtered
	}

	healthChecker := health.NewChecker(cfg, nil, db, deps)
	log.Info().Int("dependencies", len(deps)).Msg("Health checker initialized")

	log.Info().
		Str("server_name", cfg.Server.Name).
		Int("port", cfg.Server.Port).
		Int("tools_count", len(cfg.Tools)).
		Msg("Configuration loaded")

	tracer := tracing.NewTracer(cfg.Server.Name)
	log.Debug().Msg("Distributed tracing initialized")

	sessionStore := session.New()

	hooks := &server.Hooks{}
	hooks.AddOnRegisterSession(func(ctx context.Context, sess server.ClientSession) {
		log.Debug().Str("session_id", sess.SessionID()).Msg("Session registered")
	})
	hooks.AddOnUnregisterSession(func(ctx context.Context, sess server.ClientSession) {
		sessionStore.Delete(sess.SessionID())
		log.Debug().Str("session_id", sess.SessionID()).Msg("Session unregistered, user_id removed")
	})
	hooks.AddAfterInitialize(func(ctx context.Context, id any, message *mcp.InitializeRequest, result *mcp.InitializeResult) {
		if message == nil || message.Params.Capabilities.Experimental == nil {
			return
		}
		if userID, ok := message.Params.Capabilities.Experimental["user_id"].(string); ok {
			if sess := server.ClientSessionFromContext(ctx); sess != nil {
				sessionStore.Set(sess.SessionID(), userID)
				log.Info().Str("session_id", sess.SessionID()).Str("user_id", userID).Msg("Session associated with user")
			}
		}
	})

	exec := executor.NewWithTracerSessionStoreAndPool(cfg, tracer, sessionStore, 5)

	mcpServer := server.NewMCPServer(
		cfg.Server.Name,
		Version,
		server.WithToolCapabilities(true),
		server.WithPromptCapabilities(true),
		server.WithLogging(),
		server.WithRecovery(),
		server.WithHooks(hooks),
	)

	for _, toolCfg := range cfg.Tools {
		if err := executor.ValidateToolConfig(&toolCfg); err != nil {
			log.Fatal().
				Err(err).
				Str("tool", toolCfg.Name).
				Msg("Invalid tool configuration")
		}
		registerTool(mcpServer, exec, toolCfg)
	}

	if len(cfg.Prompts) > 0 {
		log.Info().Int("count", len(cfg.Prompts)).Msg("Registering prompts from configuration")
		prompts.RegisterPrompts(mcpServer, cfg.Prompts)
	} else {
		log.Debug().Msg("No prompts configured")
	}

	log.Info().Msg("Server started with static configuration")

	mcpTransport := transport.NewMCPServer(mcpServer, transport.MCPConfig{
		Host:           cfg.Server.Host,
		Port:           cfg.Server.Port,
		ServerName:     cfg.Server.Name,
		Version:        Version,
		Tools:          cfg.Tools,
		RateLimitRPS:   cfg.Server.RateLimitRPS,
		RateLimitBurst: cfg.Server.RateLimitBurst,
		AllowedOrigins: cfg.Server.AllowedOrigins,
		Tracer:         tracer,
		Upload:         cfg.Upload,
		FilesDir:       filepath.Join(cfg.Execution.WorkingDir, cfg.Execution.ReportsDir),
		HealthChecker:  healthChecker,
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(sigChan)

	go func() {
		if err := 	mcpTransport.Start(); err != nil {
			log.Error().Err(err).Msg("Server error")
			cancel()
		}
	}()

	select {
	case sig := <-sigChan:
		log.Info().Str("signal", sig.String()).Msg("Received shutdown signal")
	case <-ctx.Done():
	}

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), cfg.Server.ShutdownTimeout)
	defer shutdownCancel()

	log.Info().
		Dur("timeout", cfg.Server.ShutdownTimeout).
		Msg("Shutting down server")

	if err := 	mcpTransport.Shutdown(shutdownCtx); err != nil {
		log.Error().Err(err).Msg("Error during shutdown")
	}

	exec.Close()

	log.Info().Msg("Server stopped")
}

func truncateClientError(msg string) string {
	if len(msg) > 500 {
		msg = msg[:500] + "... (truncated)"
	}
	return msg
}

func registerTool(mcpServer *server.MCPServer, exec *executor.Executor, toolCfg config.ToolConfig) {
	inputSchema := buildInputSchema(toolCfg)

	toolOpts := []mcp.ToolOption{
		mcp.WithDescription(toolCfg.Description),
	}

	if toolCfg.ReadOnlyHint != nil {
		toolOpts = append(toolOpts, mcp.WithReadOnlyHintAnnotation(*toolCfg.ReadOnlyHint))
	}
	if toolCfg.DestructiveHint != nil {
		toolOpts = append(toolOpts, mcp.WithDestructiveHintAnnotation(*toolCfg.DestructiveHint))
	}
	if toolCfg.IdempotentHint != nil {
		toolOpts = append(toolOpts, mcp.WithIdempotentHintAnnotation(*toolCfg.IdempotentHint))
	}
	if toolCfg.OpenWorldHint != nil {
		toolOpts = append(toolOpts, mcp.WithOpenWorldHintAnnotation(*toolCfg.OpenWorldHint))
	}

	tool := mcp.NewTool(toolCfg.Name, toolOpts...)

	if inputSchema != nil {
		tool.InputSchema = *inputSchema
	}

	handler := createToolHandler(exec, toolCfg.Name)

	mcpServer.AddTool(tool, handler)

	log.Debug().
		Str("tool", toolCfg.Name).
		Str("command", toolCfg.Command).
		Msg("Registered tool")
}

func buildInputSchema(toolCfg config.ToolConfig) *mcp.ToolInputSchema {
	if toolCfg.InputSchema == nil {
		return nil
	}

	schema := &mcp.ToolInputSchema{
		Type:       "object",
		Properties: make(map[string]interface{}),
	}

	if props, ok := toolCfg.InputSchema["properties"].(map[string]interface{}); ok {
		schema.Properties = props
	} else {
		log.Warn().
			Str("tool", toolCfg.Name).
			Msg("InputSchema 'properties' field is not a map or is missing")
	}

	if required, ok := toolCfg.InputSchema["required"].([]interface{}); ok {
		for _, r := range required {
			if s, ok := r.(string); ok {
				schema.Required = append(schema.Required, s)
			} else {
				log.Warn().
					Str("tool", toolCfg.Name).
					Interface("value", r).
					Msg("InputSchema 'required' contains non-string value")
			}
		}
	}

	return schema
}

func createToolHandler(exec *executor.Executor, toolName string) server.ToolHandlerFunc {
	return func(ctx context.Context, request mcp.CallToolRequest) (result *mcp.CallToolResult, err error) {
		defer func() {
			if r := recover(); r != nil {
				log.Error().
					Interface("panic", r).
					Str("tool", toolName).
					Msg("Tool handler panicked")
				result = mcp.NewToolResultError(fmt.Sprintf("Tool '%s' encountered an internal error", toolName))
				err = nil
			}
		}()

		log.Debug().
			Str("tool", toolName).
			Interface("arguments", request.Params.Arguments).
			Msg("Executing tool")

		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return mcp.NewToolResultError("Invalid arguments format"), nil
		}

		argsJSON, err := json.Marshal(args)
		if err != nil {
			return mcp.NewToolResultError("Failed to serialize arguments"), nil
		}
		if len(argsJSON) > maxArgsSize {
			log.Warn().
				Str("tool", toolName).
				Int("size", len(argsJSON)).
				Int("max_size", maxArgsSize).
				Msg("Arguments exceed maximum size")
			return mcp.NewToolResultError(fmt.Sprintf("Arguments exceed maximum size of %dMB", maxArgsSizeMB)), nil
		}

		resultExec, err := exec.Execute(ctx, toolName, args)
		if err != nil {
			log.Error().Err(err).Str("tool", toolName).Msg("Tool execution failed")
			return mcp.NewToolResultError("Tool execution failed. Check server logs for details."), nil
		}

		if !resultExec.Success {
			if resultExec.Error != nil {
				log.Error().
					Str("tool", toolName).
					Str("error_code", resultExec.Error.Code).
					Str("error_message", resultExec.Error.Message).
					Str("error_details", resultExec.Error.Details).
					Msg("Tool returned error")
				return mcp.NewToolResultError(truncateClientError(resultExec.Error.Message)), nil
			}
			return mcp.NewToolResultError("Tool execution failed with no error details"), nil
		}

		if len(resultExec.Content) > 0 {
			contents := make([]mcp.Content, 0, len(resultExec.Content))
			for _, item := range resultExec.Content {
				switch item.Type {
				case "text":
					contents = append(contents, mcp.TextContent{
						Type: "text",
						Text: item.Text,
					})
				case "image":
					contents = append(contents, mcp.ImageContent{
						Type:     "image",
						Data:     item.Data,
						MIMEType: item.MIMEType,
					})
				case "resource":
					if item.Resource != nil {
						resourceContent := mcp.EmbeddedResource{
							Type: "resource",
							Resource: mcp.TextResourceContents{
								URI:      item.Resource.URI,
								MIMEType: item.Resource.MIMEType,
								Text:     item.Resource.Text,
							},
						}
						contents = append(contents, resourceContent)
						log.Debug().
							Str("tool", toolName).
							Str("uri", item.Resource.URI).
							Str("mime_type", item.Resource.MIMEType).
							Int("text_length", len(item.Resource.Text)).
							Msg("Returning resource content")
					} else {
						log.Warn().
							Str("tool", toolName).
							Msg("Resource type with nil resource field, skipping")
					}
				default:
					log.Warn().
						Str("tool", toolName).
						Str("content_type", item.Type).
						Msg("Unknown content type, skipping")
				}
			}
			return &mcp.CallToolResult{
				Content: contents,
			}, nil
		}

		return mcp.NewToolResultText(""), nil
	}
}

func containsParam(rawURL, param string) bool {
	return strings.Contains(rawURL, param+"=")
}

func containsChar(s string, c byte) bool {
	return strings.IndexByte(s, c) >= 0
}
