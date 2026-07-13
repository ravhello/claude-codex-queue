<p align="center">
  <img src="https://raw.githubusercontent.com/ravhello/claude-codex-queue/main/assets/claude-codex-queue-128.png" width="112" alt="Claude + Codex Queue icon">
</p>

<h1 align="center">Claude + Codex Queue</h1>

<p align="center">
  Queue prompts for existing Claude Code sessions and Codex App tasks.<br>
  Wait out usage limits, retry the interrupted turn correctly, then resume the queue without wasting the next prompt.
</p>

<p align="center">
  <a href="https://github.com/ravhello/claude-codex-queue/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ravhello/claude-codex-queue/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/ravhello/claude-codex-queue/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/ravhello/claude-codex-queue"></a>
  <a href="https://pypi.org/project/claude-codex-queue/"><img alt="PyPI" src="https://img.shields.io/pypi/v/claude-codex-queue"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <a href="https://github.com/ravhello/claude-codex-queue/blob/main/LICENSE"><img alt="MIT license" src="https://img.shields.io/github/license/ravhello/claude-codex-queue"></a>
  <a href="https://github.com/ravhello/claude-codex-queue/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/ravhello/claude-codex-queue?style=social"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> |
  <a href="#how-limit-recovery-works">Limit recovery</a> |
  <a href="https://github.com/ravhello/claude-codex-queue/blob/main/ROADMAP.md">Roadmap</a> |
  <a href="https://github.com/ravhello/claude-codex-queue/discussions">Discussions</a>
</p>

