# o3de-cli

Package manager and CLI for the [Open 3D Engine](https://o3de.org) ecosystem.

Manage engines, projects, gems, templates, repos, and workspaces from the command line — with optional AI assistance.

## Install

```bash
pip install o3de-cli
```

Or with AI support:

```bash
pip install "o3de-cli[ai]"
```

## Quick Start

```bash
# Register an engine
o3de register --engine-path /path/to/o3de

# Create a new project
o3de project create --name MyGame --engine o3de

# Search the registry for gems
o3de search particle

# Install a gem
o3de install org.o3de.gem.stars

# Create and build a workspace
o3de workspace create --project ./MyGame
o3de workspace build

# AI assistance (optional, BYOK)
o3de ai setup
o3de ai ask "How do I add a gem to my project?"
```

## Commands

| Command | Description |
|---------|-------------|
| `engine` | Manage O3DE engines |
| `project` | Manage O3DE projects |
| `gem` | Manage O3DE gems |
| `template` | Manage O3DE templates |
| `repo` | Manage object registries |
| `workspace` | Manage build workspaces (create, solve, build) |
| `manifest` | Manage the O3DE manifest |
| `register` / `unregister` | Register/unregister objects |
| `registry` | Package registry operations (search, install, login) |
| `publish` | Pack and push objects to a registry |
| `deps` | Dependency tree and analysis |
| `audit` | Audit dependency tree for issues |
| `config` | Manage CLI configuration |
| `ai` | AI-powered assistance (chat, ask, diagnose, voice) |
| `mcp` | Start MCP server for IDE integration |
| `gui` | Launch the GUI (requires [o3de-pilot](https://github.com/byrcolin/o3de-pilot)) |

All commands support `--json` for structured output and `--help` for usage details.

## Schema 2.0

o3de-cli uses the O3DE Schema 2.0 format for all object types. Objects use reverse-DNS naming (`org.o3de.gem.atom`) and semantic versioning with full dependency resolution.

```json
{
  "$schemaVersion": "2.0.0",
  "gem": {
    "name": "org.o3de.gem.atom",
    "version": "2.0.0",
    "display_name": "Atom Renderer"
  }
}
```

## AI (Optional, BYOK)

AI is an optional layer — everything works without it. Bring your own API key for cloud providers, or use a free local model via Ollama.

**11 providers supported:** Ollama (free/local), Anthropic, OpenAI, Google Gemini, Groq, DeepSeek, Mistral, xAI, OpenRouter, Together, Perplexity.

```bash
# Free local model (requires Ollama)
o3de ai local

# Cloud provider setup
o3de ai setup

# Interactive chat with tool calling
o3de ai chat

# Voice interaction (optional)
o3de ai voice
```

## MCP Server

Expose the CLI as tools for AI-powered IDEs:

```json
{
  "mcpServers": {
    "o3de": {
      "command": "o3de-mcp"
    }
  }
}
```

18 tools available: workspace management, registry search/install, manifest operations, dependency analysis, and more.

## Development

```bash
git clone https://github.com/byrcolin/o3de-cli.git
cd o3de-cli
pip install -e ".[dev]"
pytest
```

1248 tests covering CLI commands, core resolver, dependency solver, AI integration, MCP server, and more.

## License

Licensed under either of:

- Apache License, Version 2.0 ([LICENSE_APACHE2.TXT](LICENSE_APACHE2.TXT))
- MIT License ([LICENSE_MIT.TXT](LICENSE_MIT.TXT))

at your option. See [LICENSE.txt](LICENSE.txt) for details.
