# Changelog

## Unreleased

## 0.2.3 - 2026-07-12

- Fixed Windows-to-WSL path conversion in the visual launcher.
- Published the package through PyPI Trusted Publishing.

## 0.2.2 - 2026-07-12

- Added CI across supported Python versions plus Windows packaging smoke tests.
- Added automated GitHub release assets and a manual PyPI Trusted Publishing workflow.
- Added an anonymized dashboard screenshot, social preview, and faster Quick Start documentation.
- Added contribution, support, architecture, roadmap, issue, pull request, and conduct guidance.
- Restored declared Python 3.10 compatibility and made reset-delay tests timezone-independent.

## 0.2.1 - 2026-07-11

- Forced LF line endings for shell launchers so fresh Windows checkouts run correctly in WSL.

## 0.2.0 - 2026-07-11

- Renamed the project and package to Claude + Codex Queue / `claude-codex-queue`.
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
