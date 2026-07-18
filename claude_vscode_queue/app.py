from __future__ import annotations

import argparse
import base64
import copy
import contextlib
import dataclasses
import datetime as dt
import functools
import hashlib
import json
import mmap
import os
import queue as queue_module
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, unquote, urlparse


APP_DIR_NAME = ".claude-codex-queue"
LEGACY_APP_DIR_NAME = ".claude-vscode-queue"
QUEUE_FILE_NAME = "queue.json"
ACCOUNT_INDEX_FILE_NAME = "accounts.json"
DESKTOP_SYNC_STATE_FILE_NAME = "desktop-sync-state.json"
LOG_DIR_NAME = "logs"
AUTO_CONTINUE_CANCEL_DIR_NAME = "auto-continue-cancellations"
QUEUE_VERSION = 2

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_RECOVERY = "recovery"
RECOVERY_PROMPT = "continua"
CODEX_RECOVERY_PRIORITY = -1000
CLAUDE_DESKTOP_TRY_AGAIN_EXIT = 4

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

UTC = dt.timezone.utc

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

DESKTOP_STATE_ACTIVE = "active"
DESKTOP_STATE_ARCHIVED = "archived"
DESKTOP_STATE_DELETED = "deleted"
DESKTOP_SYNC_STATE_VERSION = 4
DESKTOP_ACCOUNT_LOG_TAIL_BYTES = 4 * 1024 * 1024
DESKTOP_ACCOUNT_EVENT_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?"
    r"\[sessions-bridge\] account-change reevaluate: .*?(?:→|->)\s*"
    r"(?:(?P<organization>[0-9a-fA-F-]{36}):(?P<account>[0-9a-fA-F-]{36})|(?P<none><none>))"
)
DESKTOP_LOG_TIMESTAMP_PATTERN = re.compile(r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
DESKTOP_ACCOUNT_RUNTIME_PATTERNS = (
    re.compile(
        r"claude-code-sessions[\\/]+(?P<account>[0-9a-fA-F-]{36})"
        r"[\\/]+(?P<organization>[0-9a-fA-F-]{36})",
        re.IGNORECASE,
    ),
    re.compile(
        r"local-agent-mode-sessions[\\/]+skills-plugin[\\/]+"
        r"(?P<organization>[0-9a-fA-F-]{36})[\\/]+(?P<account>[0-9a-fA-F-]{36})",
        re.IGNORECASE,
    ),
)
CLAUDE_ARTIFACT_URL_PATTERN = re.compile(
    r"https://claude\.ai/code/artifact/(?P<slug>[A-Za-z0-9_-]{16,64})"
)
CLAUDE_FRAME_API_ORIGIN = "https://api.anthropic.com"
CLAUDE_FRAME_STYLE = (
    "<style>:root{color-scheme:light}body{margin:0;padding:0;"
    "font:14px -apple-system,BlinkMacSystemFont,sans-serif;"
    "background:#faf9f5;color:#141413}img{max-width:100%}</style>"
)
CLAUDE_FRAME_MAX_BYTES = 16 * 1024 * 1024
DESKTOP_ARTIFACT_LOCAL_ONLY_FIELDS = {
    "autoPublish",
    "importedAt",
    "lastRefreshCheckedAt",
    "mcpTools",
    "shareCounter",
    "sharedAnchorConversationUuid",
    "sharedArtifactUuid",
}
CLAUDE_OAUTH_SESSION_CACHE_TTL_SECONDS = 55.0
_DESKTOP_SYNC_LOCK = threading.RLock()
_ACCOUNT_INDEX_LOCK = threading.RLock()
_CLAUDE_OAUTH_SESSION_CACHE_LOCK = threading.RLock()
_DESKTOP_SESSION_FILE_CACHE_LOCK = threading.RLock()
_DESKTOP_ACCOUNT_LOG_CACHE_LOCK = threading.RLock()
_POWERSHELL_SCRIPT_CACHE_LOCK = threading.RLock()
_CLAUDE_OAUTH_SESSION_CACHE: dict[
    str,
    tuple[tuple[str, ...], float, dict[str, dict[str, str]]],
] = {}
_DESKTOP_SESSION_FILE_CACHE: dict[
    str,
    tuple[tuple[int, int, int], dict[str, Any]],
] = {}
_DESKTOP_ACCOUNT_LOG_CACHE: dict[
    str,
    tuple[tuple[str, ...], list[DesktopAccountContext]],
] = {}
_STATE_FILE_LOCKS = threading.local()
ATOMIC_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0)


class StateFileError(ValueError):
    """A durable state file exists but cannot be trusted."""


def _acquire_os_file_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_os_file_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _state_file_lock(thread_lock: threading.RLock, state_path: Path) -> Any:
    """Serialize a state transaction across threads and local processes."""

    lock_path = state_path.with_name(f"{state_path.name}.lock")
    try:
        key = str(lock_path.resolve())
    except OSError:
        key = str(lock_path.absolute())

    with thread_lock:
        active = getattr(_STATE_FILE_LOCKS, "active", None)
        if active is None:
            active = {}
            _STATE_FILE_LOCKS.active = active
        entry = active.get(key)
        if entry is not None:
            entry["depth"] += 1
            try:
                yield
            finally:
                entry["depth"] -= 1
            return

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            _acquire_os_file_lock(handle)
            active[key] = {"depth": 1, "handle": handle}
            try:
                yield
            finally:
                del active[key]
                _release_os_file_lock(handle)
        finally:
            handle.close()


def account_index_lock(paths: Paths) -> Any:
    return _state_file_lock(_ACCOUNT_INDEX_LOCK, account_index_path(paths))


def desktop_sync_lock(paths: Paths) -> Any:
    return _state_file_lock(_DESKTOP_SYNC_LOCK, desktop_sync_state_path(paths))


