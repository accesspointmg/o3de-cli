# Security Policy

## Reporting a Vulnerability

Please **do not** open a public issue, discussion or pull request for a
security vulnerability.

Report it privately through GitHub instead:
[**Report a vulnerability**](https://github.com/accesspointmg/o3de-cli/security/advisories/new)
(the *Security* tab → *Report a vulnerability*). Only the maintainers can see
the report.

Please include:

- a description of the vulnerability and its impact
- steps to reproduce, or a proof of concept
- the affected version or commit
- a suggested fix, if you have one

## What to Expect

- We aim to acknowledge a report within 48 hours.
- We will keep you updated as we investigate and work on a fix.
- Once a fix is released we publish a GitHub security advisory, crediting you
  unless you would rather stay anonymous.

## Supported Versions

The project is in early development, so only the latest release receives
security fixes.

| Version | Supported          |
| ------- | ------------------ |
| 0.x.x   | :white_check_mark: |

## Scope

o3de-cli downloads, builds and runs code from O3DE objects, including object
`hooks` scripts. Issues where the CLI runs or installs something without the
user's consent, writes outside the paths it should, or leaks registry
credentials (`~/.o3de/credentials.json`) are in scope.
