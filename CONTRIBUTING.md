# Contributing

Thanks for helping improve Claude + Codex Queue. Small, focused pull requests
are preferred because this project interacts with local AI sessions and must
avoid sending prompts unexpectedly.

## Before opening an issue

- Use GitHub Discussions for setup questions and general troubleshooting.
- Use the bug form for reproducible defects.
- Search existing issues before creating a new one.
- Remove account identifiers, tokens, private prompts, transcripts, and local
  paths from screenshots and logs.
- Use GitHub private vulnerability reporting for security-sensitive findings.

## Development setup

The application targets Windows with WSL. Its Python runtime has no third-party
dependencies.

```bash
git clone https://github.com/ravhello/claude-codex-queue.git
cd claude-codex-queue
python3 -m unittest discover -s tests -v
```

Packaging checks:

```bash
python3 -m pip install build twine
python3 -m build
python3 -m twine check dist/*
```

The full unit suite is expected to run in Linux or WSL. Native Windows CI uses
compile, CLI, and packaging smoke tests because provider process fakes in a few
runner tests rely on POSIX executable semantics.

## Change guidelines

- Preserve existing CLI and state-directory compatibility unless a breaking
  release is explicitly planned.
- Never add behavior that bypasses provider limits or silently weakens sandbox,
  approval, or permission settings.
- Do not make a network service publicly reachable by default. The web UI must
  continue binding to localhost unless the user explicitly overrides it.
- Add focused tests for queue ordering, limit handling, authentication
  environment cleanup, settings preservation, and account ownership changes.
- Keep runtime dependencies at zero unless the benefit clearly justifies a new
  installation and security burden.

## Pull requests

1. Create a branch from `main`.
2. Keep the diff scoped to one behavior or documentation goal.
3. Run the relevant tests and packaging checks.
4. Explain user impact and any safety implications.
5. Link the related issue when one exists.

By contributing, you agree that your contribution is licensed under the MIT
License and that you will follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
