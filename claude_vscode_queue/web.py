from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__, app


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = Path(__file__).resolve().parent / "assets"
FAVICON_PATH = ASSET_DIR / "claude-codex-queue.ico"
FAVICON_PNG_PATH = ASSET_DIR / "claude-codex-queue-32.png"
RUNNER_MODULES = ("claude_codex_queue", "claude_vscode_queue")


def split_messages(raw: str) -> list[str]:
    messages: list[str] = []
    buffer: list[str] = []
    for line in raw.splitlines():
        if line.strip() == "---":
            text = "\n".join(buffer).strip()
            if text:
                messages.append(text)
            buffer = []
        else:
            buffer.append(line)
    text = "\n".join(buffer).strip()
    if text:
        messages.append(text)
    return messages


def chat_to_dict(chat: app.Chat) -> dict[str, Any]:
    return {
        "provider": chat.provider,
        "session_id": chat.session_id,
        "short_id": chat.session_id[:8],
        "title": chat.title,
        "cwd": chat.cwd,
        "permission_mode": chat.permission_mode,
        "model": chat.model,
        "effort_level": chat.effort_level,
        "sandbox_mode": chat.sandbox_mode,
        "approval_policy": chat.approval_policy,
        "personality": chat.personality,
        "archived": chat.archived,
        "last_timestamp": chat.last_timestamp,
        "message_count": chat.message_count,
        "last_prompt": chat.last_prompt,
        "jsonl_path": str(chat.jsonl_path),
        "source": chat.source,
        "source_key": chat.source_key,
        "can_queue": chat.can_queue,
        "remote_kind": chat.remote_kind,
        "remote_host": chat.remote_host,
        "remote_cwd": chat.remote_cwd,
        "remote_uri": chat.remote_uri,
        "account_key": chat.account_key,
        "account_short_key": chat.account_key[:8] if chat.account_key else None,
        "account_label": chat.account_label,
        "account_status": chat.account_status,
    }


def queue_summary(queue: dict[str, Any]) -> dict[str, int]:
    counts = {
        status: 0
        for status in [app.STATUS_PENDING, app.STATUS_DONE, app.STATUS_FAILED, app.STATUS_BLOCKED, app.STATUS_RECOVERY]
    }
    for item in queue.get("items", []):
        status = item.get("status")
        if status in counts:
            counts[status] += 1
    if app.active_recovery(queue):
        counts[app.STATUS_RECOVERY] += 1
    return counts


class WebState:
    def __init__(self, paths: app.Paths, claude_exe: Path | None, codex_exe: Path | None = None):
        self.paths = paths
        self.claude_exe = claude_exe
        self.codex_exe = codex_exe
        self.runner: subprocess.Popen[str] | None = None
        self.runner_log = paths.state_dir / "visual-runner.log"
        self.runner_pid_file = paths.state_dir / "visual-runner.pid"
        self.lock = threading.RLock()
        self._chats_cache: list[app.Chat] = []
        self._chats_cache_at = 0.0
        self._chats_refreshing = False
        self._chats_refresh_started_at = 0.0
        self._version_cache: str | None = None
        self._version_cache_at = 0.0
        self._codex_version_cache: str | None = None
        self._codex_version_cache_at = 0.0

    def base_command(self) -> list[str]:
        command = [
            sys.executable,
            "-u",
            "-m",
            "claude_codex_queue",
            "--windows-home",
            str(self.paths.windows_home),
            "--state-dir",
            str(self.paths.state_dir),
        ]
        if self.claude_exe is not None:
            command.extend(["--claude", str(self.claude_exe)])
        if self.codex_exe is not None:
            command.extend(["--codex", str(self.codex_exe)])
        return command

    def process_command(self, pid: int) -> str | None:
        try:
            raw = Path("/proc") / str(pid) / "cmdline"
            text = raw.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        except OSError:
            return None
        return text or None

    def process_running(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def runner_process_from_pid_file(self) -> tuple[int | None, str | None]:
        try:
            pid = int(self.runner_pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None, None
        command = self.process_command(pid)
        padded = f" {command or ''} "
        if not command or not any(f" -m {module} " in padded for module in RUNNER_MODULES) or " run " not in padded:
            return None, None
        return (pid, command) if self.process_running(pid) else (None, None)

    def discover_external_runner(self) -> tuple[int | None, str | None]:
        proc_root = Path("/proc")
        if not proc_root.exists():
            return None, None
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            command = self.process_command(pid)
            if not command:
                continue
            padded = f" {command} "
            if any(f" -m {module} " in padded for module in RUNNER_MODULES) and " run " in padded and ".web" not in padded:
                return pid, command
        return None, None

    def runner_status(self) -> dict[str, Any]:
        with self.lock:
            running = self.runner is not None and self.runner.poll() is None
            exit_code = None if self.runner is None or running else self.runner.returncode
            pid = self.runner.pid if self.runner is not None and running else None
            command = " ".join(self.runner.args) if self.runner is not None and running and isinstance(self.runner.args, list) else None
        source = "managed" if running else None
        if not running:
            pid, command = self.runner_process_from_pid_file()
            if pid is not None:
                running = True
                exit_code = None
                source = "pid-file"
        if not running:
            pid, command = self.discover_external_runner()
            if pid is not None:
                running = True
                exit_code = None
                source = "external"
        tail = ""
        try:
            lines = self.runner_log.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-80:])
        except OSError:
            pass
        return {
            "app_version": __version__,
            "running": running,
            "exit_code": exit_code,
            "pid": pid,
            "source": source,
            "command": command,
            "log_path": str(self.runner_log),
            "log_tail": tail,
        }

    def quick_chats(self) -> list[app.Chat]:
        desktop_chats = app.discover_claude_windows_app_sessions(
            self.paths,
            sync_accounts=False,
            active_only=True,
        )
        claude_chats = app.annotate_chats_with_accounts(
            self.paths,
            app.merge_claude_chat_sources([], desktop_chats),
        )
        codex_chats = app.annotate_codex_chats_with_accounts(
            self.paths,
            app.discover_codex_app_sessions(self.paths),
        )
        return sorted(claude_chats + codex_chats, key=app.chat_sort_key, reverse=True)

    def refresh_chats_background(self) -> None:
        now = time.monotonic()
        with self.lock:
            if self._chats_refreshing:
                return
            self._chats_refreshing = True
            self._chats_refresh_started_at = now

        def worker() -> None:
            try:
                chats = app.discover_agent_chats(
                    self.paths,
                    sync_desktop_accounts=True,
                    active_desktop_only=True,
                )
            except Exception:
                chats = []
            with self.lock:
                if chats:
                    self._chats_cache = chats
                    self._chats_cache_at = time.monotonic()
                self._chats_refreshing = False

        threading.Thread(target=worker, daemon=True).start()

    def chats(self, max_age_seconds: int = 15) -> list[app.Chat]:
        now = time.monotonic()
        with self.lock:
            if self._chats_cache and now - self._chats_cache_at < max_age_seconds:
                return list(self._chats_cache)
            cached = list(self._chats_cache)
            refreshing = self._chats_refreshing
        if cached:
            if not refreshing:
                self.refresh_chats_background()
            return cached
        try:
            chats = self.quick_chats()
        except Exception:
            chats = []
        with self.lock:
            if chats:
                self._chats_cache = chats
                self._chats_cache_at = time.monotonic()
        self.refresh_chats_background()
        return list(chats)

    def cached_chats(self) -> list[app.Chat]:
        with self.lock:
            return list(self._chats_cache)

    def invalidate_chats(self) -> None:
        with self.lock:
            self._chats_cache = []
            self._chats_cache_at = 0.0
            self._chats_refreshing = False
            self._chats_refresh_started_at = 0.0

    def claude_version(self, max_age_seconds: int = 300) -> str | None:
        if self.claude_exe is None:
            return None
        now = time.monotonic()
        with self.lock:
            if self._version_cache is not None and now - self._version_cache_at < max_age_seconds:
                return self._version_cache
        try:
            result = subprocess.run(
                [str(self.claude_exe), "--version"],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                timeout=5,
            )
            version = (result.stdout or result.stderr).strip()
        except subprocess.SubprocessError as exc:
            version = f"errore: {exc}"
        with self.lock:
            self._version_cache = version
            self._version_cache_at = now
        return version

    def codex_version(self, max_age_seconds: int = 300) -> str | None:
        if self.codex_exe is None:
            return None
        now = time.monotonic()
        with self.lock:
            if self._codex_version_cache is not None and now - self._codex_version_cache_at < max_age_seconds:
                return self._codex_version_cache
        try:
            result = app.run_codex_cli_command(self.codex_exe, ["--version"], timeout=8)
            version = (result.stdout or result.stderr).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            version = f"errore: {exc}"
        with self.lock:
            self._codex_version_cache = version
            self._codex_version_cache_at = now
        return version

    def start_runner(self, poll_seconds: int = 60) -> dict[str, Any]:
        with self.lock:
            if self.runner is not None and self.runner.poll() is None:
                return self.runner_status()
            self.runner_log.parent.mkdir(parents=True, exist_ok=True)
            log_handle = self.runner_log.open("a", encoding="utf-8")
            log_handle.write(f"\n--- runner start {app.now_utc()} ---\n")
            log_handle.flush()
            command = self.base_command() + ["run", "--poll-seconds", str(poll_seconds)]
            self.runner = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.runner_pid_file.write_text(str(self.runner.pid), encoding="utf-8")
        time.sleep(0.2)
        return self.runner_status()

    def stop_runner(self) -> dict[str, Any]:
        with self.lock:
            proc = self.runner
            if proc is not None and proc.poll() is None:
                proc.terminate()
            elif proc is None:
                pid, _ = self.runner_process_from_pid_file()
                if pid is None:
                    pid, _ = self.discover_external_runner()
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        return self.runner_status()