![Claude + Codex Queue dashboard with anonymized sample data](https://raw.githubusercontent.com/ravhello/claude-codex-queue/main/docs/assets/dashboard.png)

## Why use it?

AI coding sessions often stop at the usage-limit boundary. Sending the next real
prompt immediately can waste it on another limit response. Claude + Codex Queue
keeps that prompt pending, waits for the reset, and recovers the interrupted turn
without duplicating a failed message. It then processes the rest of the queue in order.

The app works with sessions you already use. It does not create a separate chat
system and does not require external Anthropic or OpenAI API keys.

## What it supports

| Source | Discover | Queue prompts | Auto-continue | Preserve settings |
| --- | :---: | :---: | :---: | :---: |
| Claude Code in VS Code | Yes | Yes | Yes | Model, effort, permissions |
| Claude Desktop Code sessions | Yes | Yes, when locally resumable | Yes | Model, effort, permissions |
| Claude Code over Remote SSH | Yes | When enough metadata exists | Yes | Remote effective settings |
| Codex App tasks | Yes | Yes | Yes | Model, reasoning, sandbox, approvals |

Additional capabilities:

- newest-real-message sorting across Claude and Codex;
- persistent FIFO queue with per-prompt priorities;
- one-minute safety delay after a parsed reset time;
- multi-account Claude Code synchronization with archive, unarchive and delete propagation;
- Codex task forks for the active ChatGPT-authenticated account, with linked lifecycle sync;
- account mismatch and view-only checks before actions are enabled;
- transcript confirmation after every Codex send;
- local web UI plus a scriptable CLI;
- no runtime Python dependencies outside the standard library.

### Multi-account chat copies

Claude Desktop Code sessions are replicated once per known account. Archive and
unarchive changes propagate in either direction. A missing known replica is
observed twice before it is treated as a deletion; the app then writes a durable
tombstone, backs up the remaining metadata and removes the other replicas. A
surviving Claude transcript cannot recreate or relist a tombstoned chat. Remote bridge
identifiers remain owned by their original Claude account and are never copied
to another account, preventing failed remote deletes from restoring a local
replica. Account switches and session-directory changes wake the sync monitor
immediately instead of waiting for the normal polling deadline.

The chat list keeps one row per logical Claude session and shows every account
that currently owns a replica. Account identifiers are searchable, so switching
accounts never makes the previous account's sessions appear to vanish. Each row
also shows the beginning of the latest real user message from the transcript,
excluding assistant output and tool results.

For Codex, **Copy to active ChatGPT account** uses Codex app-server
`thread/fork` and creates a new thread ID instead of relabelling the old task.
Only copies linked by this app share archive, unarchive and delete state. Those
operations use the official `codex archive`, `codex unarchive` and
`codex delete --force` commands; the project never edits Codex SQLite or rollout
files directly.

The local web process runs a fast linked-account check on a ten-second cadence
and a full Claude transcript discovery every minute. Slow transcript discovery
does not add another fixed delay before the next metadata check, so lifecycle
synchronization does not depend on an open or foreground browser tab. The
Windows Startup entry keeps the program running after login.

Codex changes for a copy owned by another ChatGPT account remain pending until
that account becomes active, then the monitor applies them automatically. State
updates are locked across local processes; a corrupt or unreadable state file
stops synchronization instead of being replaced with an empty one.

Here, “ChatGPT account” means the account authenticating Codex. This feature
does not copy ordinary conversations from chatgpt.com, and the Claude feature
does not copy ordinary claude.ai chats.

## Quick start

### Windows app and Desktop shortcut

Requirements: Windows, WSL, Python 3.10+ in WSL, and at least one authenticated
Claude Code or Codex CLI installation.

```powershell
git clone https://github.com/ravhello/claude-codex-queue.git
cd claude-codex-queue
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-desktop-shortcut.ps1
```

Open **Claude + Codex Queue** from the Desktop. The launcher starts the local
server and opens [http://127.0.0.1:8765/](http://127.0.0.1:8765/). Both the
Desktop and Startup shortcuts use the windowless Windows Script Host; WSL,
PowerShell, Claude and Codex child processes are launched with hidden/no-window
settings, so the app does not create terminal windows in normal operation.

The installer also registers the app in the current Windows user's Startup
folder. At login it starts the server in the background and opens the browser
after the health check succeeds. The page refreshes every five seconds and
again whenever its tab regains focus. A server-side supervisor starts the queue
runner whenever pending messages, recovery work or auto-continue need it, so
the **Refresh** and **Start runner** buttons are not required for normal use.

To install or repair only the automatic startup entry:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-autostart.ps1
```

To run it without installing a shortcut:

```powershell
.\start-claude-codex-queue.ps1
```

### Install the CLI from the latest wheel

Run this inside WSL:

```bash
python3 -m pip install claude-codex-queue
claude-codex-queue doctor
claude-codex-queue-web --host 127.0.0.1 --port 8765
```

The source checkout is recommended if you want the Windows launcher and Desktop
shortcut. The wheel is useful for CLI-only installations.

## How limit recovery works

```mermaid
flowchart LR
    A["Queued prompt"] --> B["Resume selected session"]
    B --> C{"Usage limit?"}
    C -- "No" --> D["Confirm prompt in transcript"]
    C -- "Yes" --> E["Keep prompt pending"]
    E --> F["Wait until reset + 60 seconds"]
    F --> G{"Provider and turn state"}
    G -- "Claude Desktop Code" --> H["Invoke native Try again"]
    G -- "Codex prompt never started" --> I["Rollback failed turn and resend the same prompt"]
    G -- "Codex work stopped before recap" --> J["Send 'continua'"]
    I --> K["Queue any additional failed prompts in order"]
    J --> K
    H --> A
    K --> A
```

Auto-continue can monitor any number of selected sessions with an empty queue.
Each session keeps independent status, timing, settings and cancellation state;
one failed or waiting session does not replace or stop the others.

For Claude Desktop Code sessions, auto-continue opens the exact local session
through Claude's supported deep link and invokes the visible `Try again` control
through Windows UI Automation. It checks the native control directly because
Claude Desktop can show it without recording a limit in the transcript, and it
keeps monitoring after a successful invocation. It never substitutes a new
`continua` message.
For Codex App tasks, the runner reads structured turn state through app-server:
a failed turn with no agent progress is removed with `thread/rollback` and sent
again unchanged, while an interrupted turn that already contains agent activity
receives `continua`. Additional failed text prompts are placed in the persistent
queue in their original order.

## Safety guarantees

- The project does not bypass or evade provider limits.
- The next queued prompt remains pending when a limit is detected.
- Failed Codex prompts are rolled back before replay, so the task does not show a duplicate user message.
- Claude Desktop `Try again` failures never fall back to sending a new prompt.
- Chat settings are fingerprinted and checked before sending.
- External Anthropic and OpenAI API-key/base-URL overrides are removed from
  child processes, so local CLI authentication remains authoritative.
- Codex dangerous bypass mode is never enabled implicitly.
- Codex account copies receive a new thread ID and require an explicit confirmation.
- Destructive lifecycle changes are debounced and verified before linked copies are changed.
- Controls are disabled for sessions that are genuinely view-only.
- Queue state and logs stay local under the detected Windows profile.

Read [SECURITY.md](https://github.com/ravhello/claude-codex-queue/blob/main/SECURITY.md) before sharing diagnostics. Never publish private
transcripts, authentication files, tokens, or unredacted logs.

## CLI

```bash
claude-codex-queue doctor
claude-codex-queue list --limit 30
claude-codex-queue add --chat <selector> [--priority 100] "message"
claude-codex-queue status -v
claude-codex-queue check-settings
claude-codex-queue run
claude-codex-queue remove <item-id>
claude-codex-queue reset <item-id>
claude-codex-queue clear
```

`<selector>` accepts a visible row number, session-ID prefix, title fragment,
or working-directory fragment. Multiple messages can be supplied in order or
loaded from `@prompt.md`.

## State and compatibility

New installations use:

```text
<Windows user profile>\.claude-codex-queue
```

Existing installations automatically keep using `.claude-vscode-queue`, so an
upgrade does not reset queues or logs. The old Python modules, commands, and
launcher names remain as compatibility aliases.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m build
python3 -m twine check dist/*
```

See [CONTRIBUTING.md](https://github.com/ravhello/claude-codex-queue/blob/main/CONTRIBUTING.md),
[ARCHITECTURE.md](https://github.com/ravhello/claude-codex-queue/blob/main/docs/ARCHITECTURE.md),
and the [roadmap](https://github.com/ravhello/claude-codex-queue/blob/main/ROADMAP.md). Questions and setup help belong in
[Discussions](https://github.com/ravhello/claude-codex-queue/discussions);
reproducible defects belong in
[Issues](https://github.com/ravhello/claude-codex-queue/issues).

## Project status

Current release: **v0.2.3**. The project is alpha software tested on Windows/WSL
with local Claude Code and Codex App workflows. Upstream desktop metadata is not
a public compatibility contract and may change between provider releases.

Claude + Codex Queue is an independent open-source project. It is not affiliated
with, endorsed by, or sponsored by Anthropic or OpenAI.

Released under the [MIT License](https://github.com/ravhello/claude-codex-queue/blob/main/LICENSE).
