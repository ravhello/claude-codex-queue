from __future__ import annotations

import base64
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_codex_queue
from claude_vscode_queue import app


class QueueAppTests(unittest.TestCase):
    def test_public_version_and_legacy_state_compatibility(self) -> None:
        self.assertEqual(claude_codex_queue.__version__, "0.2.3")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / app.LEGACY_APP_DIR_NAME
            legacy.mkdir()

            legacy_paths = app.resolve_paths(str(root))
            self.assertEqual(legacy_paths.state_dir, legacy)

            preferred = root / app.APP_DIR_NAME
            preferred.mkdir()
            preferred_paths = app.resolve_paths(str(root))
            self.assertEqual(preferred_paths.state_dir, preferred)

    def test_find_codex_executable_prefers_windows_cmd_inside_wsl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = root / "AppData" / "Local" / "npm" / "codex.cmd"
            command.parent.mkdir(parents=True)
            command.write_text("@echo off\n", encoding="utf-8")
            paths = app.Paths(root, root / ".claude", root / ".state", root / ".state" / "queue.json", root / ".state" / "logs")

            with patch.object(app, "is_wsl", return_value=True), patch.object(
                app.shutil, "which", return_value="/mnt/c/fake/npm/codex"
            ):
                found = app.find_codex_executable(paths)

            self.assertEqual(found, command)

    def test_windows_cli_commands_use_hidden_powershell_inside_wsl(self) -> None:
        with patch.object(app, "is_wsl", return_value=True), patch.object(
            app.shutil, "which", return_value="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        ):
            command = app.codex_cli_command(
                Path("/mnt/c/Users/test/AppData/Local/npm/codex.cmd"),
                ["--version"],
            )

        self.assertIn("-WindowStyle", command)
        self.assertIn("Hidden", command)
        encoded = command[command.index("-EncodedCommand") + 1]
        script = base64.b64decode(encoded).decode("utf-16-le")
        self.assertIn("C:\\Users\\test\\AppData\\Local\\npm\\codex.cmd", script)
        self.assertIn("--version", script)
        self.assertIn("HiddenProcessProxy", script)
        self.assertIn("Add-Type", script)
        self.assertIn("JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", app.WINDOWS_HIDDEN_PROXY_CSHARP)
        self.assertIn("destination.Flush()", app.WINDOWS_HIDDEN_PROXY_CSHARP)

    def test_auto_continue_monitor_does_not_send_without_active_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            session = "dddddddd-4444-4444-8444-dddddddddddd"
            transcript = project / f"{session}.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": session,
                        "timestamp": app.now_utc(),
                        "cwd": str(root),
                        "message": {"content": "working"},
                    }
                ),
                encoding="utf-8",
            )
            paths = app.Paths(root, claude_home, root / ".state", root / ".state" / "queue.json", root / ".state" / "logs")
            chat = app.discover_chats(claude_home)[0]
            marker = root / "was-called"
            fake = root / "fake-claude"
            fake.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | 0o111)
            app.save_queue(
                paths.queue_file,
                {
                    "version": 1,
                    "items": [],
                    "recovery": None,
                    "auto_continue": {
                        "enabled": True,
                        "status": "armed",
                        "monitor_limit": True,
                        "created_at": app.now_utc(),
                        "session_id": session,
                        "title": "monitor",
                        "cwd": str(root),
                        "prompt": app.RECOVERY_PROMPT,
                        "attempts": 0,
                        "not_before": None,
                        "fingerprint": app.settings_fingerprint(paths, chat),
                    },
                },
            )

            result = app.main(
                [
                    "--windows-home",
                    str(root),
                    "--state-dir",
                    str(paths.state_dir),
                    "--claude",
                    str(fake),
                    "run",
                    "--once",
                    "--no-ide",
                    "--poll-seconds",
                    "1",
                ]
            )

            queue = app.load_queue(paths.queue_file)
            self.assertEqual(result, app.RATE_LIMIT_EXIT)
            self.assertFalse(marker.exists())
            self.assertEqual(queue["auto_continue"]["status"], "monitoring")
            self.assertIsNotNone(queue["auto_continue"]["not_before"])
            queue["items"] = [
                {
                    "id": "pending-while-monitoring",
                    "status": app.STATUS_PENDING,
                    "created_at": app.now_utc(),
                    "order": 0,
                    "session_id": session,
                    "title": "queued",
                    "cwd": str(root),
                    "prompt": "queued prompt",
                    "attempts": 0,
                    "not_before": None,
                    "fingerprint": app.settings_fingerprint(paths, chat),
                }
            ]
            queue["auto_continue"]["not_before"] = (
                dt.datetime.now(app.UTC) + dt.timedelta(hours=1)
            ).replace(microsecond=0).isoformat()
            app.save_queue(paths.queue_file, queue)

            queued_result = app.main(
                [
                    "--windows-home",
                    str(root),
                    "--state-dir",
                    str(paths.state_dir),
                    "--claude",
                    str(fake),
                    "run",
                    "--once",
                    "--no-ide",
                    "--poll-seconds",
                    "1",
                ]
            )

            refreshed = app.load_queue(paths.queue_file)
            self.assertEqual(queued_result, 0)
            self.assertTrue(marker.exists())
            self.assertEqual(refreshed["items"][0]["status"], app.STATUS_DONE)
            self.assertTrue(refreshed["auto_continue"]["enabled"])

    def test_cancelled_auto_continue_cannot_be_resurrected_by_stale_runner_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_file = root / ".state" / "queue.json"
            active = {
                "enabled": True,
                "status": "armed",
                "session_id": "dddddddd-4444-4444-8444-dddddddddddd",
                "created_at": "2026-07-13T02:00:00+02:00",
            }
            app.save_queue(
                queue_file,
                {"version": 1, "items": [], "recovery": None, "auto_continue": active},
            )
            stale_runner_queue = app.load_queue(queue_file)

            current = app.load_queue(queue_file)
            app.mark_auto_continue_cancelled(queue_file, current["auto_continue"])
            activation_id = current["auto_continue"]["activation_id"]
            current["auto_continue"].update(enabled=False, status="disabled")
            app.save_queue(queue_file, current)

            stale_runner_queue["auto_continue"].update(enabled=True, status="monitoring")
            app.save_queue(queue_file, stale_runner_queue)

            final = app.load_queue(queue_file)["auto_continue"]
            self.assertFalse(final["enabled"])
            self.assertEqual(final["status"], "disabled")
            self.assertEqual(final["activation_id"], activation_id)

    def test_discover_codex_app_sessions_reads_index_and_thread_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            sessions = codex_home / "sessions" / "2026" / "01" / "02"
            sessions.mkdir(parents=True)
            session = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
            rollout = sessions / f"rollout-2026-01-02T10-00-00-{session}.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-02T10:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": session, "cwd": str(root)},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (codex_home / "session_index.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"id": session, "thread_name": "Old title", "updated_at": "2026-01-02T10:00:00Z"}),
                        json.dumps({"id": session, "thread_name": "Newest Codex task", "updated_at": "2026-01-02T11:00:00Z"}),
                    ]
                ),
                encoding="utf-8",
            )
            connection = sqlite3.connect(codex_home / "state_5.sqlite")
            connection.execute(
                "CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, model TEXT, reasoning_effort TEXT, "
                "sandbox_policy TEXT, approval_mode TEXT, archived INTEGER, updated_at_ms INTEGER, "
                "recency_at_ms INTEGER, first_user_message TEXT)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session,
                    str(rollout),
                    str(root),
                    "gpt-test-codex",
                    "ultra",
                    json.dumps({"type": "disabled"}),
                    "never",
                    0,
                    1767351600000,
                    1767351600000,
                    "first prompt",
                ),
            )
            connection.commit()
            connection.close()
            payload = base64.urlsafe_b64encode(json.dumps({"email": "codex@example.com", "sub": "user-1"}).encode()).rstrip(b"=").decode()
            (codex_home / "auth.json").write_text(
                json.dumps({"auth_mode": "chatgpt", "tokens": {"account_id": "acct-1", "id_token": f"x.{payload}.x"}}),
                encoding="utf-8",
            )
            paths = app.resolve_paths(str(root), str(root / ".state"))

            with patch.object(app, "find_codex_executable", return_value=root / "codex"):
                chats = app.discover_codex_app_sessions(paths)

            self.assertEqual(len(chats), 1)
            chat = chats[0]
            self.assertEqual(chat.provider, app.PROVIDER_CODEX)
            self.assertEqual(chat.title, "Newest Codex task")
            self.assertEqual(chat.model, "gpt-test-codex")
            self.assertEqual(chat.effort_level, "ultra")
            self.assertEqual(chat.sandbox_mode, "danger-full-access")
            self.assertEqual(chat.approval_policy, "never")
            self.assertTrue(chat.can_queue)

    def test_run_codex_preserves_settings_and_clears_external_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "prompt = sys.stdin.read()\n"
                "print(json.dumps({'args': sys.argv[1:], 'prompt': prompt, 'api_key': os.environ.get('OPENAI_API_KEY'), 'codex_home': os.environ.get('CODEX_HOME')}))\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | 0o111)
            paths = app.Paths(root, root / ".claude", root / ".state", root / ".state" / "queue.json", root / ".state" / "logs")
            item = {
                "provider": app.PROVIDER_CODEX,
                "session_id": "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
                "cwd": str(root),
                "prompt": "continua",
                "fingerprint": {
                    "effective": {
                        "model": "gpt-test-codex",
                        "effortLevel": "ultra",
                        "sandboxMode": "workspace-write",
                        "approvalPolicy": "on-request",
                    }
                },
            }

            with patch.dict(app.os.environ, {"OPENAI_API_KEY": "invalid-external-key"}):
                result = app.run_codex(paths, fake, item, timeout=10)

            self.assertEqual(result.returncode, 0)
            output = json.loads(result.stdout)
            self.assertIsNone(output["api_key"])
            self.assertEqual(output["codex_home"], str(paths.codex_home))
            self.assertEqual(output["prompt"], "continua")
            self.assertEqual(output["args"][:4], ["exec", "resume", "--json", "--skip-git-repo-check"])
            self.assertIn("model_reasoning_effort='ultra'", output["args"])
            self.assertIn("sandbox_mode='workspace-write'", output["args"])
            self.assertIn("approval_policy='on-request'", output["args"])
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", output["args"])

    def test_codex_subprocess_env_converts_home_for_windows_cli(self) -> None:
        codex_home = Path("/mnt/c/Users/rikyr/.codex")
        with patch.dict(
            app.os.environ,
            {"CODEX_HOME": "/tmp/wrong-account", "OPENAI_API_KEY": "invalid-external-key"},
        ):
            env = app.codex_subprocess_env(codex_home, windows=True)

        self.assertEqual(env["CODEX_HOME"], r"C:\Users\rikyr\.codex")
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_codex_transcript_detects_prompt_and_structured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "rollout.jsonl"
            reset_epoch = int((dt.datetime.now(app.UTC) + dt.timedelta(hours=1)).timestamp())
            rows = [
                {
                    "timestamp": app.now_utc(),
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "continua"},
                },
                {
                    "timestamp": app.now_utc(),
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "rate_limits": {
                            "rate_limit_reached_type": "primary",
                            "primary": {"used_percent": 100, "resets_at": reset_epoch},
                        },
                    },
                },
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            chat = app.Chat(
                session_id="cccccccc-3333-4333-8333-cccccccccccc",
                title="Codex",
                cwd=str(root),
                permission_mode=None,
                model="gpt-test",
                jsonl_path=transcript,
                last_timestamp=app.now_utc(),
                message_count=-1,
                last_prompt=None,
                provider=app.PROVIDER_CODEX,
            )

            self.assertTrue(app.codex_prompt_recorded_after(transcript, "continua", dt.datetime.now(app.UTC) - dt.timedelta(minutes=1)))
            reset = app.latest_rate_limit_reset_from_chat(chat)
            self.assertIsNotNone(reset)
            self.assertEqual(int(reset.timestamp()), reset_epoch)

    def test_codex_recovery_retries_failed_prompt_without_duplicate(self) -> None:
        turns = [
            {
                "id": "done",
                "status": "completed",
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "prima"}]},
                    {"type": "agentMessage", "text": "completato"},
                ],
            },
            {
                "id": "failed-1",
                "status": "failed",
                "error": {"message": "usage limit reached"},
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "messaggio oltre il limite"}]},
                ],
            },
        ]

        plan = app.codex_recovery_plan_from_turns(turns)

        self.assertEqual(plan.kind, "retry_failed_prompt")
        self.assertEqual(plan.prompt, "messaggio oltre il limite")
        self.assertEqual(plan.rollback_turn_ids, ("failed-1",))
        self.assertEqual(plan.followup_prompts, ())

    def test_codex_recovery_continues_interrupted_work_then_queues_failed_messages(self) -> None:
        turns = [
            {
                "id": "interrupted",
                "status": "interrupted",
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "completa il progetto"}]},
                    {"type": "agentMessage", "text": "Ho modificato i file, ora eseguo i test."},
                    {"type": "commandExecution", "status": "completed"},
                ],
            },
            {
                "id": "failed-1",
                "status": "failed",
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "primo messaggio in coda"}]},
                ],
            },
            {
                "id": "failed-2",
                "status": "failed",
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "secondo messaggio in coda"}]},
                ],
            },
        ]

        plan = app.codex_recovery_plan_from_turns(turns)

        self.assertEqual(plan.kind, "continue_interrupted")
        self.assertEqual(plan.prompt, app.RECOVERY_PROMPT)
        self.assertEqual(plan.rollback_turn_ids, ("failed-1", "failed-2"))
        self.assertEqual(
            plan.followup_prompts,
            ("primo messaggio in coda", "secondo messaggio in coda"),
        )

    def test_prepare_codex_recovery_rolls_back_once_and_queues_followups_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = app.Paths(root, root / ".claude", root / ".state", root / ".state" / "queue.json", root / ".state" / "logs")
            state = {
                "session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "title": "Codex recovery",
                "cwd": str(root),
                "provider": app.PROVIDER_CODEX,
                "source": "Codex App",
                "source_key": "codex_app",
                "jsonl_path": str(root / "rollout.jsonl"),
                "fingerprint": {"effective": {"model": "gpt-test"}},
            }
            queue = {"version": 1, "items": [], "recovery": None, "auto_continue": state}
            plan = app.CodexRecoveryPlan(
                prompt="primo",
                kind="retry_failed_prompt",
                rollback_turn_ids=("turn-1", "turn-2", "turn-3"),
                followup_prompts=("secondo", "terzo"),
                source_turn_ids=("turn-1", "turn-2", "turn-3"),
            )

            with patch.object(app, "codex_recovery_plan", return_value=plan), patch.object(
                app, "apply_codex_rollback", return_value=True
            ) as rollback:
                app.prepare_codex_recovery_state(paths, root / "codex", queue, state)
                app.prepare_codex_recovery_state(paths, root / "codex", queue, state)

            rollback.assert_called_once_with(
                root / "codex",
                state["session_id"],
                ("turn-1", "turn-2", "turn-3"),
                codex_home=paths.codex_home,
            )
            self.assertEqual(state["prompt"], "primo")
            self.assertEqual(state["action"], "retry_failed_prompt")
            self.assertTrue(state["rollback_applied"])
            self.assertTrue(state["followups_queued"])
            self.assertEqual([item["prompt"] for item in queue["items"]], ["secondo", "terzo"])
            self.assertTrue(all(item["priority"] == app.CODEX_RECOVERY_PRIORITY for item in queue["items"]))
            self.assertEqual([item["recovery_sequence"] for item in queue["items"]], [1, 2])

    def test_apply_codex_rollback_is_idempotent_after_persisted_rollback(self) -> None:
        with patch.object(
            app,
            "codex_thread_turns",
            return_value=[{"id": "completed", "status": "completed", "items": []}],
        ), patch.object(app, "codex_app_server_request") as request:
            changed = app.apply_codex_rollback(
                Path("codex"),
                "thread-id",
                ("already-removed",),
                codex_home=Path("home"),
            )

        self.assertFalse(changed)
        request.assert_not_called()

    def test_claude_desktop_auto_continue_uses_native_try_again_not_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            session = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            transcript = project / f"{session}.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": session,
                        "timestamp": app.now_utc(),
                        "cwd": str(root),
                        "message": {"content": "messaggio fallito"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            paths = app.Paths(root, claude_home, root / ".state", root / ".state" / "queue.json", root / ".state" / "logs")
            chat = app.discover_chats(claude_home)[0]
            fake = root / "claude"
            fake.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | 0o111)
            app.save_queue(
                paths.queue_file,
                {
                    "version": 1,
                    "items": [],
                    "recovery": None,
                    "auto_continue": {
                        "enabled": True,
                        "status": "waiting_limit",
                        "monitor_limit": False,
                        "created_at": app.now_utc(),
                        "session_id": session,
                        "title": "Claude Desktop",
                        "cwd": str(root),
                        "prompt": "Try again",
                        "source": "Claude Windows App",
                        "source_key": "claude_windows_app",
                        "provider": app.PROVIDER_CLAUDE,
                        "attempts": 0,
                        "not_before": None,
                        "fingerprint": app.settings_fingerprint(paths, chat),
                    },
                },
            )
            native_result = app.ClaudeRunResult(0, "CLAUDE_TRY_AGAIN_INVOKED:Try again", "", False, None)

            with patch.object(app, "run_claude_desktop_try_again", return_value=native_result) as native, patch.object(
                app, "run_agent"
            ) as cli:
                result = app.main(
                    [
                        "--windows-home",
                        str(root),
                        "--state-dir",
                        str(paths.state_dir),
                        "--claude",
                        str(fake),
                        "run",
                        "--once",
                        "--no-ide",
                    ]
                )

            self.assertEqual(result, 0)
            native.assert_called_once()
            cli.assert_not_called()
            refreshed = app.load_queue(paths.queue_file)
            self.assertEqual(refreshed["auto_continue"]["status"], "done")
            self.assertEqual(refreshed["auto_continue"]["action"], "claude_try_again")
            self.assertEqual(refreshed["auto_continue"]["prompt"], "Try again")

            refreshed["auto_continue"].update(
                {
                    "enabled": True,
                    "status": "waiting_limit",
                    "not_before": None,
                    "last_error": None,
                }
            )
            app.save_queue(paths.queue_file, refreshed)
            missing_button = app.ClaudeRunResult(
                app.CLAUDE_DESKTOP_TRY_AGAIN_EXIT,
                "",
                "CLAUDE_TRY_AGAIN_NOT_FOUND",
                False,
                None,
            )
            with patch.object(app, "run_claude_desktop_try_again", return_value=missing_button), patch.object(
                app, "run_agent"
            ) as cli:
                retry_result = app.main(
                    [
                        "--windows-home",
                        str(root),
                        "--state-dir",
                        str(paths.state_dir),
                        "--claude",
                        str(fake),
                        "run",
                        "--once",
                        "--no-ide",
                        "--poll-seconds",
                        "1",
                    ]
                )

            self.assertEqual(retry_result, app.RATE_LIMIT_EXIT)
            cli.assert_not_called()
            waiting = app.load_queue(paths.queue_file)["auto_continue"]
            self.assertTrue(waiting["enabled"])
            self.assertEqual(waiting["status"], "waiting_retry")
            self.assertIn("Non ho inviato alcun messaggio", waiting["last_error"])

    def test_run_codex_uses_structured_transcript_reset_when_output_has_no_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reset_epoch = int((dt.datetime.now(app.UTC) + dt.timedelta(hours=2)).timestamp())
            transcript = root / "rollout.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "timestamp": app.now_utc(),
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": {
                                "rate_limit_reached_type": "primary",
                                "primary": {"used_percent": 100, "resets_at": reset_epoch},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            fake = root / "codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdin.read()\n"
                "print('usage limit reached', file=sys.stderr)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | 0o111)
            paths = app.Paths(root, root / ".claude", root / ".state", root / ".state" / "queue.json", root / ".state" / "logs")
            item = {
                "provider": app.PROVIDER_CODEX,
                "session_id": "eeeeeeee-5555-4555-8555-eeeeeeeeeeee",
                "cwd": str(root),
                "prompt": "continua",
                "jsonl_path": str(transcript),
                "fingerprint": {"effective": {}},
            }

            result = app.run_codex(paths, fake, item, timeout=10)

            self.assertTrue(result.rate_limited)
            self.assertIsNotNone(result.reset_at)
            self.assertEqual(int(app.parse_iso(result.reset_at).timestamp()), reset_epoch)

    def test_discover_chats_parses_latest_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude_home = Path(tmp) / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            session = "11111111-1111-4111-8111-111111111111"
            transcript = project / f"{session}.jsonl"
            rows = [
                {"type": "user", "sessionId": session, "timestamp": "2026-01-01T10:00:00Z", "cwd": "C:\\work", "permissionMode": "default"},
                {"type": "assistant", "sessionId": session, "timestamp": "2026-01-01T10:01:00Z", "message": {"role": "assistant", "model": "opus"}},
                {"type": "ai-title", "sessionId": session, "aiTitle": "Fix tests"},
                {"type": "last-prompt", "sessionId": session, "lastPrompt": "please fix tests"},
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

            chats = app.discover_chats(claude_home)

            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0].session_id, session)
            self.assertEqual(chats[0].title, "Fix tests")
            self.assertEqual(chats[0].cwd, "C:\\work")
            self.assertEqual(chats[0].permission_mode, "default")
            self.assertEqual(chats[0].model, "opus")

    def test_discover_chats_ignores_synthetic_model_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude_home = Path(tmp) / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            session = "12121212-1212-4121-8121-121212121212"
            transcript = project / f"{session}.jsonl"
            rows = [
                {
                    "type": "assistant",
                    "sessionId": session,
                    "timestamp": "2026-01-01T10:00:00Z",
                    "cwd": str(Path(tmp)),
                    "message": {"role": "assistant", "model": "claude-opus-4-8"},
                },
                {
                    "type": "assistant",
                    "sessionId": session,
                    "timestamp": "2026-01-01T10:01:00Z",
                    "cwd": str(Path(tmp)),
                    "message": {"role": "assistant", "model": "<synthetic>"},
                },
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

            chats = app.discover_chats(claude_home)

            self.assertEqual(chats[0].model, "claude-opus-4-8")

    def test_run_claude_removes_external_api_auth_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = app.Paths(
                windows_home=root,
                claude_home=root / ".claude",
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            fake = root / "fake_claude.py"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "sys.stdin.read()\n"
                "blocked = ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_USE_VERTEX']\n"
                "present = [name for name in blocked if os.environ.get(name)]\n"
                "if present:\n"
                "    print(','.join(present), file=sys.stderr)\n"
                "    raise SystemExit(7)\n"
                "print('clean')\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | 0o111)
            item = {
                "id": "x",
                "session_id": "11111111-1111-4111-8111-111111111111",
                "cwd": str(root),
                "prompt": "hello",
                "fingerprint": {},
            }

            with patch.dict(
                app.os.environ,
                {
                    "ANTHROPIC_API_KEY": "bad",
                    "ANTHROPIC_AUTH_TOKEN": "bad",
                    "CLAUDE_CODE_USE_VERTEX": "1",
                },
            ):
                result = app.run_claude(paths, fake, item, timeout=10, use_ide=False)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "clean")

    def write_account_files(self, root: Path, account_uuid: str, email: str, refresh: str) -> None:
        claude_home = root / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        (root / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "accountUuid": account_uuid,
                        "emailAddress": email,
                        "organizationUuid": "org-1",
                    }
                }
            ),
            encoding="utf-8",
        )
        (claude_home / ".credentials.json").write_text(
            json.dumps(
                {
                    "organizationUuid": "org-1",
                    "claudeAiOauth": {
                        "accessToken": f"access-{refresh}",
                        "refreshToken": f"refresh-{refresh}",
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_active_claude_account_reads_masked_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_account_files(root, "account-a", "riccardo@example.com", "a")
            paths = app.Paths(
                windows_home=root,
                claude_home=root / ".claude",
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )

            account = app.active_claude_account(paths)

            self.assertIsNotNone(account)
            self.assertEqual(account.label, "ri***@example.com")
            self.assertIsNotNone(account.key)
            self.assertIsNotNone(account.email_hash)

    def test_known_other_account_chat_is_visible_but_not_queueable_until_account_switches_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            session = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            (project / f"{session}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": session,
                        "timestamp": "2026-01-01T10:00:00Z",
                        "cwd": str(root),
                        "message": {"role": "user", "content": "hello"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            self.write_account_files(root, "old-account", "old@example.com", "old")
            old = app.register_active_account(paths)
            self.assertIsNotNone(old)
            app.save_account_index(
                paths,
                {
                    "version": 1,
                    "accounts": {
                        old.key: {
                            "key": old.key,
                            "label": old.label,
                            "first_seen_at": app.now_utc(),
                            "last_seen_at": app.now_utc(),
                        }
                    },
                    "sessions": {
                        f"local:{session}": {
                            "account_key": old.key,
                            "label": old.label,
                            "session_id": session,
                        }
                    },
                },
            )

            self.write_account_files(root, "new-account", "new@example.com", "new")
            with_new_account = app.discover_claude_chats(paths)[0]
            self.assertEqual(with_new_account.account_status, "other")
            self.assertFalse(with_new_account.can_queue)
            self.assertIn("account attivo", app.account_mismatch_for_chat(paths, with_new_account) or "")

            self.write_account_files(root, "old-account", "old@example.com", "old")
            with_old_account = app.discover_claude_chats(paths)[0]
            self.assertEqual(with_old_account.account_status, "active")
            self.assertTrue(with_old_account.can_queue)
            self.assertIsNone(app.account_mismatch_for_chat(paths, with_old_account))

    def test_latest_rate_limit_reset_from_chat_reads_transcript_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "s.jsonl"
            session = "11111111-1111-4111-8111-111111111111"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": session,
                        "timestamp": "2026-07-07T15:21:13Z",
                        "message": {
                            "role": "assistant",
                            "model": "<synthetic>",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "You've hit your session limit · resets 6:40pm (Europe/Berlin)",
                                }
                            ],
                        },
                        "error": "rate_limit",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            chat = app.Chat(
                session_id=session,
                title="limited",
                cwd=str(root),
                permission_mode=None,
                model=None,
                jsonl_path=transcript,
                last_timestamp="2026-07-07T15:21:13Z",
                message_count=1,
                last_prompt=None,
            )

            reset = app.latest_rate_limit_reset_from_chat(
                chat,
                now=dt.datetime(2026, 7, 7, 16, 0, tzinfo=app.UTC),
            )

            self.assertIsNotNone(reset)
            self.assertEqual(reset.hour, 18)
            self.assertEqual(reset.minute, 40)

    def test_settings_fingerprint_detects_changed_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            claude_home.mkdir()
            settings = claude_home / "settings.json"
            settings.write_text('{"model":"opus","effortLevel":"max"}', encoding="utf-8")
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            chat = app.Chat(
                session_id="s",
                title="t",
                cwd=str(root),
                permission_mode=None,
                model=None,
                jsonl_path=root / "s.jsonl",
                last_timestamp=None,
                message_count=0,
                last_prompt=None,
            )

            before = app.settings_fingerprint(paths, chat)
            settings.write_text('{"model":"sonnet","effortLevel":"max"}', encoding="utf-8")
            after = app.settings_fingerprint(paths, chat)

            self.assertTrue(app.compare_fingerprints(before, after))

    def test_discover_chats_sorts_by_last_real_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude_home = Path(tmp) / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            old_session = "33333333-3333-4333-8333-333333333333"
            new_session = "44444444-4444-4444-8444-444444444444"
            (project / f"{old_session}.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": old_session,
                                "timestamp": "2026-01-01T10:00:00Z",
                                "cwd": "C:\\old",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "queue-operation",
                                "sessionId": old_session,
                                "timestamp": "2026-01-03T10:00:00Z",
                                "operation": "enqueue",
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            (project / f"{new_session}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": new_session,
                        "timestamp": "2026-01-02T10:00:00Z",
                        "cwd": "C:\\new",
                        "message": {"role": "assistant", "model": "opus"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            chats = app.discover_chats(claude_home)

            self.assertEqual([chat.session_id for chat in chats], [new_session, old_session])

    def test_discover_chats_uses_max_real_message_timestamp_not_line_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude_home = Path(tmp) / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            session = "66666666-6666-4666-8666-666666666666"
            (project / f"{session}.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "assistant",
                                "sessionId": session,
                                "timestamp": "2026-01-03T10:00:00Z",
                                "cwd": "C:\\work",
                                "message": {"role": "assistant", "model": "opus", "content": "new"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": session,
                                "timestamp": "2026-01-01T10:00:00Z",
                                "cwd": "C:\\work",
                                "message": {"role": "user", "content": "old"},
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            chats = app.discover_chats(claude_home)

            self.assertEqual(chats[0].last_timestamp, "2026-01-03T10:00:00Z")

    def test_discover_chats_dedupes_session_by_latest_real_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude_home = Path(tmp) / ".claude"
            old_project = claude_home / "projects" / "old"
            new_project = claude_home / "projects" / "new"
            old_project.mkdir(parents=True)
            new_project.mkdir(parents=True)
            session = "77777777-7777-4777-8777-777777777777"
            (old_project / f"{session}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": session,
                        "timestamp": "2026-01-01T10:00:00Z",
                        "cwd": "C:\\old",
                        "message": {"role": "assistant", "model": "opus", "content": "old"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (new_project / f"{session}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": session,
                        "timestamp": "2026-01-04T10:00:00Z",
                        "cwd": "C:\\new",
                        "message": {"role": "assistant", "model": "opus", "content": "new"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            chats = app.discover_chats(claude_home)

            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0].last_timestamp, "2026-01-04T10:00:00Z")
            self.assertEqual(chats[0].cwd, "C:\\new")

    def test_discover_claude_chats_reads_only_claude_code_agent_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            (claude_home / "projects").mkdir(parents=True)
            workspace = root / "AppData" / "Roaming" / "Code" / "User" / "workspaceStorage" / "ws"
            workspace.mkdir(parents=True)
            db_path = workspace / "state.vscdb"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("create table ItemTable (key text primary key, value blob)")
                connection.execute(
                    "insert into ItemTable values (?, ?)",
                    (
                        "agentSessions.model.cache",
                        json.dumps(
                            [
                                {
                                    "providerType": "openai-codex",
                                    "providerLabel": "Codex",
                                    "resource": "openai-codex://route/local/abc",
                                    "label": "Codex task",
                                    "timing": {"created": 1783100000000},
                                },
                                {
                                    "providerType": "claude-code",
                                    "providerLabel": "Claude",
                                    "resource": "claude-code:/57ac320c-1111-4111-8111-111111111111",
                                    "label": "continua",
                                    "timing": {"created": 1783090000000, "lastRequestEnded": 1783101000000},
                                    "metadata": {"workingDirectoryPath": str(root)},
                                },
                            ]
                        ),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )

            chats = app.discover_claude_chats(paths)

            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0].title, "continua")
            self.assertEqual(chats[0].session_id, "57ac320c-1111-4111-8111-111111111111")
            self.assertEqual(chats[0].source_key, "claude_code_vscode")
            self.assertTrue(chats[0].can_queue)

    def test_discover_claude_chats_reads_windows_app_sessions_for_active_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            session = "c5169372-f100-45f5-9057-e8032ca446d1"
            (project / f"{session}.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "ai-title",
                                "sessionId": session,
                                "timestamp": "2026-07-09T20:00:00Z",
                                "aiTitle": "Old transcript title",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "sessionId": session,
                                "timestamp": "2026-07-09T22:40:00Z",
                                "cwd": str(root),
                                "permissionMode": "default",
                                "message": {"role": "assistant", "model": "claude-sonnet-4"},
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            app_root = root / "AppData" / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
            active_account = "bdbb2afc-2aa7-4899-9748-a34c09c178b2"
            other_account = "5bd43226-1111-4111-8111-111111111111"
            (app_root / "config.json").parent.mkdir(parents=True)
            (app_root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": active_account}), encoding="utf-8")
            session_root = app_root / "claude-code-sessions"
            active_session_dir = session_root / active_account / "profile"
            active_session_dir.mkdir(parents=True)
            active_session_dir.joinpath("local_active.json").write_text(
                json.dumps(
                    {
                        "sessionId": "local_3dc5f453-d730-422e-8e78-9796e9f071fa",
                        "cliSessionId": session,
                        "title": "VSCode Claude chat session recovery",
                        "cwd": str(root),
                        "lastActivityAt": int(dt.datetime(2026, 7, 9, 22, 32, tzinfo=app.UTC).timestamp() * 1000),
                        "model": "claude-opus-4-8",
                        "effort": "xhigh",
                        "permissionMode": "bypassPermissions",
                        "completedTurns": 4,
                        "isArchived": False,
                    }
                ),
                encoding="utf-8",
            )
            other_session_dir = session_root / other_account / "profile"
            other_session_dir.mkdir(parents=True)
            other_session_dir.joinpath("local_other.json").write_text(
                json.dumps(
                    {
                        "cliSessionId": "961bec34-e1d2-4ff6-b1e8-3826af508a79",
                        "title": "Other account session",
                        "cwd": str(root),
                        "lastActivityAt": int(dt.datetime(2026, 7, 9, 21, 0, tzinfo=app.UTC).timestamp() * 1000),
                        "model": "claude-opus-4-8",
                        "effort": "high",
                        "permissionMode": "bypassPermissions",
                        "completedTurns": 2,
                        "isArchived": False,
                    }
                ),
                encoding="utf-8",
            )
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )

            chats = app.discover_claude_chats(paths)
            active = next(chat for chat in chats if chat.session_id == session)
            other = next(chat for chat in chats if chat.session_id.startswith("961bec34"))

            self.assertEqual(active.title, "VSCode Claude chat session recovery")
            self.assertEqual(active.source_key, "claude_windows_app")
            self.assertEqual(active.last_timestamp, "2026-07-09T22:40:00Z")
            self.assertEqual(active.model, "claude-opus-4-8")
            self.assertEqual(active.effort_level, "xhigh")
            self.assertEqual(active.permission_mode, "bypassPermissions")
            self.assertEqual(active.account_status, "active")
            self.assertTrue(active.can_queue)
            self.assertEqual(
                set(active.account_copies),
                {f"Claude app {active_account[:8]}", f"Claude app {other_account[:8]}"},
            )
            self.assertIsNone(app.account_mismatch_for_chat(paths, active))
            self.assertEqual(app.remember_chat_account(paths, active).account_key, f"claude-app:{active_account}")

            self.assertEqual(other.account_status, "active")
            self.assertTrue(other.can_queue)
            self.assertEqual(
                set(other.account_copies),
                {f"Claude app {active_account[:8]}", f"Claude app {other_account[:8]}"},
            )
            self.assertIsNone(app.account_mismatch_for_chat(paths, other))
            active_profile_records = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (session_root / active_account / "profile").glob("*.json")
            ]
            self.assertTrue(any(record.get("cliSessionId", "").startswith("961bec34") for record in active_profile_records))

    def test_sync_claude_desktop_accounts_copies_latest_session_to_every_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            (claude_home / "projects").mkdir(parents=True)
            app_root = root / "AppData" / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
            active_account = "active-account"
            old_account = "old-account"
            workspace = "workspace"
            (app_root / "config.json").parent.mkdir(parents=True)
            (app_root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": active_account}), encoding="utf-8")
            old_dir = app_root / "claude-code-sessions" / old_account / workspace
            old_dir.mkdir(parents=True)
            session = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            old_dir.joinpath("local_shared.json").write_text(
                json.dumps(
                    {
                        "sessionId": "local_shared",
                        "cliSessionId": session,
                        "title": "Old account latest chat",
                        "cwd": str(root),
                        "lastActivityAt": int(dt.datetime(2026, 7, 9, 12, 0, tzinfo=app.UTC).timestamp() * 1000),
                        "model": "claude-opus-4-8",
                        "effort": "xhigh",
                        "permissionMode": "bypassPermissions",
                        "isArchived": False,
                        "bridgeSessionIds": ["session_source_account"],
                    }
                ),
                encoding="utf-8",
            )
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )

            result = app.sync_claude_desktop_accounts(paths, include_transcripts=False)
            chats = app.discover_claude_chats(paths)

            self.assertEqual(result["created"], 1)
            self.assertFalse(result["transcripts_scanned"])
            target_path = app_root / "claude-code-sessions" / active_account / workspace / "local_shared.json"
            source_path = old_dir / "local_shared.json"
            self.assertTrue(target_path.exists())
            self.assertEqual(json.loads(source_path.read_text(encoding="utf-8"))["bridgeSessionIds"], ["session_source_account"])
            self.assertNotIn("bridgeSessionIds", json.loads(target_path.read_text(encoding="utf-8")))

            contaminated = json.loads(target_path.read_text(encoding="utf-8"))
            contaminated["bridgeSessionIds"] = ["session_source_account"]
            target_path.write_text(json.dumps(contaminated), encoding="utf-8")
            source_path.touch()
            app.sync_claude_desktop_accounts(paths, include_transcripts=False)
            self.assertEqual(json.loads(source_path.read_text(encoding="utf-8"))["bridgeSessionIds"], ["session_source_account"])
            self.assertNotIn("bridgeSessionIds", json.loads(target_path.read_text(encoding="utf-8")))
            self.assertEqual(len([chat for chat in chats if chat.session_id == session]), 1)
            synced = next(chat for chat in chats if chat.session_id == session)
            self.assertEqual(synced.account_status, "active")
            self.assertTrue(synced.can_queue)
            self.assertEqual(
                set(synced.account_copies),
                {f"Claude app {active_account[:8]}", f"Claude app {old_account[:8]}"},
            )

    def test_sync_claude_desktop_accounts_dedupes_same_chat_inside_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            (claude_home / "projects").mkdir(parents=True)
            app_root = root / "AppData" / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
            account = "account-a"
            other_account = "account-b"
            workspace = "workspace"
            (app_root / "config.json").parent.mkdir(parents=True)
            (app_root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": account}), encoding="utf-8")
            account_dir = app_root / "claude-code-sessions" / account / workspace
            other_dir = app_root / "claude-code-sessions" / other_account / workspace
            account_dir.mkdir(parents=True)
            other_dir.mkdir(parents=True)
            session = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
            old_file = account_dir / "local_old.json"
            new_file = account_dir / "local_new.json"
            old_file.write_text(
                json.dumps(
                    {
                        "sessionId": "local_old",
                        "cliSessionId": session,
                        "title": "Older duplicate",
                        "cwd": str(root),
                        "lastActivityAt": int(dt.datetime(2026, 7, 9, 10, 0, tzinfo=app.UTC).timestamp() * 1000),
                        "isArchived": False,
                    }
                ),
                encoding="utf-8",
            )
            new_file.write_text(
                json.dumps(
                    {
                        "sessionId": "local_new",
                        "cliSessionId": session,
                        "title": "Newer duplicate",
                        "cwd": str(root),
                        "lastActivityAt": int(dt.datetime(2026, 7, 9, 11, 0, tzinfo=app.UTC).timestamp() * 1000),
                        "isArchived": False,
                    }
                ),
                encoding="utf-8",
            )
            other_dir.joinpath("local_marker.json").write_text(
                json.dumps(
                    {
                        "sessionId": "local_marker",
                        "cliSessionId": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                        "title": "Other account marker",
                        "cwd": str(root),
                        "lastActivityAt": int(dt.datetime(2026, 7, 9, 9, 0, tzinfo=app.UTC).timestamp() * 1000),
                        "isArchived": False,
                    }
                ),
                encoding="utf-8",
            )
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )

            result = app.sync_claude_desktop_accounts(paths)

            self.assertEqual(result["deduped"], 1)
            self.assertFalse(old_file.exists())
            self.assertTrue(new_file.exists())
            self.assertTrue(result["backups"])

    def test_sync_claude_desktop_accounts_promotes_transcript_only_chat_to_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            session = "12121212-3434-4567-8567-121212121212"
            project.joinpath(f"{session}.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "ai-title",
                                "sessionId": session,
                                "timestamp": "2026-07-09T12:00:00Z",
                                "aiTitle": "Transcript only chat",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "sessionId": session,
                                "timestamp": "2026-07-09T12:01:00Z",
                                "cwd": str(root),
                                "permissionMode": "bypassPermissions",
                                "effortLevel": "high",
                                "message": {"role": "assistant", "model": "claude-opus-4-8", "content": "ok"},
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            app_root = root / "AppData" / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
            active_account = "active-account"
            other_account = "other-account"
            workspace = "workspace"
            (app_root / "config.json").parent.mkdir(parents=True)
            (app_root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": active_account}), encoding="utf-8")
            (app_root / "claude-code-sessions" / active_account / workspace).mkdir(parents=True)
            (app_root / "claude-code-sessions" / other_account / workspace).mkdir(parents=True)
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )

            result = app.sync_claude_desktop_accounts(paths)
            chats = app.discover_claude_chats(paths)

            self.assertEqual(result["transcripts_created"], 2)
            for account in [active_account, other_account]:
                records = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in (app_root / "claude-code-sessions" / account / workspace).glob("*.json")
                ]
                self.assertTrue(any(record.get("cliSessionId") == session for record in records))
            matching = [chat for chat in chats if chat.session_id == session]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].account_status, "active")
            self.assertTrue(matching[0].can_queue)
            self.assertEqual(matching[0].model, "claude-opus-4-8")
            self.assertEqual(matching[0].effort_level, "high")

    def test_synthetic_desktop_session_data_omits_null_optional_fields(self) -> None:
        chat = app.Chat(
            session_id="abababab-abab-4aba-8bab-abababababab",
            title="Synthetic chat",
            cwd="C:\\work",
            permission_mode=None,
            model=None,
            jsonl_path=Path("chat.jsonl"),
            last_timestamp="2026-07-09T12:00:00Z",
            message_count=3,
            last_prompt=None,
            effort_level=None,
        )

        data = app.synthetic_desktop_session_data(chat)

        self.assertNotIn("model", data)
        self.assertNotIn("effort", data)
        self.assertNotIn("permissionMode", data)
        self.assertNotIn(None, data.values())

    def test_sync_claude_desktop_accounts_repairs_null_desktop_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            (claude_home / "projects").mkdir(parents=True)
            app_root = root / "AppData" / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
            active_account = "active-account"
            workspace = "workspace"
            (app_root / "config.json").parent.mkdir(parents=True)
            (app_root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": active_account}), encoding="utf-8")
            session_dir = app_root / "claude-code-sessions" / active_account / workspace
            session_dir.mkdir(parents=True)
            session_file = session_dir / "local_dirty.json"
            session_file.write_text(
                "\ufeff"
                + json.dumps(
                    {
                        "sessionId": "local_dirty",
                        "cliSessionId": "cdcdcdcd-cdcd-4cdc-8dcd-cdcdcdcdcdcd",
                        "title": "Dirty session",
                        "cwd": str(root),
                        "lastActivityAt": int(dt.datetime(2026, 7, 9, 12, 0, tzinfo=app.UTC).timestamp() * 1000),
                        "model": None,
                        "effort": None,
                        "permissionMode": None,
                        "isArchived": False,
                    }
                ),
                encoding="utf-8",
            )
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )

            result = app.sync_claude_desktop_accounts(paths)
            repaired = json.loads(session_file.read_text(encoding="utf-8"))

            self.assertEqual(result["repaired"], 1)
            self.assertFalse(session_file.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertNotIn("model", repaired)
            self.assertNotIn("effort", repaired)
            self.assertNotIn("permissionMode", repaired)
            self.assertTrue(result["backups"])

    def test_transfer_windows_app_session_to_active_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            (claude_home / "projects").mkdir(parents=True)
            app_root = root / "AppData" / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
            active_account = "active-account"
            other_account = "other-account"
            workspace = "workspace"
            (app_root / "config.json").parent.mkdir(parents=True)
            (app_root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": active_account}), encoding="utf-8")
            other_dir = app_root / "claude-code-sessions" / other_account / workspace
            other_dir.mkdir(parents=True)
            session = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            source = other_dir / "local_source.json"
            source.write_text(
                json.dumps(
                    {
                        "sessionId": "local_source",
                        "cliSessionId": session,
                        "title": "Other account chat",
                        "cwd": str(root),
                        "lastActivityAt": int(dt.datetime(2026, 7, 9, 12, 0, tzinfo=app.UTC).timestamp() * 1000),
                        "model": "claude-opus-4-8",
                        "effort": "high",
                        "permissionMode": "bypassPermissions",
                        "isArchived": False,
                    }
                ),
                encoding="utf-8",
            )
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            chat = app.discover_claude_windows_app_sessions(paths, sync_accounts=False)[0]
            self.assertEqual(chat.account_status, "other")

            result = app.transfer_chat_to_active_desktop_account(paths, chat)

            self.assertEqual(result["status"], "copied")
            copied = Path(result["destination"])
            self.assertTrue(copied.exists())
            self.assertTrue(str(copied).endswith("claude-code-sessions/active-account/workspace/local_source.json"))
            data = json.loads(copied.read_text(encoding="utf-8"))
            self.assertEqual(data["cliSessionId"], session)
            self.assertFalse(data["isArchived"])
            refreshed = app.discover_claude_chats(paths)[0]
            self.assertEqual(refreshed.account_status, "active")
            self.assertTrue(refreshed.can_queue)

    def test_transfer_windows_app_session_move_removes_other_account_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            (claude_home / "projects").mkdir(parents=True)
            app_root = root / "AppData" / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
            active_account = "active-account"
            other_account = "other-account"
            workspace = "workspace"
            (app_root / "config.json").parent.mkdir(parents=True)
            (app_root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": active_account}), encoding="utf-8")
            other_dir = app_root / "claude-code-sessions" / other_account / workspace
            other_dir.mkdir(parents=True)
            session = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            source = other_dir / "local_source.json"
            source.write_text(
                json.dumps(
                    {
                        "sessionId": "local_source",
                        "cliSessionId": session,
                        "title": "Move me",
                        "cwd": str(root),
                        "lastActivityAt": int(dt.datetime(2026, 7, 9, 12, 0, tzinfo=app.UTC).timestamp() * 1000),
                        "model": "claude-opus-4-8",
                        "effort": "xhigh",
                        "permissionMode": "bypassPermissions",
                        "isArchived": False,
                    }
                ),
                encoding="utf-8",
            )
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            chat = app.discover_claude_windows_app_sessions(paths, sync_accounts=False)[0]

            result = app.transfer_chat_to_active_desktop_account(paths, chat, move=True)

            self.assertEqual(result["status"], "moved")
            self.assertFalse(source.exists())
            self.assertTrue(Path(result["destination"]).exists())
            self.assertTrue(Path(result["backup"]).exists())

    def test_transfer_local_transcript_creates_active_windows_app_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            session = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            transcript = project / f"{session}.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": session,
                        "timestamp": "2026-07-09T12:00:00Z",
                        "cwd": str(root),
                        "permissionMode": "bypassPermissions",
                        "message": {"role": "assistant", "model": "claude-opus-4-8", "content": "ok"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            app_root = root / "AppData" / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
            active_account = "active-account"
            workspace = "workspace"
            (app_root / "config.json").parent.mkdir(parents=True)
            (app_root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": active_account}), encoding="utf-8")
            (app_root / "claude-code-sessions" / active_account / workspace).mkdir(parents=True)
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            chat = app.discover_chats(claude_home)[0]
            self.assertEqual(chat.source_key, "claude_code")

            result = app.transfer_chat_to_active_desktop_account(paths, chat)

            self.assertTrue(result["synthetic"])
            copied = Path(result["destination"])
            data = json.loads(copied.read_text(encoding="utf-8"))
            self.assertEqual(data["cliSessionId"], session)
            self.assertEqual(data["cwd"], str(root))
            self.assertEqual(data["permissionMode"], "bypassPermissions")
            refreshed = app.discover_claude_chats(paths)[0]
            self.assertEqual(refreshed.source_key, "claude_windows_app")
            self.assertEqual(refreshed.account_status, "active")

    def test_discover_remote_ssh_claude_code_agent_cache_is_queueable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            (claude_home / "projects").mkdir(parents=True)
            workspace = root / "AppData" / "Roaming" / "Code" / "User" / "workspaceStorage" / "remote"
            workspace.mkdir(parents=True)
            (workspace / "workspace.json").write_text(
                json.dumps(
                    {
                        "folder": "vscode-remote://ssh-remote%2Bhomeserver/c%3A/Users/serveradmin/sample-project"
                    }
                ),
                encoding="utf-8",
            )
            db_path = workspace / "state.vscdb"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("create table ItemTable (key text primary key, value blob)")
                connection.execute(
                    "insert into ItemTable values (?, ?)",
                    (
                        "agentSessions.model.cache",
                        json.dumps(
                            [
                                {
                                    "providerType": "claude-code",
                                    "providerLabel": "Claude",
                                    "resource": "claude-code:/57ac320c-1111-4111-8111-111111111111",
                                    "label": "continua",
                                    "timing": {"created": 1783090000000, "lastRequestEnded": 1783101000000},
                                    "metadata": {"workingDirectoryPath": "C:\\Users\\dev\\sample-project"},
                                }
                            ]
                        ),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )

            with patch.object(app, "ssh_run", side_effect=app.subprocess.SubprocessError("no ssh")):
                chats = app.discover_claude_chats(paths)

            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0].title, "continua")
            self.assertEqual(chats[0].source_key, "claude_code_vscode_remote_ssh")
            self.assertTrue(chats[0].can_queue)
            self.assertEqual(chats[0].remote_host, "homeserver")
            self.assertEqual(chats[0].cwd, "C:\\Users\\serveradmin\\sample-project")

    def test_remote_ssh_transcripts_enrich_agent_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            (claude_home / "projects").mkdir(parents=True)
            workspace = root / "AppData" / "Roaming" / "Code" / "User" / "workspaceStorage" / "remote"
            workspace.mkdir(parents=True)
            (workspace / "workspace.json").write_text(
                json.dumps(
                    {
                        "folder": "vscode-remote://ssh-remote%2Bhomeserver/c%3A/Users/serveradmin/sample-project"
                    }
                ),
                encoding="utf-8",
            )
            db_path = workspace / "state.vscdb"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("create table ItemTable (key text primary key, value blob)")
                connection.execute(
                    "insert into ItemTable values (?, ?)",
                    (
                        "agentSessions.model.cache",
                        json.dumps(
                            [
                                {
                                    "providerType": "claude-code",
                                    "providerLabel": "Claude",
                                    "resource": "claude-code:/57ac320c-1111-4111-8111-111111111111",
                                    "label": "continua",
                                    "timing": {"created": 1783090000000, "lastRequestEnded": 1783091000000},
                                    "metadata": {"workingDirectoryPath": "C:\\Users\\dev\\sample-project"},
                                }
                            ]
                        ),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            summary = {
                "session_id": "57ac320c-1111-4111-8111-111111111111",
                "title": "Transcript title",
                "cwd": "C:\\Users\\dev\\sample-project",
                "permission_mode": "bypassPermissions",
                "model": "opus",
                "jsonl_path": "C:\\Users\\serveradmin\\.claude\\projects\\p\\57ac320c-1111-4111-8111-111111111111.jsonl",
                "last_timestamp": "2026-07-07T02:00:00Z",
                "message_count": 42,
                "last_prompt": "continua",
            }
            encoded = base64.b64encode(json.dumps(summary).encode("utf-8")).decode("ascii")

            with patch.object(
                app,
                "ssh_run",
                return_value=app.subprocess.CompletedProcess(args=[], returncode=0, stdout=encoded + "\n", stderr=""),
            ):
                chats = app.discover_claude_chats(paths)

            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0].title, "continua")
            self.assertEqual(chats[0].source_key, "claude_code_vscode_remote_ssh")
            self.assertEqual(chats[0].message_count, 42)
            self.assertEqual(chats[0].model, "opus")
            self.assertEqual(chats[0].permission_mode, "bypassPermissions")
            self.assertEqual(chats[0].last_timestamp, "2026-07-07T02:00:00Z")
            self.assertEqual(chats[0].cwd, "C:\\Users\\serveradmin\\sample-project")
            self.assertTrue(chats[0].can_queue)

    def test_build_command_preserves_effective_settings(self) -> None:
        item = {
            "session_id": "abc",
            "source_key": "claude_code_vscode",
            "fingerprint": {
                "effective": {
                    "model": "opus",
                    "effortLevel": "max",
                    "permissionMode": "bypassPermissions",
                }
            },
        }
        command = app.build_claude_command(Path("/bin/claude"), item, use_ide=True)

        self.assertIn("--resume", command)
        self.assertIn("abc", command)
        self.assertIn("--model", command)
        self.assertIn("opus", command)
        self.assertIn("--effort", command)
        self.assertIn("max", command)
        self.assertIn("--permission-mode", command)
        self.assertIn("bypassPermissions", command)
        self.assertIn("--ide", command)

    def test_build_command_does_not_attach_ide_to_claude_desktop(self) -> None:
        item = {
            "session_id": "abc",
            "source": "Claude Windows App",
            "source_key": "claude_windows_app",
            "fingerprint": {"effective": {"model": "opus", "effortLevel": "xhigh"}},
        }

        command = app.build_claude_command(Path("/bin/claude"), item, use_ide=True)

        self.assertNotIn("--ide", command)
        self.assertEqual(command[-2:], ["--resume", "abc"])

    def test_auto_continue_item_preserves_claude_desktop_source(self) -> None:
        item = app.auto_continue_as_item(
            {
                "session_id": "desktop-session",
                "source": "Claude Windows App",
                "source_key": "claude_windows_app",
            }
        )

        self.assertEqual(item["source_key"], "claude_windows_app")
        self.assertFalse(app.claude_item_uses_ide(item, True))

    def test_claude_desktop_uses_its_versioned_app_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = app.Paths(
                windows_home=root,
                claude_home=root / ".claude",
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            sessions = paths.claude_home / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "9308.json").write_text(
                json.dumps({"sessionId": "desktop-session", "version": "2.1.205", "entrypoint": "claude-desktop"}),
                encoding="utf-8",
            )
            executable = root / "AppData" / "Roaming" / "Claude" / "claude-code" / "2.1.205" / "claude.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"")
            item = {
                "session_id": "desktop-session",
                "source": "Claude Windows App",
                "source_key": "claude_windows_app",
            }

            selected = app.find_claude_desktop_executable(paths, item)

            self.assertEqual(selected, executable)

    def test_build_command_uses_permission_override(self) -> None:
        item = {
            "session_id": "abc",
            "fingerprint": {"effective": {"permissionMode": None}},
            "permission_mode_override": "bypassPermissions",
        }

        command = app.build_claude_command(Path("/bin/claude"), item, use_ide=True)

        self.assertIn("--permission-mode", command)
        self.assertIn("bypassPermissions", command)

    def test_build_command_uses_model_and_effort_overrides(self) -> None:
        item = {
            "session_id": "abc",
            "fingerprint": {"effective": {"model": "opus", "effortLevel": "high", "permissionMode": "default"}},
            "model_override": "sonnet",
            "effort_level_override": "xhigh",
        }

        command = app.build_claude_command(Path("/bin/claude"), item, use_ide=True)

        self.assertIn("sonnet", command)
        self.assertNotIn("opus", command)
        self.assertIn("xhigh", command)
        self.assertNotIn("high", command)

    def test_discover_chats_marks_windows_cwd_runnable_when_windows_sees_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude_home = Path(tmp) / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            session = "13131313-1313-4131-8131-131313131313"
            (project / f"{session}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": session,
                        "timestamp": "2026-01-01T10:00:00Z",
                        "cwd": "V:\\",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(app, "windows_path_accessible", return_value=True):
                chats = app.discover_chats(claude_home)

            self.assertTrue(chats[0].can_queue)

    def test_run_claude_uses_windows_launcher_for_windows_only_cwd(self) -> None:
        item = {
            "session_id": "abc",
            "cwd": "V:\\",
            "prompt": "continua",
            "fingerprint": {"effective": {"model": "opus", "effortLevel": None, "permissionMode": "bypassPermissions"}},
        }
        captured_payload = {}

        def fake_hidden(command, cwd=None):
            payload_path = Path(command[3].split()[-1].strip('"'))
            captured_payload.update(json.loads(payload_path.read_text(encoding="utf-8")))
            return ["hidden-proxy", command[3]]

        def fake_run(command, **kwargs):
            return app.subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with (
            patch.object(app, "cwd_accessible", return_value=False),
            patch.object(app, "windows_path_accessible", return_value=True),
            patch.object(app, "is_wsl", return_value=True),
            patch.object(app, "local_windows_hidden_command", side_effect=fake_hidden) as hidden,
            patch.object(app.subprocess, "run", side_effect=fake_run) as run,
        ):
            result = app.run_claude(app.Paths(Path("/tmp"), Path("/tmp/.claude"), Path("/tmp/s"), Path("/tmp/q"), Path("/tmp/l")), Path("/mnt/c/claude.exe"), item, 10, True)

        self.assertEqual(result.returncode, 0)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "hidden-proxy")
        wrapped = hidden.call_args.args[0]
        self.assertEqual(wrapped[:3], ["cmd.exe", "/d", "/c"])
        self.assertIn('set "ANTHROPIC_API_KEY="', wrapped[3])
        self.assertIn("py -3", wrapped[3])
        self.assertEqual(captured_payload["prompt"], "continua")
        self.assertIn("--resume", captured_payload["args"])
        self.assertIn("--ide", captured_payload["args"])

    def test_windows_claude_launcher_preserves_utf8_output_under_cp1252(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "launch_claude.py"
            child = root / "unicode_child.py"
            payload = root / "payload.json"
            launcher.write_text(app.WINDOWS_CLAUDE_LAUNCHER, encoding="utf-8")
            child.write_text(
                "import sys\n"
                "sys.stdout.buffer.write('done ✅'.encode('utf-8'))\n"
                "sys.stderr.buffer.write('warning ⚠'.encode('utf-8'))\n",
                encoding="utf-8",
            )
            payload.write_text(
                json.dumps(
                    {
                        "exe": app.sys.executable,
                        "args": [str(child)],
                        "prompt": "continua",
                        "cwd": str(root),
                        "timeout": 10,
                        "clear_env": [],
                    }
                ),
                encoding="utf-8",
            )
            env = app.os.environ.copy()
            env["PYTHONIOENCODING"] = "cp1252"

            result = app.subprocess.run(
                [app.sys.executable, str(launcher), str(payload)],
                capture_output=True,
                timeout=15,
                env=env,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.decode("utf-8"), "done ✅")
            self.assertEqual(result.stderr.decode("utf-8"), "warning ⚠")

    def test_remote_settings_fingerprint_reads_remote_effective_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = app.Paths(
                windows_home=root,
                claude_home=root / ".claude",
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            chat = app.Chat(
                session_id="57ac320c-1111-4111-8111-111111111111",
                title="continua",
                cwd="C:\\Users\\serveradmin\\repo",
                permission_mode=None,
                model=None,
                jsonl_path=root / "57ac320c.jsonl",
                last_timestamp=None,
                message_count=0,
                last_prompt=None,
                remote_kind="ssh",
                remote_host="homeserver",
                remote_cwd="C:\\Users\\serveradmin\\repo",
            )
            calls = []

            def fake_ssh_run(*args, **kwargs):
                calls.append(args)
                if len(calls) == 1:
                    stdout = "C:\\Users\\serveradmin\\.claude\\settings.json|1|abc123\n"
                else:
                    stdout = '{"model":"opus","effortLevel":"xhigh","permissionMode":"bypassPermissions"}'
                return app.subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

            with patch.object(app, "ssh_run", side_effect=fake_ssh_run):
                fingerprint = app.settings_fingerprint(paths, chat)

            self.assertEqual(fingerprint["effective"]["model"], "opus")
            self.assertEqual(fingerprint["effective"]["effortLevel"], "xhigh")
            self.assertEqual(fingerprint["effective"]["permissionMode"], "bypassPermissions")
            self.assertEqual(len(calls), 2)

    def test_rate_limit_detection_and_relative_reset(self) -> None:
        text = "Claude AI usage limit reached. Try again in 1 hour 15 minutes."
        self.assertTrue(app.is_rate_limit_text(text))
        reset = app.parse_reset_time(text)
        self.assertIsNotNone(reset)

    def test_weekly_limit_detection_and_reset_clock(self) -> None:
        text = "You've hit your weekly limit · resets 9am (Europe/Rome)"
        self.assertTrue(app.is_rate_limit_text(text))
        reset = app.parse_reset_time(text)
        self.assertIsNotNone(reset)
        self.assertEqual(reset.hour, 9)
        self.assertEqual(reset.minute, 0)

    def test_retry_time_after_limit_adds_reset_delay(self) -> None:
        result = app.ClaudeRunResult(
            returncode=1,
            stdout="",
            stderr="You've hit your weekly limit · resets 9am (Europe/Rome)",
            rate_limited=True,
            reset_at="2026-01-01T09:00:00+01:00",
        )

        retry_at = app.parse_iso(app.retry_time_after_limit(result, poll_seconds=300))

        self.assertIsNotNone(retry_at)
        expected = app.parse_iso(result.reset_at) + dt.timedelta(seconds=app.RATE_LIMIT_RESET_DELAY_SECONDS)
        self.assertEqual(retry_at, expected)

    def test_permission_wait_detection(self) -> None:
        text = "Le scritture e ora anche i comandi Bash richiedono approvazione."
        self.assertTrue(app.is_permission_wait_text(text))

    def test_queue_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "queue.json"
            data = {"version": 1, "items": [{"id": "a", "status": "pending"}]}
            app.save_queue(queue, data)
            loaded = app.load_queue(queue)
            self.assertEqual(loaded["items"][0]["id"], "a")

    def test_pending_items_sort_by_priority_then_order(self) -> None:
        queue = {
            "items": [
                {"id": "normal-1", "status": "pending", "priority": 100, "order": 0},
                {"id": "urgent", "status": "pending", "priority": 0, "order": 5},
                {"id": "normal-2", "status": "pending", "priority": 100, "order": 1},
                {"id": "done", "status": "done", "priority": 0, "order": 0},
            ]
        }

        self.assertEqual(
            [item["id"] for item in app.pending_items(queue)],
            ["urgent", "normal-1", "normal-2"],
        )

    def test_runner_sends_continue_after_unrecorded_rate_limit_then_retries_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            (claude_home / "settings.json").write_text('{"model":"opus","effortLevel":"max"}', encoding="utf-8")
            session = "22222222-2222-4222-8222-222222222222"
            transcript = project / f"{session}.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": session,
                        "timestamp": "2026-01-01T10:00:00Z",
                        "cwd": str(root),
                        "permissionMode": "default",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            chat = app.discover_chats(claude_home)[0]
            queue = {
                "version": 1,
                "items": [
                    {
                        "id": "rate1",
                        "status": "pending",
                        "created_at": app.now_utc(),
                        "order": 0,
                        "session_id": session,
                        "title": "fake",
                        "cwd": str(root),
                        "prompt": "hello",
                        "attempts": 0,
                        "not_before": None,
                        "last_error": None,
                        "last_log": None,
                        "fingerprint": app.settings_fingerprint(paths, chat),
                    }
                ],
            }
            app.save_queue(paths.queue_file, queue)

            fake = root / "fake_claude.py"
            counter = root / "counter.txt"
            prompts = root / "prompts.txt"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                f"prompts = Path({str(prompts)!r})\n"
                "count = int(counter.read_text() or '0') if counter.exists() else 0\n"
                "counter.write_text(str(count + 1))\n"
                "prompt = __import__('sys').stdin.read()\n"
                "with prompts.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(prompt + '\\n---\\n')\n"
                "if count == 0:\n"
                "    import sys\n"
                "    print('Claude AI usage limit reached. Try again 2026-01-01T00:00:00Z.', file=sys.stderr)\n"
                "    raise SystemExit(1)\n"
                "print('ok')\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | 0o111)

            first = app.main(
                [
                    "--windows-home",
                    str(root),
                    "--state-dir",
                    str(paths.state_dir),
                    "--claude",
                    str(fake),
                    "run",
                    "--once",
                    "--no-ide",
                    "--poll-seconds",
                    "1",
                ]
            )
            self.assertEqual(first, app.RATE_LIMIT_EXIT)
            loaded = app.load_queue(paths.queue_file)
            self.assertEqual(loaded["items"][0]["status"], "pending")
            self.assertIsNotNone(app.active_recovery(loaded))

            second = app.main(
                [
                    "--windows-home",
                    str(root),
                    "--state-dir",
                    str(paths.state_dir),
                    "--claude",
                    str(fake),
                    "run",
                    "--once",
                    "--no-ide",
                    "--poll-seconds",
                    "1",
                ]
            )
            loaded = app.load_queue(paths.queue_file)
            self.assertEqual(second, 0)
            self.assertEqual(loaded["items"][0]["status"], "pending")
            self.assertIsNone(app.active_recovery(loaded))

            third = app.main(
                [
                    "--windows-home",
                    str(root),
                    "--state-dir",
                    str(paths.state_dir),
                    "--claude",
                    str(fake),
                    "run",
                    "--once",
                    "--no-ide",
                    "--poll-seconds",
                    "1",
                ]
            )
            loaded = app.load_queue(paths.queue_file)
            self.assertEqual(third, 0)
            self.assertEqual(loaded["items"][0]["status"], "done")
            self.assertEqual(loaded["items"][0]["attempts"], 2)
            sent = prompts.read_text(encoding="utf-8").split("\n---\n")
            self.assertEqual(sent[:3], ["hello", "continua", "hello"])

    def test_runner_sends_continue_after_recorded_rate_limit_then_marks_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            (claude_home / "settings.json").write_text('{"model":"opus","effortLevel":"max"}', encoding="utf-8")
            session = "55555555-5555-4555-8555-555555555555"
            transcript = project / f"{session}.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": session,
                        "timestamp": "2026-01-01T10:00:00Z",
                        "cwd": str(root),
                        "permissionMode": "default",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            chat = app.discover_chats(claude_home)[0]
            app.save_queue(
                paths.queue_file,
                {
                    "version": 1,
                    "items": [
                        {
                            "id": "rate2",
                            "status": "pending",
                            "created_at": app.now_utc(),
                            "order": 0,
                            "session_id": session,
                            "title": "fake",
                            "cwd": str(root),
                            "prompt": "hello",
                            "attempts": 0,
                            "not_before": None,
                            "last_error": None,
                            "last_log": None,
                            "fingerprint": app.settings_fingerprint(paths, chat),
                        }
                    ],
                },
            )

            fake = root / "fake_claude.py"
            counter = root / "counter.txt"
            prompts = root / "prompts.txt"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "from datetime import datetime, timezone\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                f"prompts = Path({str(prompts)!r})\n"
                f"transcript = Path({str(transcript)!r})\n"
                f"session = {session!r}\n"
                f"cwd = {str(root)!r}\n"
                "count = int(counter.read_text() or '0') if counter.exists() else 0\n"
                "counter.write_text(str(count + 1))\n"
                "prompt = sys.stdin.read()\n"
                "with prompts.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(prompt + '\\n---\\n')\n"
                "if count == 0:\n"
                "    row = {'type': 'user', 'sessionId': session, 'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'cwd': cwd, 'message': {'role': 'user', 'content': prompt}}\n"
                "    with transcript.open('a', encoding='utf-8') as handle:\n"
                "        handle.write(json.dumps(row) + '\\n')\n"
                "    print('Claude AI usage limit reached. Try again 2026-01-01T00:00:00Z.', file=sys.stderr)\n"
                "    raise SystemExit(1)\n"
                "print('ok')\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | 0o111)

            first = app.main(
                [
                    "--windows-home",
                    str(root),
                    "--state-dir",
                    str(paths.state_dir),
                    "--claude",
                    str(fake),
                    "run",
                    "--once",
                    "--no-ide",
                    "--poll-seconds",
                    "1",
                ]
            )
            self.assertEqual(first, app.RATE_LIMIT_EXIT)
            loaded = app.load_queue(paths.queue_file)
            self.assertEqual(loaded["items"][0]["status"], "recovery")
            self.assertTrue(app.active_recovery(loaded)["source_prompt_recorded"])

            second = app.main(
                [
                    "--windows-home",
                    str(root),
                    "--state-dir",
                    str(paths.state_dir),
                    "--claude",
                    str(fake),
                    "run",
                    "--once",
                    "--no-ide",
                    "--poll-seconds",
                    "1",
                ]
            )
            loaded = app.load_queue(paths.queue_file)
            self.assertEqual(second, 0)
            self.assertIsNone(app.active_recovery(loaded))
            self.assertEqual(loaded["items"][0]["status"], "done")
            sent = prompts.read_text(encoding="utf-8").split("\n---\n")
            self.assertEqual(sent[:2], ["hello", "continua"])

    def test_auto_continue_retries_before_pending_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            project = claude_home / "projects" / "p"
            project.mkdir(parents=True)
            (claude_home / "settings.json").write_text('{"model":"opus","effortLevel":"max"}', encoding="utf-8")
            session = "88888888-8888-4888-8888-888888888888"
            transcript = project / f"{session}.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": session,
                        "timestamp": "2026-01-01T10:00:00Z",
                        "cwd": str(root),
                        "permissionMode": "default",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            paths = app.Paths(
                windows_home=root,
                claude_home=claude_home,
                state_dir=root / ".state",
                queue_file=root / ".state" / "queue.json",
                log_dir=root / ".state" / "logs",
            )
            chat = app.discover_chats(claude_home)[0]
            fingerprint = app.settings_fingerprint(paths, chat)
            app.save_queue(
                paths.queue_file,
                {
                    "version": 1,
                    "items": [
                        {
                            "id": "queued",
                            "status": "pending",
                            "created_at": app.now_utc(),
                            "order": 0,
                            "session_id": session,
                            "title": "fake",
                            "cwd": str(root),
                            "prompt": "queued prompt",
                            "attempts": 0,
                            "not_before": None,
                            "last_error": None,
                            "last_log": None,
                            "fingerprint": fingerprint,
                        }
                    ],
                    "recovery": None,
                    "auto_continue": {
                        "enabled": True,
                        "status": "armed",
                        "monitor_limit": False,
                        "created_at": app.now_utc(),
                        "session_id": session,
                        "title": "fake",
                        "cwd": str(root),
                        "prompt": app.RECOVERY_PROMPT,
                        "attempts": 0,
                        "not_before": None,
                        "last_error": None,
                        "last_log": None,
                        "fingerprint": fingerprint,
                    },
                },
            )

            fake = root / "fake_claude.py"
            counter = root / "counter.txt"
            prompts = root / "prompts.txt"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                f"prompts = Path({str(prompts)!r})\n"
                "count = int(counter.read_text() or '0') if counter.exists() else 0\n"
                "counter.write_text(str(count + 1))\n"
                "prompt = sys.stdin.read()\n"
                "with prompts.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(prompt + '\\n---\\n')\n"
                "if count == 0:\n"
                "    print('Claude AI usage limit reached. Try again 2026-01-01T00:00:00Z.', file=sys.stderr)\n"
                "    raise SystemExit(1)\n"
                "print('ok')\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | 0o111)

            first = app.main(
                [
                    "--windows-home",
                    str(root),
                    "--state-dir",
                    str(paths.state_dir),
                    "--claude",
                    str(fake),
                    "run",
                    "--once",
                    "--no-ide",
                    "--poll-seconds",
                    "1",
                ]
            )
            loaded = app.load_queue(paths.queue_file)
            self.assertEqual(first, app.RATE_LIMIT_EXIT)
            self.assertTrue(app.active_auto_continue(loaded))
            self.assertEqual(loaded["auto_continue"]["status"], "waiting_limit")
            self.assertEqual(loaded["items"][0]["status"], "pending")

            second = app.main(
                [
                    "--windows-home",
                    str(root),
                    "--state-dir",
                    str(paths.state_dir),
                    "--claude",
                    str(fake),
                    "run",
                    "--once",
                    "--no-ide",
                    "--poll-seconds",
                    "1",
                ]
            )
            loaded = app.load_queue(paths.queue_file)
            self.assertEqual(second, 0)
            self.assertIsNone(app.active_auto_continue(loaded))
            self.assertEqual(loaded["auto_continue"]["status"], "done")
            self.assertEqual(loaded["items"][0]["status"], "pending")

            third = app.main(
                [
                    "--windows-home",
                    str(root),
                    "--state-dir",
                    str(paths.state_dir),
                    "--claude",
                    str(fake),
                    "run",
                    "--once",
                    "--no-ide",
                    "--poll-seconds",
                    "1",
                ]
            )
            loaded = app.load_queue(paths.queue_file)
            self.assertEqual(third, 0)
            self.assertEqual(loaded["items"][0]["status"], "done")
            sent = prompts.read_text(encoding="utf-8").split("\n---\n")
            self.assertEqual(sent[:3], ["continua", "continua", "queued prompt"])


if __name__ == "__main__":
    unittest.main()
