# Changelog

## 0.2.0

- Added Codex App task discovery from the local task index and state database.
- Added ordered Codex queue sends and auto-continue through `codex exec resume`.
- Preserved Codex model, effort, sandbox and approval policy by default.
- Added structured Codex limit detection and transcript confirmation after sends.
- Added provider filters, Codex settings controls and disabled states for view-only tasks.
- Cleared external OpenAI API environment overrides before Codex runs.

## 0.1.0

- Local web UI for choosing Claude Code / Claude Desktop sessions.
- Persistent queue with rate-limit/session-limit recovery.
- Auto-continue mode that sends `continua` after the reset window.
- Settings fingerprint checks for model, effort, permission mode and config files.
- Claude Desktop Windows Code-tab session discovery and multi-account sync.
- Queue priority support: lower priority numbers run first while preserving FIFO order within the same priority.
