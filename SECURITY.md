# Security Policy

This project controls local Claude Code and Codex CLIs and reads local
Claude/VS Code/Codex metadata. Treat it as a local automation tool, not as a
hosted service.

## Supported Versions

| Version | Supported |
| --- | :---: |
| 0.2.x | Yes |
| 0.1.x | No |

The current `main` branch receives fixes before the next tagged release.

## Reporting a Vulnerability

Use
[GitHub private vulnerability reporting](https://github.com/ravhello/claude-codex-queue/security/advisories/new)
with a minimal reproduction. Do not open a public issue for a suspected
vulnerability.

Do not include API keys, OAuth tokens, authentication files, unredacted account
identifiers, logs containing secrets, or private transcripts. If a minimal
reproduction needs sensitive context, describe the shape of the data and wait
for a maintainer response before sharing it.

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

WSL-to-Windows PowerShell source is cached as content-addressed files below the
private local state directory instead of being carried in command-line
`EncodedCommand` values. Subprocess commands, encoded payloads, CLIXML and
credential-shaped values are filtered before an error reaches the web API or a
persisted queue status.

Claude Code artifact replication may decrypt Claude Desktop's current-user OAuth
cache in a consoleless child process. Each token is verified against
`/api/oauth/profile`, used only in memory for Claude's frame API and never stored
in state, backups or logs. Verified sessions may remain in process memory until
the next full check; credential-file changes invalidate them immediately. A
profile mismatch blocks the operation.

`desktop-sync-state.json` contains replica paths, artifact mappings, lifecycle
state and durable deletion tombstones, but no transcript contents or
credentials. Rendered Claude Code artifact content is cached separately under
the local application state directory and must be treated as private. Claude metadata
changed by replication is copied to `account-transfer-backups/` before it is
overwritten or removed. Treat those backups as private because titles and local
working-directory paths may be present.

Copying a Codex task to another account copies the task context into a new local
thread ID. The web UI requires an explicit confirmation. Archive, unarchive and
delete propagation is performed only for copies linked by this action and only
through official Codex commands. Deletion is destructive; use disposable tasks
when testing it.

Lifecycle state files are updated under local process and OS file locks. If an
account index or Claude sync journal is malformed or unreadable, the app pauses
that synchronization path rather than overwriting the file or inferring a mass
deletion. Codex changes targeting another account wait until that account is
active.
