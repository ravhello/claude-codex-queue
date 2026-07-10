# Security Policy

This project controls a local Claude Code CLI and reads local Claude/VS Code
metadata. Treat it as a local automation tool, not as a hosted service.

## Supported Versions

Only the current `main` branch is supported until the project has tagged
releases.

## Reporting a Vulnerability

Open a GitHub issue with a minimal reproduction. Do not include API keys,
OAuth tokens, Claude logs containing secrets, or private transcripts.

## Local Data

The tool stores queue state and run logs under `.claude-vscode-queue` in the
detected Windows user profile unless `--state-dir` is provided. Do not commit
that directory.

The app intentionally clears external Anthropic API-key environment variables
before invoking Claude Code, so queued sends use the authenticated Claude Code
session rather than an unrelated API key.

