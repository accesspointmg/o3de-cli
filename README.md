# o3de-cli

Package manager and CLI for the [Open 3D Engine](https://o3de.org) ecosystem.

Manage engines, projects, gems, templates, repos, overlays and build workspaces
from the command line, with npm-style dependency resolution.

> **Status:** pre-alpha. Not yet published to PyPI; install from source (see
> [Development](#development)).

## Install

```bash
pip install o3de-cli
```

This installs two commands: `o3de` (the CLI) and `o3de-mcp` (an MCP server for
AI-enabled editors).

## Quick Start

```bash
# Register an engine you have checked out
o3de register /path/to/o3de

# Create a project
o3de init MyGame --path ./MyGame

# Find and install a gem from the registered remote repos
o3de search particle
o3de install org.o3de.gem.stars

# Compose a build workspace from the engine and project, then build it
o3de workspace create mygame-ws --engine /path/to/o3de --project ./MyGame
o3de workspace build mygame-ws
```

## Commands

| Command | Description |
|---------|-------------|
| `engine`, `project`, `gem`, `template`, `repo`, `overlay` | Create, list, register and inspect each object type |
| `register` / `unregister` | Register or unregister any object (upgrading its JSON to Schema 2.0 if needed) |
| `workspace` | Compose, solve, build, lock and verify build workspaces |
| `object` | Build/install a single gem as a binary package; split platform code into overlays; hoist a gem into its own repo; declarative composition plans |
| `manifest` | Show, resolve, upgrade and edit the O3DE manifest |
| `registry` | Search, install, update and manage remote repos; log in to private registries |
| `search`, `install`, `init`, `list` | Shortcuts for the most common registry and project operations |
| `publish` | Validate, pack and push objects to a registry |
| `deps` | Dependency tree, list and "why is this here?" |
| `audit` | Check the dependency tree for license, deprecation and security issues |
| `config` | Manage CLI configuration |
| `mcp` | Start the MCP server on stdio |
| `gui` | Launch the GUI (requires [o3de-pilot](https://github.com/accesspointmg/o3de-pilot)) |

Most commands support `--json` for machine-readable output, and every command
has `--help`. With `--json`, success prints
`{"status": "ok", "data": ...}` and exits 0; failure prints
`{"status": "error", "error": ..., "code": "E_..."}` and exits 1.

## Installing from remote repos safely

Remote `repo.json` trees are written by third parties, so o3de-cli treats
every URL, branch and archive they name as untrusted:

- **Archives** are downloaded over HTTPS only (redirects included) and must
  publish a SHA-256, which is checked before anything is unpacked. Unpacking
  refuses entries that would land outside the target folder, and refuses links.
- **Git clones** accept only `https://` and ssh URLs, and the URL can never be
  read as a git option.
- **Hooks** (an object's `post_install` or `pre_build` script) never run
  silently. You are shown the script and asked first. With `--json`, or with
  no terminal to ask on, they are skipped unless you pass `--yes-hooks`;
  `--no-hooks` always skips them. A hook path pointing outside its object is
  refused.

To loosen this for development or a private setup:

| Option | Effect |
|--------|--------|
| `--allow-unverified` (or `O3DE_ALLOW_UNVERIFIED=1`) | Install an archive that publishes no SHA-256 |
| `O3DE_ALLOW_HTTP=1` | Allow plain-HTTP downloads and clones, e.g. a registry on localhost |

## Schema 2.0

o3de-cli uses the O3DE Schema 2.0 format for all object types. Objects use
reverse-DNS naming (`org.o3de.gem.atom`) and semantic versioning with full
dependency resolution.

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

The canonical JSON schemas live in
[canonical.o3de.org](https://github.com/accesspointmg/canonical.o3de.org) and
ship with the package, so validation works offline. Set `O3DE_SCHEMA_DIR` to
validate against a different copy.

## MCP Server

Expose the CLI as tools for MCP clients such as VS Code or Claude:

```json
{
  "mcpServers": {
    "o3de": {
      "command": "o3de-mcp"
    }
  }
}
```

It provides 18 tools covering workspaces, registry search and install, the
manifest, dependency analysis, auditing and configuration.

## Development

```bash
git clone https://github.com/accesspointmg/o3de-cli.git
cd o3de-cli
pip install -e ".[dev]"
pytest
```

Tests run with a throwaway home directory and no network access, so they never
touch your real `~/.o3de`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
branching model and how to get a change merged.

## License

Licensed under either of:

- Apache License, Version 2.0 ([LICENSE_APACHE2.TXT](LICENSE_APACHE2.TXT))
- MIT License ([LICENSE_MIT.TXT](LICENSE_MIT.TXT))

at your option. See [LICENSE.txt](LICENSE.txt) for details.
