from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Route, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_OUT = ROOT / "docs" / "assets" / "dashboard.png"
SOCIAL_OUT = ROOT / "assets" / "social-preview.png"
ICON_OUT = ROOT / "assets" / "claude-codex-queue-128.png"


RUNNER = {
    "running": True,
    "pid": 4242,
    "app_version": "0.2.2",
    "log_tail": (
        "20:14:03  Runner ready\n"
        "20:14:04  Usage limit detected for Claude session\n"
        "20:14:04  Next check scheduled after reset + 60 seconds"
    ),
}

DOCTOR = {
    "app_version": "0.2.2",
    "local_time": "2026-07-11T20:14:04+02:00",
    "claude_version": "2.1.206 (Claude Code)",
    "codex_version": "codex-cli 0.144.1",
    "claude_chat_count": 4,
    "codex_chat_count": 3,
    "queueable_chat_count": 6,
    "queue_file": r"C:\Users\you\.claude-codex-queue\queue.json",
    "claude_exe": r"C:\Tools\Claude\claude.exe",
    "codex_exe": r"C:\Tools\Codex\codex.cmd",
    "active_account": {"label": "cl***@example.com"},
    "active_codex_account": {"label": "co***@example.com"},
    "account_index": {"accounts": [{"key": "claude"}, {"key": "codex"}]},
    "runner": RUNNER,
}

CHATS = {
    "chats": [
        {
            "session_id": "claude-release-review",
            "short_id": "c41a9f2e",
            "title": "Review release and finish documentation",
            "cwd": r"C:\Projects\sample-app",
            "source": "Claude Code VS Code",
            "source_key": "claude_code",
            "provider": "claude",
            "can_queue": True,
            "account_status": "active",
            "account_label": "cl***@example.com",
            "model": "claude-opus-4-8",
            "effort_level": "high",
            "permission_mode": "acceptEdits",
            "message_count": 86,
            "last_prompt": "Finish the release review",
            "last_timestamp": "2026-07-11T20:12:00+02:00",
        },
        {
            "session_id": "codex-integration-tests",
            "short_id": "b31d712a",
            "title": "Add integration tests for the queue",
            "cwd": r"C:\Projects\queue-tests",
            "source": "Codex App",
            "source_key": "codex_app",
            "provider": "codex",
            "can_queue": True,
            "account_status": "active",
            "account_label": "co***@example.com",
            "model": "gpt-5.4",
            "effort_level": "high",
            "sandbox_mode": "workspace-write",
            "approval_policy": "on-request",
            "message_count": 42,
            "last_prompt": "Verify the Windows runner",
            "last_timestamp": "2026-07-11T19:55:00+02:00",
        },
        {
            "session_id": "claude-migrate-config",
            "short_id": "25ab880d",
            "title": "Migrate configuration safely",
            "cwd": r"C:\Projects\desktop-tool",
            "source": "Claude Desktop",
            "source_key": "claude_windows_app",
            "provider": "claude",
            "can_queue": True,
            "account_status": "active",
            "account_label": "cl***@example.com",
            "model": "claude-sonnet-4-5",
            "effort_level": "medium",
            "permission_mode": "default",
            "message_count": 31,
            "last_prompt": "Continue after the migration check",
            "last_timestamp": "2026-07-11T18:40:00+02:00",
        },
        {
            "session_id": "codex-archived-task",
            "short_id": "8d47c19b",
            "title": "Archived performance investigation",
            "cwd": r"C:\Projects\sample-api",
            "source": "Codex App (archived)",
            "source_key": "codex_app_archived",
            "provider": "codex",
            "can_queue": False,
            "account_status": "known",
            "account_label": "co***@example.com",
            "model": "gpt-5.4",
            "message_count": 18,
            "last_prompt": "Profile the parser",
            "last_timestamp": "2026-07-10T16:05:00+02:00",
        },
    ]
}

QUEUE = {
    "items": [
        {
            "id": "q-104",
            "status": "pending",
            "provider": "codex",
            "title": "Add integration tests for the queue",
            "session_id": "codex-integration-tests",
            "prompt": "Run the full suite and fix only reproducible failures.",
            "priority": 0,
            "attempts": 0,
        },
        {
            "id": "q-105",
            "status": "pending",
            "provider": "claude",
            "title": "Review release and finish documentation",
            "session_id": "claude-release-review",
            "prompt": "Update the changelog and prepare the final release notes.",
            "priority": 100,
            "attempts": 0,
        },
    ],
    "recovery": None,
    "auto_continue": {
        "enabled": True,
        "status": "waiting_limit",
        "session_id": "claude-release-review",
        "title": "Review release and finish documentation",
        "provider": "claude",
        "attempts": 1,
        "next_check_in_seconds": 247,
        "last_check_at": "2026-07-11T20:14:04+02:00",
        "updated_at": "2026-07-11T20:14:04+02:00",
        "not_before": "2026-07-11T20:18:11+02:00",
        "last_error": "Usage limit active: waiting for reset plus the safety delay.",
        "fingerprint": {
            "effective": {
                "model": "claude-opus-4-8",
                "effortLevel": "high",
                "permissionMode": "acceptEdits",
            }
        },
    },
    "runner": RUNNER,
}


