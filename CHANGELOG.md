# Changelog

All notable changes to o3de-cli will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-06-07

### Added

- **Core**: Schema 2.0 object models (engine, project, gem, template, repo, overlay) with Pydantic validation
- **Core**: Dependency resolver with SAT-based constraint solving (resolvelib)
- **Core**: Object store with manifest management and git-aware path resolution
- **Core**: Workspace system — create, solve, build, lock, verify
- **Core**: Schema upgrade pipeline (1.0 → 2.0 migration)
- **Core**: Network layer with httpx for registry operations
- **Core**: Policy enforcement (license compliance, security advisories, deprecation)
- **Core**: Auth token management for registry login/logout
- **Core**: Lockfile generation and verification
- **Commands**: engine, project, gem, template, repo, manifest, workspace, register, unregister, config, deps, audit, publish, registry
- **Commands**: All commands support `--json` for structured output
- **Commands**: Destructive commands support `--dry-run`
- **Build**: CMake workspace build integration (configure + build)
- **AI**: 11-provider support (Ollama, Anthropic, OpenAI, Gemini, Groq, DeepSeek, Mistral, xAI, OpenRouter, Together, Perplexity)
- **AI**: Conversational agent with tool-calling loop and confirmation gates
- **AI**: Command router with pattern matching + AI fallback
- **AI**: Dynamic model discovery across all providers
- **AI**: Configurable thinking effort (off/low/medium/high/max) for Claude, OpenAI o-series, Gemini
- **AI**: Free local model tier via Ollama (llama3.2:3b, auto-pull)
- **Voice**: 4 STT providers (Whisper local, Google free, Deepgram, OpenAI Whisper API)
- **Voice**: 3 TTS providers (OS-native, ElevenLabs, OpenAI TTS)
- **Voice**: AudioCapture with silence detection and cross-platform playback
- **MCP**: JSON-RPC 2.0 stdio server with 18 tools for IDE integration
- **Tests**: 1248 tests covering all modules
