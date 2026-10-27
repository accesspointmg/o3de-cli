# Contributing to o3de-cli

Thanks for your interest in contributing! This document covers how changes get
into the project.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md) first. To report a
security problem, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Branching Model

| Branch        | Purpose                                                        |
| ------------- | -------------------------------------------------------------- |
| `development` | Integration branch. **All contributor pull requests target it.** |
| `main`        | Release-quality code. Only receives merges from `development`.  |
| your branch   | Your work, branched from `development` (on your fork).         |

Both `main` and `development` are protected:

- Changes land only through pull requests; nobody pushes to them directly.
- CI (tests on Linux, Windows and macOS, plus lint) must pass, and the branch
  must be up to date with its target.
- A maintainer must approve the pull request, and review comments must be
  resolved.
- Pull requests into `development` are squashed or rebased, so its history
  stays linear.
- A pull request into `main` from anything other than `development` fails the
  `check-source-branch` check.

## Making a Change

1. Fork the repository and branch from `development`.
2. Set up a development install:

   ```bash
   pip install -e ".[dev]"
   ```

3. Make your change, with tests.
4. Check it locally — CI runs the same commands:

   ```bash
   pytest
   ruff check o3de_cli/
   ruff format --check o3de_cli/
   ```

5. Open a pull request against `development` and fill in the template.

GitHub Actions does not run on a pull request from a fork until a maintainer
approves the run. That is a safety measure for public repositories, not a
judgement on your change.

## Coding Standards

- Python 3.10+ syntax, formatted with `ruff format` (line length 100).
- Lint rules are pinned in `pyproject.toml`; `ruff check` must pass.
- New behaviour comes with tests under `tests/`.
- Commands that support `--json` use the shared envelope in
  `o3de_cli/core/json_output.py`.

## Commit Messages

We use [Conventional Commits](https://www.conventionalcommits.org/):
`feat(scope): ...`, `fix: ...`, `docs: ...`, `ci: ...`, `chore: ...`.

## License

By contributing you agree that your contributions are licensed under the
project's dual Apache-2.0 / MIT license (see [LICENSE.txt](LICENSE.txt)).