class QueueRequestHandler(BaseHTTPRequestHandler):
    state: WebState

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_html(HTML)
            elif parsed.path == "/favicon.ico":
                self.send_file(FAVICON_PATH, "image/x-icon")
            elif parsed.path == "/favicon-32.png":
                self.send_file(FAVICON_PNG_PATH, "image/png")
            elif parsed.path == "/api/doctor":
                self.send_json(self.api_doctor())
            elif parsed.path == "/api/chats":
                chats = self.state.chats()
                self.send_json({"chats": [chat_to_dict(chat) for chat in chats]})
            elif parsed.path == "/api/queue":
                queue = app.load_queue(self.state.paths.queue_file)
                self.send_json(
                    {
                        "items": queue.get("items", []),
                        "recovery": app.active_recovery(queue),
                        "auto_continue": queue.get("auto_continue"),
                        "summary": queue_summary(queue),
                        "queue_file": str(self.state.paths.queue_file),
                        "runner": self.state.runner_status(),
                    }
                )
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/add":
                self.send_json(self.api_add(payload))
            elif parsed.path == "/api/remove":
                self.send_json(self.api_remove(payload))
            elif parsed.path == "/api/reset":
                self.send_json(self.api_reset(payload))
            elif parsed.path == "/api/check-settings":
                self.send_json(self.api_check_settings(payload))
            elif parsed.path == "/api/run-once":
                self.send_json(self.api_run_once(payload))
            elif parsed.path == "/api/auto-continue":
                self.send_json(self.api_auto_continue(payload))
            elif parsed.path == "/api/transfer-chat":
                self.send_json(self.api_transfer_chat(payload))
            elif parsed.path == "/api/runner/start":
                self.send_json(self.state.start_runner(int(payload.get("poll_seconds") or 60)))
            elif parsed.path == "/api/runner/stop":
                self.send_json(self.state.stop_runner())
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        if not length:
            return {}
        data = self.rfile.read(length)
        value = json.loads(data.decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", content_type)
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def api_doctor(self) -> dict[str, Any]:
        chats = self.state.cached_chats()
        if not chats:
            chats = self.state.chats()
        version = self.state.claude_version()
        codex_version = self.state.codex_version()
        claude_chats = [chat for chat in chats if chat.provider == app.PROVIDER_CLAUDE]
        codex_chats = [chat for chat in chats if chat.provider == app.PROVIDER_CODEX]
        return {
            "windows_home": str(self.state.paths.windows_home),
            "claude_home": str(self.state.paths.claude_home),
            "state_dir": str(self.state.paths.state_dir),
            "queue_file": str(self.state.paths.queue_file),
            "claude_exe": str(self.state.claude_exe) if self.state.claude_exe else None,
            "claude_version": version,
            "codex_home": str(self.state.paths.codex_home),
            "codex_exe": str(self.state.codex_exe) if self.state.codex_exe else None,
            "codex_version": codex_version,
            "chat_count": len(chats),
            "claude_chat_count": len(claude_chats),
            "codex_chat_count": len(codex_chats),
            "queueable_chat_count": len([chat for chat in chats if chat.can_queue]),
            "sources": sorted({chat.source for chat in chats}),
            "local_time": app.now_utc(),
            "active_account": app.account_public_dict(app.active_claude_account(self.state.paths)),
            "active_codex_account": app.account_public_dict(app.active_codex_account(self.state.paths)),
            "account_index": app.account_index_public(self.state.paths),
            "runner": self.state.runner_status(),
        }

    def api_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id") or "")
        raw_messages = str(payload.get("messages") or "")
        messages = split_messages(raw_messages)
        if not session_id:
            raise ValueError("Seleziona una chat.")
        if not messages:
            raise ValueError("Inserisci almeno un messaggio.")
        chats = self.state.chats(max_age_seconds=0)
        chat = app.find_chat_by_session(chats, session_id)
        if chat is None:
            raise ValueError("Chat non trovata.")
        account_error = app.account_mismatch_for_chat(self.state.paths, chat)
        if account_error:
            raise ValueError(account_error)
        if not chat.can_queue:
            raise ValueError(
                "Questa chat/task e' solo visibile: il suo contesto non e' disponibile per l'invio."
            )
        chat = app.remember_chat_account(self.state.paths, chat)
        fingerprint = app.settings_fingerprint(self.state.paths, chat)
        overrides = app.selected_setting_overrides(payload)
        priority = int(payload.get("priority") if isinstance(payload.get("priority"), int) else 100)
        queue = app.load_queue(self.state.paths.queue_file)
        created = app.now_utc()
        start_order = len(queue.get("items", []))
        for offset, message in enumerate(messages):
            queue["items"].append(
                {
                    "id": str(app.uuid.uuid4())[:8],
                    "status": app.STATUS_PENDING,
                    "created_at": created,
                    "order": start_order + offset,
                    "priority": priority,
                    "session_id": chat.session_id,
                    "title": chat.title,
                    "cwd": chat.cwd,
                    "prompt": message,
                    "attempts": 0,
                    "not_before": None,
                    "last_error": None,
                    "last_log": None,
                    "fingerprint": fingerprint,
                    **overrides,
                    **app.chat_execution_fields(chat),
                }
            )
        app.save_queue(self.state.paths.queue_file, queue)
        return {
            "added": len(messages),
            "items": queue.get("items", []),
            "recovery": app.active_recovery(queue),
            "auto_continue": queue.get("auto_continue"),
            "summary": queue_summary(queue),
        }

    def api_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = str(payload.get("id") or "")
        if not item_id:
            raise ValueError("ID mancante.")
        queue = app.load_queue(self.state.paths.queue_file)
        before = len(queue.get("items", []))
        queue["items"] = [item for item in queue.get("items", []) if item.get("id") != item_id]
        app.save_queue(self.state.paths.queue_file, queue)
        return {
            "removed": before - len(queue["items"]),
            "items": queue["items"],
            "recovery": app.active_recovery(queue),
            "auto_continue": queue.get("auto_continue"),
            "summary": queue_summary(queue),
        }

    def api_reset(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = str(payload.get("id") or "")
        queue = app.load_queue(self.state.paths.queue_file)
        changed = 0
        for item in queue.get("items", []):
            if item_id and item.get("id") != item_id:
                continue
            if item.get("status") in {app.STATUS_FAILED, app.STATUS_BLOCKED}:
                item["status"] = app.STATUS_PENDING
                item["last_error"] = None
                item["not_before"] = None
                changed += 1
        app.save_queue(self.state.paths.queue_file, queue)
        return {
            "reset": changed,
            "items": queue.get("items", []),
            "recovery": app.active_recovery(queue),
            "auto_continue": queue.get("auto_continue"),
            "summary": queue_summary(queue),
        }

    def api_check_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = str(payload.get("id") or "")
        queue = app.load_queue(self.state.paths.queue_file)
        chats = self.state.chats(max_age_seconds=0)
        results = []
        for item in queue.get("items", []):
            if item_id and item.get("id") != item_id:
                continue
            chat = app.find_chat_for_item(chats, item)
            if chat is None:
                diffs = ["chat non trovata"]
            else:
                diffs = app.compare_fingerprints(item["fingerprint"], app.settings_fingerprint(self.state.paths, chat))
            results.append({"id": item.get("id"), "ok": not diffs, "diffs": diffs})
        return {"results": results}

    def api_run_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        dry_run = bool(payload.get("dry_run", True))
        command = self.state.base_command() + ["run", "--once"]
        if dry_run:
            command.append("--dry-run")
        result = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True, timeout=120)
        queue = app.load_queue(self.state.paths.queue_file)
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "items": queue.get("items", []),
            "recovery": app.active_recovery(queue),
            "auto_continue": queue.get("auto_continue"),
            "summary": queue_summary(queue),
        }

    def api_auto_continue(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(payload.get("enabled"))
        queue = app.load_queue(self.state.paths.queue_file)
        if not enabled:
            auto_continue = queue.get("auto_continue")
            if isinstance(auto_continue, dict):
                auto_continue["enabled"] = False
                auto_continue["status"] = "disabled"
                auto_continue["disabled_at"] = app.now_utc()
            app.save_queue(self.state.paths.queue_file, queue)
            return {
                "items": queue.get("items", []),
                "recovery": app.active_recovery(queue),
                "auto_continue": queue.get("auto_continue"),
                "summary": queue_summary(queue),
                "runner": self.state.runner_status(),
            }

        session_id = str(payload.get("session_id") or "")
        if not session_id:
            raise ValueError("Seleziona una chat.")
        chats = self.state.chats(max_age_seconds=0)
        chat = app.find_chat_by_session(chats, session_id)
        if chat is None:
            raise ValueError("Chat non trovata.")
        account_error = app.account_mismatch_for_chat(self.state.paths, chat)
        if account_error:
            raise ValueError(account_error)
        if not chat.can_queue:
            raise ValueError(
                "Questa chat/task e' solo visibile: il suo contesto non e' disponibile per auto-continua."
            )
        chat = app.remember_chat_account(self.state.paths, chat)
        overrides = app.selected_setting_overrides(payload)
        reset_at = app.latest_rate_limit_reset_from_chat(chat)
        not_before = None
        status = "armed"
        last_error = None
        if reset_at is not None:
            ready_at = reset_at + app.dt.timedelta(seconds=app.RATE_LIMIT_RESET_DELAY_SECONDS)
            not_before = ready_at.astimezone().replace(microsecond=0).isoformat()
            status = "waiting_limit"
            last_error = "Usage limit attivo: attendo reset + 1 minuto prima di riprovare."

        queue["auto_continue"] = {
            "enabled": True,
            "status": status,
            "created_at": app.now_utc(),
            "session_id": chat.session_id,
            "title": chat.title,
            "cwd": chat.cwd,
            "source": chat.source,
            "provider": chat.provider,
            "jsonl_path": str(chat.jsonl_path),
            "prompt": app.RECOVERY_PROMPT,
            "monitor_limit": True,
            "attempts": 0,
            "not_before": not_before,
            "last_error": last_error,
            "last_log": None,
            "fingerprint": app.settings_fingerprint(self.state.paths, chat),
            "allow_cwd_fallback": False,
            "cwd_fallback": None,
            "last_check_at": app.now_utc(),
            **overrides,
            **app.chat_execution_fields(chat),
        }
        app.save_queue(self.state.paths.queue_file, queue)
        runner = self.state.start_runner(60)
        return {
            "items": queue.get("items", []),
            "recovery": app.active_recovery(queue),
            "auto_continue": queue.get("auto_continue"),
            "summary": queue_summary(queue),
            "runner": runner,
        }

    def api_transfer_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            raise ValueError("Seleziona una chat.")
        chats = self.state.chats(max_age_seconds=0)
        chat = app.find_chat_by_session(chats, session_id)
        if chat is None:
            raise ValueError("Chat non trovata.")
        if chat.provider == app.PROVIDER_CODEX:
            result = app.transfer_codex_chat_to_active_account(self.state.paths, chat)
        else:
            result = app.transfer_chat_to_active_desktop_account(
                self.state.paths,
                chat,
                move=bool(payload.get("move")),
            )
        self.state.invalidate_chats()
        chats = self.state.chats(max_age_seconds=0)
        return {
            "transfer": result,
            "chats": [chat_to_dict(item) for item in chats],
        }


HTML = r"""<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/favicon.ico?v=4" sizes="any">
  <link rel="shortcut icon" href="/favicon.ico?v=4">
  <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png?v=4">
  <title>Claude + Codex Queue</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --ink: #202124;
      --muted: #666b73;
      --line: #d9d8d2;
      --accent: #0f766e;
      --accent-2: #7c2d12;
      --blue: #1d4ed8;
      --danger: #b42318;
      --ok: #15803d;
      --warn: #b7791f;
      --shadow: 0 1px 2px rgba(16, 24, 40, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      height: 56px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 20px;
      border-bottom: 1px solid var(--line);
      background: #fbfbf8;
      position: sticky;
      top: 0;
      z-index: 3;
    }
    h1 { font-size: 17px; margin: 0; font-weight: 650; letter-spacing: 0; }
    h2 { font-size: 13px; margin: 0 0 10px; text-transform: uppercase; color: var(--muted); letter-spacing: 0; }
    main {
      display: grid;
      grid-template-columns: minmax(280px, 360px) minmax(420px, 1fr);
      gap: 16px;
      padding: 16px;
      max-width: 1420px;
      margin: 0 auto;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 14px;
    }
    .stack { display: grid; gap: 16px; }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .grow { flex: 1; min-width: 160px; }
    .settings-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 8px;
      margin-top: 10px;
    }
    .chat-filters { display: grid; grid-template-columns: minmax(0, 1fr) 120px; gap: 8px; }
    .hidden { display: none !important; }
    .field-label {
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      color: var(--muted);
      white-space: nowrap;
      font-size: 13px;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--warn);
      display: inline-block;
    }
    .dot.on { background: var(--ok); }
    .dot.off { background: var(--muted); }
    button, select, textarea, input {
      font: inherit;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
    }
    button {
      min-height: 34px;
      padding: 0 11px;
      cursor: pointer;
      font-weight: 560;
    }
    button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
    button.blue { background: var(--blue); color: #fff; border-color: var(--blue); }
    button.danger { background: #fff5f3; color: var(--danger); border-color: #f1bbb5; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    select, input { min-height: 36px; padding: 0 10px; width: 100%; }
    input[type="checkbox"] { width: auto; min-height: 0; padding: 0; }
    .inline-check {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    textarea {
      width: 100%;
      min-height: 210px;
      resize: vertical;
      padding: 10px;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 13px;
      line-height: 1.5;
    }
    .meta {
      color: var(--muted);
      font-size: 12px;
      word-break: break-word;
    }
    .chat-list {
      display: grid;
      gap: 7px;
      max-height: 360px;
      overflow: auto;
      padding-right: 3px;
    }
    .chat {
      width: 100%;
      text-align: left;
      padding: 9px 10px;
      height: auto;
      background: #fff;
    }
    .chat.active { border-color: var(--accent); outline: 2px solid rgba(15, 118, 110, .15); }
    .chat.view-only { opacity: .84; }
    .chat-title { font-weight: 650; display: block; overflow-wrap: anywhere; }
    .chat-sub { color: var(--muted); display: block; font-size: 12px; overflow-wrap: anywhere; margin-top: 3px; }
    .badge {
      display: inline-flex;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 1px 6px;
      margin-right: 5px;
      color: var(--muted);
      font-size: 11px;
      background: #f8fafc;
    }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td { border-bottom: 1px solid var(--line); padding: 9px 7px; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; font-weight: 650; }
    td { overflow-wrap: anywhere; }
    .status-pill {
      display: inline-flex;
      min-width: 66px;
      justify-content: center;
      border-radius: 999px;
      padding: 2px 7px;
      font-size: 12px;
      border: 1px solid var(--line);
      background: #f8fafc;
    }
    .pending { color: var(--blue); border-color: #bfdbfe; background: #eff6ff; }
    .done { color: var(--ok); border-color: #bbf7d0; background: #f0fdf4; }
    .failed, .blocked { color: var(--danger); border-color: #fecaca; background: #fff1f2; }
    .recovery { color: var(--accent-2); border-color: #fed7aa; background: #fff7ed; }
    .recovery-box {
      border: 1px solid #fed7aa;
      background: #fff7ed;
      color: var(--accent-2);
      border-radius: 8px;
      padding: 10px;
      margin-bottom: 10px;
      font-size: 13px;
    }
    .auto-box {
      border: 1px solid #bfdbfe;
      background: #eff6ff;
      color: var(--blue);
      border-radius: 8px;
      padding: 10px;
      margin-bottom: 10px;
      font-size: 13px;
    }
    .auto-feedback {
      flex-basis: 100%;
      min-height: 18px;
      color: var(--blue);
    }
    .auto-feedback.ok { color: var(--ok); }
    .auto-feedback.error { color: var(--danger); }
    .auto-feedback.warn { color: var(--warn); }
    pre {
      margin: 0;
      white-space: pre-wrap;
      background: #191b1f;
      color: #f4f4f5;
      border-radius: 8px;
      padding: 10px;
      min-height: 140px;
      max-height: 300px;
      overflow: auto;
      font-size: 12px;
    }
    .empty { color: var(--muted); padding: 18px 0; text-align: center; }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      header { height: auto; padding: 12px 14px; align-items: flex-start; }
      .settings-grid { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
    }
  </style>
</head>
<body>
  <header>
    <h1>Claude + Codex Queue</h1>
    <div class="row">
      <span class="status"><span id="runner-dot" class="dot off"></span><span id="runner-text">Runner fermo</span></span>
      <button id="refresh-btn">Aggiorna</button>
      <button id="start-btn" class="primary">Avvia runner</button>
      <button id="stop-btn" class="danger">Ferma</button>
    </div>
  </header>
  <main>
    <div class="stack">
      <section>
        <h2>Ambiente</h2>
        <div id="doctor" class="meta">Caricamento...</div>
      </section>
      <section>
        <h2>Chat</h2>
        <div class="chat-filters">
          <input id="chat-filter" placeholder="Filtra chat o task">
          <select id="provider-filter" aria-label="Provider">
            <option value="">Tutte</option>
            <option value="claude">Claude</option>
            <option value="codex">Codex</option>
          </select>
        </div>
        <div id="chat-list" class="chat-list"></div>
      </section>
    </div>
    <div class="stack">
      <section>
        <h2>Messaggi</h2>
        <textarea id="messages" spellcheck="false" placeholder="Un messaggio oppure piu' messaggi separati da una riga con solo ---"></textarea>
        <div class="settings-grid">
          <label class="field-label">Modello<select id="model-select"></select></label>
          <label class="field-label">Effort<select id="effort-select"></select></label>
          <label id="permission-field" class="field-label">Permessi<select id="permission-select"></select></label>
          <label id="approval-field" class="field-label hidden">Approvazioni<select id="approval-select"></select></label>
          <label class="field-label">Priorita'
            <select id="priority-select">
              <option value="100">Normale</option>
              <option value="0">Urgente</option>
              <option value="50">Alta</option>
              <option value="200">Bassa</option>
            </select>
          </label>
        </div>
        <div class="row" style="margin-top:10px">
          <button id="add-btn" class="primary">Aggiungi alla coda</button>
          <button id="check-btn">Controlla impostazioni</button>
          <button id="auto-btn" class="blue">Auto-continua</button>
          <button id="transfer-btn">Importa nell'account attivo</button>
          <label class="inline-check"><input id="transfer-move" type="checkbox"> rimuovi sorgente</label>
          <span id="selected-chat" class="meta grow">Nessuna chat selezionata</span>
          <span id="auto-feedback" class="meta auto-feedback"></span>
        </div>
      </section>
      <section>
        <h2>Coda</h2>
        <div id="queue"></div>
      </section>
      <section>
        <h2>Log</h2>
        <pre id="log"></pre>
      </section>
    </div>
  </main>
  <script>
    const state = { chats: [], selected: null, queue: [], autoContinue: null, runner: {}, autoBusy: false, autoBusyAction: null, settingsSession: null, transferBusy: false, refreshBusy: false };
    const $ = (id) => document.getElementById(id);
    const CLAUDE_MODEL_OPTIONS = ["opus", "sonnet", "haiku", "claude-opus-4-8", "claude-sonnet-4-5"];
    const CODEX_MODEL_OPTIONS = ["gpt-5.6-sol", "gpt-5.5", "gpt-5.4", "gpt-5.1-codex-max"];
    const EFFORT_OPTIONS = ["low", "medium", "high", "xhigh", "ultra", "max"];
    const PERMISSION_OPTIONS = ["default", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan"];
    const SANDBOX_OPTIONS = ["read-only", "workspace-write", "danger-full-access"];
    const APPROVAL_OPTIONS = ["untrusted", "on-request", "on-failure", "never"];
    const USER_TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const USER_LOCALE = navigator.language || "it-IT";
    const DATE_TIME_OPTIONS = {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    };
    if (USER_TIME_ZONE) DATE_TIME_OPTIONS.timeZone = USER_TIME_ZONE;
    const DATE_TIME_FORMAT = new Intl.DateTimeFormat(USER_LOCALE, DATE_TIME_OPTIONS);

    async function api(path, options = {}) {
      const res = await fetch(path, {
        ...options,
        headers: { "content-type": "application/json", ...(options.headers || {}) },
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || res.statusText);
      return data;
    }

    function log(text) {
      $("log").textContent = text || "";
    }

    function appendLog(text) {
      $("log").textContent = [text, $("log").textContent].filter(Boolean).join("\n\n");
    }

    function renderDoctor(data) {
      const account = data.active_account;
      const codexAccount = data.active_codex_account;
      const accounts = data.account_index && Array.isArray(data.account_index.accounts) ? data.account_index.accounts : [];
      $("doctor").innerHTML = [
        `<b>Ora PC</b>: ${escapeHtml(formatDateTime(data.local_time || new Date().toISOString()))}`,
        `<b>Versione app</b>: ${escapeHtml(data.app_version || "")}`,
        `<b>Claude</b>: ${escapeHtml(data.claude_version || "non trovato")}`,
        `<b>Account Claude</b>: ${escapeHtml(account ? account.label : "non rilevato")}`,
        `<b>Codex</b>: ${escapeHtml(data.codex_version || "non trovato")}`,
        `<b>Account Codex</b>: ${escapeHtml(codexAccount ? codexAccount.label : "non rilevato")}`,
        `<b>Account registrati</b>: ${escapeHtml(String(accounts.length))}`,
        `<b>Chat Claude</b>: ${data.claude_chat_count || 0}`,
        `<b>Task Codex</b>: ${data.codex_chat_count || 0}`,
        `<b>Accodabili</b>: ${data.queueable_chat_count}`,
        `<b>Coda</b>: ${escapeHtml(data.queue_file || "")}`,
        `<b>CLI Claude</b>: ${escapeHtml(data.claude_exe || "non trovato")}`,
        `<b>CLI Codex</b>: ${escapeHtml(data.codex_exe || "non trovato")}`,
      ].join("<br>");
      renderRunner(data.runner || {});
    }

    function renderRunner(runner) {
      state.runner = runner || {};
      const running = !!runner.running;
      $("runner-dot").className = "dot " + (running ? "on" : "off");
      $("runner-text").textContent = running ? `Runner attivo${runner.pid ? ` #${runner.pid}` : ""}` : "Runner fermo";
      $("start-btn").disabled = running;
      $("stop-btn").disabled = !running;
      if (runner.log_tail) log(runner.log_tail);
    }

    function selectedChat() {
      return state.chats.find((chat) => chat.session_id === state.selected) || null;
    }

    function accountText(chat) {
      if (!chat) return "";
      const provider = chat.provider === "codex" ? "Codex" : "Claude";
      if (chat.account_status === "active") return `${provider} attivo: ${chat.account_label || chat.account_short_key || ""}`;
      if (chat.account_status === "other") return `altro account: ${chat.account_label || chat.account_short_key || ""}`;
      if (chat.account_status === "known") return `account: ${chat.account_label || chat.account_short_key || ""}`;
      return "account non associato";
    }

    function renderSelect(select, currentValue, options, previousValue) {
      const normalizedCurrent = currentValue || "";
      const values = [...options];
      if (normalizedCurrent && !values.includes(normalizedCurrent)) values.unshift(normalizedCurrent);
      select.innerHTML = "";
      const base = document.createElement("option");
      base.value = "";
      base.textContent = normalizedCurrent ? `Chat (${normalizedCurrent})` : "Chat";
      select.appendChild(base);
      for (const value of values) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        select.appendChild(option);
      }
      select.value = previousValue && values.includes(previousValue) ? previousValue : "";
    }

    function renderSettingsControls() {
      const chat = selectedChat();
      const isCodex = !!(chat && chat.provider === "codex");
      const reset = state.settingsSession !== state.selected;
      const previousModel = reset ? "" : $("model-select").value;
      const previousEffort = reset ? "" : $("effort-select").value;
      const previousPermission = reset ? "" : $("permission-select").value;
      const previousApproval = reset ? "" : $("approval-select").value;
      renderSelect($("model-select"), chat && chat.model, isCodex ? CODEX_MODEL_OPTIONS : CLAUDE_MODEL_OPTIONS, previousModel);
      renderSelect($("effort-select"), chat && chat.effort_level, EFFORT_OPTIONS, previousEffort);
      renderSelect(
        $("permission-select"),
        chat && (isCodex ? chat.sandbox_mode : chat.permission_mode),
        isCodex ? SANDBOX_OPTIONS : PERMISSION_OPTIONS,
        previousPermission,
      );
      renderSelect($("approval-select"), chat && chat.approval_policy, APPROVAL_OPTIONS, previousApproval);
      $("permission-field").childNodes[0].nodeValue = isCodex ? "Sandbox" : "Permessi";
      $("approval-field").classList.toggle("hidden", !isCodex);
      const disabled = !(chat && chat.can_queue);
      $("model-select").disabled = disabled;
      $("effort-select").disabled = disabled;
      $("permission-select").disabled = disabled;
      $("approval-select").disabled = disabled || !isCodex;
      $("priority-select").disabled = disabled;
      $("add-btn").disabled = disabled;
      state.settingsSession = state.selected;
      renderTransferButton();
    }

    function selectedSettingPayload() {
      const chat = selectedChat();
      const isCodex = !!(chat && chat.provider === "codex");
      return {
        model_override: $("model-select").value,
        effort_level_override: $("effort-select").value,
        permission_mode_override: isCodex ? "" : $("permission-select").value,
        sandbox_mode_override: isCodex ? $("permission-select").value : "",
        approval_policy_override: isCodex ? $("approval-select").value : "",
        priority: Number($("priority-select").value || 100),
      };
    }

    function renderAutoButton() {
      const auto = state.autoContinue;
      const selected = selectedChat();
      const selectedAvailable = !!(selected && selected.can_queue);
      const activeForSelected = !!(auto && auto.enabled && auto.session_id === state.selected);
      const activeOther = !!(auto && auto.enabled && auto.session_id !== state.selected);
      $("auto-btn").disabled = !selectedAvailable || state.autoBusy;
      $("auto-btn").className = activeForSelected ? "danger" : "blue";
      $("auto-btn").textContent = state.autoBusy
        ? (state.autoBusyAction === "disable" ? "Disattivo..." : "Attivo...")
        : activeForSelected
        ? "Disattiva auto-continua"
        : (activeOther ? "Sposta auto-continua" : "Auto-continua");
    }

    function transferAvailable(chat) {
      if (!chat || chat.remote_kind) return false;
      if (chat.provider === "codex") return chat.account_status !== "active";
      if (chat.source_key === "claude_windows_app" && chat.account_status === "active") return false;
      return true;
    }

    function renderTransferButton() {
      const chat = selectedChat();
      const available = transferAvailable(chat);
      const moveToggle = $("transfer-move");
      const isCodex = !!(chat && chat.provider === "codex");
      moveToggle.disabled = isCodex || !available || state.transferBusy || !(chat && chat.account_status === "other");
      if (moveToggle.disabled) moveToggle.checked = false;
      $("transfer-btn").disabled = !available || state.transferBusy;
      $("transfer-btn").textContent = state.transferBusy
        ? "Importo..."
        : isCodex
        ? "Copia nell'account ChatGPT attivo"
        : chat && chat.account_status === "other"
        ? (moveToggle.checked ? "Sposta da altro account" : "Importa da altro account")
        : "Importa nell'account attivo";
    }

    function formatDateTime(value) {
      if (!value) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return DATE_TIME_FORMAT.format(date);
    }

    function formatDuration(seconds) {
      const total = Math.max(0, Math.round(Number(seconds) || 0));
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const secs = total % 60;
      if (hours) return `${hours}h ${minutes}m`;
      if (minutes) return `${minutes}m ${secs}s`;
      return `${secs}s`;
    }

    function autoStatusText(auto) {
      if (!auto) return "";
      const shortId = (auto.session_id || "").slice(0, 8);
      const title = auto.title ? ` · ${auto.title}` : "";
      const runnerText = state.runner && state.runner.running ? "runner attivo" : "runner fermo";
      if (auto.enabled) {
        if (auto.status === "armed" || auto.status === "monitoring") {
          const wait = auto.next_check_in_seconds ? `, nuovo controllo tra ${formatDuration(auto.next_check_in_seconds)}` : "";
          return `Auto-continua attivo su ${shortId}${title}: monitoraggio del limite, nessun messaggio inviato${wait}, ${runnerText}.`;
        }
        if (auto.status === "waiting_limit") {
          const wait = auto.next_check_in_seconds ? `, controllo tra ${formatDuration(auto.next_check_in_seconds)}` : "";
          return `Auto-continua attivo su ${shortId}${title}: in attesa del limite${auto.not_before ? ` fino a ${formatDateTime(auto.not_before)}` : ""}${wait}, ${runnerText}.`;
        }
        if (auto.status === "sending") return `Auto-continua attivo su ${shortId}${title}: invio di continua in corso, ${runnerText}.`;
        return `Auto-continua attivo su ${shortId}${title}, ${runnerText}.`;
      }
      if (auto.status === "failed" || auto.status === "blocked" || auto.status === "blocked_permission") {
        return `Auto-continua ${auto.status} su ${shortId}${title}: ${auto.last_error || "errore"}.`;
      }
      if (auto.status === "done") return `Auto-continua completato su ${shortId}${title}.`;
      if (auto.status === "disabled") return `Auto-continua disattivato su ${shortId}${title}.`;
      return `Auto-continua ${auto.status || "spento"} su ${shortId}${title}.`;
    }

    function renderAutoFeedback(message = null, level = null) {
      const el = $("auto-feedback");
      const auto = state.autoContinue;
      const text = message ?? autoStatusText(auto);
      el.textContent = text || "";
      const resolvedLevel = level || (
        auto && (auto.status === "failed" || auto.status === "blocked" || auto.status === "blocked_permission")
          ? "error"
          : auto && auto.enabled && !(state.runner && state.runner.running)
          ? "warn"
          : auto && auto.enabled
          ? "ok"
          : ""
      );
      el.className = `meta auto-feedback ${resolvedLevel}`.trim();
    }

    function renderChats() {
      const filter = $("chat-filter").value.trim().toLowerCase();
      const provider = $("provider-filter").value;
      const list = $("chat-list");
      const chats = state.chats.filter((chat) => {
        const hay = [chat.title, chat.cwd, chat.session_id, chat.last_prompt, chat.source, chat.provider].join(" ").toLowerCase();
        return (!provider || chat.provider === provider) && hay.includes(filter);
      }).sort((a, b) => Date.parse(b.last_timestamp || 0) - Date.parse(a.last_timestamp || 0));
      list.innerHTML = "";
      if (!chats.length) {
        list.innerHTML = `<div class="empty">Nessuna chat</div>`;
        renderSettingsControls();
        renderAutoButton();
        renderTransferButton();
        renderAutoFeedback();
        return;
      }
      for (const chat of chats) {
        const btn = document.createElement("button");
        btn.className = "chat" + (state.selected === chat.session_id ? " active" : "") + (!chat.can_queue ? " view-only" : "");
        const location = chat.remote_cwd ? `${chat.remote_host || "ssh"}:${chat.remote_cwd}` : (chat.cwd || "");
        const accountBadge = accountText(chat);
        btn.innerHTML = `
          <span class="chat-title">${escapeHtml(chat.short_id)} · ${escapeHtml(chat.title || "Senza titolo")}</span>
          <span class="chat-sub">
            <span class="badge">${escapeHtml(chat.source || "")}</span>
            <span class="badge">${chat.provider === "codex" ? "Codex" : "Claude"}</span>
            ${chat.remote_host ? `<span class="badge">SSH ${escapeHtml(chat.remote_host)}</span>` : ""}
            <span class="badge">${chat.can_queue ? "utilizzabile" : "solo visibile"}</span>
            <span class="badge">${escapeHtml(accountBadge)}</span>
            ${chat.model ? `<span class="badge">${escapeHtml(chat.model)}</span>` : ""}
            ${chat.message_count >= 0 ? `${escapeHtml(String(chat.message_count || 0))} msg` : ""}
          </span>
          <span class="chat-sub">${escapeHtml(location)}</span>
          <span class="chat-sub">${escapeHtml(formatDateTime(chat.last_timestamp))}</span>
        `;
        btn.onclick = () => {
          state.selected = chat.session_id;
          state.settingsSession = null;
          $("selected-chat").textContent = `${chat.provider === "codex" ? "Codex" : "Claude"} · ${chat.short_id} · ${chat.title || location || ""} · ${chat.can_queue ? "utilizzabile" : "solo visibile"} · ${accountText(chat)}`;
          renderSettingsControls();
          renderAutoButton();
          renderAutoFeedback();
          renderChats();
        };
        list.appendChild(btn);
      }
      renderSettingsControls();
      renderAutoButton();
      renderTransferButton();
      renderAutoFeedback();
    }

    function renderQueue(data) {
      state.queue = data.items || [];
      state.autoContinue = data.auto_continue || null;
      renderRunner(data.runner || {});
      renderAutoButton();
      renderAutoFeedback();
      const recovery = data.recovery || null;
      const recoveryBox = recovery ? `
        <div class="recovery-box">
          Recovery attiva: il prossimo invio sara' <b>continua</b> sulla chat ${escapeHtml((recovery.session_id || "").slice(0, 8))}.
          ${recovery.not_before ? `<br><span class="meta">Non prima di ${escapeHtml(formatDateTime(recovery.not_before))}</span>` : ""}
        </div>
      ` : "";
      const auto = state.autoContinue;
      const runner = state.runner || {};
      const effective = auto && auto.fingerprint && auto.fingerprint.effective ? auto.fingerprint.effective : {};
      const autoIsCodex = !!(auto && auto.provider === "codex");
      const autoDetails = auto ? [
        `Stato: ${escapeHtml(auto.status || "spento")}`,
        `Runner: ${runner.running ? `attivo${runner.pid ? ` #${escapeHtml(runner.pid)}` : ""}` : "fermo"}`,
        `Origine: ${escapeHtml(auto.source || (autoIsCodex ? "Codex App" : "Claude Code"))}`,
        !autoIsCodex && auto.source_key === "claude_windows_app" ? "Integrazione IDE: non usata" : "",
        `Modello: ${escapeHtml(auto.model_override || effective.model || "chat")}`,
        `Effort: ${escapeHtml(auto.effort_level_override || effective.effortLevel || "chat")}`,
        autoIsCodex
          ? `Sandbox: ${escapeHtml(auto.sandbox_mode_override || effective.sandboxMode || "task")}`
          : `Permessi: ${escapeHtml(auto.permission_mode_override || effective.permissionMode || "chat")}`,
        autoIsCodex ? `Approvazioni: ${escapeHtml(auto.approval_policy_override || effective.approvalPolicy || "task")}` : "",
        `Tentativi: ${escapeHtml(String(auto.attempts || 0))}`,
        auto.sending_started_at ? `Invio iniziato: ${escapeHtml(formatDateTime(auto.sending_started_at))}` : "",
        auto.not_before && auto.status !== "sending" ? `Prossimo tentativo: ${escapeHtml(formatDateTime(auto.not_before))}` : "",
        auto.next_check_in_seconds ? `Prossimo controllo: tra ${escapeHtml(formatDuration(auto.next_check_in_seconds))}` : "",
        auto.last_check_at ? `Ultimo check: ${escapeHtml(formatDateTime(auto.last_check_at))}` : "",
        auto.updated_at ? `Aggiornato: ${escapeHtml(formatDateTime(auto.updated_at))}` : "",
      ].filter(Boolean).join("<br>") : "";
      const autoBox = auto ? `
        <div class="auto-box">
          Auto-continua: <b>${auto.enabled ? "attivo" : escapeHtml(auto.status || "spento")}</b>
          sulla chat ${escapeHtml((auto.session_id || "").slice(0, 8))}
          ${auto.title ? ` · ${escapeHtml(auto.title)}` : ""}.
          <br><span class="meta">${autoDetails}</span>
          ${auto.last_error && auto.status !== "sending" ? `<br><span class="meta">${escapeHtml(auto.last_error)}</span>` : ""}
        </div>
      ` : "";
      if (!state.queue.length) {
        $("queue").innerHTML = autoBox + recoveryBox + `<div class="empty">Coda vuota</div>`;
        return;
      }
      const rows = state.queue.map((item) => `
        <tr>
          <td style="width:82px"><span class="status-pill ${escapeHtml(item.status || "")}">${escapeHtml(item.status || "")}</span></td>
          <td style="width:90px">${escapeHtml(item.id || "")}<br><span class="meta">${escapeHtml(String(item.attempts || 0))} tent.</span></td>
          <td><span class="badge">${item.provider === "codex" ? "Codex" : "Claude"}</span> ${escapeHtml(item.title || item.session_id || "")}<br><span class="meta">Priorita': ${escapeHtml(String(item.priority ?? 100))} · ${escapeHtml((item.prompt || "").slice(0, 180))}</span></td>
          <td style="width:178px">
            <button onclick="removeItem('${escapeAttr(item.id || "")}')">Rimuovi</button>
            <button onclick="resetItem('${escapeAttr(item.id || "")}')">Reset</button>
          </td>
        </tr>
      `).join("");
      $("queue").innerHTML = autoBox + recoveryBox + `<table><thead><tr><th>Stato</th><th>ID</th><th>Elemento</th><th>Azioni</th></tr></thead><tbody>${rows}</tbody></table>`;
    }

    async function refreshAll() {
      if (state.refreshBusy) return;
      state.refreshBusy = true;
      try {
        const queue = await api("/api/queue");
        renderQueue(queue);
        const [doctorResult, chatsResult] = await Promise.allSettled([
          api("/api/doctor"),
          api("/api/chats"),
        ]);
        if (doctorResult.status === "fulfilled") {
          renderDoctor(doctorResult.value);
        } else {
          appendLog("Errore ambiente: " + doctorResult.reason.message);
        }
        if (chatsResult.status === "fulfilled") {
          state.chats = chatsResult.value.chats || [];
          renderChats();
        } else {
          appendLog("Errore chat: " + chatsResult.reason.message);
        }
      } catch (err) {
        appendLog("Errore refresh: " + err.message);
      } finally {
        state.refreshBusy = false;
      }
    }

    async function addMessages() {
      try {
        const selected = state.chats.find((chat) => chat.session_id === state.selected);
        if (selected && selected.account_status === "other") throw new Error("Questa chat/task appartiene a un altro account: cambia account o associala prima di inviare.");
        if (selected && !selected.can_queue) throw new Error("Questa chat/task e' solo visibile e non e' accodabile.");
        const result = await api("/api/add", {
          method: "POST",
          body: JSON.stringify({ session_id: state.selected, messages: $("messages").value, ...selectedSettingPayload() }),
        });
        $("messages").value = "";
        appendLog(`Accodati ${result.added} messaggi.`);
        await refreshAll();
      } catch (err) {
        appendLog("Errore add: " + err.message);
      }
    }

    async function checkSettings() {
      try {
        const result = await api("/api/check-settings", { method: "POST", body: "{}" });
        const lines = result.results.map((r) => `${r.id}: ${r.ok ? "ok" : r.diffs.join("; ")}`);
        appendLog(lines.length ? lines.join("\n") : "Nessun elemento in coda.");
      } catch (err) {
        appendLog("Errore impostazioni: " + err.message);
      }
    }

    async function toggleAutoContinue() {
      const auto = state.autoContinue;
      const activeForSelected = !!(auto && auto.enabled && auto.session_id === state.selected);
      state.autoBusy = true;
      state.autoBusyAction = activeForSelected ? "disable" : "enable";
      renderAutoButton();
      renderAutoFeedback(activeForSelected ? "Disattivazione auto-continua..." : "Attivazione auto-continua: preparo il contesto e controllo le impostazioni remote...", "warn");
      appendLog(activeForSelected ? "Disattivazione auto-continua..." : "Attivazione auto-continua in corso...");
      try {
        const result = await api("/api/auto-continue", {
          method: "POST",
          body: JSON.stringify({ enabled: !activeForSelected, session_id: state.selected, ...selectedSettingPayload() }),
        });
        state.autoContinue = result.auto_continue || null;
        const message = autoStatusText(state.autoContinue) || (activeForSelected ? "Auto-continua disattivato." : "Auto-continua attivato.");
        appendLog(message);
        renderAutoFeedback(message, state.autoContinue && (state.autoContinue.status === "failed" || state.autoContinue.status === "blocked") ? "error" : "ok");
        await refreshAll();
      } catch (err) {
        renderAutoFeedback("Errore auto-continua: " + err.message, "error");
        appendLog("Errore auto-continua: " + err.message);
      } finally {
        state.autoBusy = false;
        state.autoBusyAction = null;
        renderAutoButton();
      }
    }

    async function transferChat() {
      const selected = selectedChat();
      if (!transferAvailable(selected)) return;
      const move = $("transfer-move").checked && selected.account_status === "other";
      const isCodex = selected.provider === "codex";
      const action = move ? "Spostare" : "Importare";
      const label = selected.account_status === "other" ? "da un altro account" : "dalla transcript locale";
      const question = isCodex
        ? "Creare una nuova task nell'account ChatGPT/Codex attivo? Verrà copiato l'intero contesto in un nuovo ID; archiviazione, ripristino ed eliminazione resteranno sincronizzati tra le due copie locali."
        : `${action} questa chat ${label} nell'account Claude attivo?`;
      if (!window.confirm(question)) return;
      state.transferBusy = true;
      renderTransferButton();
      try {
        const result = await api("/api/transfer-chat", {
          method: "POST",
          body: JSON.stringify({ session_id: state.selected, move }),
        });
        state.chats = result.chats || state.chats;
        const transfer = result.transfer || {};
        appendLog(`Import completato: ${transfer.status || "ok"} · ${transfer.title || selected.title || state.selected}`);
        renderChats();
        await refreshAll();
      } catch (err) {
        appendLog("Errore import account: " + err.message);
      } finally {
        state.transferBusy = false;
        renderTransferButton();
      }
    }

    async function removeItem(id) {
      await api("/api/remove", { method: "POST", body: JSON.stringify({ id }) });
      await refreshAll();
    }

    async function resetItem(id) {
      await api("/api/reset", { method: "POST", body: JSON.stringify({ id }) });
      await refreshAll();
    }

    async function startRunner() {
      const data = await api("/api/runner/start", { method: "POST", body: JSON.stringify({ poll_seconds: 60 }) });
      renderRunner(data);
      await refreshAll();
    }

    async function stopRunner() {
      const data = await api("/api/runner/stop", { method: "POST", body: "{}" });
      renderRunner(data);
      await refreshAll();
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
    }
    function escapeAttr(value) {
      return escapeHtml(value).replace(/`/g, "&#96;");
    }

    $("refresh-btn").onclick = refreshAll;
    $("add-btn").onclick = addMessages;
    $("check-btn").onclick = checkSettings;
    $("auto-btn").onclick = toggleAutoContinue;
    $("transfer-btn").onclick = transferChat;
    $("transfer-move").onchange = renderTransferButton;
    $("start-btn").onclick = startRunner;
    $("stop-btn").onclick = stopRunner;
    $("chat-filter").oninput = renderChats;
    $("provider-filter").onchange = renderChats;

    refreshAll();
    setInterval(refreshAll, 5000);
  </script>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interfaccia web locale per Claude + Codex Queue.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--windows-home")
    parser.add_argument("--state-dir")
    parser.add_argument("--claude")
    parser.add_argument("--codex")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = app.resolve_paths(args.windows_home, args.state_dir)
    claude_exe = app.find_claude_executable(paths, args.claude)
    codex_exe = app.find_codex_executable(paths, args.codex)
    state = WebState(paths, claude_exe, codex_exe)

    class Handler(QueueRequestHandler):
        pass

    Handler.state = state
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        state.stop_runner()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
