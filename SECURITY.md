# Security Policy

This project controls local Claude Code and Codex CLIs and reads local
Claude/VS Code/Codex metadata. Treat it as a local automation tool, not as a
hosted service.

## Supported Versions

Only the current `main` branch is supported until the project has tagged
releases.

## Reporting a Vulnerability

Open a GitHub issue with a minimal reproduction. Do not include API keys,
OAuth tokens, Claude logs containing secrets, or private transcripts.

## Local Data

The tool stores queue state and run logs under `.claude-codex-queue` in the
detected Windows user profile unless `--state-dir` is provided. Existing
installations keep using the legacy `.claude-vscode-queue` path automatically.
Do not commit either directory.

The app intentionally clears external Anthropic API-key environment variables
before invoking Claude Code, so queued sends use the authenticated Claude Code
session rather than an unrelated API key.

The same rule applies to Codex: external OpenAI API keys, base URLs and account
overrides are removed from the child process so the official CLI uses its local
ChatGPT authentication. Authentication files are only used to derive a hashed,
masked account identity and are never copied into queue state or logs.
