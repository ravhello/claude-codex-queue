# Claude VS Code Queue

Local queue and auto-continue runner for Claude Code sessions in VS Code and
Claude Desktop on Windows.

It lets you pick an existing Claude Code session, enqueue one or more prompts,
and send them later with `claude -p --resume <session-id>` when the session
limit is no longer active. It does not bypass Claude limits. When Claude reports
a rate/session limit, the next queued prompt is not wasted: the runner waits for
the reset window, sends `continua` first, and only then resumes the queue.

## Why This Exists

There are already useful Claude Code queue projects, including:

- [JCSnap/claude-code-queue](https://github.com/JCSnap/claude-code-queue):
  packageable CLI queue with priorities, retries, prompt bank and rate-limit
  handling.
- [vasiliyk/claude-code-batch](https://github.com/vasiliyk/claude-code-batch):
  batch-oriented Claude Code task runner.
- [cheapestinference/claude-code-queue-utility](https://github.com/cheapestinference/claude-code-queue-utility):
  lightweight queue helper.

This project focuses on a different workflow:

- discovering existing Claude Code chats from VS Code, Claude Desktop and
  remote VS Code metadata;
- selecting a real session from a local web UI;
- preserving the selected chat's model, effort and permission mode unless the
  user explicitly overrides them;
- detecting account mismatches and view-only sessions before sends;
- supporting Claude Desktop Windows Code-tab session metadata, including
  multi-account synchronization;
- auto-continue mode for a selected session that waits out the limit and sends
  `continua` without consuming the next queued prompt.

## Features

- Local web UI at `http://127.0.0.1:8765/`.
- CLI for listing chats, adding prompts, checking status and running the queue.
- Persistent queue stored under the detected Windows user profile.
- Priority queue: lower numbers run first, FIFO order is preserved within the
  same priority.
- Rate/session-limit recovery with a 60 second safety delay after parsed reset
  times.
- Settings fingerprint checks before sending.
- Auto-continue mode for sessions blocked by credits/session limits.
- Claude Desktop Windows session discovery and repair for compatible metadata.
- Remote SSH VS Code session detection where enough metadata is available.

## Requirements

- Windows with WSL and Python 3.10+ available in WSL.
- Claude Code installed and authenticated.
- VS Code Claude Code extension, or Claude Desktop Windows app if you want the
  Desktop Code-tab integration.

## Quick Start

From the project directory:

```bash
python3 -m claude_vscode_queue doctor
python3 -m claude_vscode_queue list
python3 -m claude_vscode_queue.web --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765/
```

Add prompts from the CLI:

```bash
python3 -m claude_vscode_queue add --chat 1 "Run tests and fix failures"
python3 -m claude_vscode_queue add --chat c8209e53 --priority 0 "Urgent follow-up"
python3 -m claude_vscode_queue add --chat 1 @prompt.md
```

Run the queue:

```bash
python3 -m claude_vscode_queue run
```

Run one non-sending check:

```bash
python3 -m claude_vscode_queue run --dry-run --once
```

## Web UI Launcher

On Windows, run:

```powershell
.\start-claude-queue.ps1
```

To install a Desktop shortcut:

```powershell
.\install-desktop-shortcut.ps1
```

The scripts are path-relative, so they work from any cloned folder.

## CLI Commands

```bash
python3 -m claude_vscode_queue doctor
python3 -m claude_vscode_queue list --limit 30
python3 -m claude_vscode_queue add --chat <selector> [--priority 100] "msg1" "msg2"
python3 -m claude_vscode_queue status -v
python3 -m claude_vscode_queue check-settings
python3 -m claude_vscode_queue run
python3 -m claude_vscode_queue remove <item-id>
python3 -m claude_vscode_queue reset <item-id>
python3 -m claude_vscode_queue clear
```

`<selector>` can be a visible list number, a session-id prefix, a title fragment
or a cwd fragment.

## State and Logs

By default, state lives in:

```text
<Windows user profile>\.claude-vscode-queue
```

Override it with:

```bash
python3 -m claude_vscode_queue --state-dir /path/to/state <command>
```

Do not commit the state directory. It may contain queue prompts and run logs.

## Safety Model

This tool is intentionally conservative:

- It does not bypass or evade Claude limits.
- It waits and retries after rate/session-limit messages.
- It sends `continua` before consuming the next queued prompt after a limit.
- It checks settings before sending and blocks if the chat settings changed.
- It clears external Anthropic API-key environment variables before invoking
  Claude Code, avoiding accidental sends through a stale external API key.

## Development

```bash
python3 -m py_compile claude_vscode_queue/app.py claude_vscode_queue/web.py
python3 -m unittest tests.test_app
```

The project has no runtime Python dependencies outside the standard library.

## Status

Alpha. Tested against local Windows/WSL Claude Code workflows. Claude Desktop
metadata is private implementation detail and can change between Claude app
versions.

