# Changelog

## Unreleased

## 0.2.6 - 2026-07-18

- Kept the last complete Claude and Codex chat list visible while account changes trigger a background refresh, merged newly discovered chats immediately and removed only confirmed Claude tombstones.

## 0.2.5 - 2026-07-18

- Added private Claude Code artifact replication: each Claude account receives its own server-side artifact and an account-specific derived transcript while the provider transcript remains unchanged.
- Read Claude Desktop's encrypted OAuth cache from native Windows or WSL through hidden PowerShell 7, kept tokens only in memory and verified each token through `/api/oauth/profile` before use.
- Blocked mixed-account CLI authentication when `.claude.json` and the effective OAuth credential refer to different organizations.
- Kept unavailable artifact copies pending until the matching account credential appears instead of retrying with another account's token.
- Ran a complete transcript and artifact scan on the monitor's first startup cycle, then continued with lightweight ten-second checks and full minute checks.
- Unified Claude app journal identities across Windows and WSL and migrated legacy entries, preventing stale cross-platform tombstones from hiding chats.
- Distinguished account workspace changes from real chat deletion so login switches cannot turn moved replicas into global tombstones.
- Cached verified OAuth sessions only in process memory between fast checks and skipped frame-list network calls outside full scans; credential-file changes invalidate the cache immediately.
- Reused unchanged Claude session metadata and parsed account logs in memory, reducing steady WSL synchronization from roughly fifteen seconds to about three seconds on the tested profile.
- Detected active Claude accounts from runtime session and skills-plugin log paths, so a stale logout event no longer hides current chats.
- Populated a newly selected account's organization directory immediately even before its first Claude Code activity.
- Unified Claude prompt transport over standard input on native Windows and WSL, and normalized WSL drive paths consistently in native Windows helpers.
- Rendered chat discovery as soon as it completes instead of waiting for the slower account and artifact diagnostics request.
- Added persistent artifact status to the web UI and reduced real transcript artifact scanning from tens of seconds to a few seconds.
- Made account-specific artifact transcripts byte-stable after the first replication, preventing needless rewrites and repeated background discovery invalidation.
- Used the indexed `lastPrompt` as the immediate chat-preview fallback so a transient empty preview cannot hide the latest real message.
- Verified metadata discovery and synchronization without Claude Desktop, Codex or VS Code processes running.

## 0.2.4 - 2026-07-17

- Synchronized Claude Cowork artifact manifests across accounts, remapped their session links and preserved account-local connector/share state.
- Added two-scan artifact deletion tombstones so changing account cannot resurrect a removed artifact.
- Detected Claude logout/login directly from session-bridge logs before the first activity updates the app config.
- Made startup and account-switch chat loading non-blocking, with Claude metadata first and full Claude/Codex discovery in the background.
- Retried transient Windows sharing violations during atomic state-file replacement.
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
