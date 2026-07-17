from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claude_vscode_queue import app
from claude_vscode_queue import web


class AccountSyncTests(unittest.TestCase):
    @staticmethod
    def _paths(root: Path) -> app.Paths:
        state_dir = root / ".state"
        return app.Paths(
            windows_home=root,
            claude_home=root / ".claude",
            state_dir=state_dir,
            queue_file=state_dir / "queue.json",
            log_dir=state_dir / "logs",
        )

    @staticmethod
    def _claude_fixture(
        root: Path,
        *,
        active_account: str = "account-a",
        other_account: str = "account-b",
        workspace: str = "workspace",
    ) -> tuple[app.Paths, Path, Path, Path]:
        paths = AccountSyncTests._paths(root)
        (paths.claude_home / "projects").mkdir(parents=True)
        app_root = (
            root
            / "AppData"
            / "Local"
            / "Packages"
            / "Claude_test"
            / "LocalCache"
            / "Roaming"
            / "Claude"
        )
        app_root.mkdir(parents=True)
        (app_root / "config.json").write_text(
            json.dumps({"lastKnownAccountUuid": active_account}),
            encoding="utf-8",
        )
        sessions_root = app_root / "claude-code-sessions"
        active_dir = sessions_root / active_account / workspace
        other_dir = sessions_root / other_account / workspace
        active_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        return paths, app_root, active_dir, other_dir

    @staticmethod
    def _write_claude_session(
        destination: Path,
        session_id: str,
        cwd: Path,
        *,
        archived: bool = False,
        title: str = "Shared test chat",
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        app.write_desktop_session_json(
            destination,
            {
                "sessionId": destination.stem,
                "cliSessionId": session_id,
                "title": title,
                "cwd": str(cwd),
                "lastActivityAt": 1_800_000_000_000,
                "model": "claude-test",
                "effort": "high",
                "permissionMode": "default",
                "isArchived": archived,
            },
        )
        return destination

    @staticmethod
    def _set_archived(path: Path, archived: bool) -> None:
        previous_mtime = path.stat().st_mtime_ns
        data = json.loads(path.read_text(encoding="utf-8"))
        data["isArchived"] = archived
        app.write_desktop_session_json(path, data)
        changed_mtime = max(time.time_ns(), previous_mtime + 10_000_000)
        os.utime(path, ns=(changed_mtime, changed_mtime))

    @staticmethod
    def _read(path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_transcript(paths: app.Paths, session_id: str, cwd: Path) -> Path:
        project = paths.claude_home / "projects" / "fixture"
        project.mkdir(parents=True, exist_ok=True)
        transcript = project / f"{session_id}.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "timestamp": "2026-07-12T10:00:00Z",
                    "cwd": str(cwd),
                    "message": {"role": "assistant", "model": "claude-test", "content": "ok"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return transcript

    @staticmethod
    def _write_artifact_transcript(
        paths: app.Paths,
        session_id: str,
        cwd: Path,
        source_file: Path,
        slug: str,
    ) -> Path:
        project = paths.claude_home / "projects" / "fixture"
        project.mkdir(parents=True, exist_ok=True)
        transcript = project / f"{session_id}.jsonl"
        rows = [
            {
                "type": "assistant",
                "sessionId": session_id,
                "timestamp": "2026-07-17T10:00:00Z",
                "cwd": str(cwd),
                "message": {
                    "role": "assistant",
                    "model": "claude-test",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "artifact-call",
                            "name": "Artifact",
                            "input": {
                                "file_path": str(source_file),
                                "description": "Test artifact",
                                "favicon": "T",
                                "label": "Test",
                            },
                        }
                    ],
                },
            },
            {
                "type": "frame-link",
                "sessionId": session_id,
                "path": str(source_file),
                "frameUrl": f"https://claude.ai/code/artifact/{slug}",
                "title": "Account-safe artifact",
                "timestamp": "2026-07-17T10:00:01Z",
            },
            {
                "type": "last-prompt",
                "sessionId": session_id,
                "lastPrompt": "continue the artifact work",
            },
        ]
        transcript.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        return transcript

    @staticmethod
    def _codex_rollout(codex_home: Path, session_id: str, *, archived: bool = False) -> Path:
        root = codex_home / ("archived_sessions" if archived else "sessions") / "2026" / "07" / "12"
        root.mkdir(parents=True, exist_ok=True)
        rollout = root / f"rollout-2026-07-12T10-00-00-{session_id}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "timestamp": "2026-07-12T10:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": session_id},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return rollout

    @staticmethod
    def _linked_index(source_id: str, destination_id: str) -> dict[str, object]:
        return {
            "version": 1,
            "accounts": {},
            "sessions": {},
            "codex_links": {
                "group-1": {
                    "provider": app.PROVIDER_CODEX,
                    "state": app.DESKTOP_STATE_ACTIVE,
                    "threads": {
                        source_id: {
                            "account_key": "codex:source",
                            "last_state": app.DESKTOP_STATE_ACTIVE,
                            "missing_scans": 0,
                        },
                        destination_id: {
                            "account_key": "codex:destination",
                            "last_state": app.DESKTOP_STATE_ACTIVE,
                            "missing_scans": 0,
                            "forked_from": source_id,
                        },
                    },
                }
            },
        }

    def test_atomic_replace_retries_a_transient_windows_sharing_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "new.json"
            destination = root / "state.json"
            source.write_text("new", encoding="utf-8")
            destination.write_text("old", encoding="utf-8")
            replace = os.replace
            attempts = 0

            def transient_replace(source_path: Path, destination_path: Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError(13, "sharing violation")
                replace(source_path, destination_path)

            with (
                patch.object(app.os, "replace", side_effect=transient_replace),
                patch.object(app.time, "sleep") as sleep,
            ):
                app.atomic_replace_with_retry(source, destination)

            self.assertEqual(destination.read_text(encoding="utf-8"), "new")
            self.assertFalse(source.exists())
            sleep.assert_called_once_with(app.ATOMIC_REPLACE_RETRY_DELAYS[0])

    def test_desktop_root_identity_is_equal_for_windows_and_wsl_paths(self) -> None:
        windows = r"C:\Users\Example\AppData\Local\Packages\Claude_test\LocalCache\Roaming\Claude"
        wsl = "/mnt/c/Users/Example/AppData/Local/Packages/Claude_test/LocalCache/Roaming/Claude"

        self.assertEqual(
            app.desktop_sync_root_identity(windows),
            app.desktop_sync_root_identity(wsl),
        )

    @unittest.skipUnless(os.name == "nt", "Windows path conversion")
    def test_windows_to_local_path_accepts_a_wsl_mount_path_on_windows(self) -> None:
        self.assertEqual(
            app.windows_to_local_path("/mnt/c/Users/Example/file.jsonl"),
            Path(r"C:\Users\Example\file.jsonl"),
        )

    def test_web_claude_version_timeout_is_concise_and_allows_hidden_proxy_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = web.WebState(self._paths(Path(tmp)), Path("claude.exe"))
            error = web.subprocess.TimeoutExpired(
                cmd=["powershell.exe", "-EncodedCommand", "secret-command"],
                timeout=30,
            )

            with patch.object(web.subprocess, "run", side_effect=error) as run:
                version = state.claude_version()

            self.assertEqual(version, "verifica versione non disponibile")
            self.assertEqual(run.call_args.kwargs["timeout"], 30)
            self.assertNotIn("EncodedCommand", version)

    def test_web_codex_version_timeout_is_concise_and_allows_hidden_proxy_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = web.WebState(self._paths(Path(tmp)), None, Path("codex.exe"))
            error = web.subprocess.TimeoutExpired(
                cmd=["powershell.exe", "-EncodedCommand", "secret-command"],
                timeout=30,
            )

            with patch.object(app, "run_codex_cli_command", side_effect=error) as run:
                version = state.codex_version()

            self.assertEqual(version, "verifica versione non disponibile")
            self.assertEqual(run.call_args.kwargs["timeout"], 30)
            self.assertNotIn("EncodedCommand", version)

    def test_desktop_root_state_migration_repairs_workspace_tombstone_only(self) -> None:
        moved_session = "10101010-1010-4010-8010-101010101010"
        deleted_session = "20202020-2020-4020-8020-202020202020"
        active = {
            "state": app.DESKTOP_STATE_ACTIVE,
            "replicas": {
                "account-a/workspace-old": {
                    "account_uuid": "account-a",
                    "workspace_uuid": "workspace-old",
                    "present": True,
                    "missing_scans": 0,
                    "mtime_ns": 10,
                }
            },
        }
        moved_tombstone = {
            "state": app.DESKTOP_STATE_DELETED,
            "state_changed_at_ns": 30,
            "replicas": {
                "account-a/workspace-old": {
                    "account_uuid": "account-a",
                    "workspace_uuid": "workspace-old",
                    "present": False,
                    "missing_scans": 2,
                    "mtime_ns": 10,
                },
                "account-a/workspace-new": {
                    "account_uuid": "account-a",
                    "workspace_uuid": "workspace-new",
                    "present": True,
                    "missing_scans": 0,
                    "mtime_ns": 20,
                },
            },
        }
        hard_tombstone = {
            "state": app.DESKTOP_STATE_DELETED,
            "state_changed_at_ns": 40,
            "replicas": {
                "account-a/workspace-old": {
                    "account_uuid": "account-a",
                    "workspace_uuid": "workspace-old",
                    "present": False,
                    "missing_scans": 2,
                    "mtime_ns": 10,
                },
                "account-b/workspace-old": {
                    "account_uuid": "account-b",
                    "workspace_uuid": "workspace-old",
                    "present": True,
                    "missing_scans": 0,
                    "mtime_ns": 20,
                },
            },
        }
        state = {
            "roots": {
                "legacy-windows": {
                    "sessions": {
                        moved_session: active,
                        deleted_session: active,
                    }
                },
                "legacy-wsl": {
                    "sessions": {
                        moved_session: moved_tombstone,
                        deleted_session: hard_tombstone,
                    }
                },
            }
        }

        with (
            patch.object(app, "desktop_sync_root_key", return_value="canonical"),
            patch.object(
                app,
                "desktop_legacy_sync_root_keys",
                return_value=["legacy-windows", "legacy-wsl"],
            ),
        ):
            migrated = app.desktop_sync_root_state(state, Path("unused"))

        self.assertEqual(set(state["roots"]), {"canonical"})
        self.assertEqual(migrated["sessions"][moved_session]["state"], app.DESKTOP_STATE_ACTIVE)
        self.assertEqual(migrated["sessions"][moved_session]["replicas"], {})
        self.assertEqual(migrated["sessions"][deleted_session]["state"], app.DESKTOP_STATE_DELETED)

    def test_stale_unrelated_root_tombstone_is_not_globally_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, app_root, _, _ = self._claude_fixture(root)
            session_id = "30303030-3030-4030-8030-303030303030"
            app.save_desktop_sync_state(
                paths,
                {
                    "version": app.DESKTOP_SYNC_STATE_VERSION,
                    "roots": {
                        app.desktop_sync_root_key(app_root): {
                            "sessions": {session_id: {"state": app.DESKTOP_STATE_ACTIVE}}
                        },
                        "unrelated-stale-root": {
                            "sessions": {session_id: {"state": app.DESKTOP_STATE_DELETED}}
                        },
                    },
                },
            )

            self.assertNotIn(session_id, app.desktop_tombstoned_session_ids(paths))

    def test_claude_archive_propagates_from_account_a_to_b(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _, account_a, account_b = self._claude_fixture(root)
            session_id = "11111111-1111-4111-8111-111111111111"
            source = self._write_claude_session(account_a / "local_shared.json", session_id, root)

            first = app.sync_claude_desktop_accounts(paths)
            replica = account_b / "local_shared.json"
            self.assertEqual(first["created"], 1)
            self.assertFalse(self._read(replica)["isArchived"])

            self._set_archived(source, True)
            result = app.sync_claude_desktop_accounts(paths)

            self.assertEqual(result["archived"], 1)
            self.assertTrue(self._read(source)["isArchived"])
            self.assertTrue(self._read(replica)["isArchived"])

    def test_claude_archive_propagates_from_account_b_to_a(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, app_root, account_a, account_b = self._claude_fixture(root)
            session_id = "12111111-1111-4111-8111-121111111111"
            source = self._write_claude_session(account_a / "local_shared.json", session_id, root)
            app.sync_claude_desktop_accounts(paths)
            replica = account_b / "local_shared.json"

            (app_root / "config.json").write_text(
                json.dumps({"lastKnownAccountUuid": "account-b"}),
                encoding="utf-8",
            )
            self._set_archived(replica, True)
            result = app.sync_claude_desktop_accounts(paths)

            self.assertEqual(result["archived"], 1)
            self.assertTrue(self._read(source)["isArchived"])
            self.assertTrue(self._read(replica)["isArchived"])

    def test_claude_unarchive_propagates_from_account_b_to_a(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, app_root, account_a, account_b = self._claude_fixture(root)
            session_id = "22222222-2222-4222-8222-222222222222"
            source = self._write_claude_session(account_a / "local_shared.json", session_id, root)
            app.sync_claude_desktop_accounts(paths)
            replica = account_b / "local_shared.json"

            self._set_archived(source, True)
            app.sync_claude_desktop_accounts(paths)
            (app_root / "config.json").write_text(
                json.dumps({"lastKnownAccountUuid": "account-b"}),
                encoding="utf-8",
            )
            self._set_archived(replica, False)
            result = app.sync_claude_desktop_accounts(paths)

            self.assertEqual(result["unarchived"], 1)
            self.assertFalse(self._read(source)["isArchived"])
            self.assertFalse(self._read(replica)["isArchived"])

    def test_claude_delete_requires_two_scans_and_transcript_cannot_resurrect_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _, account_a, account_b = self._claude_fixture(root)
            session_id = "33333333-3333-4333-8333-333333333333"
            source = self._write_claude_session(account_a / "local_shared.json", session_id, root)
            transcript = self._write_transcript(paths, session_id, root)
            app.sync_claude_desktop_accounts(paths)
            replica = account_b / "local_shared.json"

            source.unlink()
            first_missing = app.sync_claude_desktop_accounts(paths)

            self.assertEqual(first_missing["pending_deletions"], 1)
            self.assertFalse(source.exists())
            self.assertTrue(replica.exists())

            confirmed = app.sync_claude_desktop_accounts(paths)

            self.assertEqual(confirmed["deleted"], 1)
            self.assertEqual(confirmed["removed"], 1)
            self.assertFalse(source.exists())
            self.assertFalse(replica.exists())
            self.assertTrue(transcript.exists())

            after_tombstone = app.sync_claude_desktop_accounts(paths)

            self.assertEqual(after_tombstone["created"], 0)
            self.assertEqual(after_tombstone["transcripts_created"], 0)
            self.assertFalse(list(account_a.glob("*.json")))
            self.assertFalse(list(account_b.glob("*.json")))
            state = app.load_desktop_sync_state(paths)
            session_entries = [
                root_state["sessions"][session_id]
                for root_state in state["roots"].values()
                if session_id in root_state.get("sessions", {})
            ]
            self.assertEqual(len(session_entries), 1)
            self.assertEqual(session_entries[0]["state"], app.DESKTOP_STATE_DELETED)
            visible_ids = {
                chat.session_id
                for chat in app.discover_claude_chats(paths, sync_desktop_accounts=False)
            }
            self.assertNotIn(session_id, visible_ids)

    def test_claude_workspace_relocation_is_not_treated_as_chat_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _, account_a, _ = self._claude_fixture(root)
            session_id = "31313131-3131-4131-8131-313131313131"
            source = self._write_claude_session(account_a / "local_shared.json", session_id, root)
            self._write_transcript(paths, session_id, root)
            app.sync_claude_desktop_accounts(paths)
            relocated_dir = account_a.parent / "workspace-new"
            relocated_dir.mkdir()
            relocated = relocated_dir / source.name
            source.rename(relocated)

            first = app.sync_claude_desktop_accounts(paths)
            second = app.sync_claude_desktop_accounts(paths)

            self.assertEqual(first["pending_deletions"], 0)
            self.assertEqual(second["deleted"], 0)
            records = [
                record
                for record in app.desktop_session_records(paths)
                if app.desktop_record_cli_session_id(record) == session_id
            ]
            self.assertEqual({record.account_uuid for record in records}, {"account-a", "account-b"})
            self.assertNotIn(session_id, app.desktop_tombstoned_session_ids(paths))

    def test_claude_corrupt_journal_blocks_sync_without_recreating_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _, account_a, account_b = self._claude_fixture(root)
            session_id = "32323232-3333-4333-8333-323232323232"
            self._write_transcript(paths, session_id, root)
            journal = app.desktop_sync_state_path(paths)
            journal.parent.mkdir(parents=True, exist_ok=True)
            corrupt = '{"version": 1, "roots": '
            journal.write_text(corrupt, encoding="utf-8")

            with self.assertRaises(app.StateFileError):
                app.sync_claude_desktop_accounts(paths)

            self.assertEqual(journal.read_text(encoding="utf-8"), corrupt)
            self.assertFalse(list(account_a.glob("*.json")))
            self.assertFalse(list(account_b.glob("*.json")))

    def test_claude_uncertain_scan_resets_missing_confirmation_counter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _, account_a, account_b = self._claude_fixture(root)
            session_id = "33333333-3434-4343-8343-333333333333"
            source = self._write_claude_session(account_a / "local_shared.json", session_id, root)
            app.sync_claude_desktop_accounts(paths)
            replica = account_b / "local_shared.json"

            source.unlink()
            first_missing = app.sync_claude_desktop_accounts(paths)
            self.assertEqual(first_missing["pending_deletions"], 1)

            source.write_text("{corrupt", encoding="utf-8")
            uncertain = app.sync_claude_desktop_accounts(paths)
            self.assertEqual(uncertain["deleted"], 0)
            self.assertTrue(replica.exists())
            state = app.load_desktop_sync_state(paths)
            entry = next(
                root_state["sessions"][session_id]
                for root_state in state["roots"].values()
                if session_id in root_state.get("sessions", {})
            )
            source_snapshot = entry["replicas"]["account-a/workspace"]
            self.assertEqual(source_snapshot["missing_scans"], 0)
            self.assertTrue(source_snapshot["scan_blocked"])

            source.unlink()
            restarted = app.sync_claude_desktop_accounts(paths)
            self.assertEqual(restarted["pending_deletions"], 1)
            self.assertEqual(restarted["deleted"], 0)
            self.assertTrue(replica.exists())

            confirmed = app.sync_claude_desktop_accounts(paths)
            self.assertEqual(confirmed["deleted"], 1)
            self.assertFalse(replica.exists())

    def test_claude_delete_from_non_active_account_still_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _, account_a, account_b = self._claude_fixture(root)
            session_id = "34343434-3333-4333-8333-343434343434"
            source = self._write_claude_session(account_a / "local_shared.json", session_id, root)
            app.sync_claude_desktop_accounts(paths)
            replica = account_b / "local_shared.json"

            replica.unlink()
            app.sync_claude_desktop_accounts(paths)
            confirmed = app.sync_claude_desktop_accounts(paths)

            self.assertEqual(confirmed["deleted"], 1)
            self.assertFalse(source.exists())
            self.assertFalse(replica.exists())

    def test_claude_move_suppresses_source_replica_instead_of_resurrecting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _, account_a, account_b = self._claude_fixture(root)
            session_id = "44444444-4444-4444-8444-444444444444"
            source = self._write_claude_session(account_b / "local_source.json", session_id, root)
            chat = app.discover_claude_windows_app_sessions(paths, sync_accounts=False)[0]

            moved = app.transfer_chat_to_active_desktop_account(paths, chat, move=True)
            result = app.sync_claude_desktop_accounts(paths)

            self.assertEqual(moved["status"], "moved")
            self.assertFalse(source.exists())
            self.assertTrue(Path(moved["destination"]).exists())
            self.assertEqual(result["created"], 0)
            self.assertFalse(
                any(
                    self._read(candidate).get("cliSessionId") == session_id
                    for candidate in account_b.glob("*.json")
                )
            )
            self.assertTrue(
                any(
                    self._read(candidate).get("cliSessionId") == session_id
                    for candidate in account_a.glob("*.json")
                )
            )

    def test_claude_sync_does_not_create_account_workspace_cross_product(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, app_root, account_a, account_b = self._claude_fixture(root)
            account_b.rename(account_b.parent / "workspace-b")
            account_b = account_b.parent / "workspace-b"
            first_id = "45454545-4444-4444-8444-454545454545"
            second_id = "46464646-4444-4444-8444-464646464646"
            self._write_claude_session(account_a / "local_first.json", first_id, root)
            self._write_claude_session(account_b / "local_second.json", second_id, root)

            app.sync_claude_desktop_accounts(paths)

            sessions_root = app_root / "claude-code-sessions"
            records = app.desktop_session_records(paths)
            by_session: dict[str, list[app.DesktopSessionRecord]] = {}
            for record in records:
                session_id = app.desktop_record_cli_session_id(record)
                if session_id:
                    by_session.setdefault(session_id, []).append(record)
            self.assertEqual(len(by_session[first_id]), 2)
            self.assertEqual(len(by_session[second_id]), 2)
            self.assertEqual({record.account_uuid for record in by_session[first_id]}, {"account-a", "account-b"})
            self.assertEqual({record.account_uuid for record in by_session[second_id]}, {"account-a", "account-b"})
            self.assertFalse((sessions_root / "account-a" / "workspace-b").exists())
            self.assertFalse((sessions_root / "account-b" / "workspace").exists())

    def test_claude_account_switch_uses_log_before_config_or_first_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_account = "11111111-1111-4111-8111-111111111111"
            old_organization = "22222222-2222-4222-8222-222222222222"
            new_account = "33333333-3333-4333-8333-333333333333"
            new_organization = "44444444-4444-4444-8444-444444444444"
            paths, app_root, old_dir, new_dir = self._claude_fixture(
                root,
                active_account=old_account,
                other_account=new_account,
                workspace=old_organization,
            )
            new_dir.rmdir()
            new_dir.parent.rmdir()
            session_id = "55555555-5555-4555-8555-555555555555"
            self._write_claude_session(old_dir / "local_shared.json", session_id, root)
            logs = app_root / "logs"
            logs.mkdir()
            main_log = logs / "main.log"
            main_log.write_text(
                "2026-07-17 12:52:38 [sessions-bridge] account-change reevaluate: "
                f"{old_organization}:{old_account} -> <none>\n"
                "2026-07-17 12:52:39 [sessions-bridge] account-change reevaluate: "
                f"<none> -> {new_organization}:{new_account}\n",
                encoding="utf-8",
            )

            context = app.active_desktop_account_context(app_root)
            signature_before_logout = app.claude_desktop_change_signature(paths)
            result = app.sync_claude_desktop_accounts(paths, include_transcripts=False)

            self.assertEqual(context.account_uuid, new_account)
            self.assertEqual(context.organization_uuid, new_organization)
            self.assertEqual(app.active_desktop_account_uuid(app_root), new_account)
            self.assertEqual(app.active_desktop_workspace_uuid(app_root, new_account), new_organization)
            self.assertEqual(result["created"], 1)
            self.assertTrue(
                (app_root / "claude-code-sessions" / new_account / new_organization / "local_shared.json").exists()
            )

            with main_log.open("a", encoding="utf-8") as handle:
                handle.write(
                    "2026-07-17 12:53:00 [sessions-bridge] account-change reevaluate: "
                    f"{new_organization}:{new_account} -> <none>\n"
                )

            self.assertIsNone(app.active_desktop_account_uuid(app_root))
            self.assertNotEqual(signature_before_logout, app.claude_desktop_change_signature(paths))

    def test_claude_runtime_evidence_supersedes_an_old_logout_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = "11111111-1111-4111-8111-111111111111"
            organization = "22222222-2222-4222-8222-222222222222"
            _, app_root, _, _ = self._claude_fixture(root, active_account=account)
            logs = app_root / "logs"
            logs.mkdir()
            (logs / "main.log").write_text(
                "2026-07-17 12:52:38 [sessions-bridge] account-change reevaluate: "
                f"{organization}:{account} -> <none>\n"
                "2026-07-17 19:55:09 [info] [CCD] Using skills plugin at: "
                f"C:\\Claude\\local-agent-mode-sessions\\skills-plugin\\{organization}\\{account}\n",
                encoding="utf-8",
            )

            context = app.active_desktop_account_context(app_root)

            self.assertFalse(context.logged_out)
            self.assertEqual(context.account_uuid, account)
            self.assertEqual(context.organization_uuid, organization)

    def test_claude_oauth_session_cache_refreshes_when_credentials_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, app_root, _, _ = self._claude_fixture(root)
            profile = {
                "account_uuid": "account-a",
                "organization_uuid": "organization-a",
                "email": "account-a@example.com",
            }
            tokens = [{"token": "token-a", "cacheKey": "cache-a"}]

            with (
                patch.object(app, "claude_local_oauth_tokens", return_value=tokens) as token_reader,
                patch.object(app, "claude_oauth_profile", return_value=profile) as profile_reader,
            ):
                first = app.claude_oauth_sessions(paths)
                second = app.claude_oauth_sessions(paths)
                config = app_root / "config.json"
                previous_mtime = config.stat().st_mtime_ns
                config.write_text('{"lastKnownAccountUuid":"account-a","changed":true}', encoding="utf-8")
                changed_mtime = max(time.time_ns(), previous_mtime + 10_000_000)
                os.utime(config, ns=(changed_mtime, changed_mtime))
                third = app.claude_oauth_sessions(paths)

            self.assertEqual(first, second)
            self.assertEqual(second, third)
            self.assertEqual(token_reader.call_count, 2)
            self.assertEqual(profile_reader.call_count, 2)

    def test_claude_desktop_oauth_cache_is_read_through_hidden_powershell_in_wsl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, app_root, _, _ = self._claude_fixture(root)
            (app_root / "Local State").write_text("{}", encoding="utf-8")
            completed = app.subprocess.CompletedProcess(
                args=["powershell.exe"],
                returncode=0,
                stdout='[{"token":"test-token","cacheKey":"test-cache"}]',
                stderr="",
            )

            with (
                patch.object(app, "is_wsl", return_value=True),
                patch.object(app, "local_to_windows_path", return_value=r"C:\Claude") as convert,
                patch.object(app.shutil, "which", side_effect=lambda name: "pwsh.exe" if name == "pwsh.exe" else None),
                patch.object(app, "local_powershell_hidden_command", return_value=["pwsh.exe"]) as hidden,
                patch.object(app.subprocess, "run", return_value=completed) as run,
            ):
                tokens = app.claude_desktop_cached_oauth_tokens(app_root)

            self.assertEqual(tokens, [{"token": "test-token", "cacheKey": "test-cache"}])
            convert.assert_called_once_with(app_root)
            hidden.assert_called_once()
            self.assertEqual(hidden.call_args.kwargs["powershell"], "pwsh.exe")
            self.assertIn(r"$root = 'C:\Claude'", hidden.call_args.args[0])
            self.assertEqual(run.call_args.args[0], ["pwsh.exe"])

    def test_claude_code_artifact_gets_a_private_account_copy_and_transcript_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _, account_a_dir, account_b_dir = self._claude_fixture(root)
            session_id = "99999999-9999-4999-8999-999999999999"
            artifact_slug = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            target_slug = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            source_file = root / "artifact.html"
            source_file.write_text("<main>private artifact</main>", encoding="utf-8")
            source_transcript = self._write_artifact_transcript(
                paths,
                session_id,
                root,
                source_file,
                artifact_slug,
            )
            source_metadata = self._write_claude_session(
                account_a_dir / "local_source.json",
                session_id,
                root,
            )
            target_metadata = self._write_claude_session(
                account_b_dir / "local_target.json",
                session_id,
                root,
            )
            oauth_sessions = {
                "account-a": {
                    "account_uuid": "account-a",
                    "organization_uuid": "org-a",
                    "email": "a@example.test",
                    "token": "token-a",
                },
                "account-b": {
                    "account_uuid": "account-b",
                    "organization_uuid": "org-b",
                    "email": "b@example.test",
                    "token": "token-b",
                },
            }

            def frame_rows(token: str) -> list[dict[str, object]]:
                if token == "token-a":
                    return [
                        {
                            "slug": artifact_slug,
                            "owner_account": "account-a",
                            "rel": "mine",
                        }
                    ]
                return []

            with (
                patch.object(app, "claude_oauth_sessions", return_value=oauth_sessions),
                patch.object(app, "claude_frame_rows", side_effect=frame_rows),
                patch.object(
                    app,
                    "deploy_claude_frame_copy",
                    return_value={"slug": target_slug, "version": "1"},
                ) as deploy,
            ):
                result = app.sync_claude_desktop_accounts(paths)

            state = app.load_desktop_sync_state(paths)
            root_state = next(iter(state["roots"].values()))
            replica_id = root_state["code_artifact_session_replicas"][session_id]["account-b"]
            replica_transcript = source_transcript.with_name(f"{replica_id}.jsonl")
            replica_text = replica_transcript.read_text(encoding="utf-8")
            cache_path = paths.state_dir / "claude-code-artifacts" / artifact_slug / "index.html"

            self.assertEqual(result["code_artifact_copies_created"], 1)
            self.assertEqual(result["code_artifact_pending_accounts"], 0)
            self.assertEqual(result["code_artifact_errors"], [])
            self.assertTrue(replica_transcript.exists())
            self.assertIn(f"https://claude.ai/code/artifact/{target_slug}", replica_text)
            self.assertNotIn(f"https://claude.ai/code/artifact/{artifact_slug}", replica_text)
            self.assertIn(f'"sessionId":"{replica_id}"', replica_text)
            self.assertEqual(
                root_state["code_artifact_aliases"][replica_id]["transcript_path"],
                app.canonical_windows_path(replica_transcript),
            )
            self.assertEqual(
                root_state["code_artifacts"][artifact_slug]["cache_path"],
                app.canonical_windows_path(cache_path),
            )
            self.assertEqual(self._read(source_metadata)["cliSessionId"], session_id)
            self.assertEqual(self._read(target_metadata)["cliSessionId"], replica_id)
            self.assertIn(f"https://claude.ai/code/artifact/{artifact_slug}", source_transcript.read_text())
            self.assertEqual(
                [chat.session_id for chat in app.discover_chats(paths.claude_home, {replica_id})],
                [session_id],
            )
            deploy.assert_called_once()

    def test_artifact_transcript_replica_without_link_timestamp_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jsonl"
            destination = root / "replica.jsonl"
            source.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "source-session",
                        "timestamp": "2026-07-17T20:15:00Z",
                        "message": {"role": "user", "content": "continue"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            links = [
                {
                    "frame_url": "https://claude.ai/code/artifact/copied",
                    "title": "Copy",
                    "path": "/mnt/c/Users/Example/.claude-vscode-queue/artifact/index.html",
                }
            ]

            first = app.write_claude_artifact_transcript_replica(
                source,
                destination,
                "source-session",
                "replica-session",
                {},
                links,
            )
            links[0]["path"] = r"C:\Users\Example\.claude-vscode-queue\artifact\index.html"
            second = app.write_claude_artifact_transcript_replica(
                source,
                destination,
                "source-session",
                "replica-session",
                {},
                links,
            )

            self.assertTrue(first)
            self.assertFalse(second)
            self.assertIn('"timestamp":"2026-07-17T20:15:00Z"', destination.read_text(encoding="utf-8"))

    def test_claude_artifacts_sync_session_links_and_propagate_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, app_root, account_a, account_b = self._claude_fixture(root)
            session_id = "66666666-6666-4666-8666-666666666666"
            self._write_claude_session(account_a / "local_source.json", session_id, root)
            self._write_claude_session(account_b / "local_target.json", session_id, root)
            files_root = root / "Claude files"
            (app_root / "claude_desktop_config.json").write_text(
                json.dumps({"coworkUserFilesPath": str(files_root)}),
                encoding="utf-8",
            )
            artifact_id = "dashboard-1"
            artifact_file = files_root / "Artifacts" / artifact_id / "index.html"
            artifact_file.parent.mkdir(parents=True)
            artifact_file.write_text("<html>shared</html>", encoding="utf-8")
            source_manifest = app.desktop_artifact_manifest_path(app_root, "account-a", "workspace")
            target_manifest = app.desktop_artifact_manifest_path(app_root, "account-b", "workspace")
            source_entry = {
                "id": artifact_id,
                "name": "Dashboard",
                "createdAt": 1_800_000_000_000,
                "updatedAt": 1_800_000_001_000,
                "createdBySessionId": "local_source",
                "lastModifiedBySessionId": "local_source",
                "versions": [],
                "mcpTools": [{"server": "private-source-connector"}],
                "sharedArtifactUuid": "source-only-share",
                "autoPublish": True,
            }
            app.write_desktop_artifact_manifest(source_manifest, [source_entry])
            app.write_desktop_artifact_manifest(target_manifest, [])

            first = app.sync_claude_desktop_accounts(paths, include_transcripts=False)
            target_entry = app.load_desktop_artifact_manifest(target_manifest)[0]

            self.assertEqual(first["artifacts_created"], 1)
            self.assertEqual(target_entry["createdBySessionId"], "local_target")
            self.assertEqual(target_entry["lastModifiedBySessionId"], "local_target")
            self.assertNotIn("mcpTools", target_entry)
            self.assertNotIn("sharedArtifactUuid", target_entry)
            self.assertNotIn("autoPublish", target_entry)
            self.assertEqual(
                app.load_desktop_artifact_manifest(source_manifest)[0]["mcpTools"],
                source_entry["mcpTools"],
            )

            app.write_desktop_artifact_manifest(source_manifest, [])
            pending = app.sync_claude_desktop_accounts(paths, include_transcripts=False)
            self.assertEqual(pending["pending_artifact_deletions"], 1)
            self.assertEqual(app.load_desktop_artifact_manifest(source_manifest), [])
            self.assertEqual(len(app.load_desktop_artifact_manifest(target_manifest)), 1)

            confirmed = app.sync_claude_desktop_accounts(paths, include_transcripts=False)
            self.assertEqual(confirmed["artifacts_deleted"], 1)
            self.assertEqual(confirmed["artifacts_removed"], 1)
            self.assertEqual(app.load_desktop_artifact_manifest(target_manifest), [])
            self.assertTrue(artifact_file.exists())

    def test_discovery_and_sync_do_not_require_claude_codex_or_vscode_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _, account_a, account_b = self._claude_fixture(root)
            claude_session = "77777777-7777-4777-8777-777777777777"
            codex_session = "88888888-8888-4888-8888-888888888888"
            self._write_claude_session(account_a / "local_shared.json", claude_session, root)
            self._codex_rollout(paths.codex_home, codex_session)
            state = web.WebState(paths, None, None)

            external_process_error = AssertionError("external app process access is not allowed")
            with (
                patch.object(app.subprocess, "run", side_effect=external_process_error),
                patch.object(app.subprocess, "Popen", side_effect=external_process_error),
            ):
                sync_result = app.sync_claude_desktop_accounts(paths, include_transcripts=False)
                chats = state.quick_chats()
                codex_chats = app.discover_codex_app_sessions(paths)

            self.assertEqual(sync_result["created"], 1)
            self.assertTrue(any(chat.session_id == claude_session for chat in chats))
            self.assertTrue(any(chat.session_id == codex_session for chat in codex_chats))
            self.assertTrue(any(account_b.glob("*.json")))

    def test_web_chat_cache_keeps_complete_list_and_adds_new_account_chats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            state = web.WebState(paths, None, None)
            old_chat = app.Chat(
                session_id="old-session",
                title="Old",
                cwd=str(paths.windows_home),
                permission_mode=None,
                model="opus",
                jsonl_path=paths.claude_home / "old-session.jsonl",
                last_timestamp="2026-07-17T10:00:00Z",
                message_count=10,
                last_prompt="old prompt",
                last_user_message="old preview",
            )
            new_chat = app.Chat(
                session_id="new-session",
                title="New",
                cwd=str(paths.windows_home),
                permission_mode=None,
                model="opus",
                jsonl_path=paths.claude_home / "new-session.jsonl",
                last_timestamp="2026-07-17T11:00:00Z",
                message_count=1,
                last_prompt=None,
                source="Claude Windows App",
                source_key="claude_windows_app",
            )
            state._chats_cache = [old_chat]
            state._chats_cache_at = time.monotonic()

            with (
                patch.object(app, "claude_desktop_change_signature", return_value=("new-account",)),
                patch.object(app, "desktop_tombstoned_session_ids", return_value=set()),
                patch.object(state, "quick_chats", return_value=[new_chat]),
                patch.object(state, "refresh_chats_background") as refresh,
            ):
                chats = state.chats()

            self.assertEqual([chat.session_id for chat in chats], ["new-session", "old-session"])
            self.assertEqual(chats[1].last_user_message, "old preview")
            refresh.assert_called_once()

    def test_web_chat_cache_removes_confirmed_tombstones_without_dropping_other_chats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            state = web.WebState(paths, None, None)
            chats = [
                app.Chat(
                    session_id=session_id,
                    title=session_id,
                    cwd=str(paths.windows_home),
                    permission_mode=None,
                    model=None,
                    jsonl_path=paths.claude_home / f"{session_id}.jsonl",
                    last_timestamp=f"2026-07-17T1{index}:00:00Z",
                    message_count=1,
                    last_prompt=session_id,
                )
                for index, session_id in enumerate(("deleted", "retained"))
            ]
            state._chats_cache = chats
            state._chats_desktop_signature = ("stable",)

            with (
                patch.object(app, "claude_desktop_change_signature", return_value=("stable",)),
                patch.object(app, "desktop_tombstoned_session_ids", return_value={"deleted"}),
                patch.object(state, "quick_chats", return_value=[]),
                patch.object(state, "refresh_chats_background"),
            ):
                state.invalidate_chats()
                merged = state.chats()

            self.assertEqual([chat.session_id for chat in merged], ["retained"])

    def test_account_identity_is_stable_when_email_claim_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            paths.claude_home.mkdir(parents=True)

            def write_claude(email: str) -> None:
                (root / ".claude.json").write_text(
                    json.dumps({"oauthAccount": {"accountUuid": "stable-claude", "emailAddress": email}}),
                    encoding="utf-8",
                )

            def write_codex(email: str) -> None:
                paths.codex_home.mkdir(exist_ok=True)
                payload = base64.urlsafe_b64encode(
                    json.dumps({"email": email, "sub": "stable-subject"}).encode()
                ).rstrip(b"=").decode()
                (paths.codex_home / "auth.json").write_text(
                    json.dumps({"tokens": {"account_id": "stable-account", "id_token": f"x.{payload}.x"}}),
                    encoding="utf-8",
                )

            write_claude("first@example.com")
            write_codex("first@example.com")
            first_claude = app.active_claude_account(paths)
            first_codex = app.active_codex_account(paths)
            write_claude("second@example.com")
            write_codex("second@example.com")

            self.assertEqual(first_claude.key, app.active_claude_account(paths).key)
            self.assertEqual(first_codex.key, app.active_codex_account(paths).key)

    def test_codex_discovery_uses_store_union_but_drops_ghosts_and_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            codex_home = paths.codex_home
            codex_home.mkdir()
            indexed_id = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
            database_only_id = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
            rollout_only_id = "cccccccc-3333-4333-8333-cccccccccccc"
            ghost_id = "dddddddd-4444-4444-8444-dddddddddddd"
            subagent_id = "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"
            indexed_rollout = self._codex_rollout(codex_home, indexed_id)
            database_rollout = self._codex_rollout(codex_home, database_only_id)
            self._codex_rollout(codex_home, rollout_only_id, archived=True)
            subagent_rollout = self._codex_rollout(codex_home, subagent_id)
            (codex_home / "session_index.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": indexed_id,
                                "thread_name": "Indexed task",
                                "updated_at": "2026-07-12T10:00:00Z",
                            }
                        ),
                        json.dumps(
                            {
                                "id": ghost_id,
                                "thread_name": "Deleted ghost",
                                "updated_at": "2026-07-12T10:00:00Z",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(codex_home / "state_5.sqlite")
            try:
                connection.execute(
                    "CREATE TABLE threads ("
                    "id TEXT, rollout_path TEXT, cwd TEXT, title TEXT, first_user_message TEXT, "
                    "source TEXT, thread_source TEXT, archived INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            indexed_id,
                            str(indexed_rollout),
                            str(root),
                            "Database indexed title",
                            "indexed prompt",
                            "cli",
                            "cli",
                            0,
                        ),
                        (
                            database_only_id,
                            str(database_rollout),
                            str(root),
                            "Database-only task",
                            "database prompt",
                            "cli",
                            "cli",
                            0,
                        ),
                        (
                            subagent_id,
                            str(subagent_rollout),
                            str(root),
                            "Internal subagent",
                            "subagent prompt",
                            "subagent",
                            "subagent",
                            0,
                        ),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            with patch.object(app, "find_codex_executable", return_value=None):
                chats = app.discover_codex_app_sessions(paths)

            by_id = {chat.session_id: chat for chat in chats}
            self.assertEqual(set(by_id), {indexed_id, database_only_id, rollout_only_id})
            self.assertEqual(by_id[indexed_id].title, "Indexed task")
            self.assertEqual(by_id[database_only_id].title, "Database-only task")
            self.assertTrue(by_id[rollout_only_id].archived)
            self.assertNotIn(ghost_id, by_id)
            self.assertNotIn(subagent_id, by_id)

    def test_codex_transfer_forks_with_app_server_and_records_link_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            source_id = "ffffffff-6666-4666-8666-ffffffffffff"
            destination_id = "abababab-7777-4777-8777-abababababab"
            source_rollout = root / "source.jsonl"
            source_rollout.write_text("{}\n", encoding="utf-8")
            destination_rollout = root / "destination.jsonl"
            destination_rollout.write_text("{}\n", encoding="utf-8")
            chat = app.Chat(
                session_id=source_id,
                title="Transfer this task",
                cwd=str(root),
                permission_mode="workspace-write",
                model="gpt-test-codex",
                jsonl_path=source_rollout,
                last_timestamp="2026-07-12T10:00:00Z",
                message_count=-1,
                last_prompt="test transfer",
                source="Codex App",
                source_key="codex_app",
                provider=app.PROVIDER_CODEX,
                account_key="codex:source",
                account_label="Source account",
                account_status="other",
            )
            active = app.AccountInfo(
                key="codex:destination",
                label="Destination account",
                account_uuid_hash="destination",
                organization_uuid_hash=None,
                email_hash=None,
                source_changed_at=None,
            )

            with (
                patch.object(app, "active_codex_account", side_effect=lambda _: active),
                patch.object(app, "find_codex_executable", return_value=root / "codex"),
                patch.object(
                    app,
                    "codex_app_server_request",
                    return_value={"thread": {"id": destination_id}},
                ) as app_server,
                patch.object(
                    app,
                    "codex_local_thread_states",
                    return_value={
                        source_id: app.DESKTOP_STATE_ACTIVE,
                        destination_id: app.DESKTOP_STATE_ACTIVE,
                    },
                ),
                patch.object(
                    app,
                    "codex_thread_rows",
                    return_value={destination_id: {"rollout_path": str(destination_rollout)}},
                ),
            ):
                result = app.transfer_codex_chat_to_active_account(paths, chat)

            self.assertEqual(result["status"], "forked")
            self.assertEqual(result["session_id"], destination_id)
            app_server.assert_called_once_with(
                root / "codex",
                "thread/fork",
                {"threadId": source_id, "excludeTurns": True},
                codex_home=paths.codex_home,
            )
            index = app.load_account_index(paths)
            self.assertEqual(index["sessions"][f"codex:local:{source_id}"]["account_key"], "codex:source")
            self.assertEqual(
                index["sessions"][f"codex:local:{destination_id}"]["account_key"],
                "codex:destination",
            )
            self.assertEqual(index["sessions"][f"codex:local:{destination_id}"]["forked_from"], source_id)
            self.assertEqual(len(index["codex_links"]), 1)
            link = next(iter(index["codex_links"].values()))
            self.assertEqual(link["state"], app.DESKTOP_STATE_ACTIVE)
            self.assertEqual(set(link["threads"]), {source_id, destination_id})
            self.assertEqual(link["threads"][destination_id]["forked_from"], source_id)

    def test_codex_transfer_reuses_existing_copy_for_active_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            source_id = "fafafafa-6666-4666-8666-fafafafafafa"
            destination_id = "acacacac-7777-4777-8777-acacacacacac"
            source_rollout = root / "source.jsonl"
            source_rollout.write_text("{}\n", encoding="utf-8")
            chat = app.Chat(
                session_id=source_id,
                title="Already transferred task",
                cwd=str(root),
                permission_mode="workspace-write",
                model="gpt-test-codex",
                jsonl_path=source_rollout,
                last_timestamp="2026-07-12T10:00:00Z",
                message_count=-1,
                last_prompt="test transfer reuse",
                source="Codex App",
                source_key="codex_app",
                provider=app.PROVIDER_CODEX,
                account_key="codex:source",
                account_label="Source account",
                account_status="other",
            )
            active = app.AccountInfo(
                key="codex:destination",
                label="Destination account",
                account_uuid_hash="destination",
                organization_uuid_hash=None,
                email_hash=None,
                source_changed_at=None,
            )
            app.save_account_index(paths, self._linked_index(source_id, destination_id))

            with (
                patch.object(app, "active_codex_account", side_effect=lambda _: active),
                patch.object(app, "find_codex_executable", return_value=root / "codex"),
                patch.object(
                    app,
                    "codex_local_thread_states",
                    return_value={
                        source_id: app.DESKTOP_STATE_ACTIVE,
                        destination_id: app.DESKTOP_STATE_ACTIVE,
                    },
                ),
                patch.object(app, "codex_thread_rows", return_value={}),
                patch.object(app, "codex_app_server_request") as app_server,
            ):
                result = app.transfer_codex_chat_to_active_account(paths, chat)

            self.assertEqual(result["status"], "already_copied")
            self.assertEqual(result["session_id"], destination_id)
            app_server.assert_not_called()

    def test_codex_linked_archive_uses_cli_for_the_other_account_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            source_id = "12121212-8888-4888-8888-121212121212"
            destination_id = "34343434-9999-4999-8999-343434343434"
            app.save_account_index(paths, self._linked_index(source_id, destination_id))
            states = {
                source_id: app.DESKTOP_STATE_ARCHIVED,
                destination_id: app.DESKTOP_STATE_ACTIVE,
            }
            commands: list[list[str]] = []

            def fake_cli(
                _: Path,
                arguments: list[str],
                timeout: int = 15,
                *,
                codex_home: Path | None = None,
            ) -> app.subprocess.CompletedProcess[str]:
                self.assertEqual(codex_home, paths.codex_home)
                commands.append(arguments)
                if arguments[0] == "archive":
                    states[arguments[-1]] = app.DESKTOP_STATE_ARCHIVED
                elif arguments[0] == "unarchive":
                    states[arguments[-1]] = app.DESKTOP_STATE_ACTIVE
                return app.subprocess.CompletedProcess(arguments, 0, "", "")

            with (
                patch.object(app, "find_codex_executable", return_value=root / "codex"),
                patch.object(app, "codex_local_thread_states", side_effect=lambda _: dict(states)),
                patch.object(app, "run_codex_cli_command", side_effect=fake_cli),
                patch.object(
                    app,
                    "active_codex_account",
                    return_value=app.AccountInfo("codex:destination", "destination", None, None, None, None),
                ),
            ):
                result = app.sync_codex_linked_threads(paths)

            self.assertEqual(result["updated"], 1)
            self.assertFalse(result["errors"])
            self.assertEqual(commands, [["archive", destination_id]])
            group = app.load_account_index(paths)["codex_links"]["group-1"]
            self.assertEqual(group["state"], app.DESKTOP_STATE_ARCHIVED)
            self.assertEqual(group["threads"][source_id]["last_state"], app.DESKTOP_STATE_ARCHIVED)
            self.assertEqual(group["threads"][destination_id]["last_state"], app.DESKTOP_STATE_ARCHIVED)

    def test_codex_linked_lifecycle_waits_until_destination_account_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            source_id = "45454545-aaaa-4aaa-8aaa-454545454545"
            destination_id = "67676767-bbbb-4bbb-8bbb-676767676767"
            app.save_account_index(paths, self._linked_index(source_id, destination_id))
            states = {
                source_id: app.DESKTOP_STATE_ARCHIVED,
                destination_id: app.DESKTOP_STATE_ACTIVE,
            }
            commands: list[list[str]] = []
            active = app.AccountInfo("codex:source", "source", None, None, None, None)

            def fake_cli(_: Path, arguments: list[str], timeout: int = 15, **__: object) -> app.subprocess.CompletedProcess[str]:
                commands.append(arguments)
                states[arguments[-1]] = app.DESKTOP_STATE_ARCHIVED
                return app.subprocess.CompletedProcess(arguments, 0, "", "")

            with (
                patch.object(app, "find_codex_executable", return_value=root / "codex"),
                patch.object(app, "codex_local_thread_states", side_effect=lambda _: dict(states)),
                patch.object(app, "run_codex_cli_command", side_effect=fake_cli),
                patch.object(app, "active_codex_account", side_effect=lambda _: active),
            ):
                waiting = app.sync_codex_linked_threads(paths)
                commands_after_waiting = list(commands)
                active = app.dataclasses.replace(active, key="codex:destination", label="destination")
                applied = app.sync_codex_linked_threads(paths)

            self.assertGreaterEqual(waiting["pending"], 1)
            self.assertFalse(commands_after_waiting)
            self.assertEqual(applied["updated"], 1)
            self.assertEqual(commands, [["archive", destination_id]])

    def test_codex_linked_lifecycle_waits_when_active_account_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            source_id = "46464646-aaaa-4aaa-8aaa-464646464646"
            destination_id = "68686868-bbbb-4bbb-8bbb-686868686868"
            app.save_account_index(paths, self._linked_index(source_id, destination_id))
            states = {
                source_id: app.DESKTOP_STATE_ARCHIVED,
                destination_id: app.DESKTOP_STATE_ACTIVE,
            }

            with (
                patch.object(app, "find_codex_executable", return_value=root / "codex"),
                patch.object(app, "codex_local_thread_states", side_effect=lambda _: dict(states)),
                patch.object(app, "active_codex_account", return_value=None),
                patch.object(app, "run_codex_cli_command") as cli,
            ):
                waiting = app.sync_codex_linked_threads(paths)

            self.assertGreaterEqual(waiting["pending"], 1)
            cli.assert_not_called()
            group = app.load_account_index(paths)["codex_links"]["group-1"]
            self.assertEqual(group["state"], app.DESKTOP_STATE_ARCHIVED)
            self.assertEqual(
                group["threads"][destination_id]["pending_state"],
                app.DESKTOP_STATE_ARCHIVED,
            )

    def test_codex_linked_archive_propagates_from_destination_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            source_id = "14141414-8888-4888-8888-141414141414"
            destination_id = "36363636-9999-4999-8999-363636363636"
            app.save_account_index(paths, self._linked_index(source_id, destination_id))
            states = {
                source_id: app.DESKTOP_STATE_ACTIVE,
                destination_id: app.DESKTOP_STATE_ARCHIVED,
            }
            commands: list[list[str]] = []

            def fake_cli(
                _: Path,
                arguments: list[str],
                timeout: int = 15,
                *,
                codex_home: Path | None = None,
            ) -> app.subprocess.CompletedProcess[str]:
                self.assertEqual(codex_home, paths.codex_home)
                commands.append(arguments)
                states[arguments[-1]] = app.DESKTOP_STATE_ARCHIVED
                return app.subprocess.CompletedProcess(arguments, 0, "", "")

            with (
                patch.object(app, "find_codex_executable", return_value=root / "codex"),
                patch.object(app, "codex_local_thread_states", side_effect=lambda _: dict(states)),
                patch.object(app, "run_codex_cli_command", side_effect=fake_cli),
                patch.object(
                    app,
                    "active_codex_account",
                    return_value=app.AccountInfo("codex:source", "source", None, None, None, None),
                ),
            ):
                result = app.sync_codex_linked_threads(paths)

            self.assertEqual(result["updated"], 1)
            self.assertFalse(result["errors"])
            self.assertEqual(commands, [["archive", source_id]])
            group = app.load_account_index(paths)["codex_links"]["group-1"]
            self.assertEqual(group["state"], app.DESKTOP_STATE_ARCHIVED)
            self.assertEqual(group["threads"][source_id]["last_state"], app.DESKTOP_STATE_ARCHIVED)
            self.assertEqual(group["threads"][destination_id]["last_state"], app.DESKTOP_STATE_ARCHIVED)

    def test_codex_linked_unarchive_propagates_to_the_other_account_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            source_id = "16161616-8888-4888-8888-161616161616"
            destination_id = "38383838-9999-4999-8999-383838383838"
            index = self._linked_index(source_id, destination_id)
            group = index["codex_links"]["group-1"]
            group["state"] = app.DESKTOP_STATE_ARCHIVED
            group["threads"][source_id]["last_state"] = app.DESKTOP_STATE_ARCHIVED
            group["threads"][destination_id]["last_state"] = app.DESKTOP_STATE_ARCHIVED
            app.save_account_index(paths, index)
            states = {
                source_id: app.DESKTOP_STATE_ARCHIVED,
                destination_id: app.DESKTOP_STATE_ACTIVE,
            }
            commands: list[list[str]] = []

            def fake_cli(
                _: Path,
                arguments: list[str],
                timeout: int = 15,
                *,
                codex_home: Path | None = None,
            ) -> app.subprocess.CompletedProcess[str]:
                self.assertEqual(codex_home, paths.codex_home)
                commands.append(arguments)
                states[arguments[-1]] = app.DESKTOP_STATE_ACTIVE
                return app.subprocess.CompletedProcess(arguments, 0, "", "")

            with (
                patch.object(app, "find_codex_executable", return_value=root / "codex"),
                patch.object(app, "codex_local_thread_states", side_effect=lambda _: dict(states)),
                patch.object(app, "run_codex_cli_command", side_effect=fake_cli),
                patch.object(
                    app,
                    "active_codex_account",
                    return_value=app.AccountInfo("codex:source", "source", None, None, None, None),
                ),
            ):
                result = app.sync_codex_linked_threads(paths)

            self.assertEqual(result["updated"], 1)
            self.assertFalse(result["errors"])
            self.assertEqual(commands, [["unarchive", source_id]])
            group = app.load_account_index(paths)["codex_links"]["group-1"]
            self.assertEqual(group["state"], app.DESKTOP_STATE_ACTIVE)
            self.assertEqual(group["threads"][source_id]["last_state"], app.DESKTOP_STATE_ACTIVE)
            self.assertEqual(group["threads"][destination_id]["last_state"], app.DESKTOP_STATE_ACTIVE)

    def test_codex_linked_delete_waits_two_scans_then_uses_cli_for_remaining_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            source_id = "56565656-aaaa-4aaa-8aaa-565656565656"
            destination_id = "78787878-bbbb-4bbb-8bbb-787878787878"
            app.save_account_index(paths, self._linked_index(source_id, destination_id))
            states = {destination_id: app.DESKTOP_STATE_ACTIVE}
            commands: list[list[str]] = []

            def fake_cli(
                _: Path,
                arguments: list[str],
                timeout: int = 15,
                *,
                codex_home: Path | None = None,
            ) -> app.subprocess.CompletedProcess[str]:
                self.assertEqual(codex_home, paths.codex_home)
                commands.append(arguments)
                if arguments[0] == "delete":
                    states.pop(arguments[-1], None)
                return app.subprocess.CompletedProcess(arguments, 0, "", "")

            with (
                patch.object(app, "find_codex_executable", return_value=root / "codex"),
                patch.object(app, "codex_local_thread_states", side_effect=lambda _: dict(states)),
                patch.object(app, "run_codex_cli_command", side_effect=fake_cli),
                patch.object(
                    app,
                    "active_codex_account",
                    return_value=app.AccountInfo("codex:destination", "destination", None, None, None, None),
                ),
            ):
                first = app.sync_codex_linked_threads(paths)
                commands_after_first = list(commands)
                second = app.sync_codex_linked_threads(paths)
                settled = app.sync_codex_linked_threads(paths)

            self.assertEqual(first["pending"], 1)
            self.assertEqual(first["deleted"], 0)
            self.assertFalse(commands_after_first)
            self.assertEqual(second["deleted"], 1)
            self.assertFalse(second["errors"])
            self.assertEqual(commands, [["delete", "--force", destination_id]])
            self.assertNotIn(destination_id, states)
            group = app.load_account_index(paths)["codex_links"]["group-1"]
            self.assertEqual(group["state"], app.DESKTOP_STATE_DELETED)
            self.assertEqual(group["threads"][source_id]["last_state"], app.DESKTOP_STATE_DELETED)
            self.assertEqual(group["threads"][destination_id]["last_state"], app.DESKTOP_STATE_DELETED)
            self.assertFalse(settled["errors"])
            self.assertEqual(settled["deleted"], 0)

    def test_codex_linked_delete_from_destination_removes_source_after_two_scans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            source_id = "58585858-aaaa-4aaa-8aaa-585858585858"
            destination_id = "80808080-bbbb-4bbb-8bbb-808080808080"
            app.save_account_index(paths, self._linked_index(source_id, destination_id))
            states = {source_id: app.DESKTOP_STATE_ACTIVE}
            commands: list[list[str]] = []

            def fake_cli(
                _: Path,
                arguments: list[str],
                timeout: int = 15,
                *,
                codex_home: Path | None = None,
            ) -> app.subprocess.CompletedProcess[str]:
                self.assertEqual(codex_home, paths.codex_home)
                commands.append(arguments)
                states.pop(arguments[-1], None)
                return app.subprocess.CompletedProcess(arguments, 0, "", "")

            with (
                patch.object(app, "find_codex_executable", return_value=root / "codex"),
                patch.object(app, "codex_local_thread_states", side_effect=lambda _: dict(states)),
                patch.object(app, "run_codex_cli_command", side_effect=fake_cli),
                patch.object(
                    app,
                    "active_codex_account",
                    return_value=app.AccountInfo("codex:source", "source", None, None, None, None),
                ),
            ):
                first = app.sync_codex_linked_threads(paths)
                second = app.sync_codex_linked_threads(paths)
                settled = app.sync_codex_linked_threads(paths)

            self.assertEqual(first["pending"], 1)
            self.assertEqual(second["deleted"], 1)
            self.assertEqual(commands, [["delete", "--force", source_id]])
            self.assertFalse(states)
            self.assertFalse(second["errors"])
            self.assertFalse(settled["errors"])
            group = app.load_account_index(paths)["codex_links"]["group-1"]
            self.assertEqual(group["state"], app.DESKTOP_STATE_DELETED)
            self.assertEqual(group["threads"][source_id]["last_state"], app.DESKTOP_STATE_DELETED)
            self.assertEqual(group["threads"][destination_id]["last_state"], app.DESKTOP_STATE_DELETED)

    def test_codex_empty_store_pauses_instead_of_deleting_linked_copies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            source_id = "89898989-cccc-4ccc-8ccc-898989898989"
            destination_id = "90909090-dddd-4ddd-8ddd-909090909090"
            app.save_account_index(paths, self._linked_index(source_id, destination_id))

            with (
                patch.object(app, "codex_local_thread_states", return_value={}),
                patch.object(app, "run_codex_cli_command") as cli,
            ):
                first = app.sync_codex_linked_threads(paths)
                second = app.sync_codex_linked_threads(paths)

            self.assertTrue(first["errors"])
            self.assertTrue(second["errors"])
            cli.assert_not_called()
            group = app.load_account_index(paths)["codex_links"]["group-1"]
            self.assertEqual(group["state"], app.DESKTOP_STATE_ACTIVE)
            self.assertEqual(group["threads"][source_id]["missing_scans"], 0)
            self.assertEqual(group["threads"][destination_id]["missing_scans"], 0)

    def test_corrupt_account_index_blocks_mutation_and_preserves_link_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            index_path = app.account_index_path(paths)
            index_path.parent.mkdir(parents=True, exist_ok=True)
            corrupt = '{"version": 1, "codex_links": {"group-keep": {}}'
            index_path.write_text(corrupt, encoding="utf-8")
            active = app.AccountInfo("codex:active", "active", None, None, None, None)

            with (
                patch.object(app, "active_codex_account", return_value=active),
                self.assertRaises(app.StateFileError),
            ):
                app.register_active_codex_account(paths)

            self.assertEqual(index_path.read_text(encoding="utf-8"), corrupt)

    def test_web_account_sync_runs_without_browser_refresh_and_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            state = web.WebState(paths, None, None)
            cached_chat = SimpleNamespace(session_id="cached")
            state._chats_cache = [cached_chat]  # type: ignore[list-item]
            state._chats_cache_at = 123.0

            with (
                patch.object(app, "sync_claude_desktop_accounts", return_value={"created": 1}),
                patch.object(
                    app,
                    "sync_codex_linked_threads",
                    return_value={"updated": 0, "deleted": 0, "errors": []},
                ),
            ):
                result = state.sync_linked_accounts_once()

            self.assertTrue(result["changed"])
            self.assertFalse(result["errors"])
            self.assertEqual(state._chats_cache_at, 0.0)
            self.assertEqual(state._chats_cache, [cached_chat])
            status = state.account_sync_status()
            self.assertIsNotNone(status["last_check_at"])
            self.assertIsNotNone(status["last_full_check_at"])
            self.assertIsNotNone(status["last_duration_seconds"])
            self.assertFalse(status["in_progress"])
            self.assertIsNone(status["last_error"])

    def test_web_fast_account_sync_skips_transcript_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            state = web.WebState(paths, None, None)

            with (
                patch.object(app, "sync_claude_desktop_accounts", return_value={}) as claude_sync,
                patch.object(
                    app,
                    "sync_codex_linked_threads",
                    return_value={"updated": 0, "deleted": 0, "errors": []},
                ),
            ):
                result = state.sync_linked_accounts_once(include_claude_transcripts=False)

            claude_sync.assert_called_once_with(paths, include_transcripts=False)
            self.assertFalse(result["full_scan"])
            self.assertIsNone(state.account_sync_status()["last_full_check_at"])

    def test_web_chat_refresh_does_not_start_a_second_account_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            state = web.WebState(paths, None, None)
            observed = threading.Event()

            def discover(*_: object, **__: object) -> list[app.Chat]:
                observed.set()
                return []

            with patch.object(app, "discover_agent_chats", side_effect=discover) as discovery:
                state.refresh_chats_background()
                self.assertTrue(observed.wait(timeout=2))

            discovery.assert_called_once_with(
                paths,
                sync_desktop_accounts=False,
                active_desktop_only=False,
            )

    def test_web_account_sync_monitor_starts_immediately_and_stops_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            state = web.WebState(paths, None, None)
            observed = threading.Event()

            def codex_sync(_: app.Paths) -> dict[str, object]:
                observed.set()
                return {"updated": 0, "deleted": 0, "errors": []}

            with (
                patch.object(app, "sync_claude_desktop_accounts", return_value={}),
                patch.object(app, "sync_codex_linked_threads", side_effect=codex_sync),
            ):
                started = state.start_account_sync_monitor(poll_seconds=60)
                self.assertTrue(observed.wait(timeout=2))
                stopped = state.stop_account_sync_monitor()

            self.assertTrue(started["running"])
            self.assertFalse(stopped["running"])

    def test_web_account_sync_monitor_starts_with_full_scan_then_uses_fast_scans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            state = web.WebState(paths, None, None)
            observed = threading.Event()
            scans: list[bool] = []

            def sync_once(*, include_claude_transcripts: bool = True) -> dict[str, object]:
                scans.append(include_claude_transcripts)
                if len(scans) >= 2:
                    observed.set()
                return {"full_scan": include_claude_transcripts}

            with patch.object(state, "sync_linked_accounts_once", side_effect=sync_once):
                state.start_account_sync_monitor(poll_seconds=1, full_poll_seconds=60)
                self.assertTrue(observed.wait(timeout=3))
                state.stop_account_sync_monitor()

            self.assertEqual(scans[:2], [True, False])

    def test_web_account_sync_monitor_wakes_when_desktop_sessions_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            state = web.WebState(paths, None, None)
            observed = threading.Event()
            scans = 0
            signatures = iter([("initial",), ("initial",), ("changed",)])

            def signature(_: app.Paths) -> tuple[str, ...]:
                return next(signatures, ("changed",))

            def sync_once(*, include_claude_transcripts: bool = True) -> dict[str, object]:
                nonlocal scans
                scans += 1
                if scans >= 2:
                    observed.set()
                return {"claude": {"pending_deletions": 0}}

            with (
                patch.object(app, "claude_desktop_change_signature", side_effect=signature),
                patch.object(state, "sync_linked_accounts_once", side_effect=sync_once),
            ):
                state.start_account_sync_monitor(poll_seconds=60, full_poll_seconds=60)
                self.assertTrue(observed.wait(timeout=3))
                state.stop_account_sync_monitor()

            self.assertGreaterEqual(scans, 2)

    def test_web_account_sync_monitor_confirms_deletion_without_full_poll_delay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            state = web.WebState(paths, None, None)
            observed = threading.Event()
            scans = 0

            def sync_once(*, include_claude_transcripts: bool = True) -> dict[str, object]:
                nonlocal scans
                scans += 1
                if scans >= 2:
                    observed.set()
                return {"claude": {"pending_deletions": 1 if scans == 1 else 0}}

            with (
                patch.object(app, "claude_desktop_change_signature", return_value=("stable",)),
                patch.object(state, "sync_linked_accounts_once", side_effect=sync_once),
            ):
                state.start_account_sync_monitor(poll_seconds=60, full_poll_seconds=60)
                self.assertTrue(observed.wait(timeout=3))
                state.stop_account_sync_monitor()

            self.assertGreaterEqual(scans, 2)

    def test_web_runner_monitor_starts_pending_work_without_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            queue = app.load_queue(paths.queue_file)
            queue["items"].append({"id": "pending-1", "status": app.STATUS_PENDING})
            app.save_queue(paths.queue_file, queue)
            state = web.WebState(paths, None, None)
            observed = threading.Event()

            def start_runner(_: int = 60) -> dict[str, object]:
                observed.set()
                return {"running": True, "exit_code": None}

            with patch.object(state, "start_runner", side_effect=start_runner):
                started = state.start_runner_monitor(poll_seconds=60)
                self.assertTrue(observed.wait(timeout=2))
                stopped = state.stop_runner_monitor()

            self.assertTrue(started["automatic"])
            self.assertFalse(stopped["automatic"])
            self.assertIsNotNone(stopped["automatic_last_check_at"])
            self.assertIsNone(stopped["automatic_last_error"])

    def test_web_runner_discovery_requires_matching_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            state = web.WebState(paths, None, None)
            matching = (
                f"/usr/bin/python3 -m claude_codex_queue --state-dir {paths.state_dir} "
                "run --poll-seconds 60"
            )
            unrelated = (
                "/usr/bin/python3 -m claude_codex_queue --state-dir /tmp/other-state "
                "run --poll-seconds 60"
            )

            self.assertTrue(state.command_is_runner_for_state(matching))
            self.assertFalse(state.command_is_runner_for_state(unrelated))

    def test_web_runner_monitor_leaves_empty_queue_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            state = web.WebState(paths, None, None)

            with patch.object(state, "start_runner") as start_runner:
                result = state.ensure_runner_for_pending_work()

            self.assertFalse(result["required"])
            self.assertIsNone(result["error"])
            start_runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