def atomic_replace_with_retry(source: Path, destination: Path) -> None:
    """Replace a state file atomically, tolerating brief Windows sharing races."""

    for delay in (*ATOMIC_REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


def atomic_write_utf8(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(value)
        temp_path = Path(handle.name)
    atomic_replace_with_retry(temp_path, path)

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
    last_user_message: str | None = None
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
    account_copies: tuple[str, ...] = ()
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
class CodexRecoveryPlan:
    prompt: str
    kind: str
    rollback_turn_ids: tuple[str, ...]
    followup_prompts: tuple[str, ...]
    source_turn_ids: tuple[str, ...]


class AutoContinueCancelled(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class DesktopSessionRecord:
    root: Path
    sessions_root: Path
    account_uuid: str
    workspace_uuid: str
    path: Path
    data: dict[str, Any]
    mtime_ns: int
    active_account_uuid: str | None


@dataclasses.dataclass(frozen=True)
class DesktopAccountContext:
    account_uuid: str | None
    organization_uuid: str | None
    changed_at: str | None
    logged_out: bool = False


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
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def iso_from_epoch_ms(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return dt.datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0).isoformat()
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
    wsl_match = re.match(r"^[\\/]+mnt[\\/]([a-zA-Z])[\\/](.*)$", raw)
    if os.name == "nt" and wsl_match:
        drive = wsl_match.group(1).upper()
        rest = wsl_match.group(2).replace("/", "\\")
        return Path(f"{drive}:\\{rest}")
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
            local_powershell_hidden_command(command),
            capture_output=True,
            timeout=8,
            **background_process_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def chat_cwd_runnable(cwd: str | None) -> bool:
    return cwd_accessible(cwd) or windows_path_accessible(cwd)


def local_to_windows_path(path: Path) -> str:
    raw = str(path)
    match = re.match(r"^[\\/]+mnt[\\/]([a-zA-Z])[\\/](.*)$", raw)
    if match:
        drive = match.group(1).upper()
        rest = match.group(2).replace("/", "\\")
        return f"{drive}:\\{rest}"
    return raw


def canonical_windows_path(value: str | Path) -> str:
    return local_to_windows_path(windows_to_local_path(value))


def is_python_cli_script(executable: Path) -> bool:
    if executable.suffix.lower() == ".py":
        return True
    if executable.suffix:
        return False
    try:
        with executable.open("rb") as handle:
            first_line = handle.readline(256).lower()
    except OSError:
        return False
    return first_line.startswith(b"#!") and b"python" in first_line


def local_executable_command(executable: Path, arguments: list[str]) -> list[str]:
    if os.name == "nt" and is_python_cli_script(executable):
        return [sys.executable, str(executable), *arguments]
    return [str(executable), *arguments]


def windows_executable_command(executable: Path, arguments: list[str]) -> list[str]:
    windows_path = local_to_windows_path(executable)
    if os.name == "nt" and is_python_cli_script(executable):
        return [sys.executable, windows_path, *arguments]
    return [windows_path, *arguments]


def background_process_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"creationflags": creation_flags} if creation_flags else {}


def powershell_script_cache_dir() -> Path | None:
    explicit_state = os.environ.get("CLAUDE_CODEX_QUEUE_STATE") or os.environ.get("CLAUDE_VSCODE_QUEUE_STATE")
    if explicit_state:
        state = windows_to_local_path(explicit_state)
        if os.name == "nt" or is_windows_path(explicit_state) or re.match(r"^/mnt/[a-zA-Z]/", str(state)):
            return state / "tmp" / "powershell"

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "ClaudeCodexQueue" / "PowerShell"
        return Path.home() / APP_DIR_NAME / "tmp" / "powershell"

    home = current_windows_user_home_from_cwd()
    if home is None:
        module_path = Path(__file__).resolve()
        match = re.match(r"^/mnt/([a-zA-Z])/Users/([^/]+)(?:/|$)", module_path.as_posix(), re.IGNORECASE)
        if match:
            home = Path("/mnt") / match.group(1).lower() / "Users" / match.group(2)
    if home is None:
        users_root = Path("/mnt/c/Users")
        try:
            candidates = list(users_root.iterdir())
        except OSError:
            candidates = []
        home = next(
            (
                candidate
                for candidate in candidates
                if candidate.is_dir()
                and any(
                    (candidate / marker).exists()
                    for marker in (APP_DIR_NAME, LEGACY_APP_DIR_NAME, ".claude", ".codex")
                )
            ),
            None,
        )
    if home is None:
        return None
    preferred = home / APP_DIR_NAME
    legacy = home / LEGACY_APP_DIR_NAME
    state = preferred if preferred.exists() or not legacy.exists() else legacy
    return state / "tmp" / "powershell"


def cached_powershell_script(script: str) -> Path:
    cache_dir = powershell_script_cache_dir()
    if cache_dir is None:
        raise OSError("Cache Windows PowerShell non disponibile.")
    normalized = script.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
    payload = b"\xef\xbb\xbf" + normalized.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    path = cache_dir / f"script-{digest}.ps1"
    with _POWERSHELL_SCRIPT_CACHE_LOCK:
        try:
            if path.read_bytes() == payload:
                return path
        except OSError:
            pass
        cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=cache_dir, delete=False) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        try:
            if os.name != "nt":
                temporary.chmod(0o600)
            atomic_replace_with_retry(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return path


def local_powershell_hidden_command(script: str, powershell: str | None = None) -> list[str]:
    executable = powershell or shutil.which("powershell.exe") or "powershell.exe"
    script_path = cached_powershell_script(script)
    return [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        local_to_windows_path(script_path),
    ]


WINDOWS_HIDDEN_PROXY_CSHARP = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

public static class HiddenProcessProxy
{
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr securityAttributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        int infoClass,
        ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info,
        uint infoLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    private static string Quote(string value)
    {
        if (value.Length > 0 && value.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
            return value;
        var result = new StringBuilder("\"");
        var backslashes = 0;
        foreach (var character in value)
        {
            if (character == '\\')
            {
                backslashes++;
                continue;
            }
            if (character == '"')
                result.Append('\\', backslashes * 2 + 1);
            else
                result.Append('\\', backslashes);
            backslashes = 0;
            result.Append(character);
        }
        result.Append('\\', backslashes * 2);
        result.Append('"');
        return result.ToString();
    }

    private static void Copy(Stream source, Stream destination, bool closeDestination)
    {
        try
        {
            var buffer = new byte[8192];
            int count;
            while ((count = source.Read(buffer, 0, buffer.Length)) > 0)
            {
                destination.Write(buffer, 0, count);
                destination.Flush();
            }
        }
        catch (IOException) { }
        catch (ObjectDisposedException) { }
        finally
        {
            if (closeDestination)
            {
                try { destination.Close(); } catch { }
            }
        }
    }

    public static int Run(string fileName, string[] arguments, string workingDirectory)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = fileName,
            Arguments = String.Join(" ", Array.ConvertAll(arguments, Quote)),
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false)
        };
        if (!String.IsNullOrEmpty(workingDirectory))
            startInfo.WorkingDirectory = workingDirectory;

        using (var process = new Process { StartInfo = startInfo })
        using (var job = new SafeFileHandle(CreateJobObject(IntPtr.Zero, null), true))
        {
            if (job.IsInvalid)
                throw new InvalidOperationException("CreateJobObject failed: " + Marshal.GetLastWin32Error());
            var limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if (!SetInformationJobObject(job.DangerousGetHandle(), 9, ref limits, (uint)Marshal.SizeOf(limits)))
                throw new InvalidOperationException("SetInformationJobObject failed: " + Marshal.GetLastWin32Error());
            if (!process.Start())
                throw new InvalidOperationException("Unable to start hidden child process.");
            if (!AssignProcessToJobObject(job.DangerousGetHandle(), process.Handle))
            {
                try { process.Kill(); } catch { }
                throw new InvalidOperationException("AssignProcessToJobObject failed: " + Marshal.GetLastWin32Error());
            }

            var input = new Thread(() => Copy(Console.OpenStandardInput(), process.StandardInput.BaseStream, true));
            var output = new Thread(() => Copy(process.StandardOutput.BaseStream, Console.OpenStandardOutput(), false));
            var error = new Thread(() => Copy(process.StandardError.BaseStream, Console.OpenStandardError(), false));
            input.IsBackground = true;
            output.IsBackground = true;
            error.IsBackground = true;
            input.Start();
            output.Start();
            error.Start();
            process.WaitForExit();
            output.Join(3000);
            error.Join(3000);
            return process.ExitCode;
        }
    }
}
'''


def local_windows_hidden_command(command: list[str], cwd: str | None = None) -> list[str]:
    if not command:
        raise ValueError("Comando Windows vuoto.")
    arguments = ", ".join(powershell_single_quote(value) for value in command[1:])
    source = base64.b64encode(WINDOWS_HIDDEN_PROXY_CSHARP.encode("utf-8")).decode("ascii")
    source_hash = hashlib.sha256(WINDOWS_HIDDEN_PROXY_CSHARP.encode("utf-8")).hexdigest()[:16]
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        "$InformationPreference = 'SilentlyContinue'",
        "$utf8 = [System.Text.UTF8Encoding]::new($false)",
        "[Console]::InputEncoding = $utf8",
        "[Console]::OutputEncoding = $utf8",
        "$OutputEncoding = $utf8",
        f"$source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{source}'))",
        "$cache = Join-Path $env:LOCALAPPDATA 'ClaudeCodexQueue'",
        f"$assembly = Join-Path $cache 'HiddenProcessProxy-{source_hash}.dll'",
        f"$mutex = [Threading.Mutex]::new($false, 'Local\\ClaudeCodexQueueHiddenProxy-{source_hash}')",
        "$locked = $mutex.WaitOne(30000)",
        "if (-not $locked) { throw 'Timeout preparando il proxy Windows nascosto.' }",
        "try {",
        "  if (-not (Test-Path -LiteralPath $assembly)) {",
        "    New-Item -ItemType Directory -Path $cache -Force | Out-Null",
        "    $temporary = $assembly + '.' + $PID + '.tmp.dll'",
        "    try {",
        "      Add-Type -TypeDefinition $source -Language CSharp -OutputAssembly $temporary -OutputType Library | Out-Null",
        "      Move-Item -LiteralPath $temporary -Destination $assembly -Force",
        "    } finally {",
        "      Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue",
        "    }",
        "  }",
        "} finally {",
        "  $mutex.ReleaseMutex()",
        "  $mutex.Dispose()",
        "}",
        "Add-Type -Path $assembly | Out-Null",
    ]
    lines.extend(
        [
            (
                f"$code = [HiddenProcessProxy]::Run("
                f"{powershell_single_quote(command[0])}, "
                f"[string[]]@({arguments}), "
                f"{powershell_single_quote(cwd or '')})"
            ),
            "exit $code",
        ]
    )
    return local_powershell_hidden_command("\n".join(lines))


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
    env_home = (
        os.environ.get("CLAUDE_CODEX_QUEUE_WINDOWS_HOME")
        or os.environ.get("CLAUDE_QUEUE_WINDOWS_HOME")
        or os.environ.get("USERPROFILE")
    )
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
    selected_home = windows_to_local_path(windows_home) if windows_home else None
    if selected_home is None:
        for home in home_candidates:
            if path_exists(home / ".claude" / "projects") or path_exists(home / ".codex" / "session_index.jsonl"):
                selected_home = home
                break
    if selected_home is None:
        selected_home = home_candidates[0] if home_candidates else Path.home()
    claude_home = selected_home / ".claude"
    if state_dir:
        state = windows_to_local_path(state_dir)
    else:
        preferred_state = selected_home / APP_DIR_NAME
        legacy_state = selected_home / LEGACY_APP_DIR_NAME
        state = preferred_state if path_exists(preferred_state) or not path_exists(legacy_state) else legacy_state
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


def claude_item_is_desktop(item: dict[str, Any]) -> bool:
    return item.get("source_key") == "claude_windows_app" or item.get("source") == "Claude Windows App"


def claude_desktop_session_version(paths: Paths, session_id: str) -> str | None:
    try:
        session_files = list((paths.claude_home / "sessions").glob("*.json"))
    except OSError:
        return None
    for session_file in session_files:
        data = load_json_file(session_file)
        if data.get("sessionId") != session_id:
            continue
        version = data.get("version")
        return version if isinstance(version, str) and version else None
    return None


def find_claude_desktop_executable(paths: Paths, item: dict[str, Any]) -> Path | None:
    if not claude_item_is_desktop(item):
        return None
    root = paths.windows_home / "AppData" / "Roaming" / "Claude" / "claude-code"
    session_id = item.get("session_id")
    version = claude_desktop_session_version(paths, session_id) if isinstance(session_id, str) else None
    if version:
        exact = root / version / "claude.exe"
        if exact.exists():
            return exact
    try:
        candidates = [candidate for candidate in root.glob("*/claude.exe") if candidate.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate.stat().st_mtime_ns, reverse=True)
    return candidates[0]


def claude_desktop_local_session_id(paths: Paths, item: dict[str, Any]) -> str | None:
    session_id = item.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    account_key = item.get("account_key")
    account_uuid = (
        account_key.removeprefix("claude-app:")
        if isinstance(account_key, str) and account_key.startswith("claude-app:")
        else None
    )
    candidates: list[DesktopSessionRecord] = []
    sync_state = load_desktop_sync_state(paths)
    for record in desktop_session_records(paths):
        physical_session_id = desktop_record_cli_session_id(record)
        logical_session_id = (
            desktop_logical_session_id(desktop_existing_root_state(sync_state, record.root), physical_session_id)
            if physical_session_id
            else None
        )
        if logical_session_id != session_id:
            continue
        app_session_id = record.data.get("sessionId")
        if not isinstance(app_session_id, str) or not app_session_id:
            continue
        candidates.append(record)
    if not candidates:
        return None
    candidates.sort(
        key=lambda record: (
            record.account_uuid == account_uuid,
            record.account_uuid == record.active_account_uuid,
            desktop_record_mtime_ns(record),
        ),
        reverse=True,
    )
    value = candidates[0].data.get("sessionId")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) else None


def normalize_powershell_automation_error(text: str) -> str:
    if "CLAUDE_TRY_AGAIN_NOT_FOUND" in text:
        return "CLAUDE_TRY_AGAIN_NOT_FOUND"
    if "#< CLIXML" not in text:
        return text
    return "Automazione Windows PowerShell non riuscita."


def run_claude_desktop_try_again(
    paths: Paths,
    item: dict[str, Any],
    timeout: int = 20,
    scan_seconds: int = 12,
) -> ClaudeRunResult:
    local_session_id = claude_desktop_local_session_id(paths, item)
    if not local_session_id:
        return ClaudeRunResult(
            returncode=CLAUDE_DESKTOP_TRY_AGAIN_EXIT,
            stdout="",
            stderr="ID locale della sessione Claude Desktop non trovato.",
            rate_limited=False,
            reset_at=None,
        )
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return ClaudeRunResult(
            returncode=CLAUDE_DESKTOP_TRY_AGAIN_EXIT,
            stdout="",
            stderr="Windows PowerShell non trovato: impossibile invocare Try again.",
            rate_limited=False,
            reset_at=None,
        )
    script = r'''
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$InformationPreference = "SilentlyContinue"
$sessionId = "__SESSION_ID__"
Start-Process ("claude://code/" + $sessionId) | Out-Null
Start-Sleep -Milliseconds 1200
Add-Type -AssemblyName UIAutomationClient | Out-Null
Add-Type -AssemblyName UIAutomationTypes | Out-Null
$names = @("Try again", "Retry", "Riprova", "Prova di nuovo", "Ritenta")
$deadline = [DateTime]::UtcNow.AddSeconds(__SCAN_SECONDS__)
do {
  Start-Sleep -Milliseconds 750
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $windows = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition
  )
  $candidates = @()
  foreach ($window in $windows) {
    try {
      $process = Get-Process -Id $window.Current.ProcessId -ErrorAction Stop
    } catch {
      continue
    }
    if ($process.ProcessName -ne "claude") { continue }
    $buttonCondition = [System.Windows.Automation.PropertyCondition]::new(
      [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
      [System.Windows.Automation.ControlType]::Button
    )
    $buttons = $window.FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      $buttonCondition
    )
    foreach ($button in $buttons) {
      $name = $button.Current.Name
      if ($button.Current.IsEnabled -and $names -contains $name) {
        $candidates += $button
      }
    }
  }
  if ($candidates.Count -gt 0) {
    $button = $candidates | Sort-Object { $_.Current.BoundingRectangle.Bottom } -Descending | Select-Object -First 1
    $pattern = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    ([System.Windows.Automation.InvokePattern]$pattern).Invoke()
    Write-Output ("CLAUDE_TRY_AGAIN_INVOKED:" + $button.Current.Name)
    exit 0
  }
} while ([DateTime]::UtcNow -lt $deadline)
[Console]::Error.WriteLine("CLAUDE_TRY_AGAIN_NOT_FOUND")
exit 4
'''.replace("__SESSION_ID__", local_session_id).replace(
        "__SCAN_SECONDS__",
        str(max(3, min(int(scan_seconds), max(3, int(timeout) - 3)))),
    )
    try:
        process = subprocess.run(
            local_powershell_hidden_command(script, powershell),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            **background_process_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return ClaudeRunResult(
            returncode=CLAUDE_DESKTOP_TRY_AGAIN_EXIT,
            stdout="",
            stderr=f"Timeout dopo {timeout}s cercando il pulsante Try again.",
            rate_limited=False,
            reset_at=None,
        )
    stdout = process.stdout or ""
    stderr = process.stderr or ""
    not_found = "CLAUDE_TRY_AGAIN_NOT_FOUND" in f"{stdout}\n{stderr}"
    return ClaudeRunResult(
        returncode=CLAUDE_DESKTOP_TRY_AGAIN_EXIT if not_found else process.returncode,
        stdout=stdout,
        stderr=normalize_powershell_automation_error(stderr),
        rate_limited=False,
        reset_at=None,
    )


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


def codex_executable_is_windows(codex_exe: Path) -> bool:
    if is_python_cli_script(codex_exe):
        return False
    return codex_exe.suffix.lower() in {".cmd", ".bat", ".exe"} or (is_wsl() and is_windows_path(str(codex_exe)))


def codex_cli_command(codex_exe: Path, arguments: list[str]) -> list[str]:
    if codex_executable_is_windows(codex_exe):
        executable = local_to_windows_path(codex_exe)
        if is_wsl():
            return local_windows_hidden_command([executable, *arguments])
        return ["cmd.exe", "/d", "/c", "call", executable, *arguments]
    return [str(codex_exe), *arguments]


def run_codex_cli_command(
    codex_exe: Path,
    arguments: list[str],
    timeout: int = 15,
    *,
    codex_home: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        codex_cli_command(codex_exe, arguments),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        env=codex_subprocess_env(
            codex_home,
            windows=codex_executable_is_windows(codex_exe),
        ),
        **background_process_kwargs(),
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


def public_error_message(
    error: BaseException | str,
    fallback: str = "Operazione non riuscita.",
    *,
    timeout_message: str = "Operazione temporaneamente lenta; nuovo tentativo automatico.",
) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return timeout_message
    text = str(error or "").strip()
    if not text:
        return fallback
    if "#< CLIXML" in text:
        return fallback
    if re.search(r"(?i)Command\s+['\"]?\[", text) or re.search(r"(?i)-EncodedCommand\b", text):
        return fallback
    text = re.sub(
        r"(?i)(?:Bearer\s+|(?:api[-_ ]?key|token)\s*[:=]\s*)[A-Za-z0-9._~+/=-]{12,}",
        "<credenziale rimossa>",
        text,
    )
    text = re.sub(r"\b[A-Za-z0-9+/]{160,}={0,2}\b", "<dettaglio rimosso>", text)
    return truncate(text, 320) or fallback


def message_preview(value: str | None, length: int = 240) -> str | None:
    preview = truncate(value, length)
    return preview or None


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


def load_durable_json_object(path: Path, label: str) -> dict[str, Any] | None:
    """Read protected state, distinguishing absence from corruption."""

    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise StateFileError(f"{label} non leggibile ({path}): {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateFileError(f"{label} corrotto ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise StateFileError(f"{label} corrotto ({path}): la radice JSON deve essere un oggetto.")
    return data


def file_mtime_iso(path: Path) -> str | None:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).astimezone().replace(microsecond=0).isoformat()
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

    configured_account_uuid = (
        oauth_account.get("accountUuid") if isinstance(oauth_account.get("accountUuid"), str) else None
    )
    configured_email = (
        oauth_account.get("emailAddress") if isinstance(oauth_account.get("emailAddress"), str) else None
    )
    configured_organization_uuid = (
        oauth_account.get("organizationUuid")
        if isinstance(oauth_account.get("organizationUuid"), str)
        else None
    )
    credential_organization_uuid = (
        credentials.get("organizationUuid") if isinstance(credentials.get("organizationUuid"), str) else None
    )
    organization_uuid = credential_organization_uuid or configured_organization_uuid
    account_uuid = configured_account_uuid
    email = configured_email
    if (
        credential_organization_uuid
        and configured_organization_uuid
        and credential_organization_uuid != configured_organization_uuid
    ):
        verified = cached_claude_oauth_profile(paths, credential_organization_uuid)
        account_uuid = verified.get("account_uuid") if verified else None
        email = verified.get("email") if verified else None
    refresh_token = claude_oauth.get("refreshToken") if isinstance(claude_oauth.get("refreshToken"), str) else None
    access_token = claude_oauth.get("accessToken") if isinstance(claude_oauth.get("accessToken"), str) else None
    if not any([account_uuid, email, organization_uuid, refresh_token, access_token]):
        return None

    if account_uuid:
        key_source = account_uuid
    elif email:
        key_source = "|".join([email.lower(), organization_uuid or ""])
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

    # account_id is stable across token refreshes; email is display metadata, not identity.
    identity = account_id or subject or (email.lower() if email else "")
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
    with account_index_lock(paths):
        data = load_durable_json_object(account_index_path(paths), "Indice account")
        if data is None:
            return {"version": 1, "accounts": {}, "sessions": {}}
        for key in ("accounts", "sessions", "codex_links"):
            if key in data and not isinstance(data[key], dict):
                raise StateFileError(
                    f"Indice account corrotto ({account_index_path(paths)}): '{key}' deve essere un oggetto."
                )
        data.setdefault("version", 1)
        data.setdefault("accounts", {})
        data.setdefault("sessions", {})
        return data


def save_account_index(paths: Paths, index: dict[str, Any]) -> None:
    with account_index_lock(paths):
        path = account_index_path(paths)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(index, indent=2, ensure_ascii=False) + "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        atomic_replace_with_retry(temp_path, path)


def migrate_account_key(index: dict[str, Any], account: AccountInfo, provider: str) -> None:
    if not account.account_uuid_hash:
        return
    accounts = index.setdefault("accounts", {})
    for old_key, old_data in list(accounts.items()):
        if old_key == account.key or not isinstance(old_data, dict):
            continue
        old_provider = old_data.get("provider") or PROVIDER_CLAUDE
        if old_provider != provider or old_data.get("account_uuid_hash") != account.account_uuid_hash:
            continue
        current = accounts.get(account.key) if isinstance(accounts.get(account.key), dict) else {}
        accounts[account.key] = {**old_data, **current, "key": account.key}
        del accounts[old_key]
        sessions = index.get("sessions") if isinstance(index.get("sessions"), dict) else {}
        for session in sessions.values():
            if isinstance(session, dict) and session.get("account_key") == old_key:
                session["account_key"] = account.key
        links = index.get("codex_links") if isinstance(index.get("codex_links"), dict) else {}
        for group in links.values():
            threads = group.get("threads") if isinstance(group, dict) and isinstance(group.get("threads"), dict) else {}
            for thread in threads.values():
                if isinstance(thread, dict) and thread.get("account_key") == old_key:
                    thread["account_key"] = account.key


def _register_active_account_unlocked(paths: Paths, index: dict[str, Any] | None = None) -> AccountInfo | None:
    account = active_claude_account(paths)
    if account is None:
        return None
    index = index if index is not None else load_account_index(paths)
    accounts = index.setdefault("accounts", {})
    migrate_account_key(index, account, PROVIDER_CLAUDE)
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


def register_active_account(paths: Paths, index: dict[str, Any] | None = None) -> AccountInfo | None:
    with account_index_lock(paths):
        return _register_active_account_unlocked(paths, index)


def _register_active_codex_account_unlocked(paths: Paths, index: dict[str, Any] | None = None) -> AccountInfo | None:
    account = active_codex_account(paths)
    if account is None:
        return None
    index = index if index is not None else load_account_index(paths)
    accounts = index.setdefault("accounts", {})
    migrate_account_key(index, account, PROVIDER_CODEX)
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


def register_active_codex_account(paths: Paths, index: dict[str, Any] | None = None) -> AccountInfo | None:
    with account_index_lock(paths):
        return _register_active_codex_account_unlocked(paths, index)


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
        return dt.datetime.fromtimestamp(chat.jsonl_path.stat().st_mtime, tz=UTC)
    except OSError:
        return dt.datetime.min.replace(tzinfo=UTC)


def discover_chats(claude_home: Path, excluded_session_ids: set[str] | None = None) -> list[Chat]:
    projects = claude_home / "projects"
    if not projects.exists():
        return []

    by_session: dict[str, Chat] = {}
    excluded = excluded_session_ids or set()
    for jsonl_path in projects.glob("**/*.jsonl"):
        if jsonl_path.stem in excluded:
            continue
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
        last_user_message: str | None = None
        last_user_message_dt: dt.datetime | None = None
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
                        if obj_type == "user":
                            text = message_text(message)
                            newer_user_message = (
                                timestamp_dt is not None
                                and (last_user_message_dt is None or timestamp_dt >= last_user_message_dt)
                            ) or (timestamp_dt is None and last_user_message_dt is None)
                            if text and newer_user_message:
                                last_user_message = message_preview(text)
                                last_user_message_dt = timestamp_dt
                        parsed_model = real_model_value(message.get("model"))
                        if parsed_model:
                            model = parsed_model
        except OSError:
            continue

        if not session_id:
            continue
        display_title = title or truncate(last_prompt, 60) or (Path(cwd).name if cwd else "Claude chat")
        last_user_message = message_preview(last_prompt) or last_user_message
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
            last_user_message=last_user_message,
            effort_level=effort_level,
            can_queue=chat_cwd_runnable(cwd),
        )
        existing = by_session.get(session_id)
        if existing is None:
            by_session[session_id] = chat
        else:
            old_timestamp = parse_iso(existing.last_timestamp)
            new_timestamp = parse_iso(chat.last_timestamp)
            old_key = old_timestamp or dt.datetime.fromtimestamp(existing.jsonl_path.stat().st_mtime, tz=UTC)
            new_key = new_timestamp or dt.datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=UTC)
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
        "title",
        "first_user_message",
        "source",
        "thread_source",
        "model",
        "reasoning_effort",
        "sandbox_policy",
        "approval_mode",
        "archived",
        "archived_at",
        "created_at",
        "updated_at",
        "recency_at",
        "created_at_ms",
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
        command = [
            "py.exe",
            "-3",
            "-c",
            script,
            local_to_windows_path(database),
            json.dumps(columns),
        ]
        result = subprocess.run(
            local_windows_hidden_command(command),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=15,
            **background_process_kwargs(),
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
    for key in ["recency_at", "updated_at", "created_at"]:
        value = row.get(key)
        if not isinstance(value, (int, float)):
            continue
        try:
            values.append(dt.datetime.fromtimestamp(value, tz=UTC))
        except (OSError, OverflowError, ValueError):
            pass
        break
    if not values:
        return None
    return max(values).astimezone().replace(microsecond=0).isoformat()


def codex_app_server_request(
    codex_exe: Path,
    method: str,
    params: dict[str, Any],
    timeout: int = 45,
    *,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    command = codex_cli_command(codex_exe, ["app-server", "--listen", "stdio://"])
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=codex_subprocess_env(
            codex_home,
            windows=codex_executable_is_windows(codex_exe),
        ),
        **background_process_kwargs(),
    )
    responses: queue_module.Queue[dict[str, Any]] = queue_module.Queue()
    stderr_lines: list[str] = []

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            value = safe_json_loads(line)
            if value:
                responses.put(value)

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line.rstrip())
            if len(stderr_lines) > 50:
                del stderr_lines[:-50]

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise ValueError("stdin di Codex app-server non disponibile.")
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def wait_for(request_id: int, deadline: float) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = truncate(" ".join(stderr_lines), 500) or "timeout"
                raise ValueError(f"Codex app-server non ha risposto a {method}: {detail}")
            try:
                value = responses.get(timeout=remaining)
            except queue_module.Empty as exc:
                detail = truncate(" ".join(stderr_lines), 500) or "timeout"
                raise ValueError(f"Codex app-server non ha risposto a {method}: {detail}") from exc
            if value.get("id") == request_id:
                return value

    deadline = time.monotonic() + timeout
    try:
        send({
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "claude-codex-queue", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        })
        initialize = wait_for(1, deadline)
        if isinstance(initialize.get("error"), dict):
            raise ValueError(f"Inizializzazione Codex app-server fallita: {initialize['error']}")
        send({"method": "initialized"})
        request_id = 2
        if method == "thread/rollback":
            thread_id = params.get("threadId")
            if not isinstance(thread_id, str) or not thread_id:
                raise ValueError("thread/rollback richiede un threadId valido.")
            send(
                {
                    "id": request_id,
                    "method": "thread/resume",
                    "params": {"threadId": thread_id},
                }
            )
            resumed = wait_for(request_id, deadline)
            resume_error = resumed.get("error")
            if isinstance(resume_error, dict):
                message = (
                    resume_error.get("message")
                    if isinstance(resume_error.get("message"), str)
                    else json.dumps(resume_error)
                )
                raise ValueError(f"Codex thread/resume prima del rollback non riuscito: {message}")
            request_id += 1
        send({"id": request_id, "method": method, "params": params})
        response = wait_for(request_id, deadline)
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    error = response.get("error")
    if isinstance(error, dict):
        message = error.get("message") if isinstance(error.get("message"), str) else json.dumps(error)
        raise ValueError(f"Codex {method} non riuscito: {message}")
    value = response.get("result")
    if not isinstance(value, dict):
        raise ValueError(f"Risposta Codex {method} non valida.")
    return value


def codex_thread_turns(
    codex_exe: Path,
    session_id: str,
    *,
    codex_home: Path | None = None,
) -> list[dict[str, Any]]:
    result = codex_app_server_request(
        codex_exe,
        "thread/read",
        {"threadId": session_id, "includeTurns": True},
        codex_home=codex_home,
    )
    thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
    turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
    return [turn for turn in turns if isinstance(turn, dict)]


def codex_turn_user_prompt(turn: dict[str, Any]) -> str | None:
    items = turn.get("items") if isinstance(turn.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None
        parts: list[str] = []
        for entry in content:
            if not isinstance(entry, dict):
                return None
            if entry.get("type") != "text" or not isinstance(entry.get("text"), str):
                return None
            parts.append(entry["text"])
        return "\n".join(parts) if parts else None
    return None


def codex_turn_has_progress(turn: dict[str, Any]) -> bool:
    progress_types = {
        "agentMessage",
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "collabAgentToolCall",
        "subAgentActivity",
        "webSearch",
        "imageGeneration",
        "enteredReviewMode",
        "exitedReviewMode",
        "contextCompaction",
    }
    items = turn.get("items") if isinstance(turn.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in progress_types:
            return True
        if item_type == "reasoning" and (item.get("content") or item.get("summary")):
            return True
    return False


def codex_recovery_plan_from_turns(turns: list[dict[str, Any]]) -> CodexRecoveryPlan:
    if turns and turns[-1].get("status") in {"inProgress", "in_progress", "running"}:
        raise ValueError("La task Codex e' ancora in esecuzione; il recupero attende che il turno si fermi.")

    recoverable: list[dict[str, Any]] = []
    for turn in reversed(turns):
        if turn.get("status") not in {"failed", "interrupted"}:
            break
        recoverable.append(turn)
    recoverable.reverse()
    if not recoverable:
        return CodexRecoveryPlan(
            prompt=RECOVERY_PROMPT,
            kind="continue_interrupted",
            rollback_turn_ids=(),
            followup_prompts=(),
            source_turn_ids=(),
        )

    progress_indexes = [index for index, turn in enumerate(recoverable) if codex_turn_has_progress(turn)]
    if progress_indexes:
        progress_index = progress_indexes[-1]
        if progress_index != 0:
            raise ValueError(
                "Codex mostra un turno senza risposta prima di un turno gia' avviato: non modifico la cronologia automaticamente."
            )
        rollback_turns = recoverable[progress_index + 1 :]
        prompts: list[str] = []
        for turn in rollback_turns:
            prompt = codex_turn_user_prompt(turn)
            if not prompt:
                raise ValueError(
                    "Un messaggio Codex fallito contiene allegati o input non testuali e non puo' essere reinviato in sicurezza."
                )
            prompts.append(prompt)
        return CodexRecoveryPlan(
            prompt=RECOVERY_PROMPT,
            kind="continue_interrupted",
            rollback_turn_ids=tuple(
                str(turn.get("id")) for turn in rollback_turns if isinstance(turn.get("id"), str)
            ),
            followup_prompts=tuple(prompts),
            source_turn_ids=tuple(
                str(turn.get("id")) for turn in recoverable if isinstance(turn.get("id"), str)
            ),
        )

    prompts = []
    for turn in recoverable:
        prompt = codex_turn_user_prompt(turn)
        if not prompt:
            raise ValueError(
                "Un messaggio Codex fallito contiene allegati o input non testuali e non puo' essere reinviato in sicurezza."
            )
        prompts.append(prompt)
    rollback_ids = tuple(
        str(turn.get("id")) for turn in recoverable if isinstance(turn.get("id"), str)
    )
    if len(rollback_ids) != len(recoverable):
        raise ValueError("Codex non ha restituito tutti gli ID dei turni falliti; cronologia non modificata.")
    return CodexRecoveryPlan(
        prompt=prompts[0],
        kind="retry_failed_prompt",
        rollback_turn_ids=rollback_ids,
        followup_prompts=tuple(prompts[1:]),
        source_turn_ids=rollback_ids,
    )


def codex_recovery_plan(
    codex_exe: Path,
    session_id: str,
    *,
    codex_home: Path | None = None,
) -> CodexRecoveryPlan:
    return codex_recovery_plan_from_turns(
        codex_thread_turns(codex_exe, session_id, codex_home=codex_home)
    )


def apply_codex_rollback(
    codex_exe: Path,
    session_id: str,
    turn_ids: tuple[str, ...] | list[str],
    *,
    codex_home: Path | None = None,
) -> bool:
    expected = tuple(turn_id for turn_id in turn_ids if isinstance(turn_id, str) and turn_id)
    if not expected:
        return False
    turns = codex_thread_turns(codex_exe, session_id, codex_home=codex_home)
    current_ids = tuple(str(turn.get("id")) for turn in turns if isinstance(turn.get("id"), str))
    if not any(turn_id in current_ids for turn_id in expected):
        return False
    if len(current_ids) < len(expected) or current_ids[-len(expected) :] != expected:
        raise ValueError("La cronologia Codex e' cambiata dopo il piano di recupero; rollback annullato.")
    codex_app_server_request(
        codex_exe,
        "thread/rollback",
        {"threadId": session_id, "numTurns": len(expected)},
        codex_home=codex_home,
    )
    return True


def rollback_latest_failed_codex_prompt(
    codex_exe: Path,
    session_id: str,
    prompt: str,
    *,
    codex_home: Path | None = None,
) -> str:
    turns = codex_thread_turns(codex_exe, session_id, codex_home=codex_home)
    if not turns:
        raise ValueError("La task Codex non contiene il tentativo fallito da sostituire.")
    turn = turns[-1]
    recorded = codex_turn_user_prompt(turn)
    if (
        turn.get("status") not in {"failed", "interrupted"}
        or codex_turn_has_progress(turn)
        or normalize_prompt(recorded) != normalize_prompt(prompt)
    ):
        raise ValueError("L'ultimo turno Codex non coincide con il prompt fallito atteso; non eseguo il rollback.")
    turn_id = turn.get("id")
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("L'ultimo turno Codex non ha un ID valido.")
    apply_codex_rollback(codex_exe, session_id, (turn_id,), codex_home=codex_home)
    return turn_id


def codex_local_thread_states(paths: Paths) -> dict[str, str]:
    rows = codex_thread_rows(paths.codex_home)
    active_files, archived_files = codex_rollout_files(paths.codex_home)
    states: dict[str, str] = {}
    for session_id in set(rows) | set(active_files) | set(archived_files):
        row = rows.get(session_id, {})
        archived = bool(row.get("archived"))
        if session_id.lower() in archived_files and session_id.lower() not in active_files:
            archived = True
        states[session_id] = DESKTOP_STATE_ARCHIVED if archived else DESKTOP_STATE_ACTIVE
    return states


def run_codex_lifecycle(
    paths: Paths,
    codex_exe: Path,
    action: str,
    session_id: str,
) -> None:
    if action not in {DESKTOP_STATE_ACTIVE, DESKTOP_STATE_ARCHIVED, DESKTOP_STATE_DELETED}:
        raise ValueError(f"Stato Codex non supportato: {action}")
    arguments = {
        DESKTOP_STATE_ACTIVE: ["unarchive", session_id],
        DESKTOP_STATE_ARCHIVED: ["archive", session_id],
        DESKTOP_STATE_DELETED: ["delete", "--force", session_id],
    }[action]
    result = run_codex_cli_command(
        codex_exe,
        arguments,
        timeout=60,
        codex_home=paths.codex_home,
    )
    if result.returncode != 0:
        detail = truncate(result.stderr or result.stdout, 500) or f"exit {result.returncode}"
        raise ValueError(f"Codex {arguments[0]} non riuscito per {session_id[:8]}: {detail}")
    states = codex_local_thread_states(paths)
    observed = states.get(session_id)
    expected = None if action == DESKTOP_STATE_DELETED else action
    if observed != expected:
        raise ValueError(
            f"Codex {arguments[0]} non verificato per {session_id[:8]}: stato osservato {observed or 'assente'}."
        )


def sync_codex_linked_threads(paths: Paths) -> dict[str, Any]:
    result: dict[str, Any] = {"groups": 0, "updated": 0, "deleted": 0, "pending": 0, "errors": []}
    with account_index_lock(paths):
        index = load_account_index(paths)
        links = index.get("codex_links") if isinstance(index.get("codex_links"), dict) else {}
        if not links:
            return result
        states = codex_local_thread_states(paths)
        tracked_live_threads = any(
            isinstance(thread, dict) and thread.get("last_state") != DESKTOP_STATE_DELETED
            for group in links.values()
            if isinstance(group, dict)
            for thread in (
                group["threads"].values()
                if isinstance(group.get("threads"), dict)
                else []
            )
        )
        if tracked_live_threads and not states:
            result["errors"].append("Store Codex temporaneamente vuoto o non leggibile: sincronizzazione sospesa.")
            return result
        codex_exe = find_codex_executable(paths)
        active_account = active_codex_account(paths)
        changed = False

        for group_id, group in links.items():
            if not isinstance(group, dict):
                continue
            threads = group.get("threads") if isinstance(group.get("threads"), dict) else {}
            if not threads:
                continue
            result["groups"] += 1
            events: list[tuple[int, str]] = []
            for session_id, record in threads.items():
                if not isinstance(record, dict):
                    continue
                current = states.get(session_id)
                previous = record.get("last_state")
                if current is not None:
                    if previous in {DESKTOP_STATE_ACTIVE, DESKTOP_STATE_ARCHIVED} and current != previous:
                        priority = 2 if current == DESKTOP_STATE_ARCHIVED else 1
                        events.append((priority, current))
                    if record.get("missing_scans"):
                        record["missing_scans"] = 0
                        changed = True
                    continue
                if previous == DESKTOP_STATE_DELETED:
                    continue
                missing_scans = int(record.get("missing_scans") or 0) + 1
                record["missing_scans"] = missing_scans
                changed = True
                if missing_scans >= 2:
                    events.append((3, DESKTOP_STATE_DELETED))
                else:
                    result["pending"] += 1

            canonical = group.get("state")
            if canonical not in {DESKTOP_STATE_ACTIVE, DESKTOP_STATE_ARCHIVED, DESKTOP_STATE_DELETED}:
                canonical = next((states.get(session_id) for session_id in threads if states.get(session_id)), DESKTOP_STATE_ACTIVE)
            if events:
                canonical = max(events, key=lambda event: event[0])[1]
            if group.get("state") != canonical:
                group["state"] = canonical
                group["state_changed_at"] = now_utc()
                changed = True

            if codex_exe is not None:
                for session_id, thread_record in threads.items():
                    current = states.get(session_id)
                    needs_action = (
                        canonical == DESKTOP_STATE_DELETED and current is not None
                    ) or (
                        canonical in {DESKTOP_STATE_ACTIVE, DESKTOP_STATE_ARCHIVED}
                        and current is not None
                        and current != canonical
                    )
                    if not needs_action:
                        continue
                    linked_account_key = (
                        thread_record.get("account_key")
                        if isinstance(thread_record, dict) and isinstance(thread_record.get("account_key"), str)
                        else None
                    )
                    if linked_account_key and (
                        active_account is None or linked_account_key != active_account.key
                    ):
                        thread_record["pending_state"] = canonical
                        result["pending"] += 1
                        changed = True
                        continue
                    try:
                        run_codex_lifecycle(paths, codex_exe, canonical, session_id)
                        if isinstance(thread_record, dict):
                            thread_record.pop("pending_state", None)
                        if canonical == DESKTOP_STATE_DELETED:
                            result["deleted"] += 1
                            states.pop(session_id, None)
                        else:
                            result["updated"] += 1
                            states[session_id] = canonical
                    except subprocess.TimeoutExpired:
                        if isinstance(thread_record, dict):
                            thread_record["pending_state"] = canonical
                        result["pending"] += 1
                        changed = True
                    except (OSError, subprocess.SubprocessError, ValueError) as exc:
                        result["errors"].append(
                            public_error_message(exc, "Operazione Codex non riuscita; verra' ritentata.")
                        )

            for session_id, record in threads.items():
                if not isinstance(record, dict):
                    continue
                current = states.get(session_id)
                if current is not None:
                    if record.get("last_state") != current:
                        record["last_state"] = current
                        changed = True
                elif canonical == DESKTOP_STATE_DELETED:
                    # Once deletion is canonical, every absent linked copy is deleted.
                    # This includes copies removed by the propagation command itself,
                    # which did not accumulate their own missing-scan counter.
                    if record.get("last_state") != DESKTOP_STATE_DELETED or record.get("missing_scans"):
                        record["last_state"] = DESKTOP_STATE_DELETED
                        record["missing_scans"] = 0
                        changed = True
            group["last_error"] = result["errors"][-1] if result["errors"] else None
            links[group_id] = group

        if changed:
            index["codex_links"] = links
            save_account_index(paths, index)
    return result


CODEX_COMPACT_USER_MESSAGE_MARKERS = (
    b'"type":"event_msg","payload":{"type":"user_message"',
    b'"type":"response_item","payload":{"type":"message","role":"user"',
)
CODEX_SPACED_USER_MESSAGE_MARKERS = (
    b'"type": "event_msg", "payload": {"type": "user_message"',
    b'"type": "response_item", "payload": {"type": "message", "role": "user"',
)
MAX_CODEX_PREVIEW_LINE_BYTES = 4 * 1024 * 1024


def codex_user_message_from_object(obj: dict[str, Any]) -> str | None:
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
        message = payload.get("message") if isinstance(payload.get("message"), str) else None
        preview = message_preview(message)
        if preview:
            return preview
        if payload.get("images") or payload.get("local_images"):
            return "[Immagine allegata]"
        return None

    if obj.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
        return None
    content = payload.get("content") if isinstance(payload.get("content"), list) else []
    parts = [
        str(entry["text"])
        for entry in content
        if isinstance(entry, dict)
        and entry.get("type") in {"input_text", "text"}
        and isinstance(entry.get("text"), str)
    ]
    preview = message_preview("\n".join(parts))
    if preview:
        return preview
    if any(isinstance(entry, dict) and entry.get("type") in {"input_image", "image"} for entry in content):
        return "[Immagine allegata]"
    return None


@functools.lru_cache(maxsize=2048)
def _latest_codex_user_message_cached(path_text: str, size: int, modified_ns: int) -> str | None:
    del size, modified_ns
    path = Path(path_text)
    try:
        with path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            sample = mapped[: min(len(mapped), 64 * 1024)]
            markers = (
                CODEX_COMPACT_USER_MESSAGE_MARKERS
                if b'"type":"' in sample
                else CODEX_SPACED_USER_MESSAGE_MARKERS
            )
            cursor = len(mapped)
            while cursor > 0:
                marker_position = max(mapped.rfind(marker, 0, cursor) for marker in markers)
                if marker_position < 0:
                    return None
                line_start = mapped.rfind(b"\n", 0, marker_position) + 1
                line_end = mapped.find(b"\n", marker_position)
                if line_end < 0:
                    line_end = len(mapped)
                if line_end - line_start <= MAX_CODEX_PREVIEW_LINE_BYTES:
                    obj = safe_json_loads(mapped[line_start:line_end].decode("utf-8", errors="replace"))
                    if obj:
                        preview = codex_user_message_from_object(obj)
                        if preview:
                            return preview
                cursor = line_start
    except (OSError, ValueError):
        return None
    return None


def latest_codex_user_message(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size <= 0:
        return None
    return _latest_codex_user_message_cached(str(path), stat.st_size, stat.st_mtime_ns)


def discover_codex_app_sessions(paths: Paths) -> list[Chat]:
    try:
        sync_codex_linked_threads(paths)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    index = load_codex_session_index(paths.codex_home)
    rows = codex_thread_rows(paths.codex_home)
    active_files, archived_files = codex_rollout_files(paths.codex_home)
    session_ids = set(index) | set(rows) | set(active_files) | set(archived_files)
    if not session_ids:
        return []
    codex_exe = find_codex_executable(paths)
    authenticated = active_codex_account(paths) is not None
    chats: list[Chat] = []

    for session_id in session_ids:
        index_entry = index.get(session_id, {})
        row = rows.get(session_id, {})
        if not row and session_id.lower() not in active_files and session_id.lower() not in archived_files:
            # session_index.jsonl is append-only: an index-only entry is a deleted local ghost.
            continue
        thread_source = row.get("thread_source")
        raw_source = row.get("source")
        source_text = " ".join(
            value for value in [thread_source, raw_source] if isinstance(value, str)
        ).casefold()
        if session_id not in index and "subagent" in source_text:
            continue
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
        if not title and isinstance(row.get("title"), str):
            title = row["title"]
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
                last_user_message=None,
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


def latest_user_message_for_chat(chat: Chat) -> str | None:
    if chat.last_user_message:
        return chat.last_user_message
    if chat.provider == PROVIDER_CODEX:
        return latest_codex_user_message(chat.jsonl_path)
    return message_preview(chat.last_prompt)


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
    return [
        path
        for path in unique_paths(candidates)
        if path.exists()
        and any(
            (path / marker).exists()
            for marker in ("config.json", "claude-code-sessions", "local-agent-mode-sessions", "logs")
        )
    ]


def file_tail_text(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            offset = max(0, size - max_bytes)
            handle.seek(offset)
            payload = handle.read()
    except OSError:
        return ""
    text = payload.decode("utf-8", errors="replace")
    if offset:
        _, separator, text = text.partition("\n")
        if not separator:
            return ""
    return text


def desktop_account_log_contexts(root: Path) -> list[DesktopAccountContext]:
    contexts: list[tuple[str, int, int, DesktopAccountContext]] = []
    try:
        log_paths = list((root / "logs").glob("main*.log"))
    except OSError:
        log_paths = []
    log_stats: dict[Path, os.stat_result] = {}
    signature: list[str] = []
    for log_path in log_paths:
        try:
            stat = log_path.stat()
        except OSError:
            continue
        log_stats[log_path] = stat
        signature.append(f"{str(log_path).casefold()}|{stat.st_mtime_ns}|{stat.st_size}")
    cache_key = str(root).casefold() if os.name == "nt" or str(root).startswith("/mnt/") else str(root)
    cache_signature = tuple(sorted(signature))
    with _DESKTOP_ACCOUNT_LOG_CACHE_LOCK:
        cached = _DESKTOP_ACCOUNT_LOG_CACHE.get(cache_key)
        if cached is not None and cached[0] == cache_signature:
            return list(cached[1])
    for log_path, stat in log_stats.items():
        log_mtime_ns = stat.st_mtime_ns
        for line_number, line in enumerate(file_tail_text(log_path, DESKTOP_ACCOUNT_LOG_TAIL_BYTES).splitlines()):
            match = DESKTOP_ACCOUNT_EVENT_PATTERN.search(line)
            timestamp_match = DESKTOP_LOG_TIMESTAMP_PATTERN.search(line)
            runtime_match = next(
                (pattern.search(line) for pattern in DESKTOP_ACCOUNT_RUNTIME_PATTERNS if pattern.search(line)),
                None,
            )
            if match is None and (runtime_match is None or timestamp_match is None):
                continue
            changed_at = match.group("timestamp") if match is not None else timestamp_match.group("timestamp")
            contexts.append(
                (
                    changed_at,
                    log_mtime_ns,
                    line_number,
                    DesktopAccountContext(
                        account_uuid=(match or runtime_match).group("account"),
                        organization_uuid=(match or runtime_match).group("organization"),
                        changed_at=changed_at,
                        logged_out=match is not None and match.group("none") is not None,
                    ),
                )
            )
    contexts.sort(key=lambda item: (item[0], item[1], item[2]))
    result = [item[3] for item in contexts]
    with _DESKTOP_ACCOUNT_LOG_CACHE_LOCK:
        if len(_DESKTOP_ACCOUNT_LOG_CACHE) >= 128:
            _DESKTOP_ACCOUNT_LOG_CACHE.clear()
        _DESKTOP_ACCOUNT_LOG_CACHE[cache_key] = (cache_signature, list(result))
    return result


def active_desktop_account_context(
    root: Path,
    contexts: list[DesktopAccountContext] | None = None,
) -> DesktopAccountContext:
    contexts = desktop_account_log_contexts(root) if contexts is None else contexts
    if contexts:
        return contexts[-1]
    value = desktop_config(root).get("lastKnownAccountUuid")
    account_uuid = value if isinstance(value, str) and value else None
    return DesktopAccountContext(
        account_uuid=account_uuid,
        organization_uuid=None,
        changed_at=None,
        logged_out=False,
    )


def claude_desktop_change_signature(paths: Paths) -> tuple[str, ...]:
    entries: list[str] = []
    for root in claude_windows_app_roots(paths):
        context = active_desktop_account_context(root)
        entries.append(
            f"{str(root).casefold()}|active={context.account_uuid or ''}"
            f"|org={context.organization_uuid or ''}|changed={context.changed_at or ''}"
            f"|logged_out={int(context.logged_out)}"
        )
        sessions_root = root / "claude-code-sessions"
        try:
            workspace_dirs = [path for path in sessions_root.glob("*/*") if path.is_dir()]
        except OSError:
            workspace_dirs = []
        for workspace_dir in workspace_dirs:
            try:
                mtime_ns = workspace_dir.stat().st_mtime_ns
            except OSError:
                mtime_ns = 0
            try:
                relative = workspace_dir.relative_to(sessions_root).as_posix().casefold()
            except ValueError:
                relative = str(workspace_dir).casefold()
            entries.append(f"{str(root).casefold()}|{relative}|{mtime_ns}")
        try:
            artifact_manifests = list((root / "local-agent-mode-sessions").glob("*/*/artifacts.json"))
        except OSError:
            artifact_manifests = []
        for manifest in artifact_manifests:
            try:
                stat = manifest.stat()
                relative = manifest.relative_to(root).as_posix().casefold()
                entries.append(f"{str(root).casefold()}|{relative}|{stat.st_mtime_ns}|{stat.st_size}")
            except (OSError, ValueError):
                continue
    return tuple(sorted(entries))


def desktop_config(root: Path) -> dict[str, Any]:
    return load_json_file(root / "config.json")


def active_desktop_account_uuid(
    root: Path,
    context: DesktopAccountContext | None = None,
) -> str | None:
    context = context or active_desktop_account_context(root)
    return None if context.logged_out else context.account_uuid


def active_desktop_workspace_uuid(
    root: Path,
    account_uuid: str | None,
    context: DesktopAccountContext | None = None,
) -> str | None:
    if not account_uuid:
        return None
    context = context or active_desktop_account_context(root)
    if context.account_uuid == account_uuid and context.organization_uuid:
        return context.organization_uuid
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


def claude_desktop_cached_oauth_tokens(root: Path) -> list[dict[str, Any]]:
    """Decrypt Claude Desktop's current-user token cache without persisting secrets."""

    if (os.name != "nt" and not is_wsl()) or not (root / "config.json").is_file() or not (root / "Local State").is_file():
        return []
    script = r'''
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$InformationPreference = "SilentlyContinue"
Add-Type -AssemblyName System.Security
$root = $env:CLAUDE_QUEUE_DESKTOP_ROOT
$state = Get-Content -LiteralPath (Join-Path $root "Local State") -Raw | ConvertFrom-Json
$wrapped = [Convert]::FromBase64String([string]$state.os_crypt.encrypted_key)
if ($wrapped.Length -le 5 -or [Text.Encoding]::ASCII.GetString($wrapped[0..4]) -ne "DPAPI") { throw "unsupported key" }
$key = [Security.Cryptography.ProtectedData]::Unprotect(
  $wrapped[5..($wrapped.Length - 1)],
  $null,
  [Security.Cryptography.DataProtectionScope]::CurrentUser
)
$config = Get-Content -LiteralPath (Join-Path $root "config.json") -Raw | ConvertFrom-Json
$tokens = @()
foreach ($cacheName in @("oauth:tokenCacheV2", "oauth:tokenCache")) {
  $property = $config.PSObject.Properties[$cacheName]
  if ($null -eq $property -or -not ($property.Value -is [string])) { continue }
  $blob = [Convert]::FromBase64String($property.Value)
  if ($blob.Length -le 31 -or [Text.Encoding]::ASCII.GetString($blob[0..2]) -ne "v10") { continue }
$nonce = [byte[]]$blob[3..14]
$cipher = [byte[]]$blob[15..($blob.Length - 17)]
$tag = [byte[]]$blob[($blob.Length - 16)..($blob.Length - 1)]
  $plain = New-Object byte[] $cipher.Length
  $aes = [Security.Cryptography.AesGcm]::new($key, 16)
  try { $aes.Decrypt($nonce, $cipher, $tag, $plain, $null) } finally { $aes.Dispose() }
  $cache = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
  foreach ($entry in $cache.PSObject.Properties) {
    $value = $entry.Value
    if ($value.token -is [string] -and $value.token.Length -gt 0) {
      $tokens += [pscustomobject]@{
        token = $value.token
        expiresAt = $value.expiresAt
        cacheKey = $entry.Name
      }
    }
  }
}
ConvertTo-Json -InputObject @($tokens) -Compress
'''
    windows_root = local_to_windows_path(root)
    script = script.replace(
        "$root = $env:CLAUDE_QUEUE_DESKTOP_ROOT",
        f"$root = {powershell_single_quote(windows_root)}",
    )
    environment = os.environ.copy()
    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not powershell:
        return []
    try:
        completed = subprocess.run(
            local_powershell_hidden_command(script, powershell=powershell),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            env=environment,
            **background_process_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    try:
        payload = json.loads(completed.stdout.lstrip("\ufeff\r\n "))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict) and isinstance(entry.get("token"), str)]


def claude_local_oauth_tokens(paths: Paths) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    credentials = load_json_file(paths.claude_home / ".credentials.json")
    oauth = credentials.get("claudeAiOauth") if isinstance(credentials.get("claudeAiOauth"), dict) else {}
    token = oauth.get("accessToken")
    if isinstance(token, str) and token:
        values.append(
            {
                "token": token,
                "expiresAt": oauth.get("expiresAt"),
                "cacheKey": f"credentials:{credentials.get('organizationUuid') or ''}",
            }
        )
    for root in claude_windows_app_roots(paths):
        values.extend(claude_desktop_cached_oauth_tokens(root))
    unique: dict[str, dict[str, Any]] = {}
    for value in values:
        token_value = value.get("token")
        if not isinstance(token_value, str) or not token_value:
            continue
        digest = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
        existing = unique.get(digest)
        if existing is None or "user:file_upload" in str(value.get("cacheKey") or ""):
            unique[digest] = value
    return list(unique.values())


def claude_api_json(
    token: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "claude-codex-queue",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "anthropic-beta": "oauth-2025-04-20",
        "X-Frame-CP": "go",
        "X-Frame-Surface": "code",
        "X-Frame-Platform": "desktop",
    }
    headers.update(extra_headers or {})
    request = urllib_request.Request(
        f"{CLAUDE_FRAME_API_ORIGIN}{path}",
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            raw = response.read(CLAUDE_FRAME_MAX_BYTES + 1)
    except urllib_error.HTTPError as exc:
        raise RuntimeError(f"Claude API HTTP {exc.code}") from exc
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Claude API non raggiungibile") from exc
    if len(raw) > CLAUDE_FRAME_MAX_BYTES:
        raise RuntimeError("Risposta Claude API troppo grande")
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Risposta Claude API non valida") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Risposta Claude API inattesa")
    return value


def claude_oauth_profile(token: str) -> dict[str, str]:
    payload = claude_api_json(token, "/api/oauth/profile", timeout=10)
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    organization = payload.get("organization") if isinstance(payload.get("organization"), dict) else {}
    account_uuid = account.get("uuid")
    organization_uuid = organization.get("uuid")
    if not isinstance(account_uuid, str) or not isinstance(organization_uuid, str):
        raise RuntimeError("Profilo OAuth Claude incompleto")
    return {
        "account_uuid": account_uuid,
        "organization_uuid": organization_uuid,
        "email": account.get("email") if isinstance(account.get("email"), str) else "",
    }


def claude_oauth_cache_signature(paths: Paths) -> tuple[str, ...]:
    files = [
        paths.claude_home / ".credentials.json",
        paths.windows_home / ".claude.json",
    ]
    for root in claude_windows_app_roots(paths):
        files.extend([root / "config.json", root / "Local State"])
    signature: list[str] = []
    for path in unique_paths(files):
        try:
            stat = path.stat()
            signature.append(f"{str(path).casefold()}|{stat.st_mtime_ns}|{stat.st_size}")
        except OSError:
            signature.append(f"{str(path).casefold()}|missing")
    return tuple(sorted(signature))


def claude_oauth_sessions(
    paths: Paths,
    *,
    force_refresh: bool = False,
) -> dict[str, dict[str, str]]:
    signature = claude_oauth_cache_signature(paths)
    cache_key = str(paths.state_dir).casefold()
    now = time.monotonic()
    with _CLAUDE_OAUTH_SESSION_CACHE_LOCK:
        cached = _CLAUDE_OAUTH_SESSION_CACHE.get(cache_key)
        if (
            not force_refresh
            and cached is not None
            and cached[0] == signature
            and now - cached[1] < CLAUDE_OAUTH_SESSION_CACHE_TTL_SECONDS
        ):
            return copy.deepcopy(cached[2])

    sessions: dict[str, dict[str, str]] = {}
    for entry in claude_local_oauth_tokens(paths):
        token = entry.get("token")
        if not isinstance(token, str) or not token:
            continue
        expires_at = entry.get("expiresAt")
        if isinstance(expires_at, (int, float)) and expires_at <= time.time() * 1000 + 60_000:
            continue
        try:
            profile = claude_oauth_profile(token)
        except RuntimeError:
            continue
        candidate = {**profile, "token": token}
        existing = sessions.get(profile["account_uuid"])
        if existing is None or "user:file_upload" in str(entry.get("cacheKey") or ""):
            sessions[profile["account_uuid"]] = candidate
    with _CLAUDE_OAUTH_SESSION_CACHE_LOCK:
        _CLAUDE_OAUTH_SESSION_CACHE[cache_key] = (signature, now, copy.deepcopy(sessions))
    return sessions


def cached_desktop_session_json(path: Path, stat: os.stat_result) -> dict[str, Any]:
    key = str(path).casefold() if os.name == "nt" or str(path).startswith("/mnt/") else str(path)
    signature = (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)
    with _DESKTOP_SESSION_FILE_CACHE_LOCK:
        cached = _DESKTOP_SESSION_FILE_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            return copy.deepcopy(cached[1])
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    with _DESKTOP_SESSION_FILE_CACHE_LOCK:
        if len(_DESKTOP_SESSION_FILE_CACHE) >= 4096:
            _DESKTOP_SESSION_FILE_CACHE.clear()
        _DESKTOP_SESSION_FILE_CACHE[key] = (signature, copy.deepcopy(value))
    return value


def desktop_session_records(
    paths: Paths,
    active_only: bool = False,
    *,
    roots: list[Path] | None = None,
    account_contexts: dict[Path, DesktopAccountContext] | None = None,
) -> list[DesktopSessionRecord]:
    records: list[DesktopSessionRecord] = []
    for root in roots if roots is not None else claude_windows_app_roots(paths):
        context = account_contexts.get(root) if account_contexts is not None else None
        active_account_uuid = active_desktop_account_uuid(root, context)
        active_workspace_uuid = (
            active_desktop_workspace_uuid(root, active_account_uuid, context)
            if active_only
            else None
        )
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
            try:
                stat = json_path.stat()
            except OSError:
                continue
            data = cached_desktop_session_json(json_path, stat)
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
                    mtime_ns=stat.st_mtime_ns,
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


def desktop_record_bridge_session_ids(record: DesktopSessionRecord) -> list[str]:
    values = record.data.get("bridgeSessionIds")
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value]


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
    atomic_replace_with_retry(temp_path, path)


def desktop_session_data_for_path(
    source_data: dict[str, Any],
    cli_session_id: str,
    destination: Path,
    archived: bool | None = None,
) -> dict[str, Any]:
    data = dict(source_data)
    data["cliSessionId"] = cli_session_id
    data["sessionId"] = destination.stem
    data["isArchived"] = bool(source_data.get("isArchived")) if archived is None else archived
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
    preferred_workspace_uuid: str | None = None,
) -> list[str]:
    account_dir = sessions_root / account_uuid
    if preferred_workspace_uuid:
        return [preferred_workspace_uuid]
    if account_dir.exists():
        workspaces = [path for path in account_dir.iterdir() if path.is_dir()]
        if workspaces:
            workspaces.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
            return [workspaces[0].name]
    return [preferred_workspace_uuid or str(uuid.uuid4())]


def desktop_cowork_user_files_root(paths: Paths, root: Path) -> Path:
    value = load_json_file(root / "claude_desktop_config.json").get("coworkUserFilesPath")
    if isinstance(value, str) and value.strip():
        return windows_to_local_path(value)
    return paths.windows_home / "Claude"


def desktop_artifact_manifest_path(root: Path, account_uuid: str, organization_uuid: str) -> Path:
    return root / "local-agent-mode-sessions" / account_uuid / organization_uuid / "artifacts.json"


def load_desktop_artifact_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateFileError(f"Manifest artefatti Claude non leggibile ({path}): {exc}") from exc
    if not isinstance(payload, list):
        raise StateFileError(f"Manifest artefatti Claude non valido ({path}): deve essere una lista.")
    entries: list[dict[str, Any]] = []
    for entry in payload:
        artifact_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(artifact_id, str) or re.fullmatch(r"[a-z0-9_-]+", artifact_id) is None:
            raise StateFileError(f"Manifest artefatti Claude non valido ({path}): id artefatto non valido.")
        entries.append(dict(entry))
    return entries


def write_desktop_artifact_manifest(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(entries, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    atomic_replace_with_retry(temp_path, path)


def desktop_artifact_timestamp(entry: dict[str, Any]) -> float:
    for key in ["updatedAt", "createdAt"]:
        value = entry.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            parsed = parse_iso(value)
            if parsed is not None:
                return parsed.timestamp() * 1000
    return 0.0


def desktop_artifact_slots(
    root: Path,
    records: list[DesktopSessionRecord],
    account_uuids: set[str],
    workspace_uuids_by_account: dict[str, list[str]],
    account_contexts: list[DesktopAccountContext] | None = None,
    active_context: DesktopAccountContext | None = None,
) -> dict[str, tuple[str, str, Path]]:
    organizations: dict[str, set[str]] = {account_uuid: set() for account_uuid in account_uuids}
    base = root / "local-agent-mode-sessions"
    try:
        account_dirs = [path for path in base.iterdir() if path.is_dir() and path.name != "skills-plugin"]
    except OSError:
        account_dirs = []
    for account_dir in account_dirs:
        organizations.setdefault(account_dir.name, set())
        try:
            organizations[account_dir.name].update(
                path.name for path in account_dir.iterdir() if path.is_dir()
            )
        except OSError:
            pass
    contexts = desktop_account_log_contexts(root) if account_contexts is None else account_contexts
    for context in contexts:
        if context.account_uuid and context.organization_uuid:
            organizations.setdefault(context.account_uuid, set()).add(context.organization_uuid)
    active = active_context or active_desktop_account_context(root, contexts)
    if active.account_uuid and active.organization_uuid:
        organizations.setdefault(active.account_uuid, set()).add(active.organization_uuid)

    records_by_account: dict[str, list[DesktopSessionRecord]] = {}
    for record in records:
        records_by_account.setdefault(record.account_uuid, []).append(record)
    for account_uuid in account_uuids:
        if organizations.get(account_uuid):
            continue
        fallback = workspace_uuids_by_account.get(account_uuid) or [
            record.workspace_uuid for record in records_by_account.get(account_uuid, [])
        ]
        organizations.setdefault(account_uuid, set()).update(value for value in fallback if value)

    slots: dict[str, tuple[str, str, Path]] = {}
    for account_uuid, organization_uuids in organizations.items():
        for organization_uuid in organization_uuids:
            slot = desktop_replica_slot(account_uuid, organization_uuid)
            slots[slot] = (
                account_uuid,
                organization_uuid,
                desktop_artifact_manifest_path(root, account_uuid, organization_uuid),
            )
    return slots


def desktop_artifact_session_maps(
    records: list[DesktopSessionRecord],
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    cli_by_app: dict[tuple[str, str], str] = {}
    app_by_cli_record: dict[tuple[str, str], DesktopSessionRecord] = {}
    for record in records:
        app_session_id = record.data.get("sessionId")
        cli_session_id = desktop_record_cli_session_id(record)
        if not isinstance(app_session_id, str) or not app_session_id or not cli_session_id:
            continue
        cli_by_app[(record.account_uuid, app_session_id)] = cli_session_id
        key = (record.account_uuid, cli_session_id)
        current = app_by_cli_record.get(key)
        if current is None or desktop_record_mtime_ns(record) >= desktop_record_mtime_ns(current):
            app_by_cli_record[key] = record
    app_by_cli = {
        key: str(record.data["sessionId"])
        for key, record in app_by_cli_record.items()
        if isinstance(record.data.get("sessionId"), str)
    }
    return cli_by_app, app_by_cli


def desktop_artifact_entry_for_slot(
    source: dict[str, Any],
    source_slot: str,
    target_slot: str,
    target: dict[str, Any] | None,
    cli_by_app: dict[tuple[str, str], str],
    app_by_cli: dict[tuple[str, str], str],
) -> dict[str, Any]:
    data = dict(source)
    source_account = source_slot.split("/", 1)[0]
    target_account = target_slot.split("/", 1)[0]
    if source_slot != target_slot:
        for field in DESKTOP_ARTIFACT_LOCAL_ONLY_FIELDS:
            data.pop(field, None)
        for field in ["createdBySessionId", "lastModifiedBySessionId"]:
            source_app_session = source.get(field)
            cli_session = (
                cli_by_app.get((source_account, source_app_session))
                if isinstance(source_app_session, str)
                else None
            )
            target_app_session = app_by_cli.get((target_account, cli_session)) if cli_session else None
            if target_app_session:
                data[field] = target_app_session
            else:
                data.pop(field, None)
    if target is not None:
        for field in DESKTOP_ARTIFACT_LOCAL_ONLY_FIELDS:
            if field in target:
                data[field] = target[field]
            else:
                data.pop(field, None)
    return data


def sync_claude_desktop_artifacts(
    paths: Paths,
    root: Path,
    records: list[DesktopSessionRecord],
    account_uuids: set[str],
    workspace_uuids_by_account: dict[str, list[str]],
    state: dict[str, Any],
    root_state: dict[str, Any],
    result: dict[str, Any],
    account_contexts: list[DesktopAccountContext] | None = None,
    active_context: DesktopAccountContext | None = None,
) -> None:
    slots = desktop_artifact_slots(
        root,
        records,
        account_uuids,
        workspace_uuids_by_account,
        account_contexts,
        active_context,
    )
    if not slots:
        return
    manifests: dict[str, dict[str, dict[str, Any]]] = {}
    for slot, (_, _, manifest_path) in slots.items():
        manifests[slot] = {
            entry["id"]: entry for entry in load_desktop_artifact_manifest(manifest_path)
        }

    artifact_state = root_state.setdefault("artifacts", {})
    if not isinstance(artifact_state, dict):
        raise StateFileError("Journal sync Claude Desktop corrotto: 'artifacts' deve essere un oggetto.")
    artifact_ids = set(artifact_state)
    for entries in manifests.values():
        artifact_ids.update(entries)
    result["artifact_accounts"] += len(slots)
    result["artifacts"] += len(artifact_ids)
    cli_by_app, app_by_cli = desktop_artifact_session_maps(records)
    artifact_files_root = desktop_cowork_user_files_root(paths, root) / "Artifacts"
    dirty_slots: set[str] = set()

    for artifact_id in sorted(artifact_ids):
        state_entry = artifact_state.get(artifact_id)
        if not isinstance(state_entry, dict):
            state_entry = {}
        previous_state = state_entry.get("state")
        previous_replicas = state_entry.get("replicas")
        if not isinstance(previous_replicas, dict):
            previous_replicas = {}
        current_sources = [
            (entry, slot)
            for slot, entries in manifests.items()
            if (entry := entries.get(artifact_id)) is not None
        ]

        delete_confirmed = False
        for slot, previous in previous_replicas.items():
            if not isinstance(previous, dict) or previous.get("excluded") or slot not in slots:
                continue
            if artifact_id in manifests[slot]:
                previous.update({"present": True, "missing_scans": 0, "scan_blocked": False})
                continue
            manifest_path = slots[slot][2]
            if manifest_path.is_file():
                missing_scans = int(previous.get("missing_scans") or 0) + 1
                previous.update({"present": False, "missing_scans": missing_scans, "scan_blocked": False})
                if missing_scans >= 2:
                    delete_confirmed = True
                else:
                    result["pending_artifact_deletions"] += 1
            else:
                previous.update({"present": False, "missing_scans": 0, "scan_blocked": True})

        canonical_state = (
            DESKTOP_STATE_DELETED
            if previous_state == DESKTOP_STATE_DELETED or delete_confirmed
            else DESKTOP_STATE_ACTIVE
        )
        state_entry["state"] = canonical_state
        state_entry["replicas"] = previous_replicas
        artifact_state[artifact_id] = state_entry
        if canonical_state == DESKTOP_STATE_DELETED:
            if previous_state != DESKTOP_STATE_DELETED:
                # Keep the tombstone durable before removing any account manifest entry.
                save_desktop_sync_state(paths, state)
            for slot, entries in manifests.items():
                if entries.pop(artifact_id, None) is not None:
                    dirty_slots.add(slot)
                    result["artifacts_removed"] += 1
            if previous_state != DESKTOP_STATE_DELETED:
                result["artifacts_deleted"] += 1
            continue
        if not current_sources:
            continue
        if not (artifact_files_root / artifact_id / "index.html").is_file():
            result["artifact_missing_files"] += 1
            continue

        canonical, source_slot = max(
            current_sources,
            key=lambda item: (desktop_artifact_timestamp(item[0]), item[1]),
        )
        for target_slot, entries in manifests.items():
            target = entries.get(artifact_id)
            previous = previous_replicas.get(target_slot)
            if target is None and isinstance(previous, dict) and (
                previous.get("scan_blocked") or int(previous.get("missing_scans") or 0) > 0
            ):
                continue
            desired = desktop_artifact_entry_for_slot(
                canonical,
                source_slot,
                target_slot,
                target,
                cli_by_app,
                app_by_cli,
            )
            if target is None:
                entries[artifact_id] = desired
                dirty_slots.add(target_slot)
                result["artifacts_created"] += 1
            elif (
                desktop_artifact_timestamp(canonical) >= desktop_artifact_timestamp(target)
                and desired != target
            ):
                entries[artifact_id] = desired
                dirty_slots.add(target_slot)
                result["artifacts_updated"] += 1

        replicas: dict[str, dict[str, Any]] = {}
        for slot, entries in manifests.items():
            if artifact_id in entries:
                replicas[slot] = {"present": True, "missing_scans": 0, "scan_blocked": False}
            elif isinstance(previous_replicas.get(slot), dict):
                replicas[slot] = previous_replicas[slot]
        state_entry["replicas"] = replicas

    for slot in sorted(dirty_slots):
        manifest_path = slots[slot][2]
        if manifest_path.exists():
            backup = backup_desktop_session_file(paths, manifest_path)
            result["artifact_backups"].append(str(backup))
        entries = sorted(
            manifests[slot].values(),
            key=lambda entry: (desktop_artifact_timestamp(entry), str(entry.get("id") or "")),
            reverse=True,
        )
        write_desktop_artifact_manifest(manifest_path, entries)


def nested_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in nested_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in nested_strings(item)]
    return []


def replace_nested_strings(value: Any, replacements: dict[str, str], exact: dict[str, str]) -> Any:
    if isinstance(value, str):
        if value in exact:
            return exact[value]
        for source, destination in replacements.items():
            value = value.replace(source, destination)
        return value
    if isinstance(value, dict):
        return {key: replace_nested_strings(item, replacements, exact) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_nested_strings(item, replacements, exact) for item in value]
    return value


def claude_artifact_rendered_html(source: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=utf8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"{CLAUDE_FRAME_STYLE}</head><body>\n{source}\n</body></html>"
    )


def claude_written_file_content(transcript_path: Path, source_path: str) -> str | None:
    markers = ('"name":"Write"', '"name": "Write"')
    latest = None
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not any(marker in line for marker in markers):
                    continue
                obj = safe_json_loads(line)
                if not obj:
                    continue
                message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                blocks = message.get("content") if isinstance(message.get("content"), list) else []
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "Write":
                        continue
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    if tool_input.get("file_path") == source_path and isinstance(tool_input.get("content"), str):
                        latest = tool_input["content"]
    except OSError:
        return None
    return latest


def discover_claude_code_artifacts(
    paths: Paths,
    excluded_session_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    projects = paths.claude_home / "projects"
    if not projects.exists():
        return {}
    excluded = excluded_session_ids or set()
    discovered: dict[str, dict[str, Any]] = {}
    for jsonl_path in projects.glob("**/*.jsonl"):
        default_session_id = jsonl_path.stem
        if default_session_id in excluded:
            continue
        artifact_calls: dict[str, dict[str, Any]] = {}
        latest_artifact_input: dict[str, Any] = {}
        session_references: dict[str, dict[str, Any]] = {}
        session_id = default_session_id
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not any(
                        marker in line
                        for marker in (
                            '"name":"Artifact"',
                            '"name": "Artifact"',
                            '"type":"frame-link"',
                            '"type": "frame-link"',
                            "claude.ai/code/artifact/",
                        )
                    ):
                        continue
                    obj = safe_json_loads(line)
                    if not obj:
                        continue
                    if isinstance(obj.get("sessionId"), str):
                        session_id = obj["sessionId"]
                    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                    blocks = message.get("content") if isinstance(message.get("content"), list) else []
                    for block in blocks:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use" and block.get("name") == "Artifact":
                            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                            latest_artifact_input = dict(tool_input)
                            tool_id = block.get("id")
                            if isinstance(tool_id, str):
                                artifact_calls[tool_id] = dict(tool_input)
                        if block.get("type") == "tool_result":
                            tool_id = block.get("tool_use_id")
                            if isinstance(tool_id, str) and tool_id in artifact_calls:
                                latest_artifact_input = artifact_calls[tool_id]

                    urls: list[tuple[str, str]] = []
                    if obj.get("type") == "frame-link" and isinstance(obj.get("frameUrl"), str):
                        match = CLAUDE_ARTIFACT_URL_PATTERN.search(obj["frameUrl"])
                        if match:
                            urls.append((match.group("slug"), match.group(0)))
                    for text_value in nested_strings(obj):
                        for match in CLAUDE_ARTIFACT_URL_PATTERN.finditer(text_value):
                            pair = (match.group("slug"), match.group(0))
                            if pair not in urls:
                                urls.append(pair)
                    for slug, frame_url in urls:
                        reference = session_references.setdefault(
                            slug,
                            {
                                "session_id": session_id,
                                "transcript_path": canonical_windows_path(jsonl_path),
                                "frame_url": frame_url,
                            },
                        )
                        if obj.get("type") == "frame-link":
                            for key in ("title", "timestamp", "path"):
                                if isinstance(obj.get(key), str):
                                    reference[key] = obj[key]
                            reference["has_frame_link"] = True
                        for key in ("file_path", "description", "favicon", "label", "title"):
                            value = latest_artifact_input.get(key)
                            if isinstance(value, str) and value:
                                reference.setdefault(key, value)
        except OSError:
            continue

        if session_id in excluded:
            continue
        for slug, reference in session_references.items():
            reference["session_id"] = session_id
            source_path_value = reference.get("file_path") or reference.get("path")
            source_text = None
            if isinstance(source_path_value, str):
                source_path = windows_to_local_path(source_path_value)
                try:
                    if source_path.is_file() and source_path.stat().st_size <= CLAUDE_FRAME_MAX_BYTES:
                        source_text = source_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    source_text = None
                if source_text is None:
                    source_text = claude_written_file_content(jsonl_path, source_path_value)
            entry = discovered.setdefault(
                slug,
                {
                    "slug": slug,
                    "frame_url": f"https://claude.ai/code/artifact/{slug}",
                    "references": {},
                },
            )
            entry["references"][session_id] = reference
            for key in ("title", "description", "favicon", "label", "file_path"):
                value = reference.get(key)
                if isinstance(value, str) and value and not entry.get(key):
                    entry[key] = value
            if isinstance(source_text, str) and source_text and not entry.get("source_text"):
                entry["source_text"] = source_text
    return discovered


def claude_frame_rows(token: str) -> list[dict[str, Any]]:
    payload = claude_api_json(token, "/api/frame/frames?limit=200")
    rows = payload.get("frames")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def claude_frame_boot(token: str, slug: str, organization_uuid: str) -> dict[str, Any]:
    return claude_api_json(token, f"/api/frame/{quote(slug)}?org={quote(organization_uuid)}")


def deploy_claude_frame_copy(
    token: str,
    artifact: dict[str, Any],
    content: str,
    *,
    slug: str | None = None,
    base_version: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": artifact.get("title") or "Claude artifact",
        "favicon": artifact.get("favicon") or "📄",
        "content": content,
        "entrypoint": "claude-desktop",
    }
    for key in ("label", "description"):
        if isinstance(artifact.get(key), str) and artifact[key]:
            body[key] = artifact[key]
    if slug:
        body["slug"] = slug
    if base_version:
        body["baseVersion"] = base_version
    return claude_api_json(token, "/api/frame/deploy/direct", method="POST", body=body, timeout=60)


def code_artifact_aliases(root_state: dict[str, Any]) -> dict[str, dict[str, str]]:
    aliases = root_state.setdefault("code_artifact_aliases", {})
    if not isinstance(aliases, dict):
        aliases = {}
        root_state["code_artifact_aliases"] = aliases
    return aliases


def desktop_logical_session_id(root_state: dict[str, Any], session_id: str) -> str:
    alias = code_artifact_aliases(root_state).get(session_id)
    if isinstance(alias, dict) and isinstance(alias.get("logical_session_id"), str):
        return alias["logical_session_id"]
    return session_id


def desktop_account_session_id(root_state: dict[str, Any], session_id: str, account_uuid: str) -> str:
    replicas = root_state.get("code_artifact_session_replicas")
    by_account = replicas.get(session_id) if isinstance(replicas, dict) else None
    value = by_account.get(account_uuid) if isinstance(by_account, dict) else None
    return value if isinstance(value, str) and value else session_id


def write_claude_artifact_transcript_replica(
    source_path: Path,
    destination: Path,
    logical_session_id: str,
    replica_session_id: str,
    replacements: dict[str, str],
    missing_links: list[dict[str, Any]],
) -> bool:
    output: list[str] = []
    latest_source_timestamp: str | None = None
    try:
        with source_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                obj = safe_json_loads(line)
                if obj is None:
                    continue
                if isinstance(obj.get("timestamp"), str):
                    latest_source_timestamp = obj["timestamp"]
                transformed = replace_nested_strings(
                    obj,
                    replacements,
                    {logical_session_id: replica_session_id},
                )
                output.append(json.dumps(transformed, ensure_ascii=False, separators=(",", ":")))
    except OSError:
        return False
    for link in missing_links:
        output.append(
            json.dumps(
                {
                    "type": "frame-link",
                    "sessionId": replica_session_id,
                    "path": canonical_windows_path(link["path"])
                    if isinstance(link.get("path"), str) and link["path"]
                    else "",
                    "frameUrl": link["frame_url"],
                    "title": link.get("title") or "Claude artifact",
                    "timestamp": link.get("timestamp") or latest_source_timestamp or "1970-01-01T00:00:00Z",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    payload = "\n".join(output) + "\n"
    try:
        if destination.is_file() and destination.read_text(encoding="utf-8", errors="replace") == payload:
            return False
    except OSError:
        pass
    atomic_write_utf8(destination, payload)
    return True


def sync_claude_code_artifacts(
    paths: Paths,
    root: Path,
    account_uuids: set[str],
    root_state: dict[str, Any],
    result: dict[str, Any],
    *,
    scan_transcripts: bool,
) -> None:
    artifact_state = root_state.setdefault("code_artifacts", {})
    if not isinstance(artifact_state, dict):
        artifact_state = {}
        root_state["code_artifacts"] = artifact_state
    aliases = code_artifact_aliases(root_state)
    discovered = (
        discover_claude_code_artifacts(paths, set(aliases))
        if scan_transcripts
        else {}
    )
    cache_root = paths.state_dir / "claude-code-artifacts"
    for slug, item in discovered.items():
        state_entry = artifact_state.get(slug) if isinstance(artifact_state.get(slug), dict) else {}
        references = item.get("references") if isinstance(item.get("references"), dict) else {}
        state_entry.update(
            {
                "slug": slug,
                "frame_url": item["frame_url"],
                "title": item.get("title") or state_entry.get("title") or "Claude artifact",
                "description": item.get("description") or state_entry.get("description"),
                "favicon": item.get("favicon") or state_entry.get("favicon") or "📄",
                "label": item.get("label") or state_entry.get("label"),
                "references": references,
                "last_scanned_at": now_utc(),
            }
        )
        source_text = item.get("source_text")
        cache_path = cache_root / slug / "index.html"
        if isinstance(source_text, str) and source_text:
            rendered = claude_artifact_rendered_html(source_text)
            if len(rendered.encode("utf-8")) <= CLAUDE_FRAME_MAX_BYTES:
                atomic_write_utf8(cache_path, rendered)
                state_entry["content_sha256"] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
                state_entry["cache_path"] = canonical_windows_path(cache_path)
        state_entry.setdefault("accounts", {})
        artifact_state[slug] = state_entry

    if not artifact_state:
        return
    result["code_artifacts"] += len(artifact_state)
    sessions = claude_oauth_sessions(paths, force_refresh=scan_transcripts)
    profiles = root_state.setdefault("oauth_account_profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
        root_state["oauth_account_profiles"] = profiles
    for account_uuid, session in sessions.items():
        profiles[account_uuid] = {
            "account_uuid": account_uuid,
            "organization_uuid": session.get("organization_uuid"),
            "email": session.get("email") or "",
            "last_seen_at": now_utc(),
        }
    frame_rows_by_account: dict[str, list[dict[str, Any]]] = {}
    if scan_transcripts:
        for account_uuid, session in sessions.items():
            try:
                frame_rows_by_account[account_uuid] = claude_frame_rows(session["token"])
            except RuntimeError as exc:
                result["code_artifact_errors"].append(
                    f"{account_uuid[:8]}: {public_error_message(exc, 'Replica artefatto non riuscita.')}"
                )
                frame_rows_by_account[account_uuid] = []

    for slug, state_entry in artifact_state.items():
        if not isinstance(state_entry, dict):
            continue
        accounts = state_entry.setdefault("accounts", {})
        if not isinstance(accounts, dict):
            accounts = {}
            state_entry["accounts"] = accounts
        owner_account = state_entry.get("owner_account")
        for account_uuid, rows in frame_rows_by_account.items():
            row = next((row for row in rows if row.get("slug") == slug), None)
            row_owner = row.get("owner_account") if isinstance(row, dict) else None
            if isinstance(row_owner, str) and row_owner:
                owner_account = row_owner
                state_entry["owner_account"] = row_owner
            if row is not None and row.get("rel") == "mine" and row_owner == account_uuid:
                accounts[account_uuid] = {
                    "slug": slug,
                    "organization_uuid": sessions[account_uuid]["organization_uuid"],
                    "content_sha256": state_entry.get("content_sha256"),
                    "updated_at": now_utc(),
                }
        if not isinstance(owner_account, str) or not owner_account:
            result["code_artifact_pending_accounts"] += len(account_uuids)
            continue
        content_path_value = state_entry.get("cache_path")
        content_path = windows_to_local_path(content_path_value) if isinstance(content_path_value, str) else None
        try:
            content = content_path.read_text(encoding="utf-8") if content_path and content_path.is_file() else None
        except OSError:
            content = None
        for account_uuid in sorted(account_uuids):
            mapping = accounts.get(account_uuid) if isinstance(accounts.get(account_uuid), dict) else None
            if account_uuid == owner_account:
                continue
            oauth = sessions.get(account_uuid)
            if oauth is None or content is None:
                if mapping is None:
                    result["code_artifact_pending_accounts"] += 1
                continue
            digest = state_entry.get("content_sha256")
            if mapping is not None and mapping.get("content_sha256") == digest:
                continue
            mapped_slug = mapping.get("slug") if mapping is not None else None
            base_version = None
            if isinstance(mapped_slug, str) and mapped_slug:
                try:
                    boot = claude_frame_boot(oauth["token"], mapped_slug, oauth["organization_uuid"])
                    base_version = boot.get("live") if isinstance(boot.get("live"), str) else None
                except RuntimeError:
                    mapped_slug = None
            try:
                deployed = deploy_claude_frame_copy(
                    oauth["token"],
                    state_entry,
                    content,
                    slug=mapped_slug if isinstance(mapped_slug, str) else None,
                    base_version=base_version,
                )
            except RuntimeError as exc:
                result["code_artifact_errors"].append(
                    f"{account_uuid[:8]}: {public_error_message(exc, 'Replica transcript artefatto non riuscita.')}"
                )
                result["code_artifact_pending_accounts"] += 1
                continue
            deployed_slug = deployed.get("slug")
            if not isinstance(deployed_slug, str) or not deployed_slug:
                result["code_artifact_errors"].append(f"{account_uuid[:8]}: deploy incompleto")
                result["code_artifact_pending_accounts"] += 1
                continue
            accounts[account_uuid] = {
                "slug": deployed_slug,
                "organization_uuid": oauth["organization_uuid"],
                "content_sha256": digest,
                "version": deployed.get("version"),
                "updated_at": now_utc(),
            }
            result["code_artifact_copies_created"] += 1

    replacements_by_session: dict[str, dict[str, dict[str, str]]] = {}
    links_by_session: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for slug, state_entry in artifact_state.items():
        if not isinstance(state_entry, dict):
            continue
        accounts = state_entry.get("accounts") if isinstance(state_entry.get("accounts"), dict) else {}
        references = state_entry.get("references") if isinstance(state_entry.get("references"), dict) else {}
        original_url = state_entry.get("frame_url")
        for logical_session_id, reference in references.items():
            if not isinstance(logical_session_id, str) or not isinstance(reference, dict):
                continue
            for account_uuid, mapping in accounts.items():
                if not isinstance(account_uuid, str) or not isinstance(mapping, dict):
                    continue
                mapped_slug = mapping.get("slug")
                if not isinstance(mapped_slug, str) or not isinstance(original_url, str):
                    continue
                mapped_url = f"https://claude.ai/code/artifact/{mapped_slug}"
                if mapped_url == original_url:
                    continue
                replacements_by_session.setdefault(logical_session_id, {}).setdefault(account_uuid, {})[
                    original_url
                ] = mapped_url
                if reference.get("has_frame_link") is not True:
                    links_by_session.setdefault(logical_session_id, {}).setdefault(account_uuid, []).append(
                        {
                            "frame_url": mapped_url,
                            "title": reference.get("title") or state_entry.get("title"),
                            "timestamp": reference.get("timestamp"),
                            "path": state_entry.get("cache_path"),
                        }
                    )

    replicas = root_state.setdefault("code_artifact_session_replicas", {})
    if not isinstance(replicas, dict):
        replicas = {}
        root_state["code_artifact_session_replicas"] = replicas
    for logical_session_id, by_account in replacements_by_session.items():
        reference_path = next(
            (
                windows_to_local_path(reference["transcript_path"])
                for artifact in artifact_state.values()
                if isinstance(artifact, dict)
                for session_id, reference in (
                    artifact.get("references", {}).items()
                    if isinstance(artifact.get("references"), dict)
                    else []
                )
                if session_id == logical_session_id
                and isinstance(reference, dict)
                and isinstance(reference.get("transcript_path"), str)
            ),
            None,
        )
        if reference_path is None or not reference_path.is_file():
            continue
        account_replicas = replicas.setdefault(logical_session_id, {})
        if not isinstance(account_replicas, dict):
            account_replicas = {}
            replicas[logical_session_id] = account_replicas
        for account_uuid, replacements in by_account.items():
            replica_session_id = account_replicas.get(account_uuid)
            if not isinstance(replica_session_id, str) or not replica_session_id:
                replica_session_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"claude-codex-queue:artifact:{logical_session_id}:{account_uuid}",
                    )
                )
                account_replicas[account_uuid] = replica_session_id
            destination = reference_path.with_name(f"{replica_session_id}.jsonl")
            changed = write_claude_artifact_transcript_replica(
                reference_path,
                destination,
                logical_session_id,
                replica_session_id,
                replacements,
                links_by_session.get(logical_session_id, {}).get(account_uuid, []),
            )
            aliases[replica_session_id] = {
                "logical_session_id": logical_session_id,
                "account_uuid": account_uuid,
                "transcript_path": canonical_windows_path(destination),
            }
            if changed:
                result["code_artifact_transcripts_created"] += 1


def desktop_sync_state_path(paths: Paths) -> Path:
    return paths.state_dir / DESKTOP_SYNC_STATE_FILE_NAME


def load_desktop_sync_state(paths: Paths) -> dict[str, Any]:
    with desktop_sync_lock(paths):
        state = load_durable_json_object(desktop_sync_state_path(paths), "Journal sync Claude Desktop")
        if state is None:
            return {"version": DESKTOP_SYNC_STATE_VERSION, "roots": {}}
        if "roots" in state and not isinstance(state["roots"], dict):
            raise StateFileError(
                f"Journal sync Claude Desktop corrotto ({desktop_sync_state_path(paths)}): "
                "'roots' deve essere un oggetto."
            )
        state.setdefault("roots", {})
        state["version"] = DESKTOP_SYNC_STATE_VERSION
        return state


def save_desktop_sync_state(paths: Paths, state: dict[str, Any]) -> None:
    with desktop_sync_lock(paths):
        path = desktop_sync_state_path(paths)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        atomic_replace_with_retry(temp_path, path)


def desktop_sync_root_identity(root: str | Path) -> str:
    raw = normalize_windows_path(str(root).strip().strip('"'))
    windows_match = re.match(r"^([a-zA-Z]):[\\/](.*)$", raw)
    if windows_match:
        drive = windows_match.group(1).casefold()
        rest = windows_match.group(2).replace("\\", "/").strip("/")
        return f"{drive}:/{rest}".rstrip("/").casefold()

    wsl_match = re.match(r"^/mnt/([a-zA-Z])/(.*)$", raw)
    if wsl_match:
        drive = wsl_match.group(1).casefold()
        rest = wsl_match.group(2).replace("\\", "/").strip("/")
        return f"{drive}:/{rest}".rstrip("/").casefold()

    path = Path(raw)
    try:
        path = path.resolve()
    except OSError:
        path = path.absolute()
    normalized = local_to_windows_path(path).replace("\\", "/").rstrip("/")
    return normalized.casefold()


def desktop_sync_root_key(root: Path) -> str:
    identity = desktop_sync_root_identity(root)
    return hash_text(identity) or identity


def desktop_legacy_sync_root_keys(root: Path) -> list[str]:
    candidates: list[str] = []
    for resolver in (Path.resolve, Path.absolute):
        try:
            candidates.append(str(resolver(root)).casefold())
        except OSError:
            continue

    identity = desktop_sync_root_identity(root)
    drive_match = re.match(r"^([a-z]):/(.*)$", identity)
    if drive_match:
        drive = drive_match.group(1)
        rest = drive_match.group(2)
        windows_rest = rest.replace("/", "\\")
        candidates.extend(
            [
                f"{drive}:\\{windows_rest}",
                f"/mnt/{drive}/{rest}",
            ]
        )

    canonical = desktop_sync_root_key(root)
    keys: list[str] = []
    for candidate in candidates:
        key = hash_text(candidate.casefold()) or candidate.casefold()
        if key != canonical and key not in keys:
            keys.append(key)
    return keys


def desktop_session_entry_score(entry: dict[str, Any]) -> int:
    score = int(entry.get("state_changed_at_ns") or 0)
    replicas = entry.get("replicas") if isinstance(entry.get("replicas"), dict) else {}
    for snapshot in replicas.values():
        if isinstance(snapshot, dict):
            score = max(score, int(snapshot.get("mtime_ns") or 0))
    return score


def desktop_session_entry_has_workspace_transition(entry: dict[str, Any]) -> bool:
    replicas = entry.get("replicas") if isinstance(entry.get("replicas"), dict) else {}
    present: dict[str, set[str]] = {}
    missing: dict[str, set[str]] = {}
    for slot, snapshot in replicas.items():
        if not isinstance(snapshot, dict):
            continue
        account_uuid = snapshot.get("account_uuid")
        workspace_uuid = snapshot.get("workspace_uuid")
        if not isinstance(account_uuid, str) or not isinstance(workspace_uuid, str):
            if isinstance(slot, str) and "/" in slot:
                account_uuid, workspace_uuid = slot.split("/", 1)
            else:
                continue
        if snapshot.get("present") is True:
            present.setdefault(account_uuid, set()).add(workspace_uuid)
        elif int(snapshot.get("missing_scans") or 0) > 0:
            missing.setdefault(account_uuid, set()).add(workspace_uuid)
    return any(
        any(old_workspace != new_workspace for old_workspace in missing[account_uuid] for new_workspace in present[account_uuid])
        for account_uuid in present.keys() & missing.keys()
    )


def merge_desktop_session_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    values = [entry for entry in entries if isinstance(entry, dict)]
    if not values:
        return {}
    non_deleted = [entry for entry in values if entry.get("state") != DESKTOP_STATE_DELETED]
    hard_deleted = [
        entry
        for entry in values
        if entry.get("state") == DESKTOP_STATE_DELETED
        and not desktop_session_entry_has_workspace_transition(entry)
    ]
    if hard_deleted:
        selected = max(hard_deleted, key=desktop_session_entry_score)
    elif non_deleted:
        selected = max(non_deleted, key=desktop_session_entry_score)
    else:
        selected = max(values, key=desktop_session_entry_score)

    merged = copy.deepcopy(selected)
    recovering_workspace_transition = bool(
        non_deleted
        and not hard_deleted
        and any(
            entry.get("state") == DESKTOP_STATE_DELETED
            and desktop_session_entry_has_workspace_transition(entry)
            for entry in values
        )
    )
    if recovering_workspace_transition:
        merged["replicas"] = {}
    else:
        replicas = merged.setdefault("replicas", {})
        if not isinstance(replicas, dict):
            replicas = {}
            merged["replicas"] = replicas
        for entry in values:
            source_replicas = entry.get("replicas") if isinstance(entry.get("replicas"), dict) else {}
            for slot, snapshot in source_replicas.items():
                if not isinstance(snapshot, dict):
                    continue
                current = replicas.get(slot)
                current_score = int(current.get("mtime_ns") or 0) if isinstance(current, dict) else -1
                snapshot_score = int(snapshot.get("mtime_ns") or 0)
                if current is None or snapshot_score > current_score:
                    replicas[slot] = copy.deepcopy(snapshot)

    bridge_owners = merged.setdefault("bridge_owners", {})
    if not isinstance(bridge_owners, dict):
        bridge_owners = {}
        merged["bridge_owners"] = bridge_owners
    for entry in values:
        source_owners = entry.get("bridge_owners") if isinstance(entry.get("bridge_owners"), dict) else {}
        for bridge_id, account_uuid in source_owners.items():
            bridge_owners.setdefault(bridge_id, account_uuid)
    return merged


def merge_desktop_root_states(root_states: list[dict[str, Any]]) -> dict[str, Any]:
    values = [root_state for root_state in root_states if isinstance(root_state, dict)]
    if not values:
        return {"sessions": {}}

    def merge_missing(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if key == "sessions":
                continue
            if key not in target:
                target[key] = copy.deepcopy(value)
            elif isinstance(target[key], dict) and isinstance(value, dict):
                merge_missing(target[key], value)

    merged: dict[str, Any] = {}
    for root_state in values:
        merge_missing(merged, root_state)
    session_ids = {
        session_id
        for root_state in values
        for session_id in (
            root_state.get("sessions", {}).keys()
            if isinstance(root_state.get("sessions"), dict)
            else []
        )
        if isinstance(session_id, str)
    }
    merged["sessions"] = {
        session_id: merge_desktop_session_entries(
            [
                root_state["sessions"][session_id]
                for root_state in values
                if isinstance(root_state.get("sessions"), dict)
                and isinstance(root_state["sessions"].get(session_id), dict)
            ]
        )
        for session_id in session_ids
    }
    return merged


def desktop_replica_slot(account_uuid: str, workspace_uuid: str) -> str:
    return f"{account_uuid}/{workspace_uuid}"


def desktop_record_slot(record: DesktopSessionRecord) -> str:
    return desktop_replica_slot(record.account_uuid, record.workspace_uuid)


def desktop_record_state(record: DesktopSessionRecord) -> str:
    return DESKTOP_STATE_ARCHIVED if record.data.get("isArchived") is True else DESKTOP_STATE_ACTIVE


def desktop_record_mtime_ns(record: DesktopSessionRecord) -> int:
    return record.mtime_ns


def desktop_record_snapshot(record: DesktopSessionRecord) -> dict[str, Any]:
    try:
        relative_path = record.path.relative_to(record.sessions_root).as_posix()
    except ValueError:
        relative_path = record.path.name
    return {
        "account_uuid": record.account_uuid,
        "workspace_uuid": record.workspace_uuid,
        "path": relative_path,
        "state": desktop_record_state(record),
        "mtime_ns": desktop_record_mtime_ns(record),
        "present": True,
        "missing_scans": 0,
    }


def desktop_primary_records(
    records: list[DesktopSessionRecord],
    root_state: dict[str, Any] | None = None,
) -> dict[tuple[str, str], DesktopSessionRecord]:
    primary: dict[tuple[str, str], DesktopSessionRecord] = {}
    for record in records:
        session_id = desktop_record_cli_session_id(record)
        if not session_id:
            continue
        if root_state is not None:
            session_id = desktop_logical_session_id(root_state, session_id)
        key = (session_id, desktop_record_slot(record))
        existing = primary.get(key)
        if existing is None or desktop_record_mtime_ns(record) >= desktop_record_mtime_ns(existing):
            primary[key] = record
    return primary


def desktop_sync_root_state(state: dict[str, Any], root: Path) -> dict[str, Any]:
    roots = state.setdefault("roots", {})
    key = desktop_sync_root_key(root)
    candidate_keys = [key, *desktop_legacy_sync_root_keys(root)]
    root_state = merge_desktop_root_states(
        [roots[candidate_key] for candidate_key in candidate_keys if isinstance(roots.get(candidate_key), dict)]
    )
    root_state.setdefault("sessions", {})
    if not isinstance(root_state["sessions"], dict):
        root_state["sessions"] = {}
    for candidate_key in candidate_keys:
        roots.pop(candidate_key, None)
    roots[key] = root_state
    return root_state


def desktop_existing_root_state(state: dict[str, Any], root: Path) -> dict[str, Any]:
    roots = state.get("roots") if isinstance(state.get("roots"), dict) else {}
    candidate_keys = [desktop_sync_root_key(root), *desktop_legacy_sync_root_keys(root)]
    return merge_desktop_root_states(
        [roots[candidate_key] for candidate_key in candidate_keys if isinstance(roots.get(candidate_key), dict)]
    )


def desktop_artifact_alias_session_ids(paths: Paths) -> set[str]:
    state = load_desktop_sync_state(paths)
    aliases: set[str] = set()
    for root in claude_windows_app_roots(paths):
        root_state = desktop_existing_root_state(state, root)
        values = root_state.get("code_artifact_aliases")
        if isinstance(values, dict):
            aliases.update(session_id for session_id in values if isinstance(session_id, str))
    return aliases


def cached_claude_oauth_profile(paths: Paths, organization_uuid: str) -> dict[str, str] | None:
    state = load_desktop_sync_state(paths)
    for root in claude_windows_app_roots(paths):
        root_state = desktop_existing_root_state(state, root)
        profiles = root_state.get("oauth_account_profiles")
        if not isinstance(profiles, dict):
            continue
        for account_uuid, profile in profiles.items():
            if (
                isinstance(account_uuid, str)
                and isinstance(profile, dict)
                and profile.get("organization_uuid") == organization_uuid
            ):
                return {
                    "account_uuid": account_uuid,
                    "organization_uuid": organization_uuid,
                    "email": profile.get("email") if isinstance(profile.get("email"), str) else "",
                }
    return None


def active_desktop_account_public(paths: Paths) -> dict[str, Any] | None:
    state = load_desktop_sync_state(paths)
    for root in claude_windows_app_roots(paths):
        context = active_desktop_account_context(root)
        if context.logged_out or not context.account_uuid:
            continue
        root_state = desktop_existing_root_state(state, root)
        profiles = root_state.get("oauth_account_profiles")
        profile = profiles.get(context.account_uuid) if isinstance(profiles, dict) else None
        email = profile.get("email") if isinstance(profile, dict) else None
        return {
            "key": f"claude-app:{context.account_uuid}",
            "short_key": context.account_uuid[:8],
            "label": mask_email(email) if isinstance(email, str) and email else f"Claude app {context.account_uuid[:8]}",
            "account_uuid_hash": short_hash(context.account_uuid),
            "organization_uuid_hash": short_hash(context.organization_uuid),
            "source_changed_at": context.changed_at,
        }
    return None


def refresh_desktop_sync_snapshots(
    paths: Paths,
    state: dict[str, Any],
    records: list[DesktopSessionRecord] | None = None,
    roots: list[Path] | None = None,
) -> None:
    records_by_root: dict[Path, list[DesktopSessionRecord]] = {}
    for record in records if records is not None else desktop_session_records(paths):
        records_by_root.setdefault(record.root, []).append(record)
    for root in roots if roots is not None else claude_windows_app_roots(paths):
        root_state = desktop_sync_root_state(state, root)
        sessions = root_state["sessions"]
        primary = desktop_primary_records(records_by_root.get(root, []), root_state)
        by_session: dict[str, dict[str, DesktopSessionRecord]] = {}
        for (session_id, slot), record in primary.items():
            by_session.setdefault(session_id, {})[slot] = record
        for session_id, session_records in by_session.items():
            entry = sessions.get(session_id) if isinstance(sessions.get(session_id), dict) else {}
            if entry.get("state") == DESKTOP_STATE_DELETED:
                continue
            previous = entry.get("replicas") if isinstance(entry.get("replicas"), dict) else {}
            replicas = {slot: desktop_record_snapshot(record) for slot, record in session_records.items()}
            for slot, snapshot in previous.items():
                if slot in replicas or not isinstance(snapshot, dict):
                    continue
                if (
                    snapshot.get("excluded")
                    or snapshot.get("scan_blocked")
                    or int(snapshot.get("missing_scans") or 0) > 0
                ):
                    replicas[slot] = snapshot
            entry["replicas"] = replicas
            entry.setdefault("state", max(session_records.values(), key=desktop_record_mtime_ns) and desktop_record_state(max(session_records.values(), key=desktop_record_mtime_ns)))
            entry.setdefault("state_changed_at_ns", max(desktop_record_mtime_ns(record) for record in session_records.values()))
            sessions[session_id] = entry


def suppress_desktop_replica(paths: Paths, record: DesktopSessionRecord) -> None:
    with desktop_sync_lock(paths):
        state = load_desktop_sync_state(paths)
        root_state = desktop_sync_root_state(state, record.root)
        sessions = root_state["sessions"]
        session_id = desktop_record_cli_session_id(record)
        if not session_id:
            return
        session_id = desktop_logical_session_id(root_state, session_id)
        entry = sessions.get(session_id) if isinstance(sessions.get(session_id), dict) else {
            "state": desktop_record_state(record),
            "state_changed_at_ns": desktop_record_mtime_ns(record),
            "replicas": {},
        }
        replicas = entry.setdefault("replicas", {})
        snapshot = desktop_record_snapshot(record)
        snapshot.update({"present": False, "missing_scans": 0, "excluded": True})
        replicas[desktop_record_slot(record)] = snapshot
        sessions[session_id] = entry
        save_desktop_sync_state(paths, state)


def desktop_tombstoned_session_ids(paths: Paths) -> set[str]:
    state = load_desktop_sync_state(paths)
    tombstones: set[str] = set()
    for root in claude_windows_app_roots(paths):
        root_state = desktop_existing_root_state(state, root)
        sessions = root_state.get("sessions") if isinstance(root_state.get("sessions"), dict) else {}
        tombstones.update(
            session_id
            for session_id, entry in sessions.items()
            if isinstance(session_id, str)
            and isinstance(entry, dict)
            and entry.get("state") == DESKTOP_STATE_DELETED
        )
    return tombstones


def sync_claude_desktop_accounts(
    paths: Paths,
    *,
    include_transcripts: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "roots": 0,
        "accounts": 0,
        "sessions": 0,
        "created": 0,
        "transcripts_created": 0,
        "updated": 0,
        "repaired": 0,
        "deduped": 0,
        "archived": 0,
        "unarchived": 0,
        "deleted": 0,
        "removed": 0,
        "pending_deletions": 0,
        "tombstone_skips": 0,
        "backups": [],
        "artifact_accounts": 0,
        "artifacts": 0,
        "artifacts_created": 0,
        "artifacts_updated": 0,
        "artifacts_deleted": 0,
        "artifacts_removed": 0,
        "pending_artifact_deletions": 0,
        "artifact_missing_files": 0,
        "artifact_backups": [],
        "code_artifacts": 0,
        "code_artifact_copies_created": 0,
        "code_artifact_transcripts_created": 0,
        "code_artifact_pending_accounts": 0,
        "code_artifact_errors": [],
        "transcripts_scanned": include_transcripts,
    }

    available_transcript_chats = (
        [
            chat
            for chat in discover_chats(paths.claude_home, desktop_artifact_alias_session_ids(paths))
            if chat.session_id and chat.cwd and chat.can_queue
        ]
        if include_transcripts
        else []
    )
    roots = claude_windows_app_roots(paths)
    log_contexts_by_root = {
        root: desktop_account_log_contexts(root)
        for root in roots
    }
    active_context_by_root = {
        root: active_desktop_account_context(root, log_contexts_by_root[root])
        for root in roots
    }
    with desktop_sync_lock(paths):
        state = load_desktop_sync_state(paths)
        records = desktop_session_records(
            paths,
            roots=roots,
            account_contexts=active_context_by_root,
        )
        for record in records:
            sanitized = sanitize_desktop_session_data(record.data)
            if sanitized != record.data:
                backup = backup_desktop_session_file(paths, record.path)
                result["backups"].append(str(backup))
                write_desktop_session_json(record.path, sanitized)
                result["repaired"] += 1
        if result["repaired"]:
            records = desktop_session_records(
                paths,
                roots=roots,
                account_contexts=active_context_by_root,
            )

        records_by_root: dict[Path, list[DesktopSessionRecord]] = {}
        for record in records:
            records_by_root.setdefault(record.root, []).append(record)

        artifact_jobs: list[
            tuple[
                Path,
                set[str],
                dict[str, list[str]],
                dict[str, Any],
            ]
        ] = []
        for root in roots:
            root_records = records_by_root.get(root, [])
            sessions_root = root / "claude-code-sessions"
            root_state = desktop_sync_root_state(state, root)
            session_state = root_state["sessions"]
            active_context = active_context_by_root[root]
            active_account_uuid = active_desktop_account_uuid(root, active_context)
            active_workspace_uuid = active_desktop_workspace_uuid(
                root,
                active_account_uuid,
                active_context,
            )

            account_uuids = {record.account_uuid for record in root_records if record.account_uuid}
            if sessions_root.exists():
                account_uuids.update(path.name for path in sessions_root.iterdir() if path.is_dir())
            artifact_sessions_root = root / "local-agent-mode-sessions"
            if artifact_sessions_root.exists():
                account_uuids.update(
                    path.name
                    for path in artifact_sessions_root.iterdir()
                    if path.is_dir() and path.name != "skills-plugin"
                )
            if active_account_uuid:
                account_uuids.add(active_account_uuid)
            if not account_uuids:
                continue
            fallback_workspace_uuid = (
                max(root_records, key=desktop_record_mtime_ns).workspace_uuid
                if root_records
                else None
            )
            workspace_uuids_by_account = {
                account_uuid: desktop_account_workspace_uuids(
                    sessions_root,
                    account_uuid,
                    (active_workspace_uuid or fallback_workspace_uuid)
                    if account_uuid == active_account_uuid
                    else None,
                )
                for account_uuid in account_uuids
            }

            sync_claude_code_artifacts(
                paths,
                root,
                account_uuids,
                root_state,
                result,
                scan_transcripts=include_transcripts,
            )
            transcript_chats = {
                chat.session_id: chat
                for chat in available_transcript_chats
                if desktop_logical_session_id(root_state, chat.session_id) == chat.session_id
            }

            records_by_session: dict[str, list[DesktopSessionRecord]] = {}
            for record in root_records:
                session_id = desktop_record_cli_session_id(record)
                if session_id:
                    session_id = desktop_logical_session_id(root_state, session_id)
                    records_by_session.setdefault(session_id, []).append(record)

            # Keep one metadata file per logical session/account workspace. Every removal is backed up.
            for session_id, session_records in records_by_session.items():
                by_slot: dict[str, list[DesktopSessionRecord]] = {}
                for record in session_records:
                    by_slot.setdefault(desktop_record_slot(record), []).append(record)
                kept: list[DesktopSessionRecord] = []
                for slot_records in by_slot.values():
                    primary = max(slot_records, key=desktop_record_mtime_ns)
                    kept.append(primary)
                    for duplicate in slot_records:
                        if duplicate.path == primary.path or not duplicate.path.exists():
                            continue
                        backup = backup_desktop_session_file(paths, duplicate.path)
                        result["backups"].append(str(backup))
                        duplicate.path.unlink()
                        result["deduped"] += 1

                records_by_session[session_id] = kept

            all_session_ids = set(records_by_session) | set(session_state) | set(transcript_chats)
            result["roots"] += 1
            result["accounts"] += len(account_uuids)
            result["sessions"] += len(all_session_ids)

            for session_id in sorted(all_session_ids):
                session_records = list(records_by_session.get(session_id, []))
                entry = session_state.get(session_id) if isinstance(session_state.get(session_id), dict) else {}
                stored_bridge_owners = entry.get("bridge_owners") if isinstance(entry.get("bridge_owners"), dict) else {}
                bridge_owners: dict[str, str] = {
                    bridge_session_id: account_uuid
                    for bridge_session_id, account_uuid in stored_bridge_owners.items()
                    if isinstance(bridge_session_id, str)
                    and bridge_session_id
                    and isinstance(account_uuid, str)
                    and account_uuid
                }
                stored_bridge_owner_ids = set(bridge_owners)
                bridge_owner_keys: dict[str, tuple[int, str]] = {}
                for record in session_records:
                    owner_key = (desktop_record_mtime_ns(record), record.account_uuid)
                    for bridge_session_id in desktop_record_bridge_session_ids(record):
                        if bridge_session_id in stored_bridge_owner_ids:
                            continue
                        if bridge_session_id not in bridge_owner_keys or owner_key < bridge_owner_keys[bridge_session_id]:
                            bridge_owner_keys[bridge_session_id] = owner_key
                            bridge_owners[bridge_session_id] = record.account_uuid
                entry["bridge_owners"] = bridge_owners
                previous_state = entry.get("state") if entry.get("state") in {
                    DESKTOP_STATE_ACTIVE,
                    DESKTOP_STATE_ARCHIVED,
                    DESKTOP_STATE_DELETED,
                } else None
                previous_replicas = entry.get("replicas") if isinstance(entry.get("replicas"), dict) else {}
                if (
                    previous_state == DESKTOP_STATE_DELETED
                    and desktop_session_entry_has_workspace_transition(entry)
                    and (session_records or session_id in transcript_chats)
                ):
                    previous_state = None
                    previous_replicas = {}
                    entry["state"] = DESKTOP_STATE_ACTIVE
                    entry["replicas"] = previous_replicas
                    result["repaired"] += 1
                current_by_slot = {
                    desktop_record_slot(record): record
                    for record in session_records
                }
                events: list[tuple[int, int, str, DesktopSessionRecord | None]] = []

                if previous_state != DESKTOP_STATE_DELETED:
                    for slot, previous in previous_replicas.items():
                        if not isinstance(previous, dict) or previous.get("excluded"):
                            continue
                        current = current_by_slot.get(slot)
                        if current is not None:
                            old_state = previous.get("state")
                            new_state = desktop_record_state(current)
                            previous["missing_scans"] = 0
                            previous["present"] = True
                            previous["scan_blocked"] = False
                            if old_state in {DESKTOP_STATE_ACTIVE, DESKTOP_STATE_ARCHIVED} and new_state != old_state:
                                priority = 2 if new_state == DESKTOP_STATE_ARCHIVED else 1
                                events.append((desktop_record_mtime_ns(current), priority, new_state, current))
                            continue

                        account_uuid = previous.get("account_uuid")
                        if isinstance(account_uuid, str) and any(
                            record.account_uuid == account_uuid
                            for record in current_by_slot.values()
                        ):
                            # The account moved to a new organization/workspace directory.
                            # Its current replica proves that the missing old slot is not a delete.
                            previous["missing_scans"] = 0
                            previous["present"] = False
                            previous["scan_blocked"] = False
                            continue
                        relative_path = previous.get("path")
                        previous_path = sessions_root / str(relative_path) if isinstance(relative_path, str) else None
                        parent_readable = previous_path is not None and previous_path.parent.is_dir()
                        file_is_really_missing = previous_path is not None and not previous_path.exists()
                        if parent_readable and file_is_really_missing:
                            missing_scans = int(previous.get("missing_scans") or 0) + 1
                            previous["missing_scans"] = missing_scans
                            previous["present"] = False
                            previous["scan_blocked"] = False
                            if missing_scans >= 2:
                                events.append((time.time_ns(), 3, DESKTOP_STATE_DELETED, None))
                            else:
                                result["pending_deletions"] += 1
                        else:
                            # Missing account/workspace or an unreadable/corrupt JSON is not a delete event.
                            previous["missing_scans"] = 0
                            previous["present"] = False
                            previous["scan_blocked"] = True

                    # A new external replica can carry a newer archive/unarchive decision.
                    for slot, current in current_by_slot.items():
                        if slot in previous_replicas:
                            continue
                        current_state = desktop_record_state(current)
                        priority = 2 if current_state == DESKTOP_STATE_ARCHIVED else 1
                        events.append((desktop_record_mtime_ns(current), priority, current_state, current))

                if previous_state == DESKTOP_STATE_DELETED:
                    canonical_state = DESKTOP_STATE_DELETED
                    state_event_record = None
                elif previous_state is None and session_records:
                    active_candidates = [
                        record
                        for record in session_records
                        if record.account_uuid == active_account_uuid
                    ]
                    state_event_record = max(active_candidates or session_records, key=desktop_record_mtime_ns)
                    canonical_state = desktop_record_state(state_event_record)
                elif events:
                    _, _, canonical_state, state_event_record = max(events, key=lambda event: (event[0], event[1]))
                elif previous_state is not None:
                    canonical_state = previous_state
                    state_event_record = None
                elif session_records:
                    state_event_record = max(session_records, key=desktop_record_mtime_ns)
                    canonical_state = desktop_record_state(state_event_record)
                else:
                    canonical_state = DESKTOP_STATE_ACTIVE
                    state_event_record = None

                entry["state"] = canonical_state
                entry["state_changed_at_ns"] = max(
                    int(entry.get("state_changed_at_ns") or 0),
                    max((event[0] for event in events), default=0),
                )
                entry["replicas"] = previous_replicas
                session_state[session_id] = entry

                if canonical_state == DESKTOP_STATE_DELETED:
                    # Persist the tombstone before touching replicas so a crash cannot resurrect the chat.
                    save_desktop_sync_state(paths, state)
                    for record in session_records:
                        if not record.path.exists():
                            continue
                        backup = backup_desktop_session_file(paths, record.path)
                        result["backups"].append(str(backup))
                        record.path.unlink()
                        result["removed"] += 1
                    if previous_state != DESKTOP_STATE_DELETED:
                        result["deleted"] += 1
                    elif session_records:
                        result["tombstone_skips"] += 1
                    continue

                if previous_state != canonical_state:
                    if canonical_state == DESKTOP_STATE_ARCHIVED:
                        result["archived"] += 1
                    elif previous_state == DESKTOP_STATE_ARCHIVED:
                        result["unarchived"] += 1

                chat = transcript_chats.get(session_id)
                source_record = state_event_record
                if source_record is None and session_records:
                    source_record = max(
                        session_records,
                        key=lambda record: (desktop_record_timestamp_ms(record), desktop_record_mtime_ns(record)),
                    )
                if source_record is None and chat is None:
                    continue
                source_data = source_record.data if source_record is not None else synthetic_desktop_session_data(chat)

                for account_uuid in sorted(account_uuids):
                    for workspace_uuid in workspace_uuids_by_account[account_uuid]:
                        slot = desktop_replica_slot(account_uuid, workspace_uuid)
                        previous = previous_replicas.get(slot) if isinstance(previous_replicas.get(slot), dict) else {}
                        if previous.get("excluded"):
                            continue
                        target_records = [
                            record
                            for record in session_records
                            if record.account_uuid == account_uuid
                            and record.workspace_uuid == workspace_uuid
                        ]
                        if not target_records and (
                            previous.get("scan_blocked")
                            or int(previous.get("missing_scans") or 0) > 0
                        ):
                            continue
                        if target_records:
                            primary = max(target_records, key=desktop_record_mtime_ns)
                            base_data = source_data
                            if source_record is not None and desktop_record_timestamp_ms(primary) > desktop_record_timestamp_ms(source_record):
                                base_data = primary.data
                            data = desktop_session_data_for_path(
                                base_data,
                                desktop_account_session_id(root_state, session_id, account_uuid),
                                primary.path,
                                archived=canonical_state == DESKTOP_STATE_ARCHIVED,
                            )
                            account_bridge_ids = [
                                bridge_session_id
                                for bridge_session_id in desktop_record_bridge_session_ids(primary)
                                if bridge_owners.get(bridge_session_id) == account_uuid
                            ]
                            if account_bridge_ids:
                                data["bridgeSessionIds"] = account_bridge_ids
                            else:
                                data.pop("bridgeSessionIds", None)
                            if data != primary.data:
                                backup = backup_desktop_session_file(paths, primary.path)
                                result["backups"].append(str(backup))
                                write_desktop_session_json(primary.path, data)
                                result["updated"] += 1
                            continue

                        destination_dir = sessions_root / account_uuid / workspace_uuid
                        app_session_id = source_data.get("sessionId")
                        destination = unique_desktop_session_path(
                            destination_dir,
                            str(app_session_id).removeprefix("local_")
                            if isinstance(app_session_id, str) and app_session_id
                            else None,
                        )
                        data = desktop_session_data_for_path(
                            source_data,
                            desktop_account_session_id(root_state, session_id, account_uuid),
                            destination,
                            archived=canonical_state == DESKTOP_STATE_ARCHIVED,
                        )
                        data.pop("bridgeSessionIds", None)
                        write_desktop_session_json(destination, data)
                        created = DesktopSessionRecord(
                            root=root,
                            sessions_root=sessions_root,
                            account_uuid=account_uuid,
                            workspace_uuid=workspace_uuid,
                            path=destination,
                            data=data,
                            mtime_ns=time.time_ns(),
                            active_account_uuid=active_account_uuid,
                        )
                        session_records.append(created)
                        result["created"] += 1
                        if source_record is None:
                            result["transcripts_created"] += 1

                # Older releases produced account x workspace copies. Keep only the
                # account's chosen workspace replica, with a backup for every removal.
                by_account: dict[str, list[DesktopSessionRecord]] = {}
                for record in session_records:
                    by_account.setdefault(record.account_uuid, []).append(record)
                for account_uuid, account_records in by_account.items():
                    preferred = set(workspace_uuids_by_account.get(account_uuid, []))
                    preferred_records = [record for record in account_records if record.workspace_uuid in preferred]
                    primary = max(preferred_records or account_records, key=desktop_record_mtime_ns)
                    for duplicate in account_records:
                        if duplicate.path == primary.path or not duplicate.path.exists():
                            continue
                        backup = backup_desktop_session_file(paths, duplicate.path)
                        result["backups"].append(str(backup))
                        duplicate.path.unlink()
                        result["deduped"] += 1

            artifact_jobs.append(
                (
                    root,
                    account_uuids,
                    workspace_uuids_by_account,
                    root_state,
                )
            )

        session_files_changed = any(
            int(result[key] or 0) > 0
            for key in ("created", "updated", "repaired", "deduped", "removed")
        )
        current_records = (
            desktop_session_records(
                paths,
                roots=roots,
                account_contexts=active_context_by_root,
            )
            if session_files_changed
            else records
        )
        current_records_by_root: dict[Path, list[DesktopSessionRecord]] = {}
        for record in current_records:
            current_records_by_root.setdefault(record.root, []).append(record)
        for root, account_uuids, workspace_uuids_by_account, root_state in artifact_jobs:
            sync_claude_desktop_artifacts(
                paths,
                root,
                current_records_by_root.get(root, []),
                account_uuids,
                workspace_uuids_by_account,
                state,
                root_state,
                result,
                log_contexts_by_root[root],
                active_context_by_root[root],
            )

        refresh_desktop_sync_snapshots(paths, state, current_records, roots)
        save_desktop_sync_state(paths, state)
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
    sync_state = load_desktop_sync_state(paths)
    source_records = [
        record
        for record in records
        if desktop_record_cli_session_id(record)
        and desktop_logical_session_id(
            desktop_existing_root_state(sync_state, record.root),
            desktop_record_cli_session_id(record) or "",
        )
        == chat.session_id
    ]
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
        if record.account_uuid == active_account_uuid
        and desktop_record_cli_session_id(record)
        and desktop_logical_session_id(
            desktop_existing_root_state(sync_state, record.root),
            desktop_record_cli_session_id(record) or "",
        )
        == chat.session_id
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
                suppress_desktop_replica(paths, record)
                record.path.unlink()
                removed_source = str(record.path)
                break
        artifact_sync = sync_claude_desktop_accounts(paths, include_transcripts=False)
        return {
            "status": "already_active",
            "session_id": chat.session_id,
            "title": chat.title,
            "destination": str(active_records[0].path),
            "active_account": f"Claude app {active_account_uuid[:8]}",
            "active_workspace": active_workspace_uuid,
            "backup": str(backup_path) if backup_path else None,
            "removed_source": removed_source,
            "artifact_sync": {
                key: artifact_sync[key]
                for key in [
                    "artifacts_created",
                    "artifacts_updated",
                    "artifacts_deleted",
                    "artifacts_removed",
                    "artifact_missing_files",
                    "code_artifacts",
                    "code_artifact_copies_created",
                    "code_artifact_transcripts_created",
                    "code_artifact_pending_accounts",
                    "code_artifact_errors",
                ]
            },
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
        suppress_desktop_replica(paths, source_record)
        source_record.path.unlink()
        removed_source = str(source_record.path)

    artifact_sync = sync_claude_desktop_accounts(paths, include_transcripts=False)
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
        "artifact_sync": {
            key: artifact_sync[key]
            for key in [
                "artifacts_created",
                "artifacts_updated",
                "artifacts_deleted",
                "artifacts_removed",
                "artifact_missing_files",
                "code_artifacts",
                "code_artifact_copies_created",
                "code_artifact_transcripts_created",
                "code_artifact_pending_accounts",
                "code_artifact_errors",
            ]
        },
    }


def transfer_codex_chat_to_active_account(paths: Paths, chat: Chat) -> dict[str, Any]:
    if chat.provider != PROVIDER_CODEX:
        raise ValueError("La task selezionata non appartiene a Codex.")
    active = active_codex_account(paths)
    if active is None:
        raise ValueError("Account ChatGPT/Codex attivo non rilevato.")
    if chat.account_key == active.key:
        return {
            "status": "already_active",
            "session_id": chat.session_id,
            "title": chat.title,
            "active_account": active.label,
            "destination": str(chat.jsonl_path),
        }
    codex_exe = find_codex_executable(paths)
    if codex_exe is None:
        raise ValueError("CLI Codex non trovato: non posso creare una copia supportata della task.")

    with account_index_lock(paths):
        index = load_account_index(paths)
        register_active_codex_account(paths, index)
        links = index.setdefault("codex_links", {})
        group_id = next(
            (
                candidate
                for candidate, group in links.items()
                if isinstance(group, dict)
                and isinstance(group.get("threads"), dict)
                and chat.session_id in group["threads"]
            ),
            None,
        )
        group = links.get(group_id) if group_id and isinstance(links.get(group_id), dict) else None
        if group is not None:
            threads = group.get("threads") if isinstance(group.get("threads"), dict) else {}
            local_states = codex_local_thread_states(paths)
            for linked_id, linked in threads.items():
                if (
                    isinstance(linked, dict)
                    and linked.get("account_key") == active.key
                    and linked_id in local_states
                ):
                    return {
                        "status": "already_copied",
                        "session_id": linked_id,
                        "source_session_id": chat.session_id,
                        "title": chat.title,
                        "active_account": active.label,
                        "destination": str(codex_thread_rows(paths.codex_home).get(linked_id, {}).get("rollout_path") or linked_id),
                    }

        fork = codex_app_server_request(
            codex_exe,
            "thread/fork",
            {"threadId": chat.session_id, "excludeTurns": True},
            codex_home=paths.codex_home,
        )
        thread = fork.get("thread") if isinstance(fork.get("thread"), dict) else {}
        destination_session_id = thread.get("id") if isinstance(thread.get("id"), str) else None
        if not destination_session_id or destination_session_id == chat.session_id:
            raise ValueError("Codex non ha restituito un nuovo ID per la copia della task.")
        states: dict[str, str] = {}
        for _ in range(20):
            states = codex_local_thread_states(paths)
            if destination_session_id in states:
                break
            time.sleep(0.1)
        if destination_session_id not in states:
            raise ValueError("La copia Codex non risulta nello store locale dopo il fork.")

        sessions = index.setdefault("sessions", {})
        now = now_utc()
        source_key = chat_account_session_key(chat)
        source_previous = sessions.get(source_key) if isinstance(sessions.get(source_key), dict) else {}
        sessions[source_key] = {
            **source_previous,
            "account_key": chat.account_key or source_previous.get("account_key"),
            "label": chat.account_label or source_previous.get("label"),
            "provider": PROVIDER_CODEX,
            "session_id": chat.session_id,
            "source": chat.source,
            "title": chat.title,
            "cwd": chat.cwd,
            "first_seen_at": source_previous.get("first_seen_at") or now,
            "last_seen_at": now,
        }
        destination_chat = dataclasses.replace(
            chat,
            session_id=destination_session_id,
            archived=states[destination_session_id] == DESKTOP_STATE_ARCHIVED,
        )
        destination_key = chat_account_session_key(destination_chat)
        sessions[destination_key] = {
            "account_key": active.key,
            "label": active.label,
            "provider": PROVIDER_CODEX,
            "session_id": destination_session_id,
            "source": "Codex App (copia account)",
            "title": chat.title,
            "cwd": chat.cwd,
            "first_seen_at": now,
            "last_seen_at": now,
            "forked_from": chat.session_id,
        }

        if group is None:
            group_id = str(uuid.uuid4())
            group = {
                "provider": PROVIDER_CODEX,
                "source_session_id": chat.session_id,
                "created_at": now,
                "state": states.get(chat.session_id, DESKTOP_STATE_ARCHIVED if chat.archived else DESKTOP_STATE_ACTIVE),
                "threads": {},
            }
        threads = group.setdefault("threads", {})
        threads[chat.session_id] = {
            **(threads.get(chat.session_id) if isinstance(threads.get(chat.session_id), dict) else {}),
            "account_key": chat.account_key or source_previous.get("account_key"),
            "last_state": states.get(chat.session_id, DESKTOP_STATE_ARCHIVED if chat.archived else DESKTOP_STATE_ACTIVE),
            "missing_scans": 0,
        }
        threads[destination_session_id] = {
            "account_key": active.key,
            "last_state": states[destination_session_id],
            "missing_scans": 0,
            "forked_from": chat.session_id,
        }
        group["threads"] = threads
        links[str(group_id)] = group
        index["codex_links"] = links
        save_account_index(paths, index)

        row = codex_thread_rows(paths.codex_home).get(destination_session_id, {})
        return {
            "status": "forked",
            "session_id": destination_session_id,
            "source_session_id": chat.session_id,
            "title": chat.title,
            "active_account": active.label,
            "destination": str(row.get("rollout_path") or destination_session_id),
            "archived": states[destination_session_id] == DESKTOP_STATE_ARCHIVED,
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
    sync_state = load_desktop_sync_state(paths)
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
        cli_session_id = desktop_logical_session_id(
            desktop_existing_root_state(sync_state, record.root),
            cli_session_id,
        )
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
                last_user_message=message_preview(data.get("lastPrompt") if isinstance(data.get("lastPrompt"), str) else None),
                effort_level=data.get("effort") if isinstance(data.get("effort"), str) else None,
                source="Claude Windows App",
                source_key="claude_windows_app",
                can_queue=can_queue,
                account_key=account["account_key"],
                account_label=account["account_label"],
                account_status=str(account["account_status"] or "unknown"),
                account_copies=(str(account["account_label"]),) if account["account_label"] else (),
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
                last_user_message=message_preview(
                    metadata.get("lastPrompt") if isinstance(metadata.get("lastPrompt"), str) else None
                ),
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
    $lastUserMessage = $null
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
        $lastUserMessage = $lastPrompt
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
        if ($objType -eq "user") {
          $content = $obj.message.content
          if ($content -is [string] -and -not [string]::IsNullOrWhiteSpace($content)) {
            $lastUserMessage = [string]$content
          } elseif ($content -is [System.Array]) {
            $textParts = @(
              $content | Where-Object { $_.type -eq "text" -and $null -ne $_.text } | ForEach-Object { [string]$_.text }
            )
            if ($textParts.Count -gt 0) {
              $lastUserMessage = [string]::Join("`n", $textParts)
            }
          }
        }
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
      last_user_message = $lastUserMessage
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
            last_user_message = (
                summary.get("last_user_message") if isinstance(summary.get("last_user_message"), str) else last_prompt
            )
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
                    last_user_message=message_preview(last_user_message),
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


def merged_chat_account_copies(*chats: Chat) -> tuple[str, ...]:
    labels: list[str] = []
    ordered = [chat for chat in chats if chat.account_status == "active"] + list(chats)
    for chat in ordered:
        candidates = ([chat.account_label] if chat.account_label else []) + list(chat.account_copies)
        for label in candidates:
            if label and label not in labels:
                labels.append(label)
    return tuple(labels)


def merge_claude_chat_sources(local_chats: list[Chat], vscode_chats: list[Chat]) -> list[Chat]:
    by_session = {chat.session_id: chat for chat in local_chats}
    for chat in vscode_chats:
        existing = by_session.get(chat.session_id)
        if existing is None:
            by_session[chat.session_id] = chat
            continue
        account_copies = merged_chat_account_copies(existing, chat)
        if existing.account_status == "active" and chat.account_status == "other":
            by_session[chat.session_id] = dataclasses.replace(existing, account_copies=account_copies)
            continue
        account_chat = existing
        if chat.account_key and (
            chat.account_status == "active"
            or not existing.account_key
            or existing.account_status != "active"
        ):
            account_chat = chat
        existing_key = chat_sort_key(existing)
        cache_key = chat_sort_key(chat)
        prefer_metadata = chat.source_key != "claude_code" or bool(chat.remote_kind)
        prefer_cache_context = bool(chat.remote_kind) or (
            chat.source_key == "claude_windows_app" and bool(chat.cwd)
        ) or not chat_cwd_runnable(existing.cwd)
        cwd = chat.cwd if prefer_cache_context and chat.cwd else existing.cwd
        use_newer_timestamp = cache_key >= existing_key
        latest_user_message = (
            (chat.last_user_message or existing.last_user_message)
            if use_newer_timestamp
            else (existing.last_user_message or chat.last_user_message)
        )
        jsonl_path = existing.jsonl_path
        if (
            existing.source_key == "claude_windows_app"
            and chat.source_key == "claude_windows_app"
            and account_chat is chat
        ):
            jsonl_path = chat.jsonl_path
        by_session[chat.session_id] = Chat(
            session_id=existing.session_id,
            title=chat.title if prefer_metadata and chat.title else existing.title,
            cwd=cwd,
            permission_mode=chat.permission_mode or existing.permission_mode,
            model=chat.model or existing.model,
            jsonl_path=jsonl_path,
            last_timestamp=chat.last_timestamp if use_newer_timestamp else existing.last_timestamp,
            message_count=max(existing.message_count, chat.message_count),
            last_prompt=existing.last_prompt,
            last_user_message=latest_user_message,
            effort_level=chat.effort_level or existing.effort_level,
            source=chat.source if prefer_metadata else existing.source,
            source_key=chat.source_key if prefer_metadata else existing.source_key,
            can_queue=(chat.can_queue or existing.can_queue or chat_cwd_runnable(cwd) or bool(chat.remote_kind))
            and account_chat.account_status != "other",
            remote_kind=chat.remote_kind,
            remote_host=chat.remote_host,
            remote_cwd=chat.remote_cwd,
            remote_uri=chat.remote_uri,
            account_key=account_chat.account_key,
            account_label=account_chat.account_label,
            account_status=account_chat.account_status,
            account_copies=account_copies,
        )
    return sorted(by_session.values(), key=chat_sort_key, reverse=True)


def _annotate_chats_with_accounts_unlocked(paths: Paths, chats: list[Chat]) -> list[Chat]:
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


def annotate_chats_with_accounts(paths: Paths, chats: list[Chat]) -> list[Chat]:
    with account_index_lock(paths):
        return _annotate_chats_with_accounts_unlocked(paths, chats)


def _annotate_codex_chats_with_accounts_unlocked(paths: Paths, chats: list[Chat]) -> list[Chat]:
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


def annotate_codex_chats_with_accounts(paths: Paths, chats: list[Chat]) -> list[Chat]:
    with account_index_lock(paths):
        return _annotate_codex_chats_with_accounts_unlocked(paths, chats)


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
    transcript_chats = discover_chats(
        paths.claude_home,
        desktop_artifact_alias_session_ids(paths),
    ) + discover_remote_ssh_chats(paths, agent_chats)
    tombstones = desktop_tombstoned_session_ids(paths)
    merged = merge_claude_chat_sources(transcript_chats, agent_chats + desktop_chats)
    return annotate_chats_with_accounts(
        paths,
        [chat for chat in merged if chat.session_id not in tombstones],
    )


def discover_agent_chats(
    paths: Paths,
    sync_desktop_accounts: bool = True,
    active_desktop_only: bool = False,
) -> list[Chat]:
    claude_chats = discover_claude_chats(paths, sync_desktop_accounts, active_desktop_only)
    codex_chats = annotate_codex_chats_with_accounts(paths, discover_codex_app_sessions(paths))
    return sorted(claude_chats + codex_chats, key=chat_sort_key, reverse=True)


def _remember_chat_account_unlocked(paths: Paths, chat: Chat) -> Chat:
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


def remember_chat_account(paths: Paths, chat: Chat) -> Chat:
    with account_index_lock(paths):
        return _remember_chat_account_unlocked(paths, chat)


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


def codex_subprocess_env(
    codex_home: Path | None = None,
    *,
    windows: bool = False,
) -> dict[str, str]:
    env = os.environ.copy()
    for name in CODEX_EXTERNAL_AUTH_ENV_VARS:
        env.pop(name, None)
    if codex_home is not None:
        env["CODEX_HOME"] = local_to_windows_path(codex_home) if windows else str(codex_home)
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


def ensure_auto_continue_activation_id(auto_continue: dict[str, Any]) -> str:
    value = auto_continue.get("activation_id")
    if isinstance(value, str) and value:
        return value
    session_id = str(auto_continue.get("session_id") or "")
    created_at = str(auto_continue.get("created_at") or "")
    value = (
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"claude-codex-queue:auto:{session_id}:{created_at}"))
        if session_id or created_at
        else str(uuid.uuid4())
    )
    auto_continue["activation_id"] = value
    return value


def auto_continue_cancel_marker(queue_file: Path, auto_continue: dict[str, Any]) -> Path | None:
    value = ensure_auto_continue_activation_id(auto_continue)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return queue_file.parent / AUTO_CONTINUE_CANCEL_DIR_NAME / f"{digest}.cancelled"


def auto_continue_is_cancelled(queue_file: Path, auto_continue: dict[str, Any]) -> bool:
    marker = auto_continue_cancel_marker(queue_file, auto_continue)
    return marker is not None and marker.is_file()


def mark_auto_continue_cancelled(queue_file: Path, auto_continue: dict[str, Any]) -> None:
    ensure_auto_continue_activation_id(auto_continue)
    marker = auto_continue_cancel_marker(queue_file, auto_continue)
    if marker is None:
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="utf-8") as handle:
            handle.write(now_utc() + "\n")
    except FileExistsError:
        pass


def auto_continue_revision(auto_continue: dict[str, Any]) -> float:
    for key in ("updated_at", "created_at"):
        value = auto_continue.get(key)
        parsed = parse_iso(value if isinstance(value, str) else None)
        if parsed is not None:
            return parsed.timestamp()
    return 0.0


def compare_auto_continue_states(left: dict[str, Any], right: dict[str, Any]) -> int:
    left_activation = ensure_auto_continue_activation_id(left)
    right_activation = ensure_auto_continue_activation_id(right)
    if left_activation != right_activation:
        left_created = parse_iso(left.get("created_at"))
        right_created = parse_iso(right.get("created_at"))
        if left_created is not None and right_created is not None and left_created != right_created:
            return 1 if left_created > right_created else -1
        if left_created is not None and right_created is None:
            return 1
        if left_created is None and right_created is not None:
            return -1

    left_revision = auto_continue_revision(left)
    right_revision = auto_continue_revision(right)
    if left_revision == right_revision:
        return 0
    return 1 if left_revision > right_revision else -1


def auto_continue_state_key(auto_continue: dict[str, Any]) -> str:
    session_id = str(auto_continue.get("session_id") or "")
    if session_id:
        return f"session:{session_id}"
    return f"activation:{ensure_auto_continue_activation_id(auto_continue)}"


def auto_continue_states(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_states = data.get("auto_continues")
    candidates = [state for state in raw_states if isinstance(state, dict)] if isinstance(raw_states, list) else []
    legacy = data.get("auto_continue")
    if isinstance(legacy, dict):
        candidates.append(legacy)

    states: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for state in candidates:
        ensure_auto_continue_activation_id(state)
        key = auto_continue_state_key(state)
        position = positions.get(key)
        if position is None:
            positions[key] = len(states)
            states.append(state)
            continue
        if compare_auto_continue_states(state, states[position]) > 0:
            states[position] = state

    data["auto_continues"] = states
    sync_auto_continue_legacy(data)
    return states


def auto_continue_schedule_key(auto_continue: dict[str, Any]) -> tuple[int, float, float, str]:
    now = dt.datetime.now(UTC)
    not_before = parse_iso(auto_continue.get("not_before"))
    ready = not_before is None or not_before <= now
    last_check = parse_iso(auto_continue.get("last_check_at"))
    created = parse_iso(auto_continue.get("created_at"))
    return (
        0 if ready else 1,
        (last_check or created).timestamp() if ready and (last_check or created) else (not_before.timestamp() if not_before else 0.0),
        created.timestamp() if created else 0.0,
        str(auto_continue.get("session_id") or ""),
    )


def sync_auto_continue_legacy(data: dict[str, Any]) -> dict[str, Any] | None:
    raw_states = data.get("auto_continues")
    states = [state for state in raw_states if isinstance(state, dict)] if isinstance(raw_states, list) else []
    active = [state for state in states if state.get("enabled")]
    if active:
        primary = min(active, key=auto_continue_schedule_key)
    elif states:
        primary = max(states, key=auto_continue_revision)
    else:
        primary = None
    data["auto_continue"] = primary
    return primary


def find_auto_continue_state(data: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    return next(
        (state for state in auto_continue_states(data) if state.get("session_id") == session_id),
        None,
    )


def set_auto_continue_state(data: dict[str, Any], state: dict[str, Any]) -> None:
    states = auto_continue_states(data)
    key = auto_continue_state_key(state)
    for index, current in enumerate(states):
        if auto_continue_state_key(current) == key:
            states[index] = state
            break
    else:
        states.append(state)
    data["auto_continues"] = states
    sync_auto_continue_legacy(data)


def apply_auto_continue_cancellation(queue_file: Path, data: dict[str, Any]) -> None:
    for auto_continue in auto_continue_states(data):
        if not auto_continue_is_cancelled(queue_file, auto_continue):
            continue
        auto_continue["enabled"] = False
        auto_continue["status"] = "disabled"
        auto_continue.setdefault("disabled_at", now_utc())
        auto_continue["sending_started_at"] = None
        auto_continue["next_check_in_seconds"] = None
    sync_auto_continue_legacy(data)


def merge_auto_continue_collections(data: dict[str, Any], existing: dict[str, Any]) -> None:
    incoming_states = list(auto_continue_states(data))
    merged = list(auto_continue_states(existing))
    positions = {auto_continue_state_key(state): index for index, state in enumerate(merged)}
    for state in incoming_states:
        key = auto_continue_state_key(state)
        position = positions.get(key)
        if position is None:
            positions[key] = len(merged)
            merged.append(state)
            continue
        if compare_auto_continue_states(state, merged[position]) >= 0:
            merged[position] = state
    data["auto_continues"] = merged
    sync_auto_continue_legacy(data)


@contextlib.contextmanager
def queue_write_lock(queue_file: Path, timeout: float = 10.0):
    lock_dir = queue_file.parent / f".{queue_file.name}.lock"
    deadline = time.monotonic() + timeout
    acquired = False
    while not acquired:
        try:
            lock_dir.mkdir()
            acquired = True
        except FileExistsError:
            try:
                stale = time.time() - lock_dir.stat().st_mtime > 120
            except OSError:
                stale = False
            if stale:
                with contextlib.suppress(OSError):
                    lock_dir.rmdir()
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timeout acquisendo il lock della coda: {lock_dir}")
            time.sleep(0.02)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock_dir.rmdir()


def load_queue(queue_file: Path) -> dict[str, Any]:
    if not queue_file.exists():
        return {"version": QUEUE_VERSION, "items": [], "recovery": None, "auto_continue": None, "auto_continues": []}
    try:
        data = json.loads(queue_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": QUEUE_VERSION, "items": [], "recovery": None, "auto_continue": None, "auto_continues": []}
    if not isinstance(data, dict):
        return {"version": QUEUE_VERSION, "items": [], "recovery": None, "auto_continue": None, "auto_continues": []}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    data["version"] = QUEUE_VERSION
    data.setdefault("recovery", None)
    data.setdefault("auto_continue", None)
    auto_continue_states(data)
    apply_auto_continue_cancellation(queue_file, data)
    return data


def save_queue(queue_file: Path, data: dict[str, Any], *, merge_auto_continues: bool = True) -> None:
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    with queue_write_lock(queue_file):
        if merge_auto_continues and queue_file.is_file():
            try:
                existing = json.loads(queue_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, dict):
                merge_auto_continue_collections(data, existing)
        data["version"] = QUEUE_VERSION
        auto_continue_states(data)
        apply_auto_continue_cancellation(queue_file, data)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=queue_file.parent, delete=False) as handle:
            handle.write(payload)
            handle.write("\n")
            temp_path = Path(handle.name)
        try:
            atomic_replace_with_retry(temp_path, queue_file)
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()


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


def claude_item_uses_ide(item: dict[str, Any], use_ide: bool) -> bool:
    if not use_ide:
        return False
    return not claude_item_is_desktop(item)


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
    if claude_item_uses_ide(item, use_ide):
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
    return local_executable_command(claude_exe, build_claude_arguments(item, use_ide))


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


def write_output(stream, value):
    if not value:
        return
    raw = value.encode("utf-8", "replace") if isinstance(value, str) else value
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(raw)
        buffer.flush()
    else:
        stream.write(raw.decode("utf-8", "replace"))

payload_path = sys.argv[1]
with open(payload_path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)

for name in payload.get("clear_env", []):
    os.environ.pop(name, None)

try:
    command = payload.get("command")
    if not isinstance(command, list) or not command:
        command = [payload["exe"], *payload.get("args", [])]
    proc = subprocess.run(
        command,
        cwd=payload["cwd"],
        input=payload["prompt"].encode("utf-8"),
        capture_output=True,
        timeout=payload["timeout"],
    )
except subprocess.TimeoutExpired as exc:
    write_output(sys.stdout, exc.stdout)
    write_output(sys.stderr, exc.stderr)
    raise SystemExit(124)

write_output(sys.stdout, proc.stdout)
write_output(sys.stderr, proc.stderr)
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
                    "args": build_claude_arguments(item, use_ide),
                    "command": windows_executable_command(claude_exe, build_claude_arguments(item, use_ide)),
                    "cwd": cwd,
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
        windows_command = ["cmd.exe", "/d", "/c", command]
        return subprocess.run(
            local_windows_hidden_command(windows_command) if is_wsl() else windows_command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout + 30,
            env=claude_subprocess_env(),
            **background_process_kwargs(),
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

    runs_on_windows = codex_executable_is_windows(codex_exe)
    if runs_on_windows:
        if not is_windows_path(cwd):
            raise SystemExit(f"Il CLI Codex Windows non puo' riprendere una task con cwd non Windows: {cwd}")
        executable = local_to_windows_path(codex_exe)
        windows_command = ["cmd.exe", "/d", "/c", "cd", "/d", cwd, "&&", "call", executable, *arguments]
        proc = subprocess.run(
            local_windows_hidden_command(windows_command) if is_wsl() else windows_command,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=codex_subprocess_env(paths.codex_home, windows=True),
            **background_process_kwargs(),
        )
    else:
        proc = subprocess.run(
            local_executable_command(codex_exe, arguments),
            input=prompt,
            text=True,
            cwd=cwd_for_subprocess(cwd),
            capture_output=True,
            timeout=timeout,
            env=codex_subprocess_env(paths.codex_home, windows=False),
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
    source_executable = find_claude_desktop_executable(paths, item) or claude_exe
    return run_claude(paths, source_executable, item, timeout, use_ide)


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
    source_executable = find_claude_desktop_executable(paths, item) or claude_exe
    print_dry_run_details(paths, source_executable, item, use_ide, prompt)


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
                    candidates.append(dt.datetime.fromtimestamp(reset_epoch, tz=UTC).astimezone())
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
    active = active_auto_continues(queue)
    selected = min(active, key=auto_continue_schedule_key) if active else None
    queue["auto_continue"] = selected or sync_auto_continue_legacy(queue)
    return selected


def active_auto_continues(queue: dict[str, Any]) -> list[dict[str, Any]]:
    return [state for state in auto_continue_states(queue) if state.get("enabled")]


def item_ready(item: dict[str, Any]) -> bool:
    not_before = parse_iso(item.get("not_before"))
    return not_before is None or not_before <= dt.datetime.now(UTC)


def recovery_ready(recovery: dict[str, Any]) -> bool:
    not_before = parse_iso(recovery.get("not_before"))
    return not_before is None or not_before <= dt.datetime.now(UTC)


def auto_continue_ready(auto_continue: dict[str, Any]) -> bool:
    not_before = parse_iso(auto_continue.get("not_before"))
    return not_before is None or not_before <= dt.datetime.now(UTC)


def recovery_as_item(recovery: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"recovery-{recovery.get('source_item_id', 'manual')}",
        "status": STATUS_RECOVERY,
        "session_id": recovery["session_id"],
        "title": recovery.get("title") or "Recovery",
        "cwd": recovery.get("cwd"),
        "prompt": recovery.get("prompt") or RECOVERY_PROMPT,
        "attempts": recovery.get("attempts", 0),
        "not_before": recovery.get("not_before"),
        "last_error": recovery.get("last_error"),
        "last_log": recovery.get("last_log"),
        "fingerprint": recovery.get("fingerprint", {}),
        "provider": recovery.get("provider", PROVIDER_CLAUDE),
        "source": recovery.get("source"),
        "source_key": recovery.get("source_key"),
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
        "prompt": auto_continue.get("prompt") or RECOVERY_PROMPT,
        "attempts": auto_continue.get("attempts", 0),
        "not_before": auto_continue.get("not_before"),
        "last_error": auto_continue.get("last_error"),
        "last_log": auto_continue.get("last_log"),
        "fingerprint": auto_continue.get("fingerprint", {}),
        "provider": auto_continue.get("provider", PROVIDER_CLAUDE),
        "source": auto_continue.get("source"),
        "source_key": auto_continue.get("source_key"),
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
    return (dt.datetime.now(UTC) + dt.timedelta(seconds=poll_seconds)).replace(microsecond=0).isoformat()


def update_auto_continue_state(auto_continue: dict[str, Any], status: str | None = None, **fields: Any) -> None:
    if status is not None:
        auto_continue["status"] = status
    # Internal merge revisions need sub-second precision; the UI still renders
    # these timestamps using the machine locale at whole-second precision.
    auto_continue["updated_at"] = dt.datetime.now().astimezone().isoformat(timespec="microseconds")
    for key, value in fields.items():
        auto_continue[key] = value


def codex_recovery_plan_dict(plan: CodexRecoveryPlan) -> dict[str, Any]:
    return {
        "prompt": plan.prompt,
        "kind": plan.kind,
        "rollback_turn_ids": list(plan.rollback_turn_ids),
        "followup_prompts": list(plan.followup_prompts),
        "source_turn_ids": list(plan.source_turn_ids),
    }


def enqueue_codex_recovery_followups(
    queue: dict[str, Any],
    source: dict[str, Any],
    prompts: list[str],
    recovery_group: str,
) -> int:
    existing_sequences = {
        int(item.get("recovery_sequence"))
        for item in queue.get("items", [])
        if item.get("recovery_group") == recovery_group and isinstance(item.get("recovery_sequence"), int)
    }
    created = now_utc()
    start_order = len(queue.get("items", []))
    added = 0
    for sequence, prompt in enumerate(prompts, start=1):
        if sequence in existing_sequences:
            continue
        queue.setdefault("items", []).append(
            {
                "id": str(uuid.uuid4())[:8],
                "status": STATUS_PENDING,
                "created_at": created,
                "order": start_order + added,
                "priority": CODEX_RECOVERY_PRIORITY,
                "session_id": source["session_id"],
                "title": source.get("title") or "Recupero Codex",
                "cwd": source.get("cwd"),
                "prompt": prompt,
                "attempts": 0,
                "not_before": None,
                "last_error": "Messaggio Codex fallito ripristinato automaticamente in coda.",
                "last_log": None,
                "fingerprint": source.get("fingerprint", {}),
                "provider": PROVIDER_CODEX,
                "source": source.get("source"),
                "source_key": source.get("source_key"),
                "jsonl_path": source.get("jsonl_path"),
                "allow_cwd_fallback": bool(source.get("allow_cwd_fallback")),
                "cwd_fallback": source.get("cwd_fallback"),
                "recovery_group": recovery_group,
                "recovery_sequence": sequence,
                **item_account_fields(source),
                **item_setting_overrides(source),
            }
        )
        added += 1
    return added


def prepare_codex_recovery_state(
    paths: Paths,
    codex_exe: Path,
    queue: dict[str, Any],
    state: dict[str, Any],
) -> None:
    if state.get("activation_id") and auto_continue_is_cancelled(paths.queue_file, state):
        update_auto_continue_state(
            state,
            "disabled",
            enabled=False,
            sending_started_at=None,
            next_check_in_seconds=None,
        )
        save_queue(paths.queue_file, queue)
        raise AutoContinueCancelled("Auto-continua disattivato prima del recupero Codex.")
    raw_plan = state.get("codex_recovery_plan")
    if not isinstance(raw_plan, dict):
        plan = codex_recovery_plan(
            codex_exe,
            state["session_id"],
            codex_home=paths.codex_home,
        )
        source_prompt = state.get("source_prompt")
        if (
            not plan.source_turn_ids
            and state.get("source_prompt_recorded") is False
            and isinstance(source_prompt, str)
            and source_prompt
        ):
            plan = CodexRecoveryPlan(
                prompt=source_prompt,
                kind="retry_failed_prompt",
                rollback_turn_ids=(),
                followup_prompts=(),
                source_turn_ids=(),
            )
        raw_plan = codex_recovery_plan_dict(plan)
        state["codex_recovery_plan"] = raw_plan
        state["prompt"] = plan.prompt
        state["action"] = plan.kind
        state["recovery_prompt_preview"] = truncate(plan.prompt, 180)
        state["recovery_followup_count"] = len(plan.followup_prompts)
        state["recovery_group"] = state.get("recovery_group") or f"codex-recovery-{uuid.uuid4()}"
        state["rollback_applied"] = False
        state["followups_queued"] = False
        save_queue(paths.queue_file, queue)

    prompt = raw_plan.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("Piano di recupero Codex privo del prompt iniziale.")
    state["prompt"] = prompt

    if state.get("retry_needs_rollback"):
        if state.get("activation_id") and auto_continue_is_cancelled(paths.queue_file, state):
            raise AutoContinueCancelled("Auto-continua disattivato prima del rollback Codex.")
        turn_id = rollback_latest_failed_codex_prompt(
            codex_exe,
            state["session_id"],
            prompt,
            codex_home=paths.codex_home,
        )
        state["retry_needs_rollback"] = False
        state["last_retry_rollback_turn_id"] = turn_id
        state["rollback_applied_at"] = now_utc()
        save_queue(paths.queue_file, queue)

    if not state.get("rollback_applied"):
        if state.get("activation_id") and auto_continue_is_cancelled(paths.queue_file, state):
            raise AutoContinueCancelled("Auto-continua disattivato prima del rollback Codex.")
        raw_ids = raw_plan.get("rollback_turn_ids")
        turn_ids = tuple(value for value in raw_ids if isinstance(value, str)) if isinstance(raw_ids, list) else ()
        apply_codex_rollback(
            codex_exe,
            state["session_id"],
            turn_ids,
            codex_home=paths.codex_home,
        )
        state["rollback_applied"] = True
        state["rollback_applied_at"] = now_utc()
        save_queue(paths.queue_file, queue)

    if not state.get("followups_queued"):
        if state.get("activation_id") and auto_continue_is_cancelled(paths.queue_file, state):
            raise AutoContinueCancelled("Auto-continua disattivato prima del ripristino della coda Codex.")
        raw_prompts = raw_plan.get("followup_prompts")
        prompts = [value for value in raw_prompts if isinstance(value, str) and value] if isinstance(raw_prompts, list) else []
        recovery_group = state.get("recovery_group")
        if not isinstance(recovery_group, str) or not recovery_group:
            recovery_group = f"codex-recovery-{uuid.uuid4()}"
            state["recovery_group"] = recovery_group
        added = enqueue_codex_recovery_followups(queue, state, prompts, recovery_group)
        state["followups_queued"] = True
        state["recovery_followup_count"] = len(prompts)
        state["recovery_followups_added"] = int(state.get("recovery_followups_added") or 0) + added
        save_queue(paths.queue_file, queue)


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
        "source_prompt": item.get("prompt"),
        "session_id": item["session_id"],
        "title": item.get("title"),
        "cwd": item.get("cwd"),
        "fingerprint": item.get("fingerprint", {}),
        "provider": item.get("provider", PROVIDER_CLAUDE),
        "source": item.get("source"),
        "source_key": item.get("source_key"),
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
        item["last_error"] = "Session limit: determino il recupero corretto prima di proseguire la coda."
    else:
        item["status"] = STATUS_PENDING
        item["not_before"] = None
        item["last_error"] = "Session limit prima della conferma: recupero il turno senza consumare il prompt successivo."


def complete_recovery(queue: dict[str, Any], recovery: dict[str, Any]) -> None:
    source_item_id = recovery.get("source_item_id")
    retried_source_prompt = recovery.get("action") == "retry_failed_prompt"
    if (recovery.get("source_prompt_recorded") or retried_source_prompt) and source_item_id:
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
        "source": chat.source,
        "source_key": chat.source_key,
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
    local_chats = discover_chats(paths.claude_home, desktop_artifact_alias_session_ids(paths))
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
            version_command = [local_to_windows_path(claude_exe), "--version"]
            if is_wsl():
                version_command = local_windows_hidden_command(version_command)
            result = subprocess.run(
                version_command,
                text=True,
                capture_output=True,
                timeout=30,
                **background_process_kwargs(),
            )
            print(f"Versione:     {result.stdout.strip() or result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print("Versione:     verifica non disponibile")
        except (OSError, subprocess.SubprocessError):
            print("Versione:     verifica non riuscita")
    if codex_exe:
        try:
            version_result = run_codex_cli_command(
                codex_exe,
                ["--version"],
                timeout=30,
                codex_home=paths.codex_home,
            )
            login_result = run_codex_cli_command(
                codex_exe,
                ["login", "status"],
                timeout=30,
                codex_home=paths.codex_home,
            )
            print(f"Codex ver.:   {(version_result.stdout or version_result.stderr).strip()}")
            print(f"Codex auth:   {(login_result.stdout or login_result.stderr).strip()}")
        except subprocess.TimeoutExpired:
            print("Codex stato:  verifica non disponibile")
        except (OSError, subprocess.SubprocessError):
            print("Codex stato:  verifica non riuscita")
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
    auto_continues = active_auto_continues(queue)
    if recovery:
        print(
            f"Recovery attiva: prossima azione '{truncate(recovery.get('recovery_prompt_preview') or recovery.get('prompt') or 'analisi automatica', 80)}' "
            f"chat={str(recovery.get('session_id', ''))[:8]} not_before={recovery.get('not_before')}"
        )
    for auto_continue in auto_continues:
        print(
            f"Auto-continua attivo: chat={str(auto_continue.get('session_id', ''))[:8]} "
            f"azione={auto_continue.get('action') or 'monitor'} not_before={auto_continue.get('not_before')}"
        )
    items = queue.get("items", [])
    if not items and not recovery and not auto_continues:
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
        for auto_continue in active_auto_continues(queue):
            mark_auto_continue_cancelled(paths.queue_file, auto_continue)
        queue["items"] = []
        queue["recovery"] = None
        queue["auto_continue"] = None
        queue["auto_continues"] = []
    else:
        queue["items"] = [
            item
            for item in queue.get("items", [])
            if item.get("status") in {STATUS_PENDING, STATUS_RECOVERY}
        ]
    save_queue(paths.queue_file, queue, merge_auto_continues=not args.all)
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
    for auto_continue in active_auto_continues(queue):
        if args.item and auto_continue.get("session_id") != args.item:
            continue
        mark_auto_continue_cancelled(paths.queue_file, auto_continue)
        update_auto_continue_state(
            auto_continue,
            "disabled",
            enabled=False,
            disabled_at=now_utc(),
            sending_started_at=None,
            next_check_in_seconds=None,
        )
        changed += 1
    sync_auto_continue_legacy(queue)
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
    now = dt.datetime.now(UTC)
    recovery = active_recovery(queue)
    if recovery:
        not_before = parse_iso(recovery.get("not_before"))
        if not_before and not_before > now:
            waits.append(max(1, int((not_before - now).total_seconds())))
    for auto_continue in active_auto_continues(queue):
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
                pending_prompt = truncate(recovery.get("prompt") or RECOVERY_PROMPT, 80)
                if args.once:
                    print(f"Recovery in attesa. Prossimo '{pending_prompt}' tra circa {wait_seconds}s.")
                    return RATE_LIMIT_EXIT
                print(f"Recovery in attesa: prossimo '{pending_prompt}' tra circa {wait_seconds}s.")
                time.sleep(min(wait_seconds, args.poll_seconds))
                continue

            if recovery.get("provider") == PROVIDER_CODEX:
                if codex_exe is None:
                    recovery["blocked"] = True
                    recovery["last_error"] = "CLI Codex non trovato durante il recupero."
                    save_queue(paths.queue_file, queue)
                    return 1
                try:
                    prepare_codex_recovery_state(paths, codex_exe, queue, recovery)
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    error = public_error_message(exc, "Preparazione recovery Codex non riuscita.")
                    recovery["blocked"] = True
                    recovery["last_error"] = error
                    save_queue(paths.queue_file, queue)
                    print(f"Recovery Codex bloccata: {error}")
                    return 1

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
                print_agent_dry_run_details(paths, claude_exe, codex_exe, item, args.ide, item["prompt"])
                return 0

            print(f"Recovery: invio '{truncate(item['prompt'], 80)}' a chat {item['session_id'][:8]}")
            recovery["attempts"] = int(recovery.get("attempts", 0)) + 1
            run_started_at = dt.datetime.now(UTC) - dt.timedelta(seconds=5)
            try:
                result = run_agent(paths, claude_exe, codex_exe, item, args.timeout, args.ide)
            except subprocess.TimeoutExpired:
                recovery["blocked"] = True
                recovery["last_error"] = f"Timeout recovery dopo {args.timeout}s"
                save_queue(paths.queue_file, queue)
                return 1
            except (OSError, SystemExit) as exc:
                recovery["blocked"] = True
                recovery["last_error"] = public_error_message(exc, "Recovery non riuscita.")
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
                print(f"Recovery completata con '{truncate(item['prompt'], 80)}'. Log: {item.get('last_log') or recovery.get('last_log')}")
                if args.once:
                    return 0
                continue

            if result.rate_limited:
                recovery["last_error"] = public_error_message(
                    result.stderr or result.stdout,
                    "Recovery non confermata dal provider.",
                )
                recovery["not_before"] = retry_time_after_limit(result, args.poll_seconds)
                if recovery.get("provider") == PROVIDER_CODEX:
                    recovery["retry_needs_rollback"] = True
                save_queue(paths.queue_file, queue)
                print(f"Session limit ancora attivo. Prossimo '{truncate(item['prompt'], 80)}' dopo: {recovery['not_before']}")
                if args.once:
                    return RATE_LIMIT_EXIT
                time.sleep(args.poll_seconds)
                continue

            recovery["blocked"] = True
            recovery["last_error"] = public_error_message(
                result.stderr or result.stdout,
                "Recovery non riuscita.",
            )
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
                action_label = auto_continue.get("recovery_prompt_preview") or (
                    "Try again" if auto_continue.get("action") == "claude_try_again" else RECOVERY_PROMPT
                )
                if args.once:
                    print(f"Auto-continua in attesa. Prossima azione '{truncate(str(action_label), 80)}' tra circa {wait_seconds}s.")
                    return RATE_LIMIT_EXIT
                print(f"Auto-continua in attesa: prossima azione '{truncate(str(action_label), 80)}' tra circa {wait_seconds}s.")
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
                if args.once:
                    return 1
                continue

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
                if args.once:
                    return CONFIG_CHANGED_EXIT
                continue

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
                if args.once:
                    return 1
                continue

            if (
                auto_continue.get("monitor_limit", True)
                and auto_continue.get("status") in {"armed", "monitoring"}
                and not claude_item_is_desktop(item)
            ):
                reset_at = latest_rate_limit_reset_from_chat(chat)
                if reset_at is None:
                    next_check = (
                        dt.datetime.now(UTC) + dt.timedelta(seconds=args.poll_seconds)
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

            if item.get("provider") == PROVIDER_CODEX:
                if codex_exe is None:
                    update_auto_continue_state(
                        auto_continue,
                        "blocked",
                        enabled=False,
                        last_error="CLI Codex non trovato durante auto-continua.",
                    )
                    save_queue(paths.queue_file, queue)
                    if args.once:
                        return 1
                    continue
                try:
                    prepare_codex_recovery_state(paths, codex_exe, queue, auto_continue)
                except AutoContinueCancelled as exc:
                    print(str(exc))
                    if args.once:
                        return 0
                    continue
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    error = public_error_message(exc, "Preparazione auto-continua Codex non riuscita.")
                    update_auto_continue_state(
                        auto_continue,
                        "blocked",
                        enabled=False,
                        last_error=error,
                    )
                    save_queue(paths.queue_file, queue)
                    print(f"Auto-continua Codex bloccato: {error}")
                    if args.once:
                        return 1
                    continue
                item = auto_continue_as_item(auto_continue)
            elif claude_item_is_desktop(item):
                update_auto_continue_state(
                    auto_continue,
                    auto_continue.get("status"),
                    action="claude_try_again",
                    prompt="Try again",
                    recovery_prompt_preview="Try again",
                    recovery_followup_count=0,
                )
                item = auto_continue_as_item(auto_continue)
                save_queue(paths.queue_file, queue)

            if args.dry_run:
                print("DRY RUN AUTO-CONTINUA")
                if item.get("allow_cwd_fallback"):
                    print(f"cwd fallback: {item.get('cwd_fallback')}")
                if claude_item_is_desktop(item):
                    print(f"provider: {PROVIDER_CLAUDE}")
                    print(f"azione: Try again nativo sulla sessione {item['session_id']}")
                else:
                    print_agent_dry_run_details(paths, claude_exe, codex_exe, item, args.ide, item["prompt"])
                return 0

            action_preview = auto_continue.get("recovery_prompt_preview") or truncate(item["prompt"], 80)
            print(f"Auto-continua: eseguo '{action_preview}' sulla chat {item['session_id'][:8]}")
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
            if not auto_continue.get("enabled"):
                print("Auto-continua disattivato prima dell'invio: nessuna azione eseguita.")
                if args.once:
                    return 0
                continue
            run_started_at = dt.datetime.now(UTC) - dt.timedelta(seconds=5)
            try:
                if claude_item_is_desktop(item):
                    result = run_claude_desktop_try_again(
                        paths,
                        item,
                        timeout=min(args.timeout, 20),
                        scan_seconds=12,
                    )
                else:
                    result = run_agent(paths, claude_exe, codex_exe, item, args.timeout, args.ide)
            except subprocess.TimeoutExpired:
                update_auto_continue_state(
                    auto_continue,
                    "failed",
                    enabled=False,
                    last_error=f"Timeout auto-continua dopo {args.timeout}s",
                )
                save_queue(paths.queue_file, queue)
                if args.once:
                    return 1
                continue
            except (OSError, SystemExit) as exc:
                update_auto_continue_state(
                    auto_continue,
                    "failed",
                    enabled=False,
                    last_error=public_error_message(exc, "Auto-continua non riuscita."),
                )
                save_queue(paths.queue_file, queue)
                if args.once:
                    return 1
                continue

            auto_continue["last_log"] = write_run_log(paths, item, result)
            if result.returncode == 0:
                missing_prompt_error = (
                    None
                    if claude_item_is_desktop(item)
                    else prompt_missing_after_success(paths, item, run_started_at)
                )
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
                    if args.once:
                        return 1
                    continue
                combined_output = f"{result.stdout}\n{result.stderr}"
                if is_permission_wait_text(combined_output):
                    update_auto_continue_state(
                        auto_continue,
                        "blocked_permission",
                        enabled=False,
                        last_error=public_error_message(
                            combined_output,
                            "Auto-continua non confermata dal provider.",
                        ),
                        sending_started_at=None,
                        next_check_in_seconds=None,
                    )
                    save_queue(paths.queue_file, queue)
                    print(f"Auto-continua bloccato: il provider richiede approvazione. Log: {auto_continue.get('last_log')}")
                    if args.once:
                        return 1
                    continue
                if claude_item_is_desktop(item):
                    update_auto_continue_state(
                        auto_continue,
                        "monitoring",
                        enabled=True,
                        actions_completed=int(auto_continue.get("actions_completed", 0)) + 1,
                        last_action_at=now_utc(),
                        last_observation="try_again_invoked",
                        last_error=None,
                        not_before=(
                            dt.datetime.now(UTC) + dt.timedelta(seconds=args.poll_seconds)
                        ).replace(microsecond=0).isoformat(),
                        sending_started_at=None,
                        next_check_in_seconds=args.poll_seconds,
                    )
                    save_queue(paths.queue_file, queue)
                    print(f"Try again eseguito; monitoraggio ancora attivo. Log: {auto_continue.get('last_log')}")
                    if args.once:
                        return 0
                    continue
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

            if claude_item_is_desktop(item) and result.returncode == CLAUDE_DESKTOP_TRY_AGAIN_EXIT:
                already_recovered = int(auto_continue.get("actions_completed", 0)) > 0
                update_auto_continue_state(
                    auto_continue,
                    "monitoring" if already_recovered else "waiting_retry",
                    enabled=True,
                    last_error=None
                    if already_recovered
                    else (
                        "La chat e' stata aperta, ma il pulsante Try again non e' ancora visibile. "
                        "Non ho inviato alcun messaggio; riprovero' automaticamente."
                    ),
                    last_observation="try_again_not_visible",
                    not_before=(dt.datetime.now(UTC) + dt.timedelta(seconds=args.poll_seconds)).replace(microsecond=0).isoformat(),
                    sending_started_at=None,
                    next_check_in_seconds=args.poll_seconds,
                    last_check_at=now_utc(),
                )
                save_queue(paths.queue_file, queue)
                print("Try again non visibile: nessun messaggio inviato, nuovo tentativo programmato.")
                if args.once:
                    return RATE_LIMIT_EXIT
                continue

            if result.rate_limited:
                update_auto_continue_state(
                    auto_continue,
                    "waiting_limit",
                    last_error=public_error_message(
                        result.stderr or result.stdout,
                        "Auto-continua non confermata dal provider.",
                    ),
                    not_before=retry_time_after_limit(result, args.poll_seconds),
                    sending_started_at=None,
                    next_check_in_seconds=None,
                    last_check_at=now_utc(),
                    retry_needs_rollback=item.get("provider") == PROVIDER_CODEX,
                )
                save_queue(paths.queue_file, queue)
                print(f"Session limit ancora attivo. Auto-continua dopo: {auto_continue['not_before']}")
                if args.once:
                    return RATE_LIMIT_EXIT
                continue

            update_auto_continue_state(
                auto_continue,
                "failed",
                enabled=False,
                last_error=public_error_message(
                    result.stderr or result.stdout,
                    "Auto-continua non riuscita.",
                ),
                sending_started_at=None,
            )
            save_queue(paths.queue_file, queue)
            print(f"Auto-continua fallito. Log: {auto_continue.get('last_log')}")
            if args.once:
                return result.returncode or 1
            continue

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
        run_started_at = dt.datetime.now(UTC) - dt.timedelta(seconds=5)
        try:
            result = run_agent(paths, claude_exe, codex_exe, item, args.timeout, args.ide)
        except subprocess.TimeoutExpired:
            item["status"] = STATUS_FAILED
            item["last_error"] = f"Timeout dopo {args.timeout}s"
            save_queue(paths.queue_file, queue)
            return 1
        except (OSError, SystemExit) as exc:
            item["status"] = STATUS_FAILED
            item["last_error"] = public_error_message(exc, "Invio non riuscito.")
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
                "Session limit rilevato. Prima della coda determinero' il recupero corretto "
                f"dopo: {queue['recovery']['not_before']}"
            )
            if args.once:
                return RATE_LIMIT_EXIT
            time.sleep(args.poll_seconds)
            continue

        item["status"] = STATUS_FAILED
        item["last_error"] = public_error_message(
            result.stderr or result.stdout,
            "Invio non riuscito.",
        )
        save_queue(paths.queue_file, queue)
        print(f"Errore provider su {item['id']}. Log: {item['last_log']}")
        return result.returncode or 1


def add_common_options(parser: argparse.ArgumentParser, suppress_default: bool = False) -> None:
    default: Any = argparse.SUPPRESS if suppress_default else None
    parser.add_argument("--windows-home", default=default, help="Home Windows da usare, es. C:\\Users\\me")
    parser.add_argument(
        "--state-dir",
        default=default,
        help="Cartella stato/coda. Default: <windows-home>/.claude-codex-queue; riusa automaticamente il percorso legacy",
    )
    parser.add_argument("--claude", default=default, help="Percorso esplicito a claude/claude.exe")
    parser.add_argument("--codex", default=default, help="Percorso esplicito al CLI codex")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-codex-queue",
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
