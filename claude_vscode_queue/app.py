from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import functools
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


APP_DIR_NAME = ".claude-vscode-queue"
QUEUE_FILE_NAME = "queue.json"
ACCOUNT_INDEX_FILE_NAME = "accounts.json"
LOG_DIR_NAME = "logs"
QUEUE_VERSION = 1

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_RECOVERY = "recovery"
RECOVERY_PROMPT = "continua"

RATE_LIMIT_EXIT = 75
CONFIG_CHANGED_EXIT = 78
RATE_LIMIT_RESET_DELAY_SECONDS = 60
SYNTHETIC_MODELS = {"<synthetic>"}
CLAUDE_EXTERNAL_AUTH_ENV_VARS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_DEFAULT_HEADERS",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
}
CODEX_EXTERNAL_AUTH_ENV_VARS = {
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
}

PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"

VALID_CODEX_SANDBOX_MODES = {
    "danger-full-access",
    "read-only",
    "workspace-write",
}
VALID_CODEX_APPROVAL_POLICIES = {
    "never",
    "on-failure",
    "on-request",
    "untrusted",
}

VALID_PERMISSION_MODES = {
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
}

RATE_LIMIT_PATTERNS = [
    r"\brate[- ]?limit(?:ed)?\b",
    r"\busage limit\b",
    r"\bweekly limit\b",
    r"\bdaily limit\b",
    r"\byou(?:'ve| have)? hit (?:your|the).{0,40}\blimit\b",
    r"\blimit reached\b",
    r"\bquota\b",
    r"\btoo many requests\b",
    r"\b429\b",
    r"\btry again later\b",
    r"\bcapacity\b",
]

PERMISSION_WAIT_PATTERNS = [
    r"\brichied(?:e|ono)\s+approvazione\b",
    r"\bin attesa di approvazione\b",
    r"\bpermessi? di scrittura\b",
    r"\bapproval required\b",
    r"\brequires approval\b",
    r"\bpermission approval\b",
    r"\bcannot proceed without approval\b",
]


@dataclasses.dataclass(frozen=True)
class Chat:
    session_id: str
    title: str
    cwd: str | None
    permission_mode: str | None
    model: str | None
    jsonl_path: Path
    last_timestamp: str | None
    message_count: int
    last_prompt: str | None
    effort_level: str | None = None
    source: str = "Claude Code"
    source_key: str = "claude_code"
    can_queue: bool = True
    remote_kind: str | None = None
    remote_host: str | None = None
    remote_cwd: str | None = None
    remote_uri: str | None = None
    account_key: str | None = None
    account_label: str | None = None
    account_status: str = "unknown"
    provider: str = PROVIDER_CLAUDE
    sandbox_mode: str | None = None
    approval_policy: str | None = None
    personality: str | None = None
    archived: bool = False


@dataclasses.dataclass(frozen=True)
class Paths:
    windows_home: Path
    claude_home: Path
    state_dir: Path
    queue_file: Path
    log_dir: Path

    @property
    def codex_home(self) -> Path:
        return self.windows_home / ".codex"


@dataclasses.dataclass(frozen=True)
class AccountInfo:
    key: str
    label: str
    account_uuid_hash: str | None
    organization_uuid_hash: str | None
    email_hash: str | None
    source_changed_at: str | None


@dataclasses.dataclass(frozen=True)
class ClaudeRunResult:
    returncode: int
    stdout: str
    stderr: str
    rate_limited: bool
    reset_at: str | None


@dataclasses.dataclass(frozen=True)
class DesktopSessionRecord:
    root: Path
    sessions_root: Path
    account_uuid: str
    workspace_uuid: str
    path: Path
    data: dict[str, Any]
    active_account_uuid: str | None


def local_now() -> dt.datetime:
    return dt.datetime.now().astimezone().replace(microsecond=0)


def now_utc() -> str:
    return local_now().isoformat()


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        if value.endswith(("Z", "z")):
            value = value[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def iso_from_epoch_ms(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC).replace(microsecond=0).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path).lower() if os.name == "nt" else str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def is_wsl() -> bool:
    if os.name == "nt":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="ignore").lower()
    except OSError:
        return False


def windows_to_local_path(value: str | Path) -> Path:
    raw = normalize_windows_path(str(value).strip().strip('"'))
    match = re.match(r"^([a-zA-Z]):[\\/](.*)$", raw)
    if os.name != "nt" and match:
        drive = match.group(1).lower()
        rest = match.group(2).replace("\\", "/")
        return Path("/mnt") / drive / rest
    return Path(raw)


def normalize_windows_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def cwd_accessible(cwd: str | None) -> bool:
    if not cwd:
        return False
    try:
        return windows_to_local_path(cwd).exists()
    except OSError:
        return False


def is_windows_path(value: str | None) -> bool:
    if not value:
        return False
    normalized = normalize_windows_path(value)
    return bool(re.match(r"^[a-zA-Z]:[\\/]", normalized) or normalized.startswith("\\\\"))


@functools.lru_cache(maxsize=256)
def windows_path_accessible(value: str | None) -> bool:
    if not is_windows_path(value) or shutil.which("powershell.exe") is None:
        return False
    command = f"if (Test-Path -LiteralPath {powershell_single_quote(str(value))}) {{ exit 0 }} else {{ exit 1 }}"
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def chat_cwd_runnable(cwd: str | None) -> bool:
    return cwd_accessible(cwd) or windows_path_accessible(cwd)


def local_to_windows_path(path: Path) -> str:
    raw = str(path)
    match = re.match(r"^/mnt/([a-zA-Z])/(.*)$", raw)
    if match:
        drive = match.group(1).upper()
        rest = match.group(2).replace("/", "\\")
        return f"{drive}:\\{rest}"
    return raw


def current_windows_user_home_from_cwd() -> Path | None:
    cwd = Path.cwd().resolve()
    parts = cwd.parts
    for index in range(len(parts) - 2):
        if (
            parts[index] == "/"
            and index + 3 < len(parts)
            and parts[index + 1] == "mnt"
            and len(parts[index + 2]) == 1
            and parts[index + 3].lower() == "users"
            and index + 4 < len(parts)
        ):
            return Path("/mnt") / parts[index + 2] / "Users" / parts[index + 4]
    return None


def candidate_windows_homes(override: str | None = None) -> list[Path]:
    candidates: list[Path] = []
    if override:
        candidates.append(windows_to_local_path(override))
    env_home = os.environ.get("CLAUDE_QUEUE_WINDOWS_HOME") or os.environ.get("USERPROFILE")
    if env_home:
        candidates.append(windows_to_local_path(env_home))
    inferred = current_windows_user_home_from_cwd()
    if inferred:
        candidates.append(inferred)
    candidates.append(Path.home())
    for globbed in Path("/mnt/c/Users").glob("*") if Path("/mnt/c/Users").exists() else []:
        if path_exists(globbed / ".claude" / "projects") or path_exists(globbed / ".codex" / "session_index.jsonl"):
            candidates.append(globbed)
    return unique_paths(candidates)


def resolve_paths(windows_home: str | None = None, state_dir: str | None = None) -> Paths:
    home_candidates = candidate_windows_homes(windows_home)
    selected_home = None
    for home in home_candidates:
        if path_exists(home / ".claude" / "projects") or path_exists(home / ".codex" / "session_index.jsonl"):
            selected_home = home
            break
    if selected_home is None:
        selected_home = home_candidates[0] if home_candidates else Path.home()
    claude_home = selected_home / ".claude"
    state = windows_to_local_path(state_dir) if state_dir else selected_home / APP_DIR_NAME
    return Paths(
        windows_home=selected_home,
        claude_home=claude_home,
        state_dir=state,
        queue_file=state / QUEUE_FILE_NAME,
        log_dir=state / LOG_DIR_NAME,
    )


def parse_version_from_extension(path: Path) -> tuple[int, ...]:
    match = re.search(r"claude-code-([0-9]+(?:\.[0-9]+)*)", path.name)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def find_claude_executable(paths: Paths, override: str | None = None) -> Path | None:
    env_override = override or os.environ.get("CLAUDE_EXE")
    if env_override:
        candidate = windows_to_local_path(env_override)
        if candidate.exists():
            return candidate
    which = shutil.which("claude")
    if which:
        return Path(which)

    extension_roots = unique_paths(
        [
            paths.windows_home / ".vscode" / "extensions",
            Path.home() / ".vscode" / "extensions",
        ]
    )
    candidates: list[Path] = []
    for root in extension_roots:
        if not root.exists():
            continue
        for extension in root.glob("anthropic.claude-code-*"):
            exe = extension / "resources" / "native-binary" / "claude.exe"
            if exe.exists():
                candidates.append(exe)
        for extension in root.glob(".*"):
            exe = extension / "resources" / "native-binary" / "claude.exe"
            if exe.exists():
                candidates.append(exe)
    if not candidates:
        return None
    candidates.sort(key=lambda path: (parse_version_from_extension(path.parents[2]), path.stat().st_mtime))
    return candidates[-1]


