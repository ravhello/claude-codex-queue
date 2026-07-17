from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

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
    last_user_message = chat.last_user_message or chat.last_prompt
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
        "last_user_message": last_user_message,
        "last_user_message_loaded": last_user_message is not None,
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
        "account_copies": list(chat.account_copies),
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


def auto_continue_payload(
    queue: dict[str, Any],
    selected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    states = app.auto_continue_states(queue)
    return {
        "auto_continue": selected if isinstance(selected, dict) else app.sync_auto_continue_legacy(queue),
        "auto_continues": states,
    }


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
        self._chats_generation = 0
        self._chats_desktop_signature = app.claude_desktop_change_signature(paths)
        self._version_cache: str | None = None
        self._version_cache_at = 0.0
        self._codex_version_cache: str | None = None
        self._codex_version_cache_at = 0.0
        self._account_sync_stop = threading.Event()
        self._account_sync_thread: threading.Thread | None = None
        self._account_sync_poll_seconds = 10.0
        self._account_sync_full_poll_seconds = 60.0
        self._account_sync_last_check_at: str | None = None
        self._account_sync_last_full_check_at: str | None = None
        self._account_sync_cycle_started_at: str | None = None
        self._account_sync_last_duration_seconds: float | None = None
        self._account_sync_last_error: str | None = None
        self._account_sync_last_result: dict[str, Any] = {}
        self._runner_monitor_stop = threading.Event()
        self._runner_monitor_thread: threading.Thread | None = None
        self._runner_monitor_poll_seconds = 5.0
        self._runner_monitor_last_check_at: str | None = None
        self._runner_monitor_last_error: str | None = None

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

    def command_is_runner_for_state(self, command: str | None) -> bool:
        if not command:
            return False
        padded = f" {command} "
        state_dir = str(self.paths.state_dir)
        state_matches = (
            f" --state-dir {state_dir} " in padded
            or f" --state-dir={state_dir} " in padded
        )
        return bool(
            state_matches
            and any(f" -m {module} " in padded for module in RUNNER_MODULES)
            and " run " in padded
        )

    def runner_process_from_pid_file(self) -> tuple[int | None, str | None]:
        try:
            pid = int(self.runner_pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None, None
        command = self.process_command(pid)
        if not self.command_is_runner_for_state(command):
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
            if self.command_is_runner_for_state(command) and ".web" not in command:
                return pid, command
        return None, None

    def runner_status(self) -> dict[str, Any]:
        with self.lock:
            running = self.runner is not None and self.runner.poll() is None
            exit_code = None if self.runner is None or running else self.runner.returncode
            pid = self.runner.pid if self.runner is not None and running else None
            command = " ".join(self.runner.args) if self.runner is not None and running and isinstance(self.runner.args, list) else None
            monitor = self._runner_monitor_thread
            automatic = bool(monitor and monitor.is_alive())
            automatic_poll_seconds = self._runner_monitor_poll_seconds
            automatic_last_check_at = self._runner_monitor_last_check_at
            automatic_last_error = self._runner_monitor_last_error
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
            "automatic": automatic,
            "automatic_poll_seconds": automatic_poll_seconds,
            "automatic_last_check_at": automatic_last_check_at,
            "automatic_last_error": automatic_last_error,
        }

    def quick_chats(self) -> list[app.Chat]:
        desktop_chats = app.discover_claude_windows_app_sessions(
            self.paths,
            sync_accounts=False,
            active_only=False,
        )
        tombstones = app.desktop_tombstoned_session_ids(self.paths)
        desktop_chats = [chat for chat in desktop_chats if chat.session_id not in tombstones]
        claude_chats = app.annotate_chats_with_accounts(
            self.paths,
            app.merge_claude_chat_sources([], desktop_chats),
        )
        with self.lock:
            codex_chats = [
                chat for chat in self._chats_cache if chat.provider == app.PROVIDER_CODEX
            ]
        return sorted(claude_chats + codex_chats, key=app.chat_sort_key, reverse=True)

    def refresh_chats_background(self) -> None:
        now = time.monotonic()
        started_signature = app.claude_desktop_change_signature(self.paths)
        with self.lock:
            if self._chats_refreshing:
                return
            self._chats_refreshing = True
            self._chats_refresh_started_at = now
            generation = self._chats_generation

        def worker() -> None:
            try:
                chats = app.discover_agent_chats(
                    self.paths,
                    sync_desktop_accounts=False,
                    active_desktop_only=False,
                )
            except Exception:
                chats = []
            finished_signature = app.claude_desktop_change_signature(self.paths)
            retry = False
            with self.lock:
                if generation != self._chats_generation:
                    return
                if finished_signature != started_signature:
                    self._chats_generation += 1
                    self._chats_desktop_signature = finished_signature
                    self._chats_cache = []
                    self._chats_cache_at = 0.0
                    retry = True
                elif chats:
                    self._chats_cache = chats
                    self._chats_cache_at = time.monotonic()
                self._chats_refreshing = False
            if retry:
                self.refresh_chats_background()

        threading.Thread(target=worker, daemon=True).start()

    def chats(self, max_age_seconds: int = 15) -> list[app.Chat]:
        now = time.monotonic()
        signature = app.claude_desktop_change_signature(self.paths)
        signature_changed = False
        with self.lock:
            if signature != self._chats_desktop_signature:
                signature_changed = True
                self._chats_generation += 1
                self._chats_desktop_signature = signature
                self._chats_cache = [
                    chat for chat in self._chats_cache if chat.provider == app.PROVIDER_CODEX
                ]
                self._chats_cache_at = 0.0
                self._chats_refreshing = False
                self._chats_refresh_started_at = 0.0
            generation = self._chats_generation
            if (
                not signature_changed
                and self._chats_cache
                and now - self._chats_cache_at < max_age_seconds
            ):
                return list(self._chats_cache)
            cached = list(self._chats_cache)
            refreshing = self._chats_refreshing
        if cached and not signature_changed:
            if not refreshing:
                self.refresh_chats_background()
            return cached
        try:
            chats = self.quick_chats()
        except Exception:
            chats = []
        with self.lock:
            if chats and generation == self._chats_generation:
                self._chats_cache = chats
                self._chats_cache_at = time.monotonic()
        self.refresh_chats_background()
        return list(chats)

    def cached_chats(self) -> list[app.Chat]:
        with self.lock:
            return list(self._chats_cache)

    def invalidate_chats(self) -> None:
        signature = app.claude_desktop_change_signature(self.paths)
        with self.lock:
            self._chats_generation += 1
            self._chats_desktop_signature = signature
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
            command = [str(self.claude_exe), "--version"]
            if app.is_wsl() and app.codex_executable_is_windows(self.claude_exe):
                command = app.local_windows_hidden_command(
                    [app.local_to_windows_path(self.claude_exe), "--version"]
                )
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                timeout=30,
                **app.background_process_kwargs(),
            )
            version = (result.stdout or result.stderr).strip()
        except subprocess.TimeoutExpired:
            version = "verifica versione non disponibile"
        except (OSError, subprocess.SubprocessError):
            version = "verifica versione non riuscita"
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
            result = app.run_codex_cli_command(self.codex_exe, ["--version"], timeout=30)
            version = (result.stdout or result.stderr).strip()
        except subprocess.TimeoutExpired:
            version = "verifica versione non disponibile"
        except (OSError, subprocess.SubprocessError):
            version = "verifica versione non riuscita"
        with self.lock:
            self._codex_version_cache = version
            self._codex_version_cache_at = now
        return version

    def sync_linked_accounts_once(self, *, include_claude_transcripts: bool = True) -> dict[str, Any]:
        started = time.monotonic()
        with self.lock:
            self._account_sync_cycle_started_at = app.now_utc()
        results: dict[str, Any] = {}
        errors: list[str] = []
        changed = False
        try:
            claude = app.sync_claude_desktop_accounts(
                self.paths,
                include_transcripts=include_claude_transcripts,
            )
            results["claude"] = claude
            changed = any(
                int(claude.get(key) or 0) > 0
                for key in [
                    "created",
                    "transcripts_created",
                    "updated",
                    "repaired",
                    "deduped",
                    "archived",
                    "unarchived",
                    "deleted",
                    "removed",
                    "artifacts_created",
                    "artifacts_updated",
                    "artifacts_deleted",
                    "artifacts_removed",
                    "code_artifact_copies_created",
                    "code_artifact_transcripts_created",
                ]
            )
            errors.extend(str(error) for error in claude.get("code_artifact_errors", []) if error)
        except Exception as exc:
            errors.append(f"Claude: {exc}")
        try:
            codex = app.sync_codex_linked_threads(self.paths)
            results["codex"] = codex
            changed = changed or int(codex.get("updated") or 0) > 0 or int(codex.get("deleted") or 0) > 0
            errors.extend(str(error) for error in codex.get("errors", []) if error)
        except Exception as exc:
            errors.append(f"Codex: {exc}")

        with self.lock:
            self._account_sync_last_check_at = app.now_utc()
            if include_claude_transcripts:
                self._account_sync_last_full_check_at = self._account_sync_last_check_at
            self._account_sync_cycle_started_at = None
            self._account_sync_last_duration_seconds = round(time.monotonic() - started, 3)
            self._account_sync_last_error = " | ".join(errors) if errors else None
            self._account_sync_last_result = results
        if changed:
            self.invalidate_chats()
        return {
            **results,
            "changed": changed,
            "errors": errors,
            "full_scan": include_claude_transcripts,
        }

    def account_sync_status(self) -> dict[str, Any]:
        with self.lock:
            thread = self._account_sync_thread
            return {
                "running": bool(thread and thread.is_alive()),
                "poll_seconds": self._account_sync_poll_seconds,
                "full_poll_seconds": self._account_sync_full_poll_seconds,
                "last_check_at": self._account_sync_last_check_at,
                "last_full_check_at": self._account_sync_last_full_check_at,
                "cycle_started_at": self._account_sync_cycle_started_at,
                "in_progress": self._account_sync_cycle_started_at is not None,
                "last_duration_seconds": self._account_sync_last_duration_seconds,
                "last_error": self._account_sync_last_error,
                "last_result": self._account_sync_last_result,
            }

    def wait_for_account_sync_trigger(
        self,
        wait_seconds: float,
        signature: tuple[str, ...],
    ) -> tuple[bool, tuple[str, ...]]:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while not self._account_sync_stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, signature
            if self._account_sync_stop.wait(min(1.0, remaining)):
                return True, signature
            current = app.claude_desktop_change_signature(self.paths)
            if current != signature:
                return False, current
        return True, signature

    def start_account_sync_monitor(
        self,
        poll_seconds: float = 10.0,
        full_poll_seconds: float = 60.0,
    ) -> dict[str, Any]:
        with self.lock:
            if self._account_sync_thread is not None and self._account_sync_thread.is_alive():
                return self.account_sync_status()
            self._account_sync_poll_seconds = max(1.0, float(poll_seconds))
            self._account_sync_full_poll_seconds = max(
                self._account_sync_poll_seconds,
                float(full_poll_seconds),
            )
            self._account_sync_stop.clear()

            def worker() -> None:
                next_full_at = 0.0
                signature = app.claude_desktop_change_signature(self.paths)
                while not self._account_sync_stop.is_set():
                    cycle_started = time.monotonic()
                    full_scan = cycle_started >= next_full_at
                    result = self.sync_linked_accounts_once(include_claude_transcripts=full_scan)
                    if full_scan:
                        next_full_at = cycle_started + self._account_sync_full_poll_seconds
                    signature = app.claude_desktop_change_signature(self.paths)
                    elapsed = time.monotonic() - cycle_started
                    claude_result = result.get("claude") if isinstance(result.get("claude"), dict) else {}
                    confirmation_due = (
                        int(claude_result.get("pending_deletions") or 0) > 0
                        or int(claude_result.get("pending_artifact_deletions") or 0) > 0
                    )
                    wait_seconds = 1.0 if confirmation_due else max(0.05, self._account_sync_poll_seconds - elapsed)
                    stopped, signature = self.wait_for_account_sync_trigger(wait_seconds, signature)
                    if stopped:
                        break

            self._account_sync_thread = threading.Thread(
                target=worker,
                name="claude-codex-account-sync",
                daemon=True,
            )
            self._account_sync_thread.start()
        return self.account_sync_status()

    def stop_account_sync_monitor(self) -> dict[str, Any]:
        self._account_sync_stop.set()
        with self.lock:
            thread = self._account_sync_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        return self.account_sync_status()

    def start_runner(self, poll_seconds: int = 60) -> dict[str, Any]:
        existing = self.runner_status()
        if existing["running"]:
            return existing
        with self.lock:
            if self.runner is not None and self.runner.poll() is None:
                return self.runner_status()
            self.runner_log.parent.mkdir(parents=True, exist_ok=True)
            log_handle = self.runner_log.open("a", encoding="utf-8")
            try:
                log_handle.write(f"\n--- runner start {app.now_utc()} ---\n")
                log_handle.flush()
                command = self.base_command() + ["run", "--poll-seconds", str(poll_seconds)]
                self.runner = subprocess.Popen(
                    command,
                    cwd=PROJECT_ROOT,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    **app.background_process_kwargs(),
                )
            finally:
                log_handle.close()
            self.runner_pid_file.write_text(str(self.runner.pid), encoding="utf-8")
        time.sleep(0.2)
        return self.runner_status()

    def queue_requires_runner(self) -> bool:
        queue = app.load_queue(self.paths.queue_file)
        return bool(
            app.pending_items(queue)
            or app.active_recovery(queue)
            or app.active_auto_continue(queue)
        )

    def ensure_runner_for_pending_work(self) -> dict[str, Any]:
        requires_runner = False
        error: str | None = None
        try:
            requires_runner = self.queue_requires_runner()
            status = self.runner_status()
            if requires_runner and not status["running"]:
                status = self.start_runner()
                if not status["running"] and status.get("exit_code") not in {None, 0}:
                    error = f"Runner terminato con codice {status['exit_code']}"
        except Exception as exc:
            status = self.runner_status()
            error = str(exc)
        with self.lock:
            self._runner_monitor_last_check_at = app.now_utc()
            self._runner_monitor_last_error = error
        return {"required": requires_runner, "runner": status, "error": error}

    def start_runner_monitor(self, poll_seconds: float = 5.0) -> dict[str, Any]:
        with self.lock:
            if self._runner_monitor_thread is not None and self._runner_monitor_thread.is_alive():
                return self.runner_status()
            self._runner_monitor_poll_seconds = max(1.0, float(poll_seconds))
            self._runner_monitor_stop.clear()

            def worker() -> None:
                while not self._runner_monitor_stop.is_set():
                    self.ensure_runner_for_pending_work()
                    if self._runner_monitor_stop.wait(self._runner_monitor_poll_seconds):
                        break

            self._runner_monitor_thread = threading.Thread(
                target=worker,
                name="claude-codex-runner-monitor",
                daemon=True,
            )
            self._runner_monitor_thread.start()
        return self.runner_status()

    def stop_runner_monitor(self) -> dict[str, Any]:
        self._runner_monitor_stop.set()
        with self.lock:
            thread = self._runner_monitor_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
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
            elif parsed.path == "/api/chat-preview":
                session_id = (parse_qs(parsed.query).get("session_id") or [""])[0]
                chat = app.find_chat_by_session(self.state.cached_chats(), session_id)
                if chat is None:
                    self.state.refresh_chats_background()
                    self.send_json(
                        {"error": "Elenco chat ancora in aggiornamento."},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                else:
                    self.send_json(
                        {
                            "session_id": chat.session_id,
                            "last_user_message": app.latest_user_message_for_chat(chat),
                        }
                    )
            elif parsed.path == "/api/queue":
                queue = app.load_queue(self.state.paths.queue_file)
                self.send_json(
                    {
                        "items": queue.get("items", []),
                        "recovery": app.active_recovery(queue),
                        **auto_continue_payload(queue),
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
            "app_version": __version__,
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
            "active_account": app.active_desktop_account_public(self.state.paths),
            "active_claude_code_account": app.account_public_dict(app.active_claude_account(self.state.paths)),
            "active_codex_account": app.account_public_dict(app.active_codex_account(self.state.paths)),
            "account_index": app.account_index_public(self.state.paths),
            "account_sync": self.state.account_sync_status(),
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
            **auto_continue_payload(queue),
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
            **auto_continue_payload(queue),
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
            **auto_continue_payload(queue),
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
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=120,
            **app.background_process_kwargs(),
        )
        queue = app.load_queue(self.state.paths.queue_file)
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "items": queue.get("items", []),
            "recovery": app.active_recovery(queue),
            **auto_continue_payload(queue),
            "summary": queue_summary(queue),
        }

    def api_auto_continue(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(payload.get("enabled"))
        session_id = str(payload.get("session_id") or "")
        queue = app.load_queue(self.state.paths.queue_file)
        if not enabled:
            states = app.auto_continue_states(queue)
            targets = (
                [state for state in states if state.get("session_id") == session_id]
                if session_id
                else app.active_auto_continues(queue)
            )
            idle_monitor = any(
                state.get("status") in {
                    "armed",
                    "monitoring",
                    "waiting_limit",
                    "waiting_retry",
                }
                for state in targets
            )
            for auto_continue in targets:
                app.mark_auto_continue_cancelled(self.state.paths.queue_file, auto_continue)
                app.update_auto_continue_state(
                    auto_continue,
                    "disabled",
                    enabled=False,
                    disabled_at=app.now_utc(),
                    sending_started_at=None,
                    next_check_in_seconds=None,
                )
            app.sync_auto_continue_legacy(queue)
            app.save_queue(self.state.paths.queue_file, queue)
            runner = self.state.runner_status()
            if (
                idle_monitor
                and not app.active_auto_continues(queue)
                and not app.pending_items(queue)
                and not app.active_recovery(queue)
            ):
                runner = self.state.stop_runner()
            selected = targets[0] if len(targets) == 1 else None
            return {
                "items": queue.get("items", []),
                "recovery": app.active_recovery(queue),
                **auto_continue_payload(queue, selected),
                "summary": queue_summary(queue),
                "runner": runner,
            }

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
        execution_fields = app.chat_execution_fields(chat)
        reset_at = app.latest_rate_limit_reset_from_chat(chat)
        not_before = None
        status = "armed"
        last_error = None
        if reset_at is not None:
            ready_at = reset_at + app.dt.timedelta(seconds=app.RATE_LIMIT_RESET_DELAY_SECONDS)
            not_before = ready_at.astimezone().replace(microsecond=0).isoformat()
            status = "waiting_limit"
            last_error = "Usage limit attivo: attendo reset + 1 minuto prima di riprovare."

        existing = app.find_auto_continue_state(queue, chat.session_id)
        if existing is not None:
            app.mark_auto_continue_cancelled(self.state.paths.queue_file, existing)
        created_at = app.now_utc()
        auto_continue = {
            "activation_id": str(uuid.uuid4()),
            "enabled": True,
            "status": status,
            "created_at": created_at,
            "updated_at": created_at,
            "session_id": chat.session_id,
            "title": chat.title,
            "cwd": chat.cwd,
            "source": chat.source,
            "provider": chat.provider,
            "jsonl_path": str(chat.jsonl_path),
            "prompt": "Try again" if app.claude_item_is_desktop(execution_fields) else app.RECOVERY_PROMPT,
            "action": (
                "claude_try_again"
                if app.claude_item_is_desktop(execution_fields)
                else "codex_inspect"
                if chat.provider == app.PROVIDER_CODEX
                else "continue_interrupted"
            ),
            "recovery_prompt_preview": (
                "Try again"
                if app.claude_item_is_desktop(execution_fields)
                else "Analisi automatica del turno"
                if chat.provider == app.PROVIDER_CODEX
                else app.RECOVERY_PROMPT
            ),
            "recovery_followup_count": 0,
            "monitor_limit": True,
            "persistent": app.claude_item_is_desktop(execution_fields),
            "attempts": 0,
            "actions_completed": 0,
            "not_before": not_before,
            "last_error": last_error,
            "last_log": None,
            "last_observation": None,
            "fingerprint": app.settings_fingerprint(self.state.paths, chat),
            "allow_cwd_fallback": False,
            "cwd_fallback": None,
            "last_check_at": created_at,
            **overrides,
            **execution_fields,
        }
        app.set_auto_continue_state(queue, auto_continue)
        app.save_queue(self.state.paths.queue_file, queue)
        runner = self.state.start_runner(60)
        return {
            "items": queue.get("items", []),
            "recovery": app.active_recovery(queue),
            **auto_continue_payload(queue, auto_continue),
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
    .chat-last-message {
      color: var(--ink);
      display: -webkit-box;
      font-size: 12px;
      line-height: 1.4;
      margin-top: 5px;
      overflow: hidden;
      overflow-wrap: anywhere;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
    }
    .chat-last-message b { color: var(--muted); font-weight: 650; }
    .chat-last-message.unavailable { color: var(--muted); }
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
    @media (max-width: 1100px) {
      .settings-grid { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
    }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      header { height: auto; padding: 12px 14px; align-items: flex-start; }
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
    const state = { chats: [], selected: null, queue: [], autoContinues: [], runner: {}, autoBusy: false, autoBusyAction: null, settingsSession: null, transferBusy: false, refreshBusy: false, doctorBusy: false, messagePreviews: {}, previewRequests: {} };
    let chatPreviewObserver = null;
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
      const claudeCodeAccount = data.active_claude_code_account;
      const codexAccount = data.active_codex_account;
      const accounts = data.account_index && Array.isArray(data.account_index.accounts) ? data.account_index.accounts : [];
      const accountSync = data.account_sync || {};
      const claudeSync = accountSync.last_result && accountSync.last_result.claude
        ? accountSync.last_result.claude
        : {};
      const codeArtifacts = Number(claudeSync.code_artifacts || 0);
      const pendingArtifactAccounts = Number(claudeSync.code_artifact_pending_accounts || 0);
      const artifactStatus = codeArtifacts > 0
        ? `${codeArtifacts} · copie private create: ${Number(claudeSync.code_artifact_copies_created || 0)}`
          + (pendingArtifactAccounts > 0 ? ` · in attesa di autenticazione: ${pendingArtifactAccounts}` : " · sincronizzati")
        : "nessuno rilevato";
      const syncStatus = accountSync.running
        ? (accountSync.last_error
          ? `attiva, errore: ${accountSync.last_error}`
          : (accountSync.in_progress ? "attiva · controllo in corso" : "attiva"))
        : "ferma";
      $("doctor").innerHTML = [
        `<b>Ora PC</b>: ${escapeHtml(formatDateTime(data.local_time || new Date().toISOString()))}`,
        `<b>Versione app</b>: ${escapeHtml(data.app_version || "")}`,
        `<b>Claude</b>: ${escapeHtml(data.claude_version || "non trovato")}`,
        `<b>Account app Claude</b>: ${escapeHtml(account ? account.label : "non rilevato")}`,
        `<b>Account CLI Claude Code</b>: ${escapeHtml(claudeCodeAccount ? claudeCodeAccount.label : "non rilevato")}`,
        `<b>Codex</b>: ${escapeHtml(data.codex_version || "non trovato")}`,
        `<b>Account Codex</b>: ${escapeHtml(codexAccount ? codexAccount.label : "non rilevato")}`,
        `<b>Account registrati</b>: ${escapeHtml(String(accounts.length))}`,
        `<b>Sync account</b>: ${escapeHtml(syncStatus)}${accountSync.last_check_at ? ` · ${escapeHtml(formatDateTime(accountSync.last_check_at))}` : ""}`,
        accountSync.last_full_check_at ? `<b>Scansione completa</b>: ${escapeHtml(formatDateTime(accountSync.last_full_check_at))}` : "",
        `<b>Artefatti Claude Code</b>: ${escapeHtml(artifactStatus)}`,
        `<b>Chat Claude</b>: ${data.claude_chat_count || 0}`,
        `<b>Task Codex</b>: ${data.codex_chat_count || 0}`,
        `<b>Accodabili</b>: ${data.queueable_chat_count}`,
        `<b>Coda</b>: ${escapeHtml(data.queue_file || "")}`,
        `<b>CLI Claude</b>: ${escapeHtml(data.claude_exe || "non trovato")}`,
        `<b>CLI Codex</b>: ${escapeHtml(data.codex_exe || "non trovato")}`,
      ].filter(Boolean).join("<br>");
      renderRunner(data.runner || {});
    }

    function renderRunner(runner) {
      state.runner = runner || {};
      const running = !!runner.running;
      const automatic = !!runner.automatic;
      $("runner-dot").className = "dot " + (running || automatic ? "on" : "off");
      $("runner-text").textContent = running
        ? `${automatic ? "Automatico · " : ""}Runner attivo${runner.pid ? ` #${runner.pid}` : ""}`
        : (automatic ? "Automatico in attesa" : "Runner fermo");
      $("start-btn").disabled = running || automatic;
      $("stop-btn").disabled = !running || automatic;
      if (runner.log_tail) log(runner.log_tail);
    }

    function selectedChat() {
      return state.chats.find((chat) => chat.session_id === state.selected) || null;
    }

    function autoContinueForSession(sessionId) {
      return state.autoContinues.find((auto) => auto.session_id === sessionId) || null;
    }

    function selectedAutoContinue() {
      return autoContinueForSession(state.selected);
    }

    function activeAutoContinues() {
      return state.autoContinues.filter((auto) => auto.enabled);
    }

    function chatPreviewKey(chat) {
      return `${chat.jsonl_path || ""}|${chat.last_timestamp || ""}`;
    }

    function hydrateChatPreviews(chats) {
      for (const chat of chats) {
        const cached = state.messagePreviews[chat.session_id];
        if (cached && cached.text && cached.key === chatPreviewKey(chat)) {
          chat.last_user_message = cached.text;
          chat.last_user_message_loaded = true;
        }
      }
      return chats;
    }

    async function loadChatPreview(sessionId) {
      if (!sessionId || state.previewRequests[sessionId]) return;
      const chat = state.chats.find((item) => item.session_id === sessionId);
      if (!chat || chat.last_user_message_loaded) return;
      const key = chatPreviewKey(chat);
      state.previewRequests[sessionId] = true;
      try {
        const result = await api(`/api/chat-preview?session_id=${encodeURIComponent(sessionId)}`);
        const current = state.chats.find((item) => item.session_id === sessionId);
        if (!current || chatPreviewKey(current) !== key) return;
        const text = result.last_user_message || current.last_prompt || "";
        current.last_user_message = text;
        current.last_user_message_loaded = true;
        if (text) state.messagePreviews[sessionId] = { key, text };
        renderChats();
      } catch (err) {
        const current = state.chats.find((item) => item.session_id === sessionId);
        if (current && chatPreviewKey(current) === key) {
          current.last_user_message_loaded = false;
        }
      } finally {
        delete state.previewRequests[sessionId];
      }
    }

    function accountText(chat) {
      if (!chat) return "";
      const provider = chat.provider === "codex" ? "Codex" : "Claude";
      const copies = Array.isArray(chat.account_copies) ? chat.account_copies.filter(Boolean) : [];
      if (copies.length > 1) return `sincronizzata su ${copies.length} account: ${copies.join(", ")}`;
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
      const auto = selectedAutoContinue();
      const selected = selectedChat();
      const selectedAvailable = !!(selected && selected.can_queue);
      const activeForSelected = !!(auto && auto.enabled);
      $("auto-btn").disabled = !selectedAvailable || state.autoBusy;
      $("auto-btn").className = activeForSelected ? "danger" : "blue";
      $("auto-btn").textContent = state.autoBusy
        ? (state.autoBusyAction === "disable" ? "Disattivo..." : "Attivo...")
        : activeForSelected
        ? "Disattiva auto-continua"
        : "Attiva auto-continua";
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

    function autoActionLabel(auto) {
      if (!auto) return "continua";
      if (auto.action === "claude_try_again") return "Try again nativo";
      if (auto.action === "retry_failed_prompt") return `reinvio senza duplicato: ${auto.recovery_prompt_preview || "ultimo messaggio"}`;
      if (auto.action === "codex_inspect") return "analisi automatica del turno Codex";
      return `continua${auto.action === "continue_interrupted" ? " per completare il turno interrotto" : ""}`;
    }

    function autoStatusText(auto) {
      if (!auto) return "";
      const shortId = (auto.session_id || "").slice(0, 8);
      const title = auto.title ? ` · ${auto.title}` : "";
      const runnerText = state.runner && state.runner.running ? "runner attivo" : "runner fermo";
      if (auto.enabled) {
        if (auto.status === "armed" || auto.status === "monitoring") {
          const wait = auto.next_check_in_seconds ? `, nuovo controllo tra ${formatDuration(auto.next_check_in_seconds)}` : "";
          const action = auto.last_observation === "try_again_invoked" && auto.last_action_at
            ? `, ultimo Try again eseguito ${formatDateTime(auto.last_action_at)}`
            : "";
          return `Auto-continua attivo su ${shortId}${title}: monitoraggio nativo${action}${wait}, ${runnerText}.`;
        }
        if (auto.status === "waiting_limit") {
          const wait = auto.next_check_in_seconds ? `, controllo tra ${formatDuration(auto.next_check_in_seconds)}` : "";
          return `Auto-continua attivo su ${shortId}${title}: attendo reset + margine${auto.not_before ? ` fino a ${formatDateTime(auto.not_before)}` : ""}; poi ${autoActionLabel(auto)}${wait}, ${runnerText}.`;
        }
        if (auto.status === "waiting_retry") return `Auto-continua attivo su ${shortId}${title}: Try again non era ancora visibile; nessun messaggio inviato, riprovo automaticamente, ${runnerText}.`;
        if (auto.status === "sending") return `Auto-continua attivo su ${shortId}${title}: ${autoActionLabel(auto)} in corso, ${runnerText}.`;
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
      const auto = selectedAutoContinue();
      const activeCount = activeAutoContinues().length;
      const text = message ?? (autoStatusText(auto) || (activeCount ? `${activeCount} chat con auto-continua attivo.` : ""));
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
      if (chatPreviewObserver) chatPreviewObserver.disconnect();
      chatPreviewObserver = typeof IntersectionObserver === "undefined"
        ? null
        : new IntersectionObserver((entries, observer) => {
            for (const entry of entries) {
              if (!entry.isIntersecting) continue;
              observer.unobserve(entry.target);
              loadChatPreview(entry.target.dataset.sessionId);
            }
          }, { root: list, rootMargin: "160px 0px" });
      let previewFallbackCount = 0;
      const chats = state.chats.filter((chat) => {
        const hay = [chat.title, chat.cwd, chat.session_id, chat.last_prompt, chat.last_user_message, chat.source, chat.provider, ...(chat.account_copies || [])].join(" ").toLowerCase();
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
        const lastMessage = chat.last_user_message || chat.last_prompt || "";
        const lastMessageLoaded = !!(chat.last_user_message_loaded || lastMessage);
        const lastMessageText = lastMessage || (lastMessageLoaded ? "non disponibile" : "caricamento...");
        btn.innerHTML = `
          <span class="chat-title">${escapeHtml(chat.short_id)} · ${escapeHtml(chat.title || "Senza titolo")}</span>
          <span class="chat-last-message${lastMessage ? "" : " unavailable"}" title="${escapeAttr(`Ultimo messaggio: ${lastMessageText}`)}"><b>Ultimo messaggio:</b> ${escapeHtml(lastMessageText)}</span>
          <span class="chat-sub">
            <span class="badge">${escapeHtml(chat.source || "")}</span>
            <span class="badge">${chat.provider === "codex" ? "Codex" : "Claude"}</span>
            ${chat.remote_host ? `<span class="badge">SSH ${escapeHtml(chat.remote_host)}</span>` : ""}
            <span class="badge">${chat.can_queue ? "utilizzabile" : "solo visibile"}</span>
            ${autoContinueForSession(chat.session_id)?.enabled ? `<span class="badge">auto-continua attivo</span>` : ""}
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
        if (!lastMessageLoaded) {
          btn.dataset.sessionId = chat.session_id;
          if (chatPreviewObserver) {
            chatPreviewObserver.observe(btn);
          } else if (previewFallbackCount < 6) {
            previewFallbackCount += 1;
            loadChatPreview(chat.session_id);
          }
        }
      }
      renderSettingsControls();
      renderAutoButton();
      renderTransferButton();
      renderAutoFeedback();
    }

    function renderQueue(data) {
      state.queue = data.items || [];
      state.autoContinues = Array.isArray(data.auto_continues)
        ? data.auto_continues
        : (data.auto_continue ? [data.auto_continue] : []);
      renderRunner(data.runner || {});
      renderAutoButton();
      renderAutoFeedback();
      const recovery = data.recovery || null;
      const recoveryBox = recovery ? `
        <div class="recovery-box">
          Recovery attiva: la prossima azione sara' <b>${escapeHtml(recovery.recovery_prompt_preview || recovery.prompt || "analisi automatica")}</b> sulla chat ${escapeHtml((recovery.session_id || "").slice(0, 8))}.
          ${recovery.recovery_followup_count ? `<br><span class="meta">Altri ${escapeHtml(String(recovery.recovery_followup_count))} messaggi falliti sono in coda nello stesso ordine.</span>` : ""}
          ${recovery.not_before ? `<br><span class="meta">Non prima di ${escapeHtml(formatDateTime(recovery.not_before))}</span>` : ""}
        </div>
      ` : "";
      const runner = state.runner || {};
      const autos = [...state.autoContinues].sort((a, b) => Number(!!b.enabled) - Number(!!a.enabled));
      const autoBoxes = autos.map((auto) => {
        const effective = auto.fingerprint && auto.fingerprint.effective ? auto.fingerprint.effective : {};
        const autoIsCodex = auto.provider === "codex";
        const autoDetails = [
          `Stato: ${escapeHtml(auto.status || "spento")}`,
          `Azione: ${escapeHtml(autoActionLabel(auto))}`,
          auto.recovery_followup_count ? `Messaggi recuperati in coda: ${escapeHtml(String(auto.recovery_followup_count))}` : "",
          `Runner: ${runner.running ? `attivo${runner.pid ? ` #${escapeHtml(runner.pid)}` : ""}` : (runner.automatic ? "automatico in attesa" : "fermo")}`,
          `Origine: ${escapeHtml(auto.source || (autoIsCodex ? "Codex App" : "Claude Code"))}`,
          !autoIsCodex && auto.source_key === "claude_windows_app" ? "Integrazione IDE: non usata" : "",
          `Modello: ${escapeHtml(auto.model_override || effective.model || "chat")}`,
          `Effort: ${escapeHtml(auto.effort_level_override || effective.effortLevel || "chat")}`,
          autoIsCodex
            ? `Sandbox: ${escapeHtml(auto.sandbox_mode_override || effective.sandboxMode || "task")}`
            : `Permessi: ${escapeHtml(auto.permission_mode_override || effective.permissionMode || "chat")}`,
          autoIsCodex ? `Approvazioni: ${escapeHtml(auto.approval_policy_override || effective.approvalPolicy || "task")}` : "",
          `Controlli: ${escapeHtml(String(auto.attempts || 0))}`,
          auto.actions_completed ? `Try again eseguiti: ${escapeHtml(String(auto.actions_completed))}` : "",
          auto.last_action_at ? `Ultima azione: ${escapeHtml(formatDateTime(auto.last_action_at))}` : "",
          auto.sending_started_at ? `Invio iniziato: ${escapeHtml(formatDateTime(auto.sending_started_at))}` : "",
          auto.not_before && auto.status !== "sending" ? `Prossimo tentativo: ${escapeHtml(formatDateTime(auto.not_before))}` : "",
          auto.next_check_in_seconds ? `Prossimo controllo: tra ${escapeHtml(formatDuration(auto.next_check_in_seconds))}` : "",
          auto.last_check_at ? `Ultimo check: ${escapeHtml(formatDateTime(auto.last_check_at))}` : "",
          auto.updated_at ? `Aggiornato: ${escapeHtml(formatDateTime(auto.updated_at))}` : "",
        ].filter(Boolean).join("<br>");
        return `
          <div class="auto-box">
            Auto-continua: <b>${auto.enabled ? "attivo" : escapeHtml(auto.status || "spento")}</b>
            sulla chat ${escapeHtml((auto.session_id || "").slice(0, 8))}
            ${auto.title ? ` · ${escapeHtml(auto.title)}` : ""}.
            <br><span class="meta">${autoDetails}</span>
            ${auto.last_error && auto.status !== "sending" ? `<br><span class="meta">${escapeHtml(auto.last_error)}</span>` : ""}
          </div>
        `;
      }).join("");
      if (!state.queue.length) {
        $("queue").innerHTML = autoBoxes + recoveryBox + `<div class="empty">Coda vuota</div>`;
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
      $("queue").innerHTML = autoBoxes + recoveryBox + `<table><thead><tr><th>Stato</th><th>ID</th><th>Elemento</th><th>Azioni</th></tr></thead><tbody>${rows}</tbody></table>`;
    }

    async function refreshDoctor() {
      if (state.doctorBusy) return;
      state.doctorBusy = true;
      try {
        renderDoctor(await api("/api/doctor"));
      } catch (err) {
        appendLog("Errore ambiente: " + err.message);
      } finally {
        state.doctorBusy = false;
      }
    }

    async function refreshAll() {
      if (state.refreshBusy) return;
      state.refreshBusy = true;
      void refreshDoctor();
      try {
        const queueTask = api("/api/queue").then((queue) => renderQueue(queue));
        const chatsTask = api("/api/chats").then((chats) => {
          state.chats = hydrateChatPreviews(chats.chats || []);
          renderChats();
        });
        const [queueResult, chatsResult] = await Promise.allSettled([queueTask, chatsTask]);
        if (queueResult.status === "rejected") appendLog("Errore coda: " + queueResult.reason.message);
        if (chatsResult.status === "rejected") {
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
      const auto = selectedAutoContinue();
      const activeForSelected = !!(auto && auto.enabled);
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
        state.autoContinues = Array.isArray(result.auto_continues)
          ? result.auto_continues
          : (result.auto_continue ? [result.auto_continue] : []);
        const changed = result.auto_continue || selectedAutoContinue();
        const message = autoStatusText(changed) || (activeForSelected ? "Auto-continua disattivato." : "Auto-continua attivato.");
        appendLog(message);
        renderAutoFeedback(message, changed && (changed.status === "failed" || changed.status === "blocked") ? "error" : "ok");
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
        state.chats = hydrateChatPreviews(result.chats || state.chats);
        const transfer = result.transfer || {};
        const artifactSync = transfer.artifact_sync || {};
        const artifactChanges = [
          "artifacts_created",
          "artifacts_updated",
          "artifacts_deleted",
          "artifacts_removed",
        ].reduce((total, key) => total + Number(artifactSync[key] || 0), 0);
        const codeArtifactChanges = Number(artifactSync.code_artifact_copies_created || 0)
          + Number(artifactSync.code_artifact_transcripts_created || 0);
        const artifactText = isCodex ? "" : ` · artefatti: ${artifactChanges + codeArtifactChanges}`;
        appendLog(`Import completato: ${transfer.status || "ok"} · ${transfer.title || selected.title || state.selected}${artifactText}`);
        if (Number(artifactSync.artifact_missing_files || 0) > 0) {
          appendLog(`Attenzione: ${artifactSync.artifact_missing_files} artefatti hanno il manifesto ma manca il file locale.`);
        }
        if (Number(artifactSync.code_artifact_pending_accounts || 0) > 0) {
          appendLog(`Artefatti Claude Code in attesa: ${artifactSync.code_artifact_pending_accounts} account non ha ancora una credenziale valida in cache.`);
        }
        for (const error of (artifactSync.code_artifact_errors || [])) {
          appendLog(`Errore artefatto Claude Code: ${error}`);
        }
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
    window.addEventListener("focus", refreshAll);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshAll();
    });
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
    state.start_account_sync_monitor()
    state.start_runner_monitor()
    print(f"http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        state.stop_runner_monitor()
        state.stop_runner()
        state.stop_account_sync_monitor()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