def json_response(route: Route, payload: dict[str, object]) -> None:
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(payload),
    )


def handle_api(route: Route) -> None:
    path = urlparse(route.request.url).path
    if path == "/api/doctor":
        json_response(route, DOCTOR)
    elif path == "/api/chats":
        json_response(route, CHATS)
    elif path == "/api/queue":
        json_response(route, QUEUE)
    else:
        json_response(route, {"error": "Disabled in the marketing fixture."})


def wait_for_server(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"Server did not start at {url}")


def social_html(dashboard_data_url: str, icon_data_url: str) -> str:
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; width: 1200px; height: 630px; overflow: hidden; }}
  body {{
    background: #f7f7f4;
    color: #202124;
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;
    border-top: 14px solid #0f766e;
  }}
  main {{ height: 616px; padding: 44px 54px 0; position: relative; }}
  header {{ display: flex; align-items: center; gap: 18px; }}
  .icon {{ width: 78px; height: 78px; }}
  .name {{ font-size: 37px; font-weight: 760; letter-spacing: 0; }}
  .open {{ margin-left: auto; color: #0f766e; font-weight: 720; font-size: 18px; }}
  h1 {{ margin: 30px 0 12px; max-width: 1040px; font-size: 54px; line-height: 1.05; letter-spacing: 0; }}
  p {{ margin: 0; max-width: 1000px; color: #555b64; font-size: 25px; line-height: 1.35; }}
  .accent {{ color: #7c2d12; }}
  .preview {{
    position: absolute; left: 54px; right: 54px; top: 340px; height: 276px;
    overflow: hidden; border: 1px solid #c8c7c1; border-bottom: 0;
    border-radius: 8px 8px 0 0; background: white;
    box-shadow: 0 10px 28px rgba(32, 33, 36, .14);
  }}
  .preview img {{ width: 100%; display: block; }}
  .labels {{ position: absolute; right: 70px; top: 302px; display: flex; gap: 8px; }}
  .label {{ background: #fff; border: 1px solid #c8c7c1; border-radius: 6px; padding: 6px 10px; font-size: 15px; font-weight: 650; }}
</style>
</head>
<body>
<main>
  <header>
    <img class="icon" src="{icon_data_url}" alt="">
    <div class="name">Claude + Codex Queue</div>
    <div class="open">OPEN SOURCE</div>
  </header>
  <h1>Resume after limits. <span class="accent">Keep every prompt.</span></h1>
  <p>Queue existing Claude Code sessions and Codex App tasks with settings-aware recovery.</p>
  <div class="labels">
    <span class="label">Claude Code</span>
    <span class="label">Codex App</span>
    <span class="label">Windows + WSL</span>
  </div>
  <div class="preview"><img src="{dashboard_data_url}" alt=""></div>
</main>
</body>
</html>"""


def data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def capture(url: str) -> None:
    DASHBOARD_OUT.parent.mkdir(parents=True, exist_ok=True)
    SOCIAL_OUT.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        dashboard_page = browser.new_page(viewport={"width": 1440, "height": 960}, device_scale_factor=1)
        dashboard_page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        dashboard_page.route("**/api/**", handle_api)
        dashboard_page.goto(url, wait_until="networkidle")
        if dashboard_page.title() != "Claude + Codex Queue":
            raise RuntimeError(f"Unexpected page title: {dashboard_page.title()}")
        dashboard_page.locator(".chat").first.wait_for(state="visible")
        if dashboard_page.locator(".chat").count() != len(CHATS["chats"]):
            raise RuntimeError("The dashboard did not render every synthetic session.")
        dashboard_page.locator(".chat").first.click()
        if dashboard_page.locator(".chat.active").count() != 1:
            raise RuntimeError("Selecting a session did not update the visible state.")
        dashboard_page.locator("#messages").fill("Prepare the next verified release and summarize the completed checks.")
        if "Prepare the next verified release" not in dashboard_page.locator("#messages").input_value():
            raise RuntimeError("The prompt editor did not retain its synthetic value.")
        dashboard_page.screenshot(path=str(DASHBOARD_OUT), full_page=False)

        social_page = browser.new_page(viewport={"width": 1200, "height": 630}, device_scale_factor=1)
        social_page.set_content(social_html(data_url(DASHBOARD_OUT), data_url(ICON_OUT)), wait_until="load")
        social_page.screenshot(path=str(SOCIAL_OUT), full_page=False)
        browser.close()

    if errors:
        raise RuntimeError("Browser console errors: " + " | ".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture anonymized README and social-preview images.")
    parser.add_argument("--url", help="Use an already running web server instead of starting a temporary one.")
    args = parser.parse_args()

    server = None
    url = args.url or "http://127.0.0.1:8876/"
    try:
        if not args.url:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "claude_codex_queue.web",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8876",
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wait_for_server(url)
        capture(url)
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
    print(DASHBOARD_OUT)
    print(SOCIAL_OUT)


if __name__ == "__main__":
    main()
