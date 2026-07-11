# Roadmap

The roadmap favors reliability and transparent local behavior over adding more
ways to bypass provider safeguards.

## Current priorities

- Keep Claude Code, Claude Desktop, and Codex App discovery compatible with
  upstream metadata changes.
- Expand end-to-end tests with fake provider CLIs and synthetic transcripts.
- Add English UI localization while preserving the Italian interface.
- Improve first-run diagnostics and explain why a session is view-only.
- Publish the command-line package on PyPI through Trusted Publishing.

## Next

- Export and import queue plans without including private transcripts.
- Add opt-in desktop notifications for waiting, resumed, blocked, and completed
  states.
- Add a compact history view for completed queue items and recovery attempts.
- Support additional local operating systems when provider session locations
  and resume commands can be verified safely.

## Later

- Pluggable local providers behind a documented session adapter interface.
- Signed release provenance and automated supply-chain attestations.
- Localized documentation contributed by the community.

Suggestions belong in
[Discussions](https://github.com/ravhello/claude-codex-queue/discussions).
Implementation proposals should explain user value, safety impact, and how the
behavior can be tested without real credentials.