def find_codex_executable(paths: Paths, override: str | None = None) -> Path | None:
    env_override = override or os.environ.get("CODEX_EXE")
    if env_override:
        candidate = windows_to_local_path(env_override)
        if candidate.exists():
            return candidate

    which = shutil.which("codex")
    if which and not is_wsl():
        return Path(which)

    candidates = [
        paths.windows_home / "AppData" / "Local" / "npm" / "codex.cmd",
        paths.windows_home / "AppData" / "Local" / "npm" / "codex.exe",
        paths.windows_home / "AppData" / "Roaming" / "npm" / "codex.cmd",
        paths.windows_home / "AppData" / "Roaming" / "npm" / "codex.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    if which:
        return Path(which)
    return None


def run_codex_cli_command(codex_exe: Path, arguments: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    if codex_exe.suffix.lower() in {".cmd", ".bat", ".exe"} or is_windows_path(str(codex_exe)):
        executable = local_to_windows_path(codex_exe)
        command = ["cmd.exe", "/d", "/c", "call", executable, *arguments]
    else:
        command = [str(codex_exe), *arguments]
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        env=codex_subprocess_env(),
    )


def safe_json_loads(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def truncate(value: str | None, length: int = 90) -> str:
    if not value:
        return ""
    compact = re.sub(r"\s+", " ", value).strip()
    if len(compact) <= length:
        return compact
    return compact[: length - 1] + "..."


def hash_text(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def short_hash(value: str | None, length: int = 12) -> str | None:
    hashed = hash_text(value)
    return hashed[:length] if hashed else None


def mask_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return None
    local, domain = value.split("@", 1)
    if not local or not domain:
        return None
    visible_local = local[:2] if len(local) > 2 else local[:1]
    return f"{visible_local}***@{domain}"


def account_index_path(paths: Paths) -> Path:
    return paths.state_dir / ACCOUNT_INDEX_FILE_NAME


def file_mtime_iso(path: Path) -> str | None:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC).astimezone().replace(microsecond=0).isoformat()
    except OSError:
        return None


def latest_mtime_iso(paths: list[Path]) -> str | None:
    timestamps = [parse_iso(file_mtime_iso(path)) for path in paths]
    valid = [value for value in timestamps if value is not None]
    if not valid:
        return None
    return max(valid).astimezone().replace(microsecond=0).isoformat()


def active_claude_account(paths: Paths) -> AccountInfo | None:
    global_config = load_json_file(paths.windows_home / ".claude.json")
    credentials = load_json_file(paths.claude_home / ".credentials.json")
    oauth_account = global_config.get("oauthAccount") if isinstance(global_config.get("oauthAccount"), dict) else {}
    claude_oauth = credentials.get("claudeAiOauth") if isinstance(credentials.get("claudeAiOauth"), dict) else {}

    account_uuid = oauth_account.get("accountUuid") if isinstance(oauth_account.get("accountUuid"), str) else None
    email = oauth_account.get("emailAddress") if isinstance(oauth_account.get("emailAddress"), str) else None
    organization_uuid = (
        oauth_account.get("organizationUuid")
        if isinstance(oauth_account.get("organizationUuid"), str)
        else credentials.get("organizationUuid") if isinstance(credentials.get("organizationUuid"), str) else None
    )
    refresh_token = claude_oauth.get("refreshToken") if isinstance(claude_oauth.get("refreshToken"), str) else None
    access_token = claude_oauth.get("accessToken") if isinstance(claude_oauth.get("accessToken"), str) else None
    if not any([account_uuid, email, organization_uuid, refresh_token, access_token]):
        return None

    if account_uuid or email:
        key_source = "|".join([account_uuid or "", email.lower() if email else "", organization_uuid or ""])
    else:
        key_source = "|".join([organization_uuid or "", short_hash(refresh_token, 24) or short_hash(access_token, 24) or ""])
    key = hash_text(key_source) or "unknown"
    label = mask_email(email) or f"Account {key[:8]}"
    changed_at = latest_mtime_iso([paths.windows_home / ".claude.json", paths.claude_home / ".credentials.json"])
    return AccountInfo(
        key=key,
        label=label,
        account_uuid_hash=short_hash(account_uuid),
        organization_uuid_hash=short_hash(organization_uuid),
        email_hash=short_hash(email.lower() if email else None),
        source_changed_at=changed_at,
    )


def jwt_payload(token: str | None) -> dict[str, Any]:
    if not token or token.count(".") < 2:
        return {}
    try:
        encoded = token.split(".", 2)[1]
        encoded += "=" * (-len(encoded) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def active_codex_account(paths: Paths) -> AccountInfo | None:
    auth_path = paths.codex_home / "auth.json"
    auth = load_json_file(auth_path)
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    account_id = tokens.get("account_id") if isinstance(tokens.get("account_id"), str) else None
    claims = jwt_payload(tokens.get("id_token") if isinstance(tokens.get("id_token"), str) else None)
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    subject = claims.get("sub") if isinstance(claims.get("sub"), str) else None
    if not any([account_id, email, subject]):
        return None

    identity = "|".join([account_id or "", email.lower() if email else "", subject or ""])
    hashed = hash_text(identity) or "unknown"
    key = f"codex:{hashed}"
    label = mask_email(email) or f"Codex {hashed[:8]}"
    return AccountInfo(
        key=key,
        label=label,
        account_uuid_hash=short_hash(account_id),
        organization_uuid_hash=None,
        email_hash=short_hash(email.lower() if email else None),
        source_changed_at=file_mtime_iso(auth_path),
    )


def load_account_index(paths: Paths) -> dict[str, Any]:
    data = load_json_file(account_index_path(paths))
    if not data:
        return {"version": 1, "accounts": {}, "sessions": {}}
    data.setdefault("version", 1)
    data.setdefault("accounts", {})
    data.setdefault("sessions", {})
    if not isinstance(data["accounts"], dict):
        data["accounts"] = {}
    if not isinstance(data["sessions"], dict):
        data["sessions"] = {}
    return data


def save_account_index(paths: Paths, index: dict[str, Any]) -> None:
    path = account_index_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def register_active_account(paths: Paths, index: dict[str, Any] | None = None) -> AccountInfo | None:
    account = active_claude_account(paths)
    if account is None:
        return None
    index = index if index is not None else load_account_index(paths)
    accounts = index.setdefault("accounts", {})
    now = now_utc()
    existing = accounts.get(account.key) if isinstance(accounts.get(account.key), dict) else {}
    accounts[account.key] = {
        **existing,
        "key": account.key,
        "label": account.label,
        "account_uuid_hash": account.account_uuid_hash,
        "organization_uuid_hash": account.organization_uuid_hash,
        "email_hash": account.email_hash,
        "source_changed_at": account.source_changed_at,
        "first_seen_at": existing.get("first_seen_at") or now,
        "last_seen_at": now,
    }
    save_account_index(paths, index)
    return account


def register_active_codex_account(paths: Paths, index: dict[str, Any] | None = None) -> AccountInfo | None:
    account = active_codex_account(paths)
    if account is None:
        return None
    index = index if index is not None else load_account_index(paths)
    accounts = index.setdefault("accounts", {})
    now = now_utc()
    existing = accounts.get(account.key) if isinstance(accounts.get(account.key), dict) else {}
    accounts[account.key] = {
        **existing,
        "key": account.key,
        "provider": PROVIDER_CODEX,
        "label": account.label,
        "account_uuid_hash": account.account_uuid_hash,
        "email_hash": account.email_hash,
        "source_changed_at": account.source_changed_at,
        "first_seen_at": existing.get("first_seen_at") or now,
        "last_seen_at": now,
    }
    save_account_index(paths, index)
    return account


def chat_account_session_key(chat: Chat) -> str:
    scope = f"ssh:{chat.remote_host}" if chat.remote_kind == "ssh" and chat.remote_host else "local"
    if chat.provider != PROVIDER_CLAUDE:
        scope = f"{chat.provider}:{scope}"
    return f"{scope}:{chat.session_id}"


def chat_recent_for_account(chat: Chat, account: AccountInfo) -> bool:
    changed_at = parse_iso(account.source_changed_at)
    chat_time = parse_iso(chat.last_timestamp)
    if changed_at is None or chat_time is None:
        return False
    return chat_time >= changed_at - dt.timedelta(minutes=5)


def account_public_dict(account: AccountInfo | None) -> dict[str, Any] | None:
    if account is None:
        return None
    return {
        "key": account.key,
        "short_key": account.key[:8],
        "label": account.label,
        "account_uuid_hash": account.account_uuid_hash,
        "organization_uuid_hash": account.organization_uuid_hash,
        "email_hash": account.email_hash,
        "source_changed_at": account.source_changed_at,
    }


def account_index_public(paths: Paths) -> dict[str, Any]:
    index = load_account_index(paths)
    accounts = index.get("accounts") if isinstance(index.get("accounts"), dict) else {}
    account_items = [(key, value) for key, value in accounts.items() if isinstance(value, dict)]
    return {
        "accounts": [
            {
                "key": key,
                "short_key": key[:8],
                "label": value.get("label") or f"Account {key[:8]}",
                "first_seen_at": value.get("first_seen_at"),
                "last_seen_at": value.get("last_seen_at"),
                "source_changed_at": value.get("source_changed_at"),
            }
            for key, value in sorted(account_items, key=lambda entry: str(entry[1].get("last_seen_at") or ""), reverse=True)
        ],
        "session_count": len(index.get("sessions", {})) if isinstance(index.get("sessions"), dict) else 0,
    }


def real_model_value(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    stripped = value.strip()
    return None if stripped in SYNTHETIC_MODELS else stripped


def chat_sort_key(chat: Chat) -> dt.datetime:
    parsed = parse_iso(chat.last_timestamp)
    if parsed:
        return parsed
    try:
        return dt.datetime.fromtimestamp(chat.jsonl_path.stat().st_mtime, tz=dt.UTC)
    except OSError:
        return dt.datetime.min.replace(tzinfo=dt.UTC)


def discover_chats(claude_home: Path) -> list[Chat]:
    projects = claude_home / "projects"
    if not projects.exists():
        return []

    by_session: dict[str, Chat] = {}
    for jsonl_path in projects.glob("**/*.jsonl"):
        title: str | None = None
        cwd: str | None = None
        permission_mode: str | None = None
        model: str | None = None
        effort_level: str | None = None
        session_id: str | None = None
        last_event_timestamp: str | None = None
        last_event_dt: dt.datetime | None = None
        last_message_timestamp: str | None = None
        last_message_dt: dt.datetime | None = None
        last_prompt: str | None = None
        message_count = 0

        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    obj = safe_json_loads(line)
                    if not obj:
                        continue
                    session_id = obj.get("sessionId") or session_id
                    timestamp = obj.get("timestamp")
                    timestamp_dt = parse_iso(timestamp) if isinstance(timestamp, str) else None
                    if isinstance(timestamp, str):
                        if timestamp_dt is None or last_event_dt is None or timestamp_dt >= last_event_dt:
                            last_event_timestamp = timestamp
                            last_event_dt = timestamp_dt
                    obj_type = obj.get("type")
                    if obj_type in {"user", "assistant"} and isinstance(timestamp, str):
                        if timestamp_dt is None or last_message_dt is None or timestamp_dt >= last_message_dt:
                            last_message_timestamp = timestamp
                            last_message_dt = timestamp_dt
                    if obj_type == "ai-title" and isinstance(obj.get("aiTitle"), str):
                        title = obj["aiTitle"]
                    if obj_type == "last-prompt" and isinstance(obj.get("lastPrompt"), str):
                        last_prompt = obj["lastPrompt"]
                    if isinstance(obj.get("cwd"), str):
                        cwd = obj["cwd"]
                    if isinstance(obj.get("permissionMode"), str):
                        permission_mode = obj["permissionMode"]
                    if isinstance(obj.get("effortLevel"), str):
                        effort_level = obj["effortLevel"]
                    message = obj.get("message")
                    if isinstance(message, dict):
                        message_count += 1
                        parsed_model = real_model_value(message.get("model"))
                        if parsed_model:
                            model = parsed_model
        except OSError:
            continue

        if not session_id:
            continue
        display_title = title or truncate(last_prompt, 60) or (Path(cwd).name if cwd else "Claude chat")
        chat = Chat(
            session_id=session_id,
            title=display_title,
            cwd=cwd,
            permission_mode=permission_mode,
            model=model,
            jsonl_path=jsonl_path,
            last_timestamp=last_message_timestamp or last_event_timestamp,
            message_count=message_count,
            last_prompt=last_prompt,
            effort_level=effort_level,
            can_queue=chat_cwd_runnable(cwd),
        )
        existing = by_session.get(session_id)
        if existing is None:
            by_session[session_id] = chat
        else:
            old_timestamp = parse_iso(existing.last_timestamp)
            new_timestamp = parse_iso(chat.last_timestamp)
            old_key = old_timestamp or dt.datetime.fromtimestamp(existing.jsonl_path.stat().st_mtime, tz=dt.UTC)
            new_key = new_timestamp or dt.datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=dt.UTC)
            if new_key >= old_key:
                by_session[session_id] = chat

    return sorted(by_session.values(), key=chat_sort_key, reverse=True)


def load_codex_session_index(codex_home: Path) -> dict[str, dict[str, Any]]:
    index_path = codex_home / "session_index.jsonl"
    sessions: dict[str, dict[str, Any]] = {}
    try:
        handle = index_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return sessions
    with handle:
        for position, line in enumerate(handle):
            entry = safe_json_loads(line)
            if not entry or not isinstance(entry.get("id"), str):
                continue
            session_id = entry["id"]
            timestamp = parse_iso(entry.get("updated_at") if isinstance(entry.get("updated_at"), str) else None)
            existing = sessions.get(session_id)
            existing_timestamp = (
                parse_iso(existing.get("updated_at"))
                if isinstance(existing, dict) and isinstance(existing.get("updated_at"), str)
                else None
            )
            if existing is None or timestamp is None or existing_timestamp is None or timestamp >= existing_timestamp:
                sessions[session_id] = {**entry, "_position": position}
    return sessions


def codex_thread_rows(codex_home: Path) -> dict[str, dict[str, Any]]:
    database = codex_home / "state_5.sqlite"
    if not database.exists():
        return {}
    columns = [
        "id",
        "rollout_path",
        "cwd",
        "model",
        "reasoning_effort",
        "sandbox_policy",
        "approval_mode",
        "archived",
        "updated_at_ms",
        "recency_at_ms",
    ]
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        available = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        selected = [column for column in columns if column in available]
        if "id" not in selected:
            return {}
        rows = connection.execute(f"SELECT {', '.join(selected)} FROM threads").fetchall()
        return {str(row["id"]): dict(row) for row in rows if row["id"]}
    except sqlite3.Error:
        pass
    finally:
        if connection is not None:
            connection.close()

    if not is_wsl() or shutil.which("py.exe") is None:
        return {}
    script = r"""
import json
import sqlite3
import sys

columns = json.loads(sys.argv[2])
connection = sqlite3.connect(sys.argv[1])
connection.row_factory = sqlite3.Row
available = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
selected = [column for column in columns if column in available]
rows = connection.execute("SELECT " + ", ".join(selected) + " FROM threads").fetchall()
print(json.dumps([dict(row) for row in rows], ensure_ascii=True))
"""
    try:
        result = subprocess.run(
            [
                "py.exe",
                "-3",
                "-c",
                script,
                local_to_windows_path(database),
                json.dumps(columns),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=15,
        )
        values = json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    return {
        str(row["id"]): row
        for row in values
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }


def codex_rollout_files(codex_home: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    active: dict[str, Path] = {}
    archived: dict[str, Path] = {}
    session_pattern = re.compile(r"([0-9a-f]{8}-[0-9a-f-]{27})\.jsonl$", re.IGNORECASE)
    for root, destination in [
        (codex_home / "sessions", active),
        (codex_home / "archived_sessions", archived),
    ]:
        if not root.exists():
            continue
        for path in root.glob("**/rollout-*.jsonl"):
            match = session_pattern.search(path.name)
            if match:
                destination[match.group(1).lower()] = path
    return active, archived


def codex_sandbox_mode(value: Any) -> str | None:
    policy = value
    if isinstance(value, str):
        try:
            policy = json.loads(value)
        except json.JSONDecodeError:
            policy = {"type": value}
    mode = policy.get("type") if isinstance(policy, dict) else None
    if mode == "disabled":
        return "danger-full-access"
    return mode if mode in VALID_CODEX_SANDBOX_MODES else None


def codex_cwd_runnable(cwd: str | None, codex_exe: Path | None) -> bool:
    if not cwd or codex_exe is None:
        return False
    normalized = normalize_windows_path(cwd)
    if is_windows_path(normalized):
        return cwd_accessible(normalized) or windows_path_accessible(normalized)
    if codex_exe.suffix.lower() in {".cmd", ".bat", ".exe"}:
        return False
    return cwd_accessible(normalized)


def codex_timestamp(index_entry: dict[str, Any], row: dict[str, Any]) -> str | None:
    values: list[dt.datetime] = []
    indexed = parse_iso(index_entry.get("updated_at") if isinstance(index_entry.get("updated_at"), str) else None)
    if indexed:
        values.append(indexed)
    for key in ["recency_at_ms", "updated_at_ms"]:
        timestamp = parse_iso(iso_from_epoch_ms(row.get(key)))
        if timestamp:
            values.append(timestamp)
            break
    if not values:
        return None
    return max(values).astimezone().replace(microsecond=0).isoformat()


def discover_codex_app_sessions(paths: Paths) -> list[Chat]:
    index = load_codex_session_index(paths.codex_home)
    if not index:
        return []
    rows = codex_thread_rows(paths.codex_home)
    active_files, archived_files = codex_rollout_files(paths.codex_home)
    codex_exe = find_codex_executable(paths)
    authenticated = active_codex_account(paths) is not None
    chats: list[Chat] = []

    for session_id, index_entry in index.items():
        row = rows.get(session_id, {})
        raw_rollout = row.get("rollout_path") if isinstance(row.get("rollout_path"), str) else None
        rollout = windows_to_local_path(raw_rollout) if raw_rollout else None
        archived = bool(row.get("archived"))
        if rollout is None or not rollout.exists():
            rollout = active_files.get(session_id.lower())
            if rollout is None:
                rollout = archived_files.get(session_id.lower())
                archived = rollout is not None

        raw_cwd = row.get("cwd") if isinstance(row.get("cwd"), str) else None
        cwd = normalize_windows_path(raw_cwd) if raw_cwd else None
        title = index_entry.get("thread_name") if isinstance(index_entry.get("thread_name"), str) else None
        first_prompt = row.get("first_user_message") if isinstance(row.get("first_user_message"), str) else None
        display_title = truncate(title, 100) or truncate(first_prompt, 100) or "Codex task"
        sandbox_mode = codex_sandbox_mode(row.get("sandbox_policy"))
        approval = row.get("approval_mode") if isinstance(row.get("approval_mode"), str) else None
        can_queue = bool(
            not archived
            and authenticated
            and rollout is not None
            and rollout.exists()
            and codex_cwd_runnable(cwd, codex_exe)
        )
        chats.append(
            Chat(
                session_id=session_id,
                title=display_title,
                cwd=cwd,
                permission_mode=sandbox_mode,
                model=real_model_value(row.get("model")),
                jsonl_path=rollout or Path(session_id),
                last_timestamp=codex_timestamp(index_entry, row),
                message_count=-1,
                last_prompt=first_prompt,
                effort_level=row.get("reasoning_effort") if isinstance(row.get("reasoning_effort"), str) else None,
                source="Codex App" if not archived else "Codex App (archiviata)",
                source_key="codex_app" if not archived else "codex_app_archived",
                can_queue=can_queue,
                provider=PROVIDER_CODEX,
                sandbox_mode=sandbox_mode,
                approval_policy=approval if approval in VALID_CODEX_APPROVAL_POLICIES else None,
                archived=archived,
            )
        )
    return sorted(chats, key=chat_sort_key, reverse=True)


def workspace_storage_root(paths: Paths) -> Path:
    return paths.windows_home / "AppData" / "Roaming" / "Code" / "User" / "workspaceStorage"


def claude_windows_app_roots(paths: Paths) -> list[Path]:
    candidates: list[Path] = []
    packages = paths.windows_home / "AppData" / "Local" / "Packages"
    if packages.exists():
        for package in packages.glob("Claude_*"):
            candidates.append(package / "LocalCache" / "Roaming" / "Claude")
    candidates.extend(
        [
            paths.windows_home / "AppData" / "Roaming" / "Claude",
            paths.windows_home / "AppData" / "Local" / "Claude",
        ]
    )
    return [path for path in unique_paths(candidates) if path.exists()]


def desktop_config(root: Path) -> dict[str, Any]:
    return load_json_file(root / "config.json")


def active_desktop_account_uuid(root: Path) -> str | None:
    value = desktop_config(root).get("lastKnownAccountUuid")
    return value if isinstance(value, str) and value else None


def active_desktop_workspace_uuid(root: Path, account_uuid: str | None) -> str | None:
    if not account_uuid:
        return None
    log_path = root / "logs" / "main.log"
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    pattern = re.compile(rf"claude-code-sessions[\\/]+{re.escape(account_uuid)}[\\/]+([0-9a-fA-F-]{{36}})")
    for line in reversed(lines[-1000:]):
        match = pattern.search(line)
        if match:
            return match.group(1)

    sessions_root = root / "claude-code-sessions" / account_uuid
    try:
        workspaces = [path for path in sessions_root.iterdir() if path.is_dir()]
    except OSError:
        return None
    if not workspaces:
        return None
    workspaces.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return workspaces[0].name


def active_desktop_account_key(paths: Paths) -> tuple[str | None, str | None]:
    for root in claude_windows_app_roots(paths):
        account_uuid = active_desktop_account_uuid(root)
        if account_uuid:
            return f"claude-app:{account_uuid}", f"Claude app {account_uuid[:8]}"
    return None, None


def desktop_session_records(paths: Paths, active_only: bool = False) -> list[DesktopSessionRecord]:
    records: list[DesktopSessionRecord] = []
    for root in claude_windows_app_roots(paths):
        active_account_uuid = active_desktop_account_uuid(root)
        active_workspace_uuid = active_desktop_workspace_uuid(root, active_account_uuid) if active_only else None
        sessions_root = root / "claude-code-sessions"
        if not sessions_root.exists():
            continue
        for json_path in sessions_root.glob("*/*/*.json"):
            rel = json_path.relative_to(sessions_root)
            if len(rel.parts) < 3:
                continue
            if active_only and rel.parts[0] != active_account_uuid:
                continue
            if active_only and active_workspace_uuid and rel.parts[1] != active_workspace_uuid:
                continue
            data = load_json_file(json_path)
            if not data:
                continue
            records.append(
                DesktopSessionRecord(
                    root=root,
                    sessions_root=sessions_root,
                    account_uuid=rel.parts[0],
                    workspace_uuid=rel.parts[1],
                    path=json_path,
                    data=data,
                    active_account_uuid=active_account_uuid,
                )
            )
    return records


def desktop_record_cli_session_id(record: DesktopSessionRecord) -> str | None:
    cli_session_id = record.data.get("cliSessionId")
    if isinstance(cli_session_id, str) and cli_session_id:
        return cli_session_id
    app_session_id = record.data.get("sessionId")
    if isinstance(app_session_id, str) and app_session_id:
        return app_session_id.removeprefix("local_")
    return None


def desktop_record_timestamp_ms(record: DesktopSessionRecord) -> int:
    values = [
        value
        for value in [record.data.get("lastActivityAt"), record.data.get("lastFocusedAt"), record.data.get("createdAt")]
        if isinstance(value, (int, float))
    ]
    if values:
        return int(max(values))
    try:
        return int(record.path.stat().st_mtime * 1000)
    except OSError:
        return 0


def desktop_record_visible(record: DesktopSessionRecord) -> bool:
    return record.data.get("isArchived") is not True


def sanitize_desktop_session_data(data: dict[str, Any]) -> dict[str, Any]:
    sanitized = {key: value for key, value in data.items() if value is not None}
    for key in ["sessionId", "cliSessionId", "title", "titleSource", "cwd", "originCwd", "model", "effort"]:
        value = sanitized.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            sanitized.pop(key, None)
    permission_mode = sanitized.get("permissionMode")
    if permission_mode is not None and permission_mode not in VALID_PERMISSION_MODES:
        sanitized.pop("permissionMode", None)
    chrome_permission_mode = sanitized.get("chromePermissionMode")
    if chrome_permission_mode is not None and not isinstance(chrome_permission_mode, str):
        sanitized.pop("chromePermissionMode", None)
    for key in ["remoteMcpServersConfig", "alwaysAllowedReasons", "sessionPermissionUpdates", "bridgeSessionIds"]:
        value = sanitized.get(key)
        if value is not None and not isinstance(value, list):
            sanitized.pop(key, None)
    if "enabledMcpTools" in sanitized and not isinstance(sanitized["enabledMcpTools"], dict):
        sanitized.pop("enabledMcpTools", None)
    if "sessionSettings" in sanitized and not isinstance(sanitized["sessionSettings"], dict):
        sanitized.pop("sessionSettings", None)
    if "spawnSeed" in sanitized and not isinstance(sanitized["spawnSeed"], (dict, list)):
        sanitized.pop("spawnSeed", None)
    completed_turns = sanitized.get("completedTurns")
    if completed_turns is not None and not isinstance(completed_turns, int):
        sanitized.pop("completedTurns", None)
    for key in ["createdAt", "lastActivityAt", "lastFocusedAt"]:
        value = sanitized.get(key)
        if value is not None and not isinstance(value, (int, float)):
            sanitized.pop(key, None)
    if "isArchived" in sanitized and not isinstance(sanitized["isArchived"], bool):
        sanitized["isArchived"] = False
    return sanitized


def write_desktop_session_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(sanitize_desktop_session_data(data), ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def desktop_session_data_for_path(source_data: dict[str, Any], cli_session_id: str, destination: Path) -> dict[str, Any]:
    data = dict(source_data)
    data["cliSessionId"] = cli_session_id
    data["sessionId"] = destination.stem
    data["isArchived"] = False
    return sanitize_desktop_session_data(data)


def desktop_account_session_dir(
    sessions_root: Path,
    account_uuid: str,
    preferred_workspace_uuid: str | None = None,
) -> tuple[Path, str]:
    account_dir = sessions_root / account_uuid
    if preferred_workspace_uuid:
        return account_dir / preferred_workspace_uuid, preferred_workspace_uuid
    if account_dir.exists():
        workspaces = [path for path in account_dir.iterdir() if path.is_dir()]
        if workspaces:
            workspaces.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            return workspaces[0], workspaces[0].name
    workspace_uuid = str(uuid.uuid4())
    return account_dir / workspace_uuid, workspace_uuid


def desktop_account_workspace_uuids(
    sessions_root: Path,
    account_uuid: str,
    all_workspace_uuids: set[str],
) -> list[str]:
    account_dir = sessions_root / account_uuid
    workspace_uuids = set(all_workspace_uuids)
    if account_dir.exists():
        workspace_uuids.update(path.name for path in account_dir.iterdir() if path.is_dir())
    if not workspace_uuids:
        workspace_uuids.add(str(uuid.uuid4()))
    return sorted(workspace_uuids)


def sync_claude_desktop_accounts(paths: Paths) -> dict[str, Any]:
    result: dict[str, Any] = {
        "roots": 0,
        "accounts": 0,
        "sessions": 0,
        "created": 0,
        "transcripts_created": 0,
        "updated": 0,
        "repaired": 0,
        "deduped": 0,
        "backups": [],
    }
    transcript_chats = discover_chats(paths.claude_home)
    records = desktop_session_records(paths)
    for record in records:
        sanitized = sanitize_desktop_session_data(record.data)
        if sanitized != record.data:
            backup = backup_desktop_session_file(paths, record.path)
            result["backups"].append(str(backup))
            write_desktop_session_json(record.path, sanitized)
            result["repaired"] += 1
    if result["repaired"]:
        records = desktop_session_records(paths)
    records_by_root: dict[Path, list[DesktopSessionRecord]] = {}
    for record in records:
        records_by_root.setdefault(record.root, []).append(record)

    for root in claude_windows_app_roots(paths):
        root_records = records_by_root.get(root, [])
        sessions_root = root / "claude-code-sessions"
        account_uuids = {
            record.account_uuid
            for record in root_records
            if record.account_uuid
        }
        if sessions_root.exists():
            account_uuids.update(path.name for path in sessions_root.iterdir() if path.is_dir())
        all_workspace_uuids = {record.workspace_uuid for record in root_records if record.workspace_uuid}
        if sessions_root.exists():
            for account_dir in sessions_root.iterdir():
                if account_dir.is_dir():
                    all_workspace_uuids.update(path.name for path in account_dir.iterdir() if path.is_dir())
        active_account_uuid = active_desktop_account_uuid(root)
        if active_account_uuid:
            account_uuids.add(active_account_uuid)
        if not account_uuids:
            continue
        workspace_uuids_by_account = {
            account_uuid: desktop_account_workspace_uuids(sessions_root, account_uuid, all_workspace_uuids)
            for account_uuid in account_uuids
        }

        records_by_session: dict[str, list[DesktopSessionRecord]] = {}
        for record in root_records:
            cli_session_id = desktop_record_cli_session_id(record)
            if not cli_session_id or not desktop_record_visible(record):
                continue
            records_by_session.setdefault(cli_session_id, []).append(record)
        result["roots"] += 1
        result["accounts"] += len(account_uuids)
        result["sessions"] += len(records_by_session)

        for cli_session_id, session_records in list(records_by_session.items()):
            source_record = max(session_records, key=desktop_record_timestamp_ms)
            for account_uuid in sorted(account_uuids):
                for workspace_uuid in workspace_uuids_by_account[account_uuid]:
                    target_records = [
                        record
                        for record in session_records
                        if record.account_uuid == account_uuid
                        and record.workspace_uuid == workspace_uuid
                        and record.path.exists()
                        and desktop_record_visible(record)
                    ]
                    if target_records:
                        primary = max(target_records, key=desktop_record_timestamp_ms)
                        for duplicate in target_records:
                            if duplicate.path == primary.path:
                                continue
                            backup = backup_desktop_session_file(paths, duplicate.path)
                            result["backups"].append(str(backup))
                            duplicate.path.unlink()
                            result["deduped"] += 1

                        if source_record.path != primary.path and desktop_record_timestamp_ms(source_record) >= desktop_record_timestamp_ms(primary):
                            data = desktop_session_data_for_path(source_record.data, cli_session_id, primary.path)
                            if data != primary.data:
                                backup = backup_desktop_session_file(paths, primary.path)
                                result["backups"].append(str(backup))
                                write_desktop_session_json(primary.path, data)
                                result["updated"] += 1
                        continue

                    destination_dir = sessions_root / account_uuid / workspace_uuid
                    app_session_id = source_record.data.get("sessionId")
                    destination = unique_desktop_session_path(
                        destination_dir,
                        str(app_session_id).removeprefix("local_") if isinstance(app_session_id, str) and app_session_id else None,
                    )
                    data = desktop_session_data_for_path(source_record.data, cli_session_id, destination)
                    write_desktop_session_json(destination, data)
                    session_records.append(
                        DesktopSessionRecord(
                            root=root,
                            sessions_root=sessions_root,
                            account_uuid=account_uuid,
                            workspace_uuid=workspace_uuid,
                            path=destination,
                            data=data,
                            active_account_uuid=active_account_uuid,
                        )
                    )
                    result["created"] += 1

        for chat in transcript_chats:
            if not chat.session_id or not chat.cwd or not chat.can_queue:
                continue
            session_records = records_by_session.setdefault(chat.session_id, [])
            for account_uuid in sorted(account_uuids):
                for workspace_uuid in workspace_uuids_by_account[account_uuid]:
                    if any(
                        record.account_uuid == account_uuid
                        and record.workspace_uuid == workspace_uuid
                        and record.path.exists()
                        and desktop_record_visible(record)
                        for record in session_records
                    ):
                        continue
                    destination_dir = sessions_root / account_uuid / workspace_uuid
                    data = synthetic_desktop_session_data(chat)
                    destination = unique_desktop_session_path(destination_dir, data["sessionId"].removeprefix("local_"))
                    data = desktop_session_data_for_path(data, chat.session_id, destination)
                    write_desktop_session_json(destination, data)
                    session_records.append(
                        DesktopSessionRecord(
                            root=root,
                            sessions_root=sessions_root,
                            account_uuid=account_uuid,
                            workspace_uuid=workspace_uuid,
                            path=destination,
                            data=data,
                            active_account_uuid=active_account_uuid,
                        )
                    )
                    result["created"] += 1
                    result["transcripts_created"] += 1

    return result


def active_desktop_session_dir(paths: Paths, preferred_workspace_uuid: str | None = None) -> tuple[Path, str, str]:
    for root in claude_windows_app_roots(paths):
        active_account_uuid = active_desktop_account_uuid(root)
        if not active_account_uuid:
            continue
        sessions_root = root / "claude-code-sessions"
        session_dir, workspace_uuid = desktop_account_session_dir(sessions_root, active_account_uuid, preferred_workspace_uuid)
        return session_dir, active_account_uuid, workspace_uuid
    raise ValueError("Account Claude app attivo non trovato nei metadati locali.")


def desktop_transfer_backup_dir(paths: Paths) -> Path:
    stamp = local_now().strftime("%Y%m%d-%H%M%S")
    return paths.state_dir / "account-transfer-backups" / stamp


def backup_desktop_session_file(paths: Paths, source: Path) -> Path:
    backup_root = desktop_transfer_backup_dir(paths)
    backup_root.mkdir(parents=True, exist_ok=True)
    destination = backup_root / source.name
    counter = 1
    while destination.exists():
        destination = backup_root / f"{source.stem}-{counter}{source.suffix}"
        counter += 1
    shutil.copy2(source, destination)
    return destination


def chat_last_timestamp_ms(chat: Chat) -> int:
    parsed = parse_iso(chat.last_timestamp)
    if parsed is not None:
        return int(parsed.timestamp() * 1000)
    try:
        return int(chat.jsonl_path.stat().st_mtime * 1000)
    except OSError:
        return int(time.time() * 1000)


def synthetic_desktop_session_data(chat: Chat) -> dict[str, Any]:
    now_ms = chat_last_timestamp_ms(chat)
    app_session_uuid = str(uuid.uuid4())
    return sanitize_desktop_session_data({
        "sessionId": f"local_{app_session_uuid}",
        "cliSessionId": chat.session_id,
        "cwd": chat.cwd,
        "originCwd": chat.cwd,
        "lastFocusedAt": now_ms,
        "createdAt": now_ms,
        "lastActivityAt": now_ms,
        "model": chat.model,
        "effort": chat.effort_level,
        "isArchived": False,
        "title": chat.title or f"Claude chat {chat.session_id[:8]}",
        "titleSource": "imported",
        "permissionMode": chat.permission_mode,
        "remoteMcpServersConfig": [],
        "chromePermissionMode": "skip_all_permission_checks",
        "completedTurns": chat.message_count,
        "bridgeSessionIds": [],
        "alwaysAllowedReasons": [],
        "sessionPermissionUpdates": [],
        "classifierSummaryEnabled": True,
        "spawnSeed": {},
    })


def unique_desktop_session_path(destination_dir: Path, session_id: str | None = None) -> Path:
    name = f"local_{session_id or uuid.uuid4()}.json"
    path = destination_dir / name
    while path.exists():
        path = destination_dir / f"local_{uuid.uuid4()}.json"
    return path


def transfer_chat_to_active_desktop_account(paths: Paths, chat: Chat, move: bool = False) -> dict[str, Any]:
    if chat.remote_kind is not None:
        raise ValueError("Le chat remote SSH non possono essere spostate nei metadati locali della app Claude.")
    if not chat.cwd:
        raise ValueError("La chat non ha una cwd: non posso creare un metadato Claude app affidabile.")

    records = desktop_session_records(paths)
    source_records = [record for record in records if desktop_record_cli_session_id(record) == chat.session_id]
    source_record = next((record for record in source_records if record.account_uuid != record.active_account_uuid), None)
    if source_record is None and source_records:
        source_record = source_records[0]
    destination_dir, active_account_uuid, active_workspace_uuid = active_desktop_session_dir(
        paths,
        source_record.workspace_uuid if source_record is not None else None,
    )
    active_records = [
        record
        for record in records
        if record.account_uuid == active_account_uuid and desktop_record_cli_session_id(record) == chat.session_id
    ]
    if source_record is not None and source_record.account_uuid == active_account_uuid:
        source_record = next((record for record in source_records if record.account_uuid != active_account_uuid), source_record)

    backup_path = None
    removed_source = None
    if active_records:
        if move:
            for record in source_records:
                if record.account_uuid == active_account_uuid:
                    continue
                backup_path = backup_desktop_session_file(paths, record.path)
                record.path.unlink()
                removed_source = str(record.path)
                break
        return {
            "status": "already_active",
            "session_id": chat.session_id,
            "title": chat.title,
            "destination": str(active_records[0].path),
            "active_account": f"Claude app {active_account_uuid[:8]}",
            "active_workspace": active_workspace_uuid,
            "backup": str(backup_path) if backup_path else None,
            "removed_source": removed_source,
        }

    if source_record is not None:
        app_session_id = source_record.data.get("sessionId")
        destination = unique_desktop_session_path(
            destination_dir,
            str(app_session_id).removeprefix("local_") if isinstance(app_session_id, str) and app_session_id else None,
        )
        data = desktop_session_data_for_path(source_record.data, chat.session_id, destination)
        source = str(source_record.path)
    else:
        data = synthetic_desktop_session_data(chat)
        destination = unique_desktop_session_path(destination_dir, data["sessionId"].removeprefix("local_"))
        data = desktop_session_data_for_path(data, chat.session_id, destination)
        source = None

    write_desktop_session_json(destination, data)
    if source_record is not None and move and source_record.account_uuid != active_account_uuid:
        backup_path = backup_desktop_session_file(paths, source_record.path)
        source_record.path.unlink()
        removed_source = str(source_record.path)

    return {
        "status": "moved" if move and removed_source else "copied",
        "session_id": chat.session_id,
        "title": data.get("title") or chat.title,
        "source": source,
        "destination": str(destination),
        "active_account": f"Claude app {active_account_uuid[:8]}",
        "active_workspace": active_workspace_uuid,
        "backup": str(backup_path) if backup_path else None,
        "removed_source": removed_source,
        "synthetic": source_record is None,
    }


def transfer_codex_chat_to_active_account(paths: Paths, chat: Chat) -> dict[str, Any]:
    if chat.provider != PROVIDER_CODEX:
        raise ValueError("La task selezionata non appartiene a Codex.")
    if chat.archived:
        raise ValueError("La task Codex e' archiviata: riaprila nell'app Codex prima di associarla.")
    active = active_codex_account(paths)
    if active is None:
        raise ValueError("Account Codex attivo non rilevato.")
    index = load_account_index(paths)
    register_active_codex_account(paths, index)
    sessions = index.setdefault("sessions", {})
    key = chat_account_session_key(chat)
    previous = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
    sessions[key] = {
        **previous,
        "account_key": active.key,
        "label": active.label,
        "provider": PROVIDER_CODEX,
        "session_id": chat.session_id,
        "source": chat.source,
        "title": chat.title,
        "cwd": chat.cwd,
        "first_seen_at": previous.get("first_seen_at") or now_utc(),
        "last_seen_at": now_utc(),
    }
    save_account_index(paths, index)
    return {
        "status": "associated",
        "session_id": chat.session_id,
        "title": chat.title,
        "active_account": active.label,
        "destination": str(chat.jsonl_path),
    }


def desktop_account_fields(account_uuid: str | None, active_account_uuid: str | None) -> dict[str, str | None]:
    if not account_uuid:
        return {"account_key": None, "account_label": None, "account_status": "unknown"}
    key = f"claude-app:{account_uuid}"
    return {
        "account_key": key,
        "account_label": f"Claude app {account_uuid[:8]}",
        "account_status": "active" if account_uuid == active_account_uuid else "other",
    }


def discover_claude_windows_app_sessions(
    paths: Paths,
    sync_accounts: bool = True,
    active_only: bool = False,
) -> list[Chat]:
    if sync_accounts:
        sync_claude_desktop_accounts(paths)
    discovered: list[Chat] = []
    for record in desktop_session_records(paths, active_only=active_only):
        data = record.data
        if data.get("isArchived") is True:
            continue
        cli_session_id = data.get("cliSessionId")
        app_session_id = data.get("sessionId")
        if not isinstance(cli_session_id, str) or not cli_session_id:
            if not isinstance(app_session_id, str) or not app_session_id:
                continue
            cli_session_id = app_session_id.removeprefix("local_")
        title = data.get("title") if isinstance(data.get("title"), str) else None
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
        if not cwd and isinstance(data.get("originCwd"), str):
            cwd = data["originCwd"]
        timestamp_values = [
            value
            for value in [data.get("lastActivityAt"), data.get("lastFocusedAt"), data.get("createdAt")]
            if isinstance(value, (int, float))
        ]
        timestamp = iso_from_epoch_ms(max(timestamp_values)) if timestamp_values else None
        account = desktop_account_fields(record.account_uuid, record.active_account_uuid)
        can_queue = bool(cli_session_id and cwd and chat_cwd_runnable(cwd) and account["account_status"] != "other")
        discovered.append(
            Chat(
                session_id=cli_session_id,
                title=truncate(title, 90) or "Claude app session",
                cwd=cwd,
                permission_mode=data.get("permissionMode") if isinstance(data.get("permissionMode"), str) else None,
                model=real_model_value(data.get("model")),
                jsonl_path=record.path,
                last_timestamp=timestamp or file_mtime_iso(record.path),
                message_count=int(data.get("completedTurns") or 0) if isinstance(data.get("completedTurns"), int) else 0,
                last_prompt=None,
                effort_level=data.get("effort") if isinstance(data.get("effort"), str) else None,
                source="Claude Windows App",
                source_key="claude_windows_app",
                can_queue=can_queue,
                account_key=account["account_key"],
                account_label=account["account_label"],
                account_status=str(account["account_status"] or "unknown"),
            )
        )
    return sorted(discovered, key=chat_sort_key, reverse=True)


def windows_remote_path_from_uri_path(path: str) -> str | None:
    decoded = unquote(path).lstrip("/")
    match = re.match(r"^([a-zA-Z]):/(.*)$", decoded)
    if not match:
        return None
    drive = match.group(1).upper()
    rest = match.group(2).replace("/", "\\")
    return f"{drive}:\\{rest}"


def remote_workspace_from_storage(db_path: Path) -> dict[str, str] | None:
    workspace_file = db_path.parent / "workspace.json"
    data = load_json_file(workspace_file)
    folder = data.get("folder")
    if not isinstance(folder, str) or not folder.startswith("vscode-remote://"):
        return None
    parsed = urlparse(folder)
    authority = unquote(parsed.netloc)
    if not authority.startswith("ssh-remote+"):
        return None
    host = authority.removeprefix("ssh-remote+")
    remote_cwd = windows_remote_path_from_uri_path(parsed.path)
    if not host or not remote_cwd:
        return None
    return {
        "kind": "ssh",
        "host": host,
        "cwd": remote_cwd,
        "uri": folder,
    }


def decode_sqlite_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return None


def agent_session_time_ms(item: dict[str, Any]) -> int | float | None:
    timing = item.get("timing") if isinstance(item.get("timing"), dict) else {}
    values = [
        timing.get("lastRequestEnded"),
        timing.get("lastRequestStarted"),
        timing.get("created"),
    ]
    numeric = [value for value in values if isinstance(value, (int, float))]
    return max(numeric) if numeric else None


def discover_claude_agent_sessions(paths: Paths) -> list[Chat]:
    root = workspace_storage_root(paths)
    if not root.exists():
        return []

    by_session: dict[str, Chat] = {}
    for db_path in root.glob("*/state.vscdb"):
        remote_workspace = remote_workspace_from_storage(db_path)
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = connection.execute(
                    "select value from ItemTable where key = ?",
                    ("agentSessions.model.cache",),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            continue
        if not row:
            continue
        raw = decode_sqlite_value(row[0])
        if not raw:
            continue
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entries, list):
            continue

        for item in entries:
            if not isinstance(item, dict) or item.get("providerType") != "claude-code":
                continue
            resource = item.get("resource")
            if not isinstance(resource, str) or not resource.startswith("claude-code:/"):
                continue
            session_id = resource.removeprefix("claude-code:/").strip("/")
            if not session_id:
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            cwd = metadata.get("workingDirectoryPath") or metadata.get("repositoryPath")
            if not isinstance(cwd, str):
                cwd = None
            remote_kind = None
            remote_host = None
            remote_cwd = None
            remote_uri = None
            source = "Claude Code VS Code"
            source_key = "claude_code_vscode"
            if remote_workspace:
                remote_kind = remote_workspace["kind"]
                remote_host = remote_workspace["host"]
                remote_cwd = remote_workspace["cwd"]
                remote_uri = remote_workspace["uri"]
                cwd = remote_cwd
                source = "Claude Code VS Code Remote SSH"
                source_key = "claude_code_vscode_remote_ssh"
            label = item.get("label")
            title = truncate(label if isinstance(label, str) and label.strip() else None, 90) or "Claude Code session"
            timestamp = iso_from_epoch_ms(agent_session_time_ms(item))
            can_queue = cwd_accessible(cwd) or (remote_kind == "ssh" and bool(remote_host and remote_cwd))
            chat = Chat(
                session_id=session_id,
                title=title,
                cwd=cwd,
                permission_mode=None,
                model=None,
                jsonl_path=db_path,
                last_timestamp=timestamp,
                message_count=0,
                last_prompt=None,
                source=source,
                source_key=source_key,
                can_queue=can_queue,
                remote_kind=remote_kind,
                remote_host=remote_host,
                remote_cwd=remote_cwd,
                remote_uri=remote_uri,
            )
            existing = by_session.get(session_id)
            if existing is None or chat_sort_key(chat) >= chat_sort_key(existing):
                by_session[session_id] = chat

    return sorted(by_session.values(), key=chat_sort_key, reverse=True)


def remote_transcript_summaries(paths: Paths, host: str) -> list[dict[str, Any]]:
    script = r"""
$ErrorActionPreference = "SilentlyContinue"
function Parse-TranscriptTime($value) {
  try {
    return [DateTimeOffset]::Parse([string]$value).UtcDateTime
  } catch {
    return $null
  }
}
$root = Join-Path $env:USERPROFILE ".claude\projects"
if (Test-Path -LiteralPath $root) {
  Get-ChildItem -LiteralPath $root -Recurse -Filter "*.jsonl" -File | ForEach-Object {
    $file = $_
    $sessionId = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)
    $title = $null
    $cwd = $null
    $permissionMode = $null
    $model = $null
    $effortLevel = $null
    $lastPrompt = $null
    $lastEventTimestamp = $null
    $lastEventTime = $null
    $lastMessageTimestamp = $null
    $lastMessageTime = $null
    $messageCount = 0
    foreach ($line in [System.IO.File]::ReadLines($file.FullName)) {
      if ([string]::IsNullOrWhiteSpace($line)) {
        continue
      }
      try {
        $obj = $line | ConvertFrom-Json
      } catch {
        continue
      }
      if ($null -ne $obj.sessionId) {
        $sessionId = [string]$obj.sessionId
      }
      $timestamp = $obj.timestamp
      $timestampTime = $null
      if ($null -ne $timestamp) {
        $timestampText = [string]$timestamp
        $timestampTime = Parse-TranscriptTime $timestampText
        if ($null -eq $lastEventTime -or ($null -ne $timestampTime -and $timestampTime -ge $lastEventTime)) {
          $lastEventTimestamp = $timestampText
          $lastEventTime = $timestampTime
        }
      }
      $objType = [string]$obj.type
      if (($objType -eq "user" -or $objType -eq "assistant") -and $null -ne $timestamp) {
        if ($null -eq $lastMessageTime -or ($null -ne $timestampTime -and $timestampTime -ge $lastMessageTime)) {
          $lastMessageTimestamp = [string]$timestamp
          $lastMessageTime = $timestampTime
        }
      }
      if ($objType -eq "ai-title" -and $null -ne $obj.aiTitle) {
        $title = [string]$obj.aiTitle
      }
      if ($objType -eq "last-prompt" -and $null -ne $obj.lastPrompt) {
        $lastPrompt = [string]$obj.lastPrompt
      }
      if ($null -ne $obj.cwd) {
        $cwd = [string]$obj.cwd
      }
      if ($null -ne $obj.permissionMode) {
        $permissionMode = [string]$obj.permissionMode
      }
      if ($null -ne $obj.effortLevel) {
        $effortLevel = [string]$obj.effortLevel
      }
      if ($null -ne $obj.message) {
        $messageCount += 1
        if ($null -ne $obj.message.model -and [string]$obj.message.model -ne "<synthetic>") {
          $model = [string]$obj.message.model
        }
      }
    }
    $cwdExists = $false
    if ($null -ne $cwd) {
      $cwdExists = Test-Path -LiteralPath $cwd
    }
    $lastTimestamp = $lastMessageTimestamp
    if ($null -eq $lastTimestamp) {
      $lastTimestamp = $lastEventTimestamp
    }
    if ($null -eq $lastTimestamp) {
      $lastTimestamp = $file.LastWriteTimeUtc.ToString("o")
    }
    $summary = [ordered]@{
      session_id = $sessionId
      title = $title
      cwd = $cwd
      permission_mode = $permissionMode
      model = $model
      effort_level = $effortLevel
      jsonl_path = $file.FullName
      last_timestamp = $lastTimestamp
      message_count = $messageCount
      last_prompt = $lastPrompt
      cwd_exists = $cwdExists
    }
    $json = $summary | ConvertTo-Json -Compress -Depth 4
    [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($json))
  }
}
"""
    try:
        result = ssh_run(
            paths,
            host,
            powershell_stdin_script_command(),
            timeout=30,
            input_text=script,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return []
    if result.returncode != 0:
        return []

    summaries: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        token = line.strip()
        if not token:
            continue
        try:
            raw = base64.b64decode(token).decode("utf-8")
            summary = json.loads(raw)
        except Exception:
            continue
        if isinstance(summary, dict):
            summaries.append(summary)
    return summaries


def discover_remote_ssh_chats(paths: Paths, context_chats: list[Chat]) -> list[Chat]:
    contexts_by_host: dict[str, dict[str, Chat]] = {}
    for chat in context_chats:
        if chat.remote_kind != "ssh" or not chat.remote_host:
            continue
        contexts_by_host.setdefault(chat.remote_host, {})[chat.session_id] = chat

    discovered: list[Chat] = []
    for host, contexts in contexts_by_host.items():
        for summary in remote_transcript_summaries(paths, host):
            session_id = summary.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            context = contexts.get(session_id)
            summary_title = summary.get("title") if isinstance(summary.get("title"), str) else None
            summary_cwd = summary.get("cwd") if isinstance(summary.get("cwd"), str) else None
            summary_cwd_exists = bool(summary.get("cwd_exists"))
            last_prompt = summary.get("last_prompt") if isinstance(summary.get("last_prompt"), str) else None
            cwd = context.cwd if context and context.cwd else summary_cwd
            display_title = (
                context.title
                if context and context.title
                else summary_title or truncate(last_prompt, 60) or (Path(summary_cwd).name if summary_cwd else "Claude chat")
            )
            remote_cwd = context.remote_cwd if context and context.remote_cwd else summary_cwd
            discovered.append(
                Chat(
                    session_id=session_id,
                    title=display_title,
                    cwd=cwd,
                    permission_mode=summary.get("permission_mode") if isinstance(summary.get("permission_mode"), str) else None,
                    model=summary.get("model") if isinstance(summary.get("model"), str) else None,
                    jsonl_path=Path(str(summary.get("jsonl_path") or session_id)),
                    last_timestamp=summary.get("last_timestamp") if isinstance(summary.get("last_timestamp"), str) else None,
                    message_count=int(summary.get("message_count") or 0),
                    last_prompt=last_prompt,
                    effort_level=summary.get("effort_level") if isinstance(summary.get("effort_level"), str) else None,
                    source="Claude Code Remote SSH",
                    source_key="claude_code_remote_ssh",
                    can_queue=bool(context and context.remote_host and context.remote_cwd) or summary_cwd_exists,
                    remote_kind="ssh",
                    remote_host=host,
                    remote_cwd=remote_cwd,
                    remote_uri=context.remote_uri if context else None,
                )
            )
    return sorted(discovered, key=chat_sort_key, reverse=True)


def merge_claude_chat_sources(local_chats: list[Chat], vscode_chats: list[Chat]) -> list[Chat]:
    by_session = {chat.session_id: chat for chat in local_chats}
    for chat in vscode_chats:
        existing = by_session.get(chat.session_id)
        if existing is None:
            by_session[chat.session_id] = chat
            continue
        if existing.account_status == "active" and chat.account_status == "other":
            continue
        existing_key = chat_sort_key(existing)
        cache_key = chat_sort_key(chat)
        prefer_metadata = chat.source_key != "claude_code" or bool(chat.remote_kind)
        prefer_cache_context = bool(chat.remote_kind) or (
            chat.source_key == "claude_windows_app" and bool(chat.cwd)
        ) or not chat_cwd_runnable(existing.cwd)
        cwd = chat.cwd if prefer_cache_context and chat.cwd else existing.cwd
        use_newer_timestamp = cache_key >= existing_key
        by_session[chat.session_id] = Chat(
            session_id=existing.session_id,
            title=chat.title if prefer_metadata and chat.title else existing.title,
            cwd=cwd,
            permission_mode=chat.permission_mode or existing.permission_mode,
            model=chat.model or existing.model,
            jsonl_path=existing.jsonl_path,
            last_timestamp=chat.last_timestamp if use_newer_timestamp else existing.last_timestamp,
            message_count=max(existing.message_count, chat.message_count),
            last_prompt=existing.last_prompt,
            effort_level=chat.effort_level or existing.effort_level,
            source=chat.source if prefer_metadata else existing.source,
            source_key=chat.source_key if prefer_metadata else existing.source_key,
            can_queue=(chat.can_queue or chat_cwd_runnable(cwd) or bool(chat.remote_kind)) and chat.account_status != "other",
            remote_kind=chat.remote_kind,
            remote_host=chat.remote_host,
            remote_cwd=chat.remote_cwd,
            remote_uri=chat.remote_uri,
            account_key=chat.account_key or existing.account_key,
            account_label=chat.account_label or existing.account_label,
            account_status=chat.account_status if chat.account_key else existing.account_status,
        )
    return sorted(by_session.values(), key=chat_sort_key, reverse=True)


def annotate_chats_with_accounts(paths: Paths, chats: list[Chat]) -> list[Chat]:
    index = load_account_index(paths)
    active = register_active_account(paths, index)
    sessions = index.setdefault("sessions", {})
    accounts = index.setdefault("accounts", {})
    changed = False
    annotated: list[Chat] = []

    for chat in chats:
        if chat.account_key:
            annotated.append(dataclasses.replace(chat, can_queue=chat.can_queue and chat.account_status != "other"))
            continue

        session_key = chat_account_session_key(chat)
        session_record = sessions.get(session_key) if isinstance(sessions.get(session_key), dict) else {}
        account_key = session_record.get("account_key") if isinstance(session_record.get("account_key"), str) else None

        if account_key is None and active is not None and chat.remote_kind is None and chat_recent_for_account(chat, active):
            account_key = active.key
            sessions[session_key] = {
                "account_key": active.key,
                "label": active.label,
                "session_id": chat.session_id,
                "source": chat.source,
                "title": chat.title,
                "cwd": chat.cwd,
                "first_seen_at": now_utc(),
                "last_seen_at": now_utc(),
            }
            changed = True
        elif account_key is not None:
            session_record["last_seen_at"] = now_utc()
            changed = True

        account_label = None
        account_status = "unknown"
        if account_key:
            account_data = accounts.get(account_key) if isinstance(accounts.get(account_key), dict) else {}
            account_label = (
                session_record.get("label")
                if isinstance(session_record.get("label"), str)
                else account_data.get("label") if isinstance(account_data.get("label"), str) else f"Account {account_key[:8]}"
            )
            if active is None:
                account_status = "known"
            elif account_key == active.key:
                account_status = "active"
            else:
                account_status = "other"

        can_queue = chat.can_queue and account_status != "other"
        annotated.append(
            dataclasses.replace(
                chat,
                can_queue=can_queue,
                account_key=account_key,
                account_label=account_label,
                account_status=account_status,
            )
        )

    if changed:
        save_account_index(paths, index)
    return annotated


def annotate_codex_chats_with_accounts(paths: Paths, chats: list[Chat]) -> list[Chat]:
    index = load_account_index(paths)
    active = register_active_codex_account(paths, index)
    sessions = index.setdefault("sessions", {})
    accounts = index.setdefault("accounts", {})
    changed = False
    annotated: list[Chat] = []

    for chat in chats:
        session_key = chat_account_session_key(chat)
        session_record = sessions.get(session_key) if isinstance(sessions.get(session_key), dict) else {}
        account_key = session_record.get("account_key") if isinstance(session_record.get("account_key"), str) else None

        if account_key is None and active is not None and chat_recent_for_account(chat, active):
            account_key = active.key
            session_record = {
                "account_key": active.key,
                "label": active.label,
                "provider": PROVIDER_CODEX,
                "session_id": chat.session_id,
                "source": chat.source,
                "title": chat.title,
                "cwd": chat.cwd,
                "first_seen_at": now_utc(),
                "last_seen_at": now_utc(),
            }
            sessions[session_key] = session_record
            changed = True
        elif account_key is not None:
            session_record["last_seen_at"] = now_utc()
            sessions[session_key] = session_record
            changed = True

        account_label = None
        account_status = "unknown"
        if account_key:
            account_data = accounts.get(account_key) if isinstance(accounts.get(account_key), dict) else {}
            account_label = (
                session_record.get("label")
                if isinstance(session_record.get("label"), str)
                else account_data.get("label") if isinstance(account_data.get("label"), str) else f"Codex {account_key[-8:]}"
            )
            if active is None:
                account_status = "known"
            elif account_key == active.key:
                account_status = "active"
            else:
                account_status = "other"

        annotated.append(
            dataclasses.replace(
                chat,
                can_queue=chat.can_queue and account_status != "other",
                account_key=account_key,
                account_label=account_label,
                account_status=account_status,
            )
        )

    if changed:
        save_account_index(paths, index)
    return annotated


def discover_claude_chats(
    paths: Paths,
    sync_desktop_accounts: bool = True,
    active_desktop_only: bool = False,
) -> list[Chat]:
    agent_chats = discover_claude_agent_sessions(paths)
    desktop_chats = discover_claude_windows_app_sessions(
        paths,
        sync_accounts=sync_desktop_accounts,
        active_only=active_desktop_only,
    )
    transcript_chats = discover_chats(paths.claude_home) + discover_remote_ssh_chats(paths, agent_chats)
    return annotate_chats_with_accounts(paths, merge_claude_chat_sources(transcript_chats, agent_chats + desktop_chats))


def discover_agent_chats(
    paths: Paths,
    sync_desktop_accounts: bool = True,
    active_desktop_only: bool = False,
) -> list[Chat]:
    claude_chats = discover_claude_chats(paths, sync_desktop_accounts, active_desktop_only)
    codex_chats = annotate_codex_chats_with_accounts(paths, discover_codex_app_sessions(paths))
    return sorted(claude_chats + codex_chats, key=chat_sort_key, reverse=True)


def remember_chat_account(paths: Paths, chat: Chat) -> Chat:
    if chat.remote_kind is not None or chat.account_key:
        return chat
    active = active_codex_account(paths) if chat.provider == PROVIDER_CODEX else active_claude_account(paths)
    if active is None:
        return chat
    index = load_account_index(paths)
    if chat.provider == PROVIDER_CODEX:
        register_active_codex_account(paths, index)
    else:
        register_active_account(paths, index)
    sessions = index.setdefault("sessions", {})
    sessions[chat_account_session_key(chat)] = {
        "account_key": active.key,
        "label": active.label,
        "provider": chat.provider,
        "session_id": chat.session_id,
        "source": chat.source,
        "title": chat.title,
        "cwd": chat.cwd,
        "first_seen_at": sessions.get(chat_account_session_key(chat), {}).get("first_seen_at", now_utc())
        if isinstance(sessions.get(chat_account_session_key(chat)), dict)
        else now_utc(),
        "last_seen_at": now_utc(),
    }
    save_account_index(paths, index)
    return dataclasses.replace(chat, account_key=active.key, account_label=active.label, account_status="active")


def account_mismatch_message(
    expected_key: str | None,
    expected_label: str | None,
    active: AccountInfo | None,
    provider_label: str = "Claude",
) -> str | None:
    if not expected_key:
        return None
    if active is None:
        return f"Account {provider_label} attivo non rilevato: non invio per non usare una sessione con credenziali sbagliate."
    if expected_key != active.key:
        expected = expected_label or f"Account {expected_key[:8]}"
        return f"Chat associata a {expected}; account attivo: {active.label}. Cambia account {provider_label} o riassocia la chat prima di inviare."
    return None


def desktop_account_mismatch_message(paths: Paths, expected_key: str | None, expected_label: str | None) -> str | None:
    if not expected_key:
        return None
    active_key, active_label = active_desktop_account_key(paths)
    if active_key is None:
        return "Account Claude app attivo non rilevato: non invio per non usare una sessione con credenziali sbagliate."
    if expected_key != active_key:
        expected = expected_label or f"Claude app {expected_key.removeprefix('claude-app:')[:8]}"
        return f"Chat associata a {expected}; account attivo: {active_label}. Cambia account nella app Claude prima di inviare."
    return None


def account_mismatch_for_chat(paths: Paths, chat: Chat) -> str | None:
    if chat.remote_kind is not None:
        return None
    if chat.provider == PROVIDER_CODEX:
        return account_mismatch_message(chat.account_key, chat.account_label, active_codex_account(paths), "Codex")
    if chat.account_key and chat.account_key.startswith("claude-app:"):
        return desktop_account_mismatch_message(paths, chat.account_key, chat.account_label)
    return account_mismatch_message(chat.account_key, chat.account_label, active_claude_account(paths), "Claude")


def account_mismatch_for_item(paths: Paths, item: dict[str, Any]) -> str | None:
    if item.get("remote_kind") is not None:
        return None
    expected_key = item.get("account_key") if isinstance(item.get("account_key"), str) else None
    expected_label = item.get("account_label") if isinstance(item.get("account_label"), str) else None
    if item.get("provider") == PROVIDER_CODEX:
        return account_mismatch_message(expected_key, expected_label, active_codex_account(paths), "Codex")
    if expected_key and expected_key.startswith("claude-app:"):
        return desktop_account_mismatch_message(paths, expected_key, expected_label)
    return account_mismatch_message(expected_key, expected_label, active_claude_account(paths), "Claude")


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_settings(paths: list[Path]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in paths:
        if not path.exists():
            continue
        data = load_json_file(path)
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def parents_for_settings(cwd: str | None) -> list[Path]:
    if not cwd:
        return []
    local = windows_to_local_path(cwd)
    if not local.is_absolute():
        local = Path.cwd() / local
    parents = [local]
    parents.extend(local.parents)
    result: list[Path] = []
    for parent in parents:
        result.append(parent)
        raw = str(parent)
        if os.name != "nt" and re.fullmatch(r"/mnt/[a-zA-Z]", raw):
            break
        if os.name == "nt" and parent.parent == parent:
            break
        if raw in {"/", ""}:
            break
    return result


def candidate_settings_files(paths: Paths, cwd: str | None) -> list[Path]:
    candidates = [
        paths.claude_home / "settings.json",
        paths.claude_home / "settings.local.json",
        paths.claude_home / "policy-limits.json",
    ]
    for parent in parents_for_settings(cwd):
        candidates.extend(
            [
                parent / ".claude" / "settings.json",
                parent / ".claude" / "settings.local.json",
                parent / ".mcp.json",
                parent / "CLAUDE.md",
            ]
        )
    return unique_paths(candidates)


def candidate_codex_settings_files(paths: Paths, cwd: str | None) -> list[Path]:
    candidates = [paths.codex_home / "config.toml", paths.codex_home / "AGENTS.md"]
    for parent in parents_for_settings(cwd):
        candidates.extend([parent / ".codex" / "config.toml", parent / "AGENTS.md"])
    return unique_paths(candidates)


def sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def codex_settings_fingerprint(paths: Paths, chat: Chat) -> dict[str, Any]:
    setting_paths = candidate_codex_settings_files(paths, chat.cwd)
    files = []
    for path in setting_paths:
        exists = path.exists()
        files.append(
            {
                "path": str(path),
                "exists": exists,
                "sha256": sha256_file(path) if exists else None,
            }
        )
    return {
        "created_at": now_utc(),
        "provider": PROVIDER_CODEX,
        "session_id": chat.session_id,
        "cwd": chat.cwd,
        "jsonl_path": str(chat.jsonl_path),
        "account": {
            "key": chat.account_key,
            "label": chat.account_label,
            "status": chat.account_status,
        },
        "effective": {
            "model": real_model_value(chat.model),
            "effortLevel": chat.effort_level,
            "sandboxMode": chat.sandbox_mode or chat.permission_mode,
            "approvalPolicy": chat.approval_policy,
        },
        "files": files,
    }


def settings_fingerprint(paths: Paths, chat: Chat) -> dict[str, Any]:
    if chat.provider == PROVIDER_CODEX:
        return codex_settings_fingerprint(paths, chat)
    if chat.remote_kind == "ssh" and chat.remote_host and chat.remote_cwd:
        return remote_settings_fingerprint(paths, chat)

    files = []
    setting_paths = candidate_settings_files(paths, chat.cwd)
    for path in setting_paths:
        exists = path.exists()
        files.append(
            {
                "path": str(path),
                "exists": exists,
                "sha256": sha256_file(path) if exists else None,
            }
        )

    merged = merge_settings([path for path in setting_paths if path.suffix == ".json"])
    permissions = merged.get("permissions") if isinstance(merged.get("permissions"), dict) else {}
    permission_mode = chat.permission_mode or permissions.get("defaultMode")
    if permission_mode not in VALID_PERMISSION_MODES:
        permission_mode = None
    model = merged.get("model") or real_model_value(chat.model)
    effort_level = merged.get("effortLevel") or chat.effort_level

    return {
        "created_at": now_utc(),
        "provider": PROVIDER_CLAUDE,
        "session_id": chat.session_id,
        "cwd": chat.cwd,
        "jsonl_path": str(chat.jsonl_path),
        "account": {
            "key": chat.account_key,
            "label": chat.account_label,
            "status": chat.account_status,
        },
        "effective": {
            "model": model,
            "effortLevel": effort_level,
            "permissionMode": permission_mode,
        },
        "files": files,
    }


def compare_fingerprints(saved: dict[str, Any], current: dict[str, Any]) -> list[str]:
    diffs: list[str] = []
    saved_provider = saved.get("provider") or PROVIDER_CLAUDE
    current_provider = current.get("provider") or PROVIDER_CLAUDE
    if saved_provider != current_provider:
        diffs.append(f"provider diverso: {saved_provider} -> {current_provider}")
    if saved.get("session_id") != current.get("session_id"):
        diffs.append("session_id diverso")
    if saved.get("cwd") != current.get("cwd"):
        diffs.append(f"cwd diverso: {saved.get('cwd')} -> {current.get('cwd')}")
    if saved.get("remote") != current.get("remote"):
        diffs.append("contesto remoto diverso")
    saved_account = saved.get("account") if isinstance(saved.get("account"), dict) else {}
    current_account = current.get("account") if isinstance(current.get("account"), dict) else {}
    if saved_account.get("key") and current_account.get("key") and saved_account.get("key") != current_account.get("key"):
        diffs.append("account diverso")
    if saved.get("effective") != current.get("effective"):
        diffs.append("impostazioni effettive diverse")

    saved_files = {entry["path"]: entry for entry in saved.get("files", []) if "path" in entry}
    current_files = {entry["path"]: entry for entry in current.get("files", []) if "path" in entry}
    for path, before in saved_files.items():
        after = current_files.get(path)
        if after is None:
            diffs.append(f"file impostazioni non piu' tracciato: {path}")
            continue
        if before.get("exists") != after.get("exists"):
            diffs.append(f"presenza file cambiata: {path}")
        elif before.get("sha256") != after.get("sha256"):
            diffs.append(f"contenuto file cambiato: {path}")
    for path in sorted(set(current_files) - set(saved_files)):
        entry = current_files[path]
        if entry.get("exists"):
            diffs.append(f"nuovo file impostazioni rilevato: {path}")
    return diffs


def parse_ssh_config(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1].strip()
        if key == "host":
            current = {"patterns": value.split(), "_config_path": path}
            entries.append(current)
            continue
        if current is not None:
            current[key] = value
    return entries


def ssh_config_entries(paths: Paths) -> list[dict[str, Any]]:
    return parse_ssh_config(paths.windows_home / ".ssh" / "config") + parse_ssh_config(Path.home() / ".ssh" / "config")


def resolve_identity_file(raw: str, config_path: Path, paths: Paths) -> Path:
    value = raw.strip().strip('"')
    if value.startswith("~/") or value.startswith("~\\"):
        home = paths.windows_home if str(config_path).startswith(str(paths.windows_home)) else Path.home()
        return home / value[2:].replace("\\", "/")
    return windows_to_local_path(value)


def local_private_key_for_ssh(identity_file: Path | None, host: str) -> Path | None:
    if identity_file is None or not identity_file.exists():
        return None
    target_dir = Path.home() / APP_DIR_NAME / "ssh"
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(identity_file).encode("utf-8")).hexdigest()[:12]
    target = target_dir / f"{host}-{digest}.key"
    try:
        if not target.exists() or sha256_file(target) != sha256_file(identity_file):
            shutil.copyfile(identity_file, target)
        target.chmod(0o600)
    except OSError:
        return identity_file
    return target


def ssh_base_command(paths: Paths, host: str) -> list[str]:
    hostname = host
    user: str | None = None
    port: str | None = None
    identity_file: Path | None = None
    for entry in ssh_config_entries(paths):
        patterns = entry.get("patterns", [])
        if host not in patterns and "*" not in patterns:
            continue
        hostname = str(entry.get("hostname") or hostname)
        user = str(entry.get("user") or user) if entry.get("user") else user
        port = str(entry.get("port") or port) if entry.get("port") else port
        if entry.get("identityfile"):
            identity_file = resolve_identity_file(str(entry["identityfile"]), entry["_config_path"], paths)
        break

    destination = f"{user}@{hostname}" if user else hostname
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if port:
        command.extend(["-p", port])
    local_key = local_private_key_for_ssh(identity_file, host)
    if local_key:
        command.extend(["-i", str(local_key)])
    command.append(destination)
    return command


def ssh_run(
    paths: Paths,
    host: str,
    command: str,
    timeout: int,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ssh_base_command(paths, host) + [command],
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )


def powershell_encoded_command(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def powershell_stdin_script_command() -> str:
    script = r"""
$p = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), [System.IO.Path]::GetRandomFileName() + ".ps1")
[System.IO.File]::WriteAllText($p, [Console]::In.ReadToEnd(), [System.Text.Encoding]::UTF8)
try {
  & $p
  $code = $LASTEXITCODE
} finally {
  Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
}
if ($null -eq $code) {
  exit 0
}
exit $code
"""
    return powershell_encoded_command(script)


def remote_windows_user_home(remote_cwd: str) -> str | None:
    match = re.match(r"^([a-zA-Z]:\\Users\\[^\\]+)", remote_cwd, flags=re.IGNORECASE)
    return match.group(1) if match else None


def remote_windows_parents(remote_cwd: str) -> list[str]:
    parts = remote_cwd.replace("/", "\\").rstrip("\\").split("\\")
    if not parts:
        return []
    parents: list[str] = []
    for end in range(len(parts), 0, -1):
        value = "\\".join(parts[:end])
        if value and value not in parents:
            parents.append(value)
        if re.fullmatch(r"[a-zA-Z]:", value):
            break
    return parents


def remote_candidate_settings_files(remote_cwd: str) -> list[str]:
    candidates: list[str] = []
    home = remote_windows_user_home(remote_cwd)
    if home:
        candidates.extend(
            [
                home + "\\.claude\\settings.json",
                home + "\\.claude\\settings.local.json",
                home + "\\.claude\\policy-limits.json",
            ]
        )
    for parent in remote_windows_parents(remote_cwd):
        candidates.extend(
            [
                parent + "\\.claude\\settings.json",
                parent + "\\.claude\\settings.local.json",
                parent + "\\.mcp.json",
                parent + "\\CLAUDE.md",
            ]
        )
    result: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        key = path.lower()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def powershell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def claude_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in CLAUDE_EXTERNAL_AUTH_ENV_VARS:
        env.pop(name, None)
    return env


def codex_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in CODEX_EXTERNAL_AUTH_ENV_VARS:
        env.pop(name, None)
    return env


def clear_claude_external_auth_powershell() -> str:
    names = ", ".join(powershell_single_quote(name) for name in sorted(CLAUDE_EXTERNAL_AUTH_ENV_VARS))
    return f"""
foreach ($name in @({names})) {{
  Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
}}
"""


def remote_file_fingerprints(paths: Paths, host: str, files: list[str]) -> list[dict[str, Any]]:
    if not files:
        return []
    script_paths = ", ".join(powershell_single_quote(path) for path in files)
    script = f"""
$paths = @({script_paths})
foreach ($p in $paths) {{
  if (Test-Path -LiteralPath $p -PathType Leaf) {{
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLowerInvariant()
    Write-Output ($p + "|1|" + $hash)
  }} else {{
    Write-Output ($p + "|0|")
  }}
}}
"""
    try:
        result = ssh_run(paths, host, powershell_encoded_command(script), timeout=30)
    except (OSError, subprocess.SubprocessError):
        result = None
    by_path: dict[str, dict[str, Any]] = {}
    if result and result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.rstrip("\r").split("|", 2)
            if len(parts) < 2:
                continue
            path = parts[0]
            exists = parts[1] == "1"
            by_path[path.lower()] = {
                "path": path,
                "exists": exists,
                "sha256": parts[2] if exists and len(parts) == 3 else None,
            }
    return [by_path.get(path.lower(), {"path": path, "exists": False, "sha256": None}) for path in files]


def remote_effective_settings(paths: Paths, host: str, files: list[str]) -> dict[str, Any]:
    json_files = [path for path in files if path.lower().endswith(".json")]
    if not host or not json_files:
        return {}

    script_paths = ", ".join(powershell_single_quote(path) for path in json_files)
    script = f"""
$paths = @({script_paths})
$merged = [ordered]@{{}}
foreach ($p in $paths) {{
  if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {{
    continue
  }}
  try {{
    $data = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
  }} catch {{
    continue
  }}
  if ($null -ne $data.model) {{
    $merged["model"] = [string]$data.model
  }}
  if ($null -ne $data.effortLevel) {{
    $merged["effortLevel"] = [string]$data.effortLevel
  }}
  if ($null -ne $data.permissionMode) {{
    $merged["permissionMode"] = [string]$data.permissionMode
  }}
  if ($null -ne $data.permissions -and $null -ne $data.permissions.defaultMode) {{
    $merged["permissionMode"] = [string]$data.permissions.defaultMode
  }}
}}
$merged | ConvertTo-Json -Compress
"""
    try:
        result = ssh_run(paths, host, powershell_encoded_command(script), timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    try:
        values = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return values if isinstance(values, dict) else {}


def remote_settings_fingerprint(paths: Paths, chat: Chat) -> dict[str, Any]:
    host = chat.remote_host or ""
    candidate_files = remote_candidate_settings_files(chat.remote_cwd or "")
    files = remote_file_fingerprints(paths, host, candidate_files)
    effective_settings = remote_effective_settings(paths, host, candidate_files)
    permission_mode = chat.permission_mode or effective_settings.get("permissionMode")
    if permission_mode not in VALID_PERMISSION_MODES:
        permission_mode = None
    return {
        "created_at": now_utc(),
        "session_id": chat.session_id,
        "cwd": chat.cwd,
        "remote": {
            "kind": chat.remote_kind,
            "host": chat.remote_host,
            "cwd": chat.remote_cwd,
            "uri": chat.remote_uri,
        },
        "jsonl_path": str(chat.jsonl_path),
        "effective": {
            "model": effective_settings.get("model") or real_model_value(chat.model),
            "effortLevel": effective_settings.get("effortLevel") or chat.effort_level,
            "permissionMode": permission_mode,
        },
        "files": files,
    }


def load_queue(queue_file: Path) -> dict[str, Any]:
    if not queue_file.exists():
        return {"version": QUEUE_VERSION, "items": [], "recovery": None, "auto_continue": None}
    try:
        data = json.loads(queue_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": QUEUE_VERSION, "items": [], "recovery": None, "auto_continue": None}
    if not isinstance(data, dict):
        return {"version": QUEUE_VERSION, "items": [], "recovery": None, "auto_continue": None}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    data.setdefault("version", QUEUE_VERSION)
    data.setdefault("recovery", None)
    data.setdefault("auto_continue", None)
    return data


def save_queue(queue_file: Path, data: dict[str, Any]) -> None:
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=queue_file.parent, delete=False) as handle:
        handle.write(payload)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(queue_file)


def select_chat(selector: str | None, chats: list[Chat]) -> Chat:
    if not chats:
        raise SystemExit("Nessuna chat Claude o task Codex trovata.")
    if selector is None:
        print_chat_list(chats, limit=30)
        selector = input("Scegli numero, session id, titolo o cwd: ").strip()
    selector = selector.strip()
    if selector.isdigit():
        index = int(selector) - 1
        if 0 <= index < len(chats):
            return chats[index]
    matches = [
        chat
        for chat in chats
        if selector.lower() in chat.session_id.lower()
        or selector.lower() in chat.title.lower()
        or (chat.cwd and selector.lower() in chat.cwd.lower())
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"Chat non trovata: {selector}")
    print_chat_list(matches, limit=len(matches))
    raise SystemExit("Selector ambiguo: usa un numero o piu' caratteri del session id.")


def print_chat_list(chats: list[Chat], limit: int) -> None:
    for index, chat in enumerate(chats[:limit], start=1):
        timestamp = chat.last_timestamp or "no timestamp"
        account = chat.account_label or chat.account_status
        print(
            f"{index:>2}. {chat.session_id[:8]}  {timestamp[:19]}  "
            f"{truncate(chat.title, 42):<42}  {chat.source:<18}  "
            f"{'queue' if chat.can_queue else 'view-only':<9}  {truncate(account, 24):<24}  {truncate(chat.cwd, 70)}"
        )


def expand_items(items: list[str]) -> list[str]:
    if not items:
        return read_interactive_messages()
    prompts: list[str] = []
    for item in items:
        if item == "-":
            prompts.append(sys.stdin.read())
        elif item.startswith("@"):
            path = Path(item[1:]).expanduser()
            prompts.append(path.read_text(encoding="utf-8"))
        else:
            prompts.append(item)
    return [prompt for prompt in prompts if prompt.strip()]


def read_interactive_messages() -> list[str]:
    if not sys.stdin.isatty():
        text = sys.stdin.read()
        return [text] if text.strip() else []

    print("Incolla i messaggi. Riga con solo --- chiude un messaggio; riga con solo .fine termina.")
    prompts: list[str] = []
    buffer: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        marker = line.strip()
        if marker == ".fine":
            break
        if marker == "---":
            text = "\n".join(buffer).strip()
            if text:
                prompts.append(text)
            buffer = []
        else:
            buffer.append(line)
    text = "\n".join(buffer).strip()
    if text:
        prompts.append(text)
    return prompts


def is_rate_limit_text(text: str) -> bool:
    lower = text.lower()
    return any(re.search(pattern, lower) for pattern in RATE_LIMIT_PATTERNS)


def is_permission_wait_text(text: str) -> bool:
    lower = text.lower()
    return any(re.search(pattern, lower) for pattern in PERMISSION_WAIT_PATTERNS)


def parse_reset_time(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    now = now or dt.datetime.now().astimezone()
    lower = text.lower()

    relative = re.search(
        r"\bin\s+(?:(\d+)\s*(?:h|hr|hrs|hour|hours|ore?))?\s*(?:(\d+)\s*(?:m|min|mins|minute|minutes|minuti?))?",
        lower,
    )
    if relative and (relative.group(1) or relative.group(2)):
        hours = int(relative.group(1) or 0)
        minutes = int(relative.group(2) or 0)
        return now + dt.timedelta(hours=hours, minutes=minutes)

    clock = re.search(
        r"\b(?:at|alle|ore|resets?|reset(?:s)?(?:\s+at)?|retry(?:\s+at)?|try again(?:\s+at)?)\s+"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
        lower,
    )
    if clock:
        hour = int(clock.group(1))
        minute = int(clock.group(2) or 0)
        suffix = clock.group(3)
        if suffix == "pm" and hour < 12:
            hour += 12
        if suffix == "am" and hour == 12:
            hour = 0
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
        return candidate

    iso = re.search(r"\b(20\d\d-\d\d-\d\d[t\s]\d\d:\d\d(?::\d\d)?(?:z|[+-]\d\d:\d\d)?)\b", lower)
    if iso:
        return parse_iso(iso.group(1).replace(" ", "T"))
    return None


def build_claude_arguments(item: dict[str, Any], use_ide: bool) -> list[str]:
    command = ["-p", "--output-format", "text"]
    effective = item.get("fingerprint", {}).get("effective", {})
    if isinstance(effective, dict):
        model = effective.get("model")
        effort = effective.get("effortLevel")
        permission_mode = effective.get("permissionMode")
        model_override = item.get("model_override")
        effort_override = item.get("effort_level_override")
        permission_override = item.get("permission_mode_override")
        if isinstance(model_override, str) and model_override.strip():
            model = model_override.strip()
        if isinstance(effort_override, str) and effort_override.strip():
            effort = effort_override.strip()
        if isinstance(permission_override, str) and permission_override in VALID_PERMISSION_MODES:
            permission_mode = permission_override
        if isinstance(model, str) and model:
            command.extend(["--model", model])
        if isinstance(effort, str) and effort:
            command.extend(["--effort", effort])
        if isinstance(permission_mode, str) and permission_mode in VALID_PERMISSION_MODES:
            command.extend(["--permission-mode", permission_mode])
    if use_ide:
        command.append("--ide")
    command.extend(["--resume", item["session_id"]])
    return command


def toml_string(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    return json.dumps(value, ensure_ascii=True)


def build_codex_arguments(item: dict[str, Any]) -> list[str]:
    command = ["exec", "resume", "--json", "--skip-git-repo-check"]
    effective = item.get("fingerprint", {}).get("effective", {})
    if not isinstance(effective, dict):
        effective = {}
    model = item.get("model_override") or effective.get("model")
    effort = item.get("effort_level_override") or effective.get("effortLevel")
    sandbox = item.get("sandbox_mode_override") or effective.get("sandboxMode")
    approval = item.get("approval_policy_override") or effective.get("approvalPolicy")
    if isinstance(model, str) and model.strip():
        command.extend(["--model", model.strip()])
    if isinstance(effort, str) and effort.strip():
        command.extend(["-c", f"model_reasoning_effort={toml_string(effort.strip())}"])
    if isinstance(sandbox, str) and sandbox in VALID_CODEX_SANDBOX_MODES:
        command.extend(["-c", f"sandbox_mode={toml_string(sandbox)}"])
    if isinstance(approval, str) and approval in VALID_CODEX_APPROVAL_POLICIES:
        command.extend(["-c", f"approval_policy={toml_string(approval)}"])
    command.extend([item["session_id"], "-"])
    return command


def selected_setting_overrides(values: dict[str, Any]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    model = values.get("model_override")
    effort = values.get("effort_level_override")
    permission = values.get("permission_mode_override")
    sandbox = values.get("sandbox_mode_override")
    approval = values.get("approval_policy_override")
    if isinstance(model, str) and model.strip():
        overrides["model_override"] = model.strip()
    if isinstance(effort, str) and effort.strip():
        overrides["effort_level_override"] = effort.strip()
    if isinstance(permission, str) and permission in VALID_PERMISSION_MODES:
        overrides["permission_mode_override"] = permission
    if isinstance(sandbox, str) and sandbox in VALID_CODEX_SANDBOX_MODES:
        overrides["sandbox_mode_override"] = sandbox
    if isinstance(approval, str) and approval in VALID_CODEX_APPROVAL_POLICIES:
        overrides["approval_policy_override"] = approval
    return overrides


def item_setting_overrides(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_override": item.get("model_override"),
        "effort_level_override": item.get("effort_level_override"),
        "permission_mode_override": item.get("permission_mode_override"),
        "sandbox_mode_override": item.get("sandbox_mode_override"),
        "approval_policy_override": item.get("approval_policy_override"),
    }


def item_account_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_key": item.get("account_key"),
        "account_label": item.get("account_label"),
    }


def build_claude_command(claude_exe: Path, item: dict[str, Any], use_ide: bool) -> list[str]:
    return [str(claude_exe)] + build_claude_arguments(item, use_ide)


def cwd_for_subprocess(cwd: str | None, fallback: str | None = None) -> Path:
    if not cwd:
        if fallback:
            fallback_local = windows_to_local_path(fallback)
            if fallback_local.exists():
                return fallback_local
        raise SystemExit("La chat non ha un cwd salvato; non invio per non cambiare contesto.")
    local = windows_to_local_path(cwd)
    if local.exists():
        return local
    if fallback:
        fallback_local = windows_to_local_path(fallback)
        if fallback_local.exists():
            return fallback_local
    raise SystemExit(f"Cwd della chat non accessibile: {cwd}")


def windows_cmd_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def remote_find_claude_executable(paths: Paths, host: str) -> str:
    script = r"""
$roots = @("$env:USERPROFILE\.vscode-server\extensions", "$env:USERPROFILE\.vscode\extensions")
$found = @()
foreach ($root in $roots) {
  if (Test-Path -LiteralPath $root) {
    $found += Get-ChildItem -LiteralPath $root -Directory -Filter "anthropic.claude-code-*" |
      ForEach-Object { Join-Path $_.FullName "resources\native-binary\claude.exe" } |
      Where-Object { Test-Path -LiteralPath $_ }
  }
}
$found | Sort-Object | Select-Object -Last 1
"""
    result = ssh_run(paths, host, powershell_encoded_command(script), timeout=30)
    if result.returncode != 0:
        raise SystemExit(truncate(result.stderr or result.stdout, 500) or f"Claude remoto non trovato su {host}")
    executable = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    if not executable:
        raise SystemExit(f"Claude remoto non trovato su {host}")
    return executable


def run_claude_remote_ssh(
    paths: Paths,
    item: dict[str, Any],
    timeout: int,
    use_ide: bool,
) -> ClaudeRunResult:
    host = item.get("remote_host")
    cwd = item.get("remote_cwd") or item.get("cwd")
    if not isinstance(host, str) or not host:
        raise SystemExit("Host remoto mancante per chat Remote SSH.")
    if not isinstance(cwd, str) or not cwd:
        raise SystemExit("Cwd remoto mancante per chat Remote SSH.")
    claude_exe = item.get("remote_claude_exe")
    if not isinstance(claude_exe, str) or not claude_exe:
        claude_exe = remote_find_claude_executable(paths, host)
        item["remote_claude_exe"] = claude_exe
    args = " ".join(windows_cmd_quote(arg) for arg in build_claude_arguments(item, use_ide))
    clear_auth = " && ".join(f"set {name}=" for name in sorted(CLAUDE_EXTERNAL_AUTH_ENV_VARS))
    command = f"{clear_auth} && cd /d {windows_cmd_quote(cwd)} && {windows_cmd_quote(claude_exe)} {args}"
    result = ssh_run(paths, host, command, timeout=timeout, input_text=item["prompt"])
    combined = f"{result.stdout}\n{result.stderr}"
    limited = result.returncode != 0 and is_rate_limit_text(combined)
    reset = parse_reset_time(combined)
    return ClaudeRunResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        rate_limited=limited,
        reset_at=reset.astimezone().isoformat() if reset else None,
    )


def print_dry_run_details(paths: Paths, claude_exe: Path, item: dict[str, Any], use_ide: bool, prompt: str) -> None:
    if item.get("remote_kind") == "ssh":
        host = item.get("remote_host") or "<host-remoto>"
        cwd = item.get("remote_cwd") or item.get("cwd") or "<cwd-remoto>"
        remote_exe = item.get("remote_claude_exe") or "<claude.exe remoto>"
        args = " ".join(windows_cmd_quote(arg) for arg in build_claude_arguments(item, use_ide))
        command = f"cd /d {windows_cmd_quote(str(cwd))} && {windows_cmd_quote(str(remote_exe))} {args}"
        print(f"remote: ssh {host}")
        print(f"cwd remoto: {cwd}")
        print("cmd remoto: " + command)
    elif should_run_local_windows(item.get("cwd")):
        args = " ".join(powershell_single_quote(arg) for arg in build_claude_arguments(item, use_ide))
        command = (
            f"Set-Location -LiteralPath {powershell_single_quote(str(item.get('cwd')))}; "
            f"$prompt = [Console]::In.ReadToEnd(); "
            f"$prompt | & {powershell_single_quote(local_to_windows_path(claude_exe))} @({args})"
        )
        print("runtime: Windows PowerShell")
        print(f"cwd Windows: {item.get('cwd')}")
        print("cmd: " + command)
    else:
        command = build_claude_command(claude_exe, item, use_ide)
        print(f"cwd: {item.get('cwd')}")
        print("cmd: " + " ".join(command))
    print(f"prompt: {prompt}")


def should_run_local_windows(cwd: Any) -> bool:
    return isinstance(cwd, str) and is_windows_path(cwd) and windows_path_accessible(cwd)


WINDOWS_CLAUDE_LAUNCHER = r"""
import json
import os
import subprocess
import sys

payload_path = sys.argv[1]
with open(payload_path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)

for name in payload.get("clear_env", []):
    os.environ.pop(name, None)

try:
    proc = subprocess.run(
        [payload["exe"], *payload["args"], payload["prompt"]],
        cwd=payload["cwd"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=payload["timeout"],
    )
except subprocess.TimeoutExpired as exc:
    if exc.stdout:
        sys.stdout.write(exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode("utf-8", "replace"))
    if exc.stderr:
        sys.stderr.write(exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode("utf-8", "replace"))
    raise SystemExit(124)

sys.stdout.write(proc.stdout or "")
sys.stderr.write(proc.stderr or "")
raise SystemExit(proc.returncode)
"""


def cmd_clear_external_auth_prefix() -> str:
    return " && ".join(f'set "{name}="' for name in sorted(CLAUDE_EXTERNAL_AUTH_ENV_VARS))


def run_claude_local_windows(
    paths: Paths,
    claude_exe: Path,
    item: dict[str, Any],
    timeout: int,
    use_ide: bool,
) -> subprocess.CompletedProcess[str]:
    cwd = item.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise SystemExit("La chat non ha una cwd Windows salvata.")
    temp_root = paths.state_dir / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
        temp_path = Path(temp_dir)
        launcher_path = temp_path / "launch_claude.py"
        payload_path = temp_path / "payload.json"
        launcher_path.write_text(WINDOWS_CLAUDE_LAUNCHER, encoding="utf-8")
        payload_path.write_text(
            json.dumps(
                {
                    "exe": local_to_windows_path(claude_exe),
                    "cwd": cwd,
                    "args": build_claude_arguments(item, use_ide),
                    "prompt": item["prompt"],
                    "timeout": timeout,
                    "clear_env": sorted(CLAUDE_EXTERNAL_AUTH_ENV_VARS),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        command = (
            f"{cmd_clear_external_auth_prefix()} && "
            f"py -3 {local_to_windows_path(launcher_path)} "
            f"{local_to_windows_path(payload_path)}"
        )
        return subprocess.run(
            ["cmd.exe", "/d", "/c", command],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout + 30,
            env=claude_subprocess_env(),
        )


def run_claude(
    paths: Paths,
    claude_exe: Path,
    item: dict[str, Any],
    timeout: int,
    use_ide: bool,
) -> ClaudeRunResult:
    if item.get("remote_kind") == "ssh":
        return run_claude_remote_ssh(paths, item, timeout, use_ide)
    prompt = item["prompt"]
    if should_run_local_windows(item.get("cwd")):
        proc = run_claude_local_windows(paths, claude_exe, item, timeout, use_ide)
    else:
        command = build_claude_command(claude_exe, item, use_ide)
        proc = subprocess.run(
            command,
            input=prompt,
            text=True,
            cwd=cwd_for_subprocess(
                item.get("cwd"),
                item.get("cwd_fallback") if item.get("allow_cwd_fallback") else None,
            ),
            capture_output=True,
            timeout=timeout,
            env=claude_subprocess_env(),
        )
    combined = f"{proc.stdout}\n{proc.stderr}"
    limited = proc.returncode != 0 and is_rate_limit_text(combined)
    reset = parse_reset_time(combined)
    return ClaudeRunResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        rate_limited=limited,
        reset_at=reset.astimezone().isoformat() if reset else None,
    )


def run_codex(
    paths: Paths,
    codex_exe: Path,
    item: dict[str, Any],
    timeout: int,
) -> ClaudeRunResult:
    prompt = item["prompt"]
    arguments = build_codex_arguments(item)
    cwd = normalize_windows_path(str(item.get("cwd") or ""))
    if not cwd:
        raise SystemExit("La task Codex non ha un cwd salvato; non invio per non cambiare contesto.")

    if codex_exe.suffix.lower() in {".cmd", ".bat", ".exe"} or is_windows_path(str(codex_exe)):
        if not is_windows_path(cwd):
            raise SystemExit(f"Il CLI Codex Windows non puo' riprendere una task con cwd non Windows: {cwd}")
        executable = local_to_windows_path(codex_exe)
        proc = subprocess.run(
            ["cmd.exe", "/d", "/c", "cd", "/d", cwd, "&&", "call", executable, *arguments],
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=codex_subprocess_env(),
        )
    else:
        proc = subprocess.run(
            [str(codex_exe), *arguments],
            input=prompt,
            text=True,
            cwd=cwd_for_subprocess(cwd),
            capture_output=True,
            timeout=timeout,
            env=codex_subprocess_env(),
        )

    combined = f"{proc.stdout}\n{proc.stderr}"
    limited = proc.returncode != 0 and is_rate_limit_text(combined)
    reset = parse_reset_time(combined)
    if limited and reset is None:
        raw_path = item.get("jsonl_path")
        if not isinstance(raw_path, str):
            fingerprint = item.get("fingerprint") if isinstance(item.get("fingerprint"), dict) else {}
            raw_path = fingerprint.get("jsonl_path") if isinstance(fingerprint.get("jsonl_path"), str) else None
        if raw_path:
            reset = codex_rate_limit_reset_from_path(windows_to_local_path(raw_path))
    return ClaudeRunResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        rate_limited=limited,
        reset_at=reset.astimezone().isoformat() if reset else None,
    )


def run_agent(
    paths: Paths,
    claude_exe: Path | None,
    codex_exe: Path | None,
    item: dict[str, Any],
    timeout: int,
    use_ide: bool,
) -> ClaudeRunResult:
    if item.get("provider") == PROVIDER_CODEX:
        if codex_exe is None:
            raise SystemExit("CLI Codex non trovato. Esegui `doctor` per dettagli.")
        return run_codex(paths, codex_exe, item, timeout)
    if claude_exe is None:
        raise SystemExit("Claude Code executable non trovato. Esegui `doctor` per dettagli.")
    return run_claude(paths, claude_exe, item, timeout, use_ide)


def print_agent_dry_run_details(
    paths: Paths,
    claude_exe: Path | None,
    codex_exe: Path | None,
    item: dict[str, Any],
    use_ide: bool,
    prompt: str,
) -> None:
    if item.get("provider") == PROVIDER_CODEX:
        if codex_exe is None:
            raise SystemExit("CLI Codex non trovato.")
        print(f"provider: {PROVIDER_CODEX}")
        print(f"cwd: {item.get('cwd')}")
        print("cmd: " + " ".join([str(codex_exe), *build_codex_arguments(item)]))
        print(f"prompt: {prompt}")
        return
    if claude_exe is None:
        raise SystemExit("Claude Code executable non trovato.")
    print_dry_run_details(paths, claude_exe, item, use_ide, prompt)


def write_run_log(paths: Paths, item: dict[str, Any], result: ClaudeRunResult) -> str:
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = paths.log_dir / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{item['id']}.log"
    content = [
        f"item: {item['id']}",
        f"session_id: {item['session_id']}",
        f"returncode: {result.returncode}",
        "",
        "STDOUT",
        result.stdout,
        "",
        "STDERR",
        result.stderr,
    ]
    log_path.write_text("\n".join(content), encoding="utf-8")
    return str(log_path)


def normalize_prompt(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def message_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for entry in content:
            if isinstance(entry, dict) and entry.get("type") == "text" and isinstance(entry.get("text"), str):
                parts.append(entry["text"])
        if parts:
            return "\n".join(parts)
    return None


def codex_tail_objects(path: Path, max_bytes: int = 64 * 1024 * 1024) -> list[dict[str, Any]]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            start = max(0, size - max_bytes)
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return []
    if start:
        newline = raw.find(b"\n")
        raw = raw[newline + 1 :] if newline >= 0 else b""
    objects: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        obj = safe_json_loads(line)
        if obj:
            objects.append(obj)
    return objects


def codex_rate_limit_reset_from_path(path: Path, now: dt.datetime | None = None) -> dt.datetime | None:
    now = now or local_now()
    for obj in reversed(codex_tail_objects(path)):
        if obj.get("type") != "event_msg":
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if payload.get("type") != "token_count":
            continue
        limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
        reached = limits.get("rate_limit_reached_type")
        windows: list[tuple[str, dict[str, Any]]] = []
        for key in ["primary", "secondary"]:
            value = limits.get(key)
            if isinstance(value, dict):
                windows.append((key, value))
        candidates: list[dt.datetime] = []
        for key, window in windows:
            used = window.get("used_percent")
            is_reached = bool(reached and (str(reached).lower() in {key, "both", "primary_and_secondary"}))
            if reached and not is_reached:
                continue
            if not is_reached and not isinstance(used, (int, float)):
                continue
            if not is_reached and float(used) < 100:
                continue
            reset_epoch = window.get("resets_at")
            if isinstance(reset_epoch, (int, float)):
                try:
                    candidates.append(dt.datetime.fromtimestamp(reset_epoch, tz=dt.UTC).astimezone())
                except (OSError, OverflowError, ValueError):
                    pass
        if candidates:
            reset = max(candidates)
            return reset if reset + dt.timedelta(seconds=RATE_LIMIT_RESET_DELAY_SECONDS) > now else None
        return None
    return None


def codex_rate_limit_reset_from_chat(chat: Chat, now: dt.datetime | None = None) -> dt.datetime | None:
    return codex_rate_limit_reset_from_path(chat.jsonl_path, now)


def latest_rate_limit_reset_from_chat(chat: Chat, now: dt.datetime | None = None) -> dt.datetime | None:
    if chat.provider == PROVIDER_CODEX:
        return codex_rate_limit_reset_from_chat(chat, now)
    now = now or local_now()
    latest_timestamp: dt.datetime | None = None
    latest_reset: dt.datetime | None = None
    try:
        handle = chat.jsonl_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            obj = safe_json_loads(line)
            if not obj:
                continue
            text_parts: list[str] = []
            message = obj.get("message")
            if isinstance(message, dict):
                text = message_text(message)
                if text:
                    text_parts.append(text)
            if isinstance(obj.get("error"), str):
                text_parts.append(obj["error"])
            combined = "\n".join(text_parts)
            if not combined or not is_rate_limit_text(combined):
                continue
            timestamp = parse_iso(obj.get("timestamp")) if isinstance(obj.get("timestamp"), str) else None
            base = timestamp.astimezone() if timestamp else now
            reset = parse_reset_time(combined, base)
            if reset is None:
                continue
            key = timestamp or reset
            if latest_timestamp is None or key >= latest_timestamp:
                latest_timestamp = key
                latest_reset = reset
    if latest_reset is None:
        return None
    ready_at = latest_reset + dt.timedelta(seconds=RATE_LIMIT_RESET_DELAY_SECONDS)
    return latest_reset if ready_at > now else None


def prompt_recorded_after(claude_home: Path, session_id: str, prompt: str, after: dt.datetime) -> bool:
    expected = normalize_prompt(prompt)
    if not expected:
        return False
    projects = claude_home / "projects"
    if not projects.exists():
        return False
    for jsonl_path in projects.glob(f"**/{session_id}.jsonl"):
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    obj = safe_json_loads(line)
                    if not obj or obj.get("type") != "user":
                        continue
                    timestamp = parse_iso(obj.get("timestamp"))
                    if timestamp is None or timestamp < after:
                        continue
                    message = obj.get("message")
                    text = message_text(message) if isinstance(message, dict) else None
                    if normalize_prompt(text) == expected:
                        return True
        except OSError:
            continue
    return False


def codex_prompt_recorded_after(path: Path, prompt: str, after: dt.datetime) -> bool:
    expected = normalize_prompt(prompt)
    if not expected:
        return False
    for obj in codex_tail_objects(path):
        if obj.get("type") != "event_msg":
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if payload.get("type") != "user_message":
            continue
        timestamp = parse_iso(obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None)
        if timestamp is None or timestamp < after:
            continue
        message = payload.get("message") if isinstance(payload.get("message"), str) else None
        if normalize_prompt(message) == expected:
            return True
    return False


def item_prompt_recorded_after(paths: Paths, item: dict[str, Any], after: dt.datetime) -> bool:
    if item.get("provider") == PROVIDER_CODEX:
        raw_path = item.get("jsonl_path")
        if not isinstance(raw_path, str):
            fingerprint = item.get("fingerprint") if isinstance(item.get("fingerprint"), dict) else {}
            raw_path = fingerprint.get("jsonl_path") if isinstance(fingerprint.get("jsonl_path"), str) else None
        path = windows_to_local_path(raw_path) if raw_path else Path("")
        return bool(raw_path and codex_prompt_recorded_after(path, item.get("prompt", ""), after))
    return prompt_recorded_after(paths.claude_home, item["session_id"], item.get("prompt", ""), after)


def should_verify_prompt_recorded(item: dict[str, Any]) -> bool:
    if item.get("provider") == PROVIDER_CODEX:
        return True
    return item.get("remote_kind") is None and is_windows_path(item.get("cwd") if isinstance(item.get("cwd"), str) else None)


def prompt_missing_after_success(paths: Paths, item: dict[str, Any], after: dt.datetime) -> str | None:
    if not should_verify_prompt_recorded(item):
        return None
    for attempt in range(4):
        if item_prompt_recorded_after(paths, item, after):
            return None
        if attempt < 3:
            time.sleep(0.5)
    provider = "Codex" if item.get("provider") == PROVIDER_CODEX else "Claude"
    return f"{provider} ha restituito codice 0, ma il prompt non risulta registrato nella transcript: invio considerato non riuscito."


def item_priority(item: dict[str, Any]) -> int:
    value = item.get("priority")
    return value if isinstance(value, int) else 100


def item_order(item: dict[str, Any]) -> int:
    value = item.get("order")
    return value if isinstance(value, int) else 0


def pending_items(queue: dict[str, Any]) -> list[dict[str, Any]]:
    items = [item for item in queue.get("items", []) if item.get("status") == STATUS_PENDING]
    return sorted(items, key=lambda item: (item_priority(item), item_order(item), str(item.get("created_at") or "")))


def active_recovery(queue: dict[str, Any]) -> dict[str, Any] | None:
    recovery = queue.get("recovery")
    if not isinstance(recovery, dict) or not recovery.get("active"):
        return None
    return recovery


def active_auto_continue(queue: dict[str, Any]) -> dict[str, Any] | None:
    auto_continue = queue.get("auto_continue")
    if not isinstance(auto_continue, dict) or not auto_continue.get("enabled"):
        return None
    return auto_continue


def item_ready(item: dict[str, Any]) -> bool:
    not_before = parse_iso(item.get("not_before"))
    return not_before is None or not_before <= dt.datetime.now(dt.UTC)


def recovery_ready(recovery: dict[str, Any]) -> bool:
    not_before = parse_iso(recovery.get("not_before"))
    return not_before is None or not_before <= dt.datetime.now(dt.UTC)


def auto_continue_ready(auto_continue: dict[str, Any]) -> bool:
    not_before = parse_iso(auto_continue.get("not_before"))
    return not_before is None or not_before <= dt.datetime.now(dt.UTC)


def recovery_as_item(recovery: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"recovery-{recovery.get('source_item_id', 'manual')}",
        "status": STATUS_RECOVERY,
        "session_id": recovery["session_id"],
        "title": recovery.get("title") or "Recovery",
        "cwd": recovery.get("cwd"),
        "prompt": RECOVERY_PROMPT,
        "attempts": recovery.get("attempts", 0),
        "not_before": recovery.get("not_before"),
        "last_error": recovery.get("last_error"),
        "last_log": recovery.get("last_log"),
        "fingerprint": recovery.get("fingerprint", {}),
        "provider": recovery.get("provider", PROVIDER_CLAUDE),
        "jsonl_path": recovery.get("jsonl_path"),
        "remote_kind": recovery.get("remote_kind"),
        "remote_host": recovery.get("remote_host"),
        "remote_cwd": recovery.get("remote_cwd"),
        "remote_uri": recovery.get("remote_uri"),
        "remote_claude_exe": recovery.get("remote_claude_exe"),
        **item_account_fields(recovery),
        **item_setting_overrides(recovery),
    }


def auto_continue_as_item(auto_continue: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"auto-continue-{str(auto_continue.get('session_id', ''))[:8]}",
        "status": STATUS_RECOVERY,
        "session_id": auto_continue["session_id"],
        "title": auto_continue.get("title") or "Auto continua",
        "cwd": auto_continue.get("cwd"),
        "prompt": RECOVERY_PROMPT,
        "attempts": auto_continue.get("attempts", 0),
        "not_before": auto_continue.get("not_before"),
        "last_error": auto_continue.get("last_error"),
        "last_log": auto_continue.get("last_log"),
        "fingerprint": auto_continue.get("fingerprint", {}),
        "provider": auto_continue.get("provider", PROVIDER_CLAUDE),
        "jsonl_path": auto_continue.get("jsonl_path"),
        "allow_cwd_fallback": bool(auto_continue.get("allow_cwd_fallback")),
        "cwd_fallback": auto_continue.get("cwd_fallback"),
        "remote_kind": auto_continue.get("remote_kind"),
        "remote_host": auto_continue.get("remote_host"),
        "remote_cwd": auto_continue.get("remote_cwd"),
        "remote_uri": auto_continue.get("remote_uri"),
        "remote_claude_exe": auto_continue.get("remote_claude_exe"),
        **item_account_fields(auto_continue),
        **item_setting_overrides(auto_continue),
    }


def retry_time_after_limit(result: ClaudeRunResult, poll_seconds: int) -> str:
    reset_at = parse_iso(result.reset_at)
    if reset_at:
        return (reset_at + dt.timedelta(seconds=RATE_LIMIT_RESET_DELAY_SECONDS)).astimezone().isoformat()
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=poll_seconds)).replace(microsecond=0).isoformat()


def update_auto_continue_state(auto_continue: dict[str, Any], status: str | None = None, **fields: Any) -> None:
    if status is not None:
        auto_continue["status"] = status
    auto_continue["updated_at"] = now_utc()
    for key, value in fields.items():
        auto_continue[key] = value


def set_recovery_after_limit(
    queue: dict[str, Any],
    item: dict[str, Any],
    result: ClaudeRunResult,
    prompt_was_recorded: bool,
    poll_seconds: int,
) -> None:
    not_before = retry_time_after_limit(result, poll_seconds)
    queue["recovery"] = {
        "active": True,
        "created_at": now_utc(),
        "source_item_id": item.get("id"),
        "source_prompt_recorded": prompt_was_recorded,
        "session_id": item["session_id"],
        "title": item.get("title"),
        "cwd": item.get("cwd"),
        "fingerprint": item.get("fingerprint", {}),
        "provider": item.get("provider", PROVIDER_CLAUDE),
        "jsonl_path": item.get("jsonl_path"),
        "attempts": 0,
        "not_before": not_before,
        "last_error": truncate(result.stderr or result.stdout, 500),
        "last_log": item.get("last_log"),
        "remote_kind": item.get("remote_kind"),
        "remote_host": item.get("remote_host"),
        "remote_cwd": item.get("remote_cwd"),
        "remote_uri": item.get("remote_uri"),
        "remote_claude_exe": item.get("remote_claude_exe"),
        **item_account_fields(item),
        **item_setting_overrides(item),
    }
    if prompt_was_recorded:
        item["status"] = STATUS_RECOVERY
        item["not_before"] = None
        item["last_error"] = "Session limit: riprendero' con 'continua' prima della coda."
    else:
        item["status"] = STATUS_PENDING
        item["not_before"] = None
        item["last_error"] = "Session limit prima della conferma: prima mando 'continua', poi ritento questo prompt."


def complete_recovery(queue: dict[str, Any], recovery: dict[str, Any]) -> None:
    source_item_id = recovery.get("source_item_id")
    if recovery.get("source_prompt_recorded") and source_item_id:
        for item in queue.get("items", []):
            if item.get("id") == source_item_id:
                item["status"] = STATUS_DONE
                item["completed_at"] = now_utc()
                item["last_error"] = None
                item["not_before"] = None
                break
    queue["recovery"] = None


def find_chat_by_session(chats: list[Chat], session_id: str) -> Chat | None:
    for chat in chats:
        if chat.session_id == session_id:
            return chat
    return None


def same_optional_text(left: Any, right: Any) -> bool:
    if left is None and right is None:
        return True
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left.lower() == right.lower()


def find_chat_for_item(chats: list[Chat], item: dict[str, Any]) -> Chat | None:
    session_id = item.get("session_id")
    if not isinstance(session_id, str):
        return None
    provider = item.get("provider") if isinstance(item.get("provider"), str) else PROVIDER_CLAUDE
    remote_kind = item.get("remote_kind")
    remote_host = item.get("remote_host")
    remote_cwd = item.get("remote_cwd")
    if remote_kind or remote_host or remote_cwd:
        for chat in chats:
            if chat.session_id != session_id or chat.provider != provider:
                continue
            if (
                same_optional_text(chat.remote_kind, remote_kind)
                and same_optional_text(chat.remote_host, remote_host)
                and same_optional_text(chat.remote_cwd, remote_cwd)
            ):
                return chat
        return None
    for chat in chats:
        if chat.session_id == session_id and chat.provider == provider:
            return chat
    return None


def chat_execution_fields(chat: Chat) -> dict[str, Any]:
    return {
        "provider": chat.provider,
        "jsonl_path": str(chat.jsonl_path),
        "remote_kind": chat.remote_kind,
        "remote_host": chat.remote_host,
        "remote_cwd": chat.remote_cwd,
        "remote_uri": chat.remote_uri,
        "account_key": chat.account_key,
        "account_label": chat.account_label,
    }


def command_doctor(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.windows_home, args.state_dir)
    local_chats = discover_chats(paths.claude_home)
    vscode_chats = discover_claude_agent_sessions(paths)
    codex_chats = discover_codex_app_sessions(paths)
    all_chats = discover_agent_chats(paths)
    claude_exe = find_claude_executable(paths, args.claude)
    codex_exe = find_codex_executable(paths, getattr(args, "codex", None))
    print(f"Windows home: {paths.windows_home}")
    print(f"Claude home:  {paths.claude_home}")
    print(f"Codex home:   {paths.codex_home}")
    print(f"State dir:    {paths.state_dir}")
    print(f"Claude exe:   {claude_exe or 'NON TROVATO'}")
    print(f"Codex CLI:    {codex_exe or 'NON TROVATO'}")
    if claude_exe:
        try:
            result = subprocess.run([str(claude_exe), "--version"], text=True, capture_output=True, timeout=15)
            print(f"Versione:     {result.stdout.strip() or result.stderr.strip()}")
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Versione:     errore: {exc}")
    if codex_exe:
        try:
            version_result = run_codex_cli_command(codex_exe, ["--version"])
            login_result = run_codex_cli_command(codex_exe, ["login", "status"])
            print(f"Codex ver.:   {(version_result.stdout or version_result.stderr).strip()}")
            print(f"Codex auth:   {(login_result.stdout or login_result.stderr).strip()}")
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Codex stato:  errore: {exc}")
    print(f"Chat Claude Code locali:     {len(local_chats)}")
    print(f"Chat Claude Code VS Code:    {len(vscode_chats)}")
    print(f"Task Codex App:              {len(codex_chats)}")
    print(f"Chat/task totali:            {len(all_chats)}")
    print(f"Chat accodabili:             {len([chat for chat in all_chats if chat.can_queue])}")
    by_source: dict[str, int] = {}
    for chat in all_chats:
        by_source[chat.source] = by_source.get(chat.source, 0) + 1
    for source, count in sorted(by_source.items()):
        print(f"- {source}: {count}")
    print("Provider operativi:")
    print("- Claude Code: resume tramite CLI con protezione account e impostazioni.")
    print("- Codex App: resume tramite CLI ufficiale con verifica transcript e limite strutturato.")
    return 0 if (claude_exe or codex_exe) and all_chats else 1


def command_list(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.windows_home, args.state_dir)
    chats = discover_agent_chats(paths)
    if not chats:
        print("Nessuna chat Claude o task Codex trovata.")
        return 1
    print_chat_list(chats, args.limit)
    return 0


def command_add(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.windows_home, args.state_dir)
    chats = discover_agent_chats(paths)
    chat = select_chat(args.chat, chats)
    account_error = account_mismatch_for_chat(paths, chat)
    if account_error:
        print(account_error)
        return 1
    if not chat.can_queue:
        print(
            "Questa chat/task e' solo visibile: il suo contesto non e' disponibile per l'invio."
        )
        return 1
    prompts = expand_items(args.items)
    if not prompts:
        print("Nessun messaggio da accodare.")
        return 1

    chat = remember_chat_account(paths, chat)
    fingerprint = settings_fingerprint(paths, chat)
    overrides = selected_setting_overrides(vars(args))
    queue = load_queue(paths.queue_file)
    start_order = len(queue["items"])
    created = now_utc()
    for offset, prompt in enumerate(prompts):
        queue["items"].append(
            {
                "id": str(uuid.uuid4())[:8],
                "status": STATUS_PENDING,
                "created_at": created,
                "order": start_order + offset,
                "priority": int(args.priority),
                "session_id": chat.session_id,
                "title": chat.title,
                "cwd": chat.cwd,
                "prompt": prompt,
                "attempts": 0,
                "not_before": args.not_before,
                "last_error": None,
                "last_log": None,
                "fingerprint": fingerprint,
                **overrides,
                **chat_execution_fields(chat),
            }
        )
    save_queue(paths.queue_file, queue)
    print(f"Accodati {len(prompts)} messaggi per chat {chat.session_id[:8]}: {chat.title}")
    print(f"Coda: {paths.queue_file}")
    return 0


def command_status(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.windows_home, args.state_dir)
    queue = load_queue(paths.queue_file)
    recovery = active_recovery(queue)
    auto_continue = active_auto_continue(queue)
    if recovery:
        print(
            f"Recovery attiva: prossimo invio '{RECOVERY_PROMPT}' "
            f"chat={str(recovery.get('session_id', ''))[:8]} not_before={recovery.get('not_before')}"
        )
    if auto_continue:
        print(
            f"Auto-continua attivo: chat={str(auto_continue.get('session_id', ''))[:8]} "
            f"not_before={auto_continue.get('not_before')}"
        )
    items = queue.get("items", [])
    if not items and not recovery and not auto_continue:
        print("Coda vuota.")
        return 0
    for index, item in enumerate(items, start=1):
        print(
            f"{index:>2}. {item.get('id')}  {item.get('status'):<8} "
            f"priority={item_priority(item)}  attempts={item.get('attempts', 0)}  chat={str(item.get('session_id', ''))[:8]}  "
            f"{truncate(item.get('title'), 38)}"
        )
        if args.verbose:
            print(f"    cwd: {item.get('cwd')}")
            print(f"    not_before: {item.get('not_before')}")
            print(f"    last_error: {truncate(item.get('last_error'), 140)}")
            print(f"    prompt: {truncate(item.get('prompt'), 140)}")
    return 0


def command_clear(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.windows_home, args.state_dir)
    queue = load_queue(paths.queue_file)
    before = len(queue.get("items", []))
    if args.all:
        queue["items"] = []
        queue["recovery"] = None
        queue["auto_continue"] = None
    else:
        queue["items"] = [
            item
            for item in queue.get("items", [])
            if item.get("status") in {STATUS_PENDING, STATUS_RECOVERY}
        ]
    save_queue(paths.queue_file, queue)
    print(f"Rimossi {before - len(queue['items'])} elementi.")
    return 0


def command_remove(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.windows_home, args.state_dir)
    queue = load_queue(paths.queue_file)
    before = len(queue.get("items", []))
    selector = args.item.lower()
    queue["items"] = [
        item
        for item in queue.get("items", [])
        if not str(item.get("id", "")).lower().startswith(selector)
    ]
    removed = before - len(queue["items"])
    save_queue(paths.queue_file, queue)
    print(f"Rimossi {removed} elementi.")
    return 0 if removed else 1


def command_reset(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.windows_home, args.state_dir)
    queue = load_queue(paths.queue_file)
    changed = 0
    for item in queue.get("items", []):
        if args.item and item.get("id") != args.item:
            continue
        if item.get("status") in {STATUS_FAILED, STATUS_BLOCKED, STATUS_RECOVERY}:
            item["status"] = STATUS_PENDING
            item["last_error"] = None
            item["not_before"] = None
            changed += 1
    recovery = active_recovery(queue)
    if recovery and (not args.item or recovery.get("source_item_id") == args.item):
        queue["recovery"] = None
        changed += 1
    auto_continue = active_auto_continue(queue)
    if auto_continue and (not args.item or auto_continue.get("session_id") == args.item):
        auto_continue["enabled"] = False
        auto_continue["status"] = "disabled"
        auto_continue["disabled_at"] = now_utc()
        changed += 1
    save_queue(paths.queue_file, queue)
    print(f"Riattivati {changed} elementi.")
    return 0


def command_check_settings(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.windows_home, args.state_dir)
    queue = load_queue(paths.queue_file)
    chats = discover_agent_chats(paths)
    items = queue.get("items", [])
    if args.item:
        items = [item for item in items if item.get("id") == args.item]
    if not items:
        print("Nessun elemento trovato.")
        return 1
    exit_code = 0
    for item in items:
        chat = find_chat_for_item(chats, item)
        if chat is None:
            print(f"{item['id']}: chat non trovata")
            exit_code = 1
            continue
        diffs = compare_fingerprints(item["fingerprint"], settings_fingerprint(paths, chat))
        if diffs:
            print(f"{item['id']}: impostazioni cambiate")
            for diff in diffs:
                print(f"  - {diff}")
            exit_code = 1
        else:
            print(f"{item['id']}: impostazioni invariate")
    return exit_code


def seconds_until_ready(queue: dict[str, Any], default: int) -> int:
    waits: list[int] = []
    now = dt.datetime.now(dt.UTC)
    recovery = active_recovery(queue)
    if recovery:
        not_before = parse_iso(recovery.get("not_before"))
        if not_before and not_before > now:
            waits.append(max(1, int((not_before - now).total_seconds())))
    auto_continue = active_auto_continue(queue)
    if auto_continue:
        not_before = parse_iso(auto_continue.get("not_before"))
        if not_before and not_before > now:
            waits.append(max(1, int((not_before - now).total_seconds())))
    for item in pending_items(queue):
        not_before = parse_iso(item.get("not_before"))
        if not_before and not_before > now:
            waits.append(max(1, int((not_before - now).total_seconds())))
    return min(waits) if waits else default


def command_run(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.windows_home, args.state_dir)
    claude_exe = find_claude_executable(paths, args.claude)
    codex_exe = find_codex_executable(paths, getattr(args, "codex", None))
    if claude_exe is None and codex_exe is None:
        print("Nessun CLI Claude/Codex trovato. Esegui `doctor` per dettagli.")
        return 1

    while True:
        queue = load_queue(paths.queue_file)
        recovery = active_recovery(queue)
        if recovery:
            if recovery.get("blocked"):
                print(f"Recovery bloccata: {recovery.get('last_error')}")
                return 1
            if not recovery_ready(recovery):
                wait_seconds = seconds_until_ready(queue, args.poll_seconds)
                if args.once:
                    print(f"Recovery in attesa. Prossimo 'continua' tra circa {wait_seconds}s.")
                    return RATE_LIMIT_EXIT
                print(f"Recovery in attesa: prossimo 'continua' tra circa {wait_seconds}s.")
                time.sleep(min(wait_seconds, args.poll_seconds))
                continue

            item = recovery_as_item(recovery)
            chats = discover_agent_chats(paths)
            chat = find_chat_for_item(chats, item)
            if chat is None:
                recovery["blocked"] = True
                recovery["last_error"] = "Chat non trovata durante recovery"
                save_queue(paths.queue_file, queue)
                return 1

            diffs = compare_fingerprints(item["fingerprint"], settings_fingerprint(paths, chat))
            if diffs and not args.ignore_settings_change:
                recovery["blocked"] = True
                recovery["last_error"] = "Impostazioni cambiate: " + "; ".join(diffs)
                save_queue(paths.queue_file, queue)
                print("Recovery bloccata: impostazioni della chat cambiate.")
                for diff in diffs:
                    print(f"  - {diff}")
                return CONFIG_CHANGED_EXIT

            account_error = account_mismatch_for_item(paths, item) or account_mismatch_for_chat(paths, chat)
            if account_error:
                recovery["blocked"] = True
                recovery["last_error"] = account_error
                save_queue(paths.queue_file, queue)
                print(f"Recovery bloccata: {account_error}")
                return 1

            if args.dry_run:
                print("DRY RUN RECOVERY")
                print_agent_dry_run_details(paths, claude_exe, codex_exe, item, args.ide, RECOVERY_PROMPT)
                return 0

            print(f"Recovery: invio '{RECOVERY_PROMPT}' a chat {item['session_id'][:8]}")
            recovery["attempts"] = int(recovery.get("attempts", 0)) + 1
            run_started_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)
            try:
                result = run_agent(paths, claude_exe, codex_exe, item, args.timeout, args.ide)
            except subprocess.TimeoutExpired:
                recovery["blocked"] = True
                recovery["last_error"] = f"Timeout recovery dopo {args.timeout}s"
                save_queue(paths.queue_file, queue)
                return 1
            except (OSError, SystemExit) as exc:
                recovery["blocked"] = True
                recovery["last_error"] = str(exc)
                save_queue(paths.queue_file, queue)
                return 1

            recovery["last_log"] = write_run_log(paths, item, result)
            if result.returncode == 0:
                missing_prompt_error = prompt_missing_after_success(paths, item, run_started_at)
                if missing_prompt_error:
                    recovery["blocked"] = True
                    recovery["last_error"] = missing_prompt_error
                    save_queue(paths.queue_file, queue)
                    print(f"Recovery bloccata: {missing_prompt_error}")
                    return 1
                complete_recovery(queue, recovery)
                save_queue(paths.queue_file, queue)
                print(f"Recovery completata con '{RECOVERY_PROMPT}'. Log: {item.get('last_log') or recovery.get('last_log')}")
                if args.once:
                    return 0
                continue

            if result.rate_limited:
                recovery["last_error"] = truncate(result.stderr or result.stdout, 500)
                recovery["not_before"] = retry_time_after_limit(result, args.poll_seconds)
                save_queue(paths.queue_file, queue)
                print(f"Session limit ancora attivo. Prossimo '{RECOVERY_PROMPT}' dopo: {recovery['not_before']}")
                if args.once:
                    return RATE_LIMIT_EXIT
                time.sleep(args.poll_seconds)
                continue

            recovery["blocked"] = True
            recovery["last_error"] = truncate(result.stderr or result.stdout, 1000)
            source_item_id = recovery.get("source_item_id")
            for queued_item in queue.get("items", []):
                if queued_item.get("id") == source_item_id:
                    queued_item["status"] = STATUS_BLOCKED
                    queued_item["last_error"] = recovery["last_error"]
                    break
            save_queue(paths.queue_file, queue)
            print(f"Recovery fallita. Log: {recovery.get('last_log')}")
            return result.returncode or 1

        auto_continue = active_auto_continue(queue)
        if (
            auto_continue
            and auto_continue.get("status") == "monitoring"
            and not auto_continue_ready(auto_continue)
            and any(item_ready(item) for item in pending_items(queue))
        ):
            auto_continue = None
        if auto_continue:
            if not auto_continue_ready(auto_continue):
                wait_seconds = seconds_until_ready(queue, args.poll_seconds)
                update_auto_continue_state(
                    auto_continue,
                    auto_continue.get("status") or "waiting_limit",
                    last_check_at=now_utc(),
                    next_check_in_seconds=wait_seconds,
                )
                save_queue(paths.queue_file, queue)
                if args.once:
                    print(f"Auto-continua in attesa. Prossimo 'continua' tra circa {wait_seconds}s.")
                    return RATE_LIMIT_EXIT
                print(f"Auto-continua in attesa: prossimo 'continua' tra circa {wait_seconds}s.")
                time.sleep(min(wait_seconds, args.poll_seconds))
                continue

            item = auto_continue_as_item(auto_continue)
            chats = discover_agent_chats(paths)
            chat = find_chat_for_item(chats, item)
            if chat is None:
                update_auto_continue_state(
                    auto_continue,
                    "blocked",
                    enabled=False,
                    last_error="Chat non trovata per auto-continua",
                )
                save_queue(paths.queue_file, queue)
                return 1

            diffs = compare_fingerprints(item["fingerprint"], settings_fingerprint(paths, chat))
            if diffs and not args.ignore_settings_change:
                update_auto_continue_state(
                    auto_continue,
                    "blocked",
                    enabled=False,
                    last_error="Impostazioni cambiate: " + "; ".join(diffs),
                )
                save_queue(paths.queue_file, queue)
                print("Auto-continua bloccato: impostazioni della chat cambiate.")
                for diff in diffs:
                    print(f"  - {diff}")
                return CONFIG_CHANGED_EXIT

            account_error = account_mismatch_for_item(paths, item) or account_mismatch_for_chat(paths, chat)
            if account_error:
                update_auto_continue_state(
                    auto_continue,
                    "blocked",
                    enabled=False,
                    last_error=account_error,
                    sending_started_at=None,
                    next_check_in_seconds=None,
                )
                save_queue(paths.queue_file, queue)
                print(f"Auto-continua bloccato: {account_error}")
                return 1

            if auto_continue.get("monitor_limit", True) and auto_continue.get("status") in {"armed", "monitoring"}:
                reset_at = latest_rate_limit_reset_from_chat(chat)
                if reset_at is None:
                    next_check = (
                        dt.datetime.now(dt.UTC) + dt.timedelta(seconds=args.poll_seconds)
                    ).replace(microsecond=0).isoformat()
                    update_auto_continue_state(
                        auto_continue,
                        "monitoring",
                        not_before=next_check,
                        last_check_at=now_utc(),
                        next_check_in_seconds=args.poll_seconds,
                        last_error=None,
                    )
                    save_queue(paths.queue_file, queue)
                    print("Auto-continua armato: nessun limite attivo rilevato, non invio nulla.")
                    if args.once:
                        return RATE_LIMIT_EXIT
                    if not any(item_ready(queued_item) for queued_item in pending_items(queue)):
                        time.sleep(args.poll_seconds)
                    continue
                ready_at = reset_at + dt.timedelta(seconds=RATE_LIMIT_RESET_DELAY_SECONDS)
                update_auto_continue_state(
                    auto_continue,
                    "waiting_limit",
                    not_before=ready_at.astimezone().replace(microsecond=0).isoformat(),
                    last_check_at=now_utc(),
                    next_check_in_seconds=max(1, int((ready_at - local_now()).total_seconds())),
                    last_error="Usage limit attivo: attendo il reset e il margine di sicurezza.",
                )
                save_queue(paths.queue_file, queue)
                continue

            if args.dry_run:
                print("DRY RUN AUTO-CONTINUA")
                if item.get("allow_cwd_fallback"):
                    print(f"cwd fallback: {item.get('cwd_fallback')}")
                print_agent_dry_run_details(paths, claude_exe, codex_exe, item, args.ide, RECOVERY_PROMPT)
                return 0

            print(f"Auto-continua: invio '{RECOVERY_PROMPT}' a chat {item['session_id'][:8]}")
            update_auto_continue_state(
                auto_continue,
                "sending",
                attempts=int(auto_continue.get("attempts", 0)) + 1,
                sending_started_at=now_utc(),
                last_check_at=now_utc(),
                next_check_in_seconds=None,
                last_error=None,
                not_before=None,
            )
            save_queue(paths.queue_file, queue)
            run_started_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)
            try:
                result = run_agent(paths, claude_exe, codex_exe, item, args.timeout, args.ide)
            except subprocess.TimeoutExpired:
                update_auto_continue_state(
                    auto_continue,
                    "failed",
                    enabled=False,
                    last_error=f"Timeout auto-continua dopo {args.timeout}s",
                )
                save_queue(paths.queue_file, queue)
                return 1
            except (OSError, SystemExit) as exc:
                update_auto_continue_state(auto_continue, "failed", enabled=False, last_error=str(exc))
                save_queue(paths.queue_file, queue)
                return 1

            auto_continue["last_log"] = write_run_log(paths, item, result)
            if result.returncode == 0:
                missing_prompt_error = prompt_missing_after_success(paths, item, run_started_at)
                if missing_prompt_error:
                    update_auto_continue_state(
                        auto_continue,
                        "blocked",
                        enabled=False,
                        last_error=missing_prompt_error,
                        sending_started_at=None,
                        next_check_in_seconds=None,
                    )
                    save_queue(paths.queue_file, queue)
                    print(f"Auto-continua bloccato: {missing_prompt_error}")
                    return 1
                combined_output = f"{result.stdout}\n{result.stderr}"
                if is_permission_wait_text(combined_output):
                    update_auto_continue_state(
                        auto_continue,
                        "blocked_permission",
                        enabled=False,
                        last_error=truncate(combined_output, 1000),
                        sending_started_at=None,
                        next_check_in_seconds=None,
                    )
                    save_queue(paths.queue_file, queue)
                    print(f"Auto-continua bloccato: il provider richiede approvazione. Log: {auto_continue.get('last_log')}")
                    return 1
                update_auto_continue_state(
                    auto_continue,
                    "done",
                    enabled=False,
                    completed_at=now_utc(),
                    last_error=None,
                    not_before=None,
                    next_check_in_seconds=None,
                )
                save_queue(paths.queue_file, queue)
                print(f"Auto-continua completato. Log: {auto_continue.get('last_log')}")
                if args.once:
                    return 0
                continue

            if result.rate_limited:
                update_auto_continue_state(
                    auto_continue,
                    "waiting_limit",
                    last_error=truncate(result.stderr or result.stdout, 500),
                    not_before=retry_time_after_limit(result, args.poll_seconds),
                    sending_started_at=None,
                    next_check_in_seconds=None,
                    last_check_at=now_utc(),
                )
                save_queue(paths.queue_file, queue)
                print(f"Session limit ancora attivo. Auto-continua dopo: {auto_continue['not_before']}")
                if args.once:
                    return RATE_LIMIT_EXIT
                time.sleep(args.poll_seconds)
                continue

            update_auto_continue_state(
                auto_continue,
                "failed",
                enabled=False,
                last_error=truncate(result.stderr or result.stdout, 1000),
                sending_started_at=None,
            )
            save_queue(paths.queue_file, queue)
            print(f"Auto-continua fallito. Log: {auto_continue.get('last_log')}")
            return result.returncode or 1

        candidates = [item for item in pending_items(queue) if item_ready(item)]
        if not candidates:

            if not pending_items(queue) and not active_auto_continue(queue):
                print("Coda completata.")
                return 0
            wait_seconds = seconds_until_ready(queue, args.poll_seconds)
            if args.once:
                print(f"Nessun elemento pronto. Prossimo controllo tra circa {wait_seconds}s.")
                return RATE_LIMIT_EXIT
            print(f"In attesa: prossimo elemento tra circa {wait_seconds}s.")
            time.sleep(min(wait_seconds, args.poll_seconds))
            continue

        item = candidates[0]
        chats = discover_agent_chats(paths)
        chat = find_chat_for_item(chats, item)
        if chat is None:
            item["status"] = STATUS_FAILED
            item["last_error"] = "Chat non trovata"
            save_queue(paths.queue_file, queue)
            return 1

        diffs = compare_fingerprints(item["fingerprint"], settings_fingerprint(paths, chat))
        if diffs and not args.ignore_settings_change:
            item["status"] = STATUS_BLOCKED
            item["last_error"] = "Impostazioni cambiate: " + "; ".join(diffs)
            save_queue(paths.queue_file, queue)
            print(f"Bloccato {item['id']}: impostazioni della chat cambiate.")
            for diff in diffs:
                print(f"  - {diff}")
            return CONFIG_CHANGED_EXIT

        account_error = account_mismatch_for_item(paths, item) or account_mismatch_for_chat(paths, chat)
        if account_error:
            item["status"] = STATUS_BLOCKED
            item["last_error"] = account_error
            save_queue(paths.queue_file, queue)
            print(f"Bloccato {item['id']}: {account_error}")
            return 1

        if args.dry_run:
            print("DRY RUN")
            print_agent_dry_run_details(paths, claude_exe, codex_exe, item, args.ide, truncate(item.get("prompt"), 200))
            if args.once:
                return 0
            item["status"] = STATUS_DONE
            item["last_error"] = "dry-run"
            save_queue(paths.queue_file, queue)
            continue

        print(f"Invio {item['id']} a chat {item['session_id'][:8]}: {truncate(item.get('title'), 60)}")
        item["attempts"] = int(item.get("attempts", 0)) + 1
        run_started_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)
        try:
            result = run_agent(paths, claude_exe, codex_exe, item, args.timeout, args.ide)
        except subprocess.TimeoutExpired:
            item["status"] = STATUS_FAILED
            item["last_error"] = f"Timeout dopo {args.timeout}s"
            save_queue(paths.queue_file, queue)
            return 1
        except (OSError, SystemExit) as exc:
            item["status"] = STATUS_FAILED
            item["last_error"] = str(exc)
            save_queue(paths.queue_file, queue)
            return 1

        item["last_log"] = write_run_log(paths, item, result)
        if result.returncode == 0:
            missing_prompt_error = prompt_missing_after_success(paths, item, run_started_at)
            if missing_prompt_error:
                item["status"] = STATUS_FAILED
                item["last_error"] = missing_prompt_error
                save_queue(paths.queue_file, queue)
                print(f"Errore provider su {item['id']}: {missing_prompt_error}")
                return 1
            item["status"] = STATUS_DONE
            item["completed_at"] = now_utc()
            item["last_error"] = None
            save_queue(paths.queue_file, queue)
            print(f"Completato {item['id']}. Log: {item['last_log']}")
            if args.once:
                return 0
            continue

        if result.rate_limited:
            prompt_was_recorded = item_prompt_recorded_after(paths, item, run_started_at)
            set_recovery_after_limit(queue, item, result, prompt_was_recorded, args.poll_seconds)
            save_queue(paths.queue_file, queue)
            print(
                "Session limit rilevato. Prima della coda mandero' "
                f"'{RECOVERY_PROMPT}' dopo: {queue['recovery']['not_before']}"
            )
            if args.once:
                return RATE_LIMIT_EXIT
            time.sleep(args.poll_seconds)
            continue

        item["status"] = STATUS_FAILED
        item["last_error"] = truncate(result.stderr or result.stdout, 1000)
        save_queue(paths.queue_file, queue)
        print(f"Errore provider su {item['id']}. Log: {item['last_log']}")
        return result.returncode or 1


def add_common_options(parser: argparse.ArgumentParser, suppress_default: bool = False) -> None:
    default: Any = argparse.SUPPRESS if suppress_default else None
    parser.add_argument("--windows-home", default=default, help="Home Windows da usare, es. C:\\Users\\me")
    parser.add_argument(
        "--state-dir",
        default=default,
        help="Cartella stato/coda. Default: <windows-home>/.claude-vscode-queue",
    )
    parser.add_argument("--claude", default=default, help="Percorso esplicito a claude/claude.exe")
    parser.add_argument("--codex", default=default, help="Percorso esplicito al CLI codex")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-vscode-queue",
        description="Coda locale e auto-continua per sessioni Claude Code e task dell'app Codex.",
    )
    add_common_options(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Verifica ambiente, CLI e chat/task")
    add_common_options(doctor, suppress_default=True)
    doctor.set_defaults(func=command_doctor)

    list_cmd = sub.add_parser("list", help="Lista chat Claude e task Codex selezionabili")
    add_common_options(list_cmd, suppress_default=True)
    list_cmd.add_argument("--limit", type=int, default=25)
    list_cmd.set_defaults(func=command_list)

    add = sub.add_parser("add", help="Accoda messaggi in ordine")
    add_common_options(add, suppress_default=True)
    add.add_argument("--chat", help="Numero/list selector/session id/titolo/cwd")
    add.add_argument("--not-before", help="ISO datetime prima del quale non inviare")
    add.add_argument("--priority", type=int, default=100, help="Priorita' coda: numero piu' basso = prima")
    add.add_argument("--model-override", help="Modello da usare al posto di quello della chat/task")
    add.add_argument("--effort-level-override", help="Effort da usare al posto di quello della chat/task")
    add.add_argument(
        "--permission-mode-override",
        choices=sorted(VALID_PERMISSION_MODES),
        help="Permission mode da passare a Claude al posto di quello della chat",
    )
    add.add_argument(
        "--sandbox-mode-override",
        choices=sorted(VALID_CODEX_SANDBOX_MODES),
        help="Sandbox Codex da usare al posto di quella della task",
    )
    add.add_argument(
        "--approval-policy-override",
        choices=sorted(VALID_CODEX_APPROVAL_POLICIES),
        help="Approval policy Codex da usare al posto di quella della task",
    )
    add.add_argument("items", nargs="*", help="Messaggi in ordine. Usa @file.md per leggere un file, - per stdin.")
    add.set_defaults(func=command_add)

    status = sub.add_parser("status", help="Mostra la coda")
    add_common_options(status, suppress_default=True)
    status.add_argument("-v", "--verbose", action="store_true")
    status.set_defaults(func=command_status)

    run = sub.add_parser("run", help="Processa la coda e ritenta dopo rate/session limit")
    add_common_options(run, suppress_default=True)
    run.add_argument("--once", action="store_true", help="Un solo tentativo/controllo, poi esce")
    run.add_argument("--dry-run", action="store_true", help="Mostra cosa invierebbe senza chiamare il provider")
    run.add_argument("--poll-seconds", type=int, default=300, help="Secondi tra tentativi durante rate limit")
    run.add_argument("--timeout", type=int, default=21600, help="Timeout per singolo messaggio")
    run.add_argument("--ignore-settings-change", action="store_true", help="Invia anche se le impostazioni sono cambiate")
    run.add_argument("--no-ide", dest="ide", action="store_false", help="Non passare --ide a Claude Code")
    run.set_defaults(func=command_run, ide=True)

    check = sub.add_parser("check-settings", help="Controlla fingerprint impostazioni per elementi in coda")
    add_common_options(check, suppress_default=True)
    check.add_argument("item", nargs="?")
    check.set_defaults(func=command_check_settings)

    reset = sub.add_parser("reset", help="Riporta failed/blocked a pending")
    add_common_options(reset, suppress_default=True)
    reset.add_argument("item", nargs="?")
    reset.set_defaults(func=command_reset)

    remove = sub.add_parser("remove", help="Rimuove un singolo elemento per id/prefisso")
    add_common_options(remove, suppress_default=True)
    remove.add_argument("item")
    remove.set_defaults(func=command_remove)

    clear = sub.add_parser("clear", help="Rimuove elementi completati/falliti/bloccati")
    add_common_options(clear, suppress_default=True)
    clear.add_argument("--all", action="store_true", help="Svuota tutta la coda, inclusi pending")
    clear.set_defaults(func=command_clear)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
