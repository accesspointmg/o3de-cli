# Changelog

All notable changes to o3de-cli will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing has been published yet; the first release will be 0.1.0.

### Added

- **Core**: Schema 2.0 object models (engine, project, gem, template, repo, overlay) with Pydantic validation
- **Core**: Schema upgrade pipeline (0 → 1.0 → 2.0), writing 2.0.0 sidecars alongside legacy files
- **Core**: Dependency solver for workspaces (resolvelib), with platform and binary-ABI awareness
- **Core**: Object store that crawls remote `repo.json` trees, with an offline cache and SHA-256 integrity checks
- **Core**: Workspaces: symlinked build trees composed from an engine, project, gems and overlays, with lockfiles
- **Core**: Overlays compose into the object they extend, selected by platform and user tags, with explicit apply order
- **Core**: Policy checks (licenses, deprecation, security advisories) and token auth for private registries
- **Commands**: engine, project, gem, template, repo, overlay, register/unregister, manifest, registry, workspace, object, publish, deps, audit, config, gui, mcp
- **Commands**: `object build` / `object install` build a single gem into a binary package with a generated `<name>Config.cmake`
- **Commands**: `object split-platforms` extracts platform directories into overlay objects
- **Commands**: `object hoist` projects a gem out of its engine repo into a family repo with full git history
- **Commands**: `object compose --plan` reports what a declarative composition plan would produce (generation to follow)
- **Commands**: `--json` output with a shared envelope; errors exit 1
- **MCP**: JSON-RPC 2.0 stdio server exposing 18 CLI operations as tools
- The canonical 2.0.0 JSON schemas ship with the package
- Object hooks (`post_install` on install, `pre_build` on `workspace build`) run after showing the script and asking; `--yes-hooks` / `--no-hooks`
- Remote archives must be HTTPS and publish a SHA-256, verified before unpacking (`--allow-unverified` / `O3DE_ALLOW_UNVERIFIED=1`, `O3DE_ALLOW_HTTP=1`)
- Archive unpacking refuses path traversal and links; git clones accept only https/ssh URLs

### Changed

- AI assistance and voice moved out of the CLI into the [o3de-pilot](https://github.com/accesspointmg/o3de-pilot) GUI
- **Commands**: all `--json` output now uses the status envelope (`{"status": "ok", "data", "warnings"}` / `{"status": "error", "error", "code"}`, exit 0 / 1). `audit`, `deps tree`, `manifest resolve`/`show`, `publish validate`/`pack`/`push`, `registry search`/`list-remotes`, `search`, the `engine`/`gem`/`project`/`template`/`overlay`/`repo` `list` commands, `gem search` and `workspace list`/`show`/`solve`/`verify-lock` used to print bare documents; error codes are all `E_*` now; unexpected exceptions under `--json` become `E_INTERNAL`
- **MCP**: a tool call whose command answered `{"status": "error"}` sets `isError`
- **MCP**: the `audit` tool takes no arguments; it used to pass a path `audit` does not accept, so every call failed
- **Commands**: `unregister` and `manifest remove` exit 1 when the object is not found, in text mode as well as `--json`

### Removed

- `audit --fix`: it was accepted but never did anything
