# Changelog

## Unreleased

- Added the latest real user-message preview to every Claude and Codex chat row.
- Made the complete Windows launch chain consoleless, including Desktop/Startup shortcuts and periodic WSL-to-Windows CLI checks.
- Replaced Claude Desktop auto-continue messages with the native `Try again` action on the exact local Code session.
- Kept Claude Desktop auto-continue armed when `Try again` is temporarily absent and prevented PowerShell `CLIXML` diagnostics from leaking into the UI.
- Added independent multi-session auto-continue and direct native `Try again` monitoring for every selected Claude Desktop session.
- Scoped automatic runner discovery to the matching state directory so parallel instances cannot mask pending work.
- Added Codex turn-aware recovery: rollback and replay untouched failed prompts, continue interrupted work, and queue additional failed messages in order.
- Prevented a stale runner write from re-enabling auto-continue after the user disables it.
- Added real Codex task copying through app-server `thread/fork` for the active ChatGPT account.
- Fixed Claude Desktop recovery routing so it omits `--ide`, prefers the app CLI, and preserves UTF-8 output on Windows.
- Pinned every Codex subprocess to the indexed `CODEX_HOME`, including Windows CLI calls launched from WSL.
- Synchronized archive, unarchive and delete state across explicitly linked Codex copies using official CLI commands.
- Moved linked-account lifecycle checks into a server-side monitor so browser throttling cannot pause synchronization.
- Added cross-process locks and corruption checks for account and Claude sync state files.
- Deferred Codex lifecycle changes until the linked copy's ChatGPT account is active.
- Added durable Claude lifecycle tombstones, two-scan delete detection and metadata backups.
- Preserved all Claude account replica labels on each deduplicated logical chat.
- Added Windows login startup, focus refresh and a server-side automatic runner supervisor.
- Split account sync into fixed-cadence metadata checks and periodic full transcript scans with heartbeat status.
- Kept Claude bridge session IDs account-local, hid tombstoned transcripts and added reactive deletion/account-switch synchronization.
- Prevented deleted Claude chats from being resurrected by surviving transcripts.
- Removed account/workspace cross-product duplicates and made account state writes atomic in-process.
- Expanded Codex discovery to database- and rollout-only tasks while filtering deleted ghosts and internal subagents.

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
