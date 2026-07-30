from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        return 2

    manager = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(manager))
    import engine

    engine.set_app(engine.CLAUDE, persist=False)
    running = engine.running_profiles()
    launched: list[str] = []
    errors: list[str] = []
    for profile in engine.list_profiles():
        try:
            config_text = (profile / "config.json").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            config_text = ""
        authenticated = bool(config_text) and (
            (profile / "Network" / "Cookies").is_file() or "oauth:tokenCache" in config_text
        )
        if not authenticated or profile.name in running:
            continue
        try:
            engine.launch_profile(profile.name)
            launched.append(profile.name)
        except Exception as exc:
            errors.append(f"{profile.name}: {exc.__class__.__name__}")

    current = sorted(set(running) | set(launched), key=str.casefold)
    print(json.dumps({"running": current, "launched": launched, "errors": errors}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
