# Architecture

Claude + Codex Queue is a local Python application with two entry points:

- `claude-codex-queue` for discovery, queue management, checks, and execution;
- `claude-codex-queue-web` for the localhost web interface.

The public `claude_codex_queue` package delegates to the original
`claude_vscode_queue` implementation package so existing installations remain
compatible after the project rename.

## Data flow

```mermaid
flowchart TD
    A["Local Claude and Codex metadata"] --> B["Discovery and normalization"]
    B --> C["Account ownership and resumability checks"]
    C --> D["Local web UI / CLI"]
    D --> E["Persistent queue"]
    E --> F["Runner"]
    F --> G["Official Claude or Codex CLI"]
    G --> H["Local transcript confirmation"]
    H --> E
```

## Components

### Discovery

`claude_vscode_queue.app` reads supported local indexes and metadata, converts
them into a common chat model, and sorts by the timestamp of the latest real
message. Synthetic markers do not affect ordering.

Large Codex transcripts are not scanned during the normal list operation.
Discovery uses the union of the task index, state database and rollout paths,
filters internal subagent threads, and drops append-only index ghosts. It reads
a transcript only when execution or limit confirmation needs it.

### Multi-account replication

Claude Desktop replication stores lifecycle observations in
`desktop-sync-state.json`. A logical session has a canonical `active`,
`archived` or `deleted` state plus per-account replica observations. Delete
requires two complete scans, writes its tombstone before removing anything and
never deletes the underlying `.claude/projects` transcript. Every overwritten
or removed Claude metadata file is backed up under
`account-transfer-backups/`.

Codex uses one local store per Windows profile, so account copies must have
different thread IDs. The transfer action calls app-server `thread/fork` while
the destination ChatGPT-authenticated account is active and records the linked
IDs in `accounts.json`. Lifecycle propagation is limited to those explicit
links and delegates to the stable Codex CLI commands. SQLite,
`session_index.jsonl` and rollout JSONL files remain provider-owned.

### Queue and recovery

Queue state is JSON stored under the detected Windows user profile. Items carry
their session identity, provider, prompt, priority, attempts, execution
settings, and a settings fingerprint.

The runner preserves FIFO order within each priority. A rate-limit response
creates recovery state without consuming the queued prompt. Recovery waits for
the parsed reset plus a safety delay, sends `continua`, and returns to the
pending prompt only after the interrupted session can proceed.

### Provider execution

Claude sessions resume through the official Claude Code executable. Codex tasks
resume through `codex exec resume`. The child environment removes external API
authentication overrides so the locally authenticated CLI account remains the
source of truth.

### Web UI

The web server uses Python's standard-library HTTP server and binds to
`127.0.0.1` by default. It serves one dependency-free HTML application and a
small JSON API. Controls are derived from the server's actual resumability and
account checks; view-only actions are disabled rather than allowed to fail
later.

## Safety invariants

- Never consume the next queued prompt as the recovery probe.
- Never enable bypass permissions implicitly.
- Never silently change model, effort, sandbox, approval, or permission mode.
- Never use an external API key accidentally inherited from the environment.
- Never expose the web UI beyond localhost by default.
- Never persist raw authentication material in queue state.
- Never write Codex SQLite, task indexes or rollout files directly.
- Never recreate a tombstoned Claude session from a surviving transcript.
- Never propagate a deletion from an empty or unreadable Codex store.

Changes touching these invariants require focused tests and explicit review.
