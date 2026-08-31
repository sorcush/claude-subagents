#!/usr/bin/env python3
"""Hermes profile-aware adapter for Cursor probe and delegate routing."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

MAX_RESULT_BYTES = 2 * 1024 * 1024
STREAM_MAX_BYTES = 67108864
TERM_GRACE_SECONDS = 5
BASH_VERSION_TIMEOUT_SECONDS = 3
STRUCTURED_COMMANDS = frozenset({"probe", "worktree", "state", "code", "review"})
RUN_ID_RE = re.compile(r"^[a-f0-9]{16}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
GIT_OBJECT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
PROBE_FIELDS = frozenset({"status", "role", "key", "reason", "diagnostic"})
PROBE_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "USER",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_NUMERIC",
    "LC_TIME",
    "LC_MONETARY",
    "LC_COLLATE",
)
TRUSTED_BASH_PATHS = (
    Path("/opt/homebrew/bin/bash"),
    Path("/usr/local/bin/bash"),
    Path("/usr/bin/bash"),
    Path("/bin/bash"),
)
PLUGIN_SETTINGS_PREFIX = "plugins.entries.claude-subagents.settings"
STATE_SCHEMA_VERSION = 1
PRUNE_AGE = timedelta(days=30)
FAILURE_MAX_BYTES = 8 * 1024
MAX_STATE_BYTES = 1024 * 1024
REVIEW_TIMEOUT_SECONDS = 1800
CODE_TIMEOUT_SECONDS = 7800
MAX_CURSOR_CALLS = 9
MAX_CONTROLLER_REVIEW_ROUNDS = 5
MAX_ACTIVE_WORKER_SECONDS = 5 * 60 * 60
OBJECT_FORMAT_WIDTHS = {"sha1": 40, "sha256": 64}
CODER_BUDGET_FIELDS = frozenset(
    {"cursor_calls", "controller_review_rounds", "active_worker_seconds"}
)
PROTECTED_MANIFEST_FIELDS = frozenset(
    {"git_control_sha256", "controller_source_sha256"}
)
DISPATCH_BASELINE_FIELDS = frozenset(
    {
        "pre_dispatch_work_branch_commit",
        "pre_dispatch_feature_branch_commit",
        "verified_worker_tree",
        "verified_worker_index",
    }
)
REVIEW_SNAPSHOT_OUTCOMES = frozenset({"unchanged", "changed", "failed"})
REVIEW_LENSES = frozenset({"backend", "frontend", "ui"})
REVIEW_FIELDS = frozenset(
    {
        "status",
        "reviewer",
        "session_id",
        "target",
        "lenses",
        "report",
        "diagnostic",
    }
)
REVIEW_OUTPUT_FIELDS = frozenset(
    {
        "status",
        "run_id",
        "generation",
        "reviewer",
        "session_id",
        "target",
        "lenses",
        "report",
        "diagnostic",
        "snapshot_path",
    }
)
CODE_FIELDS = frozenset(
    {
        "status",
        "coder",
        "session_id",
        "attempts",
        "verified",
        "changed",
        "commit_id",
        "result",
        "verify_output",
    }
)
CODE_OUTPUT_FIELDS = frozenset(
    CODE_FIELDS | {"run_id", "generation", "diagnostic"}
)
WORKTREE_PREPARE_FIELDS = frozenset(
    {
        "status",
        "run_id",
        "generation",
        "worktree",
        "work_branch",
        "feature_branch",
        "copied",
        "skipped",
        "neutralized_symlinks",
        "purged_secrets",
        "clone_supported",
        "diagnostic",
    }
)
WORKTREE_REMOVE_FIELDS = frozenset(
    {"status", "run_id", "work_branch", "unmerged", "generation", "diagnostic"}
)
STATE_SHOW_FIELDS = frozenset({"status", "run_id", "generation", "record", "diagnostic"})
STATE_MUTATION_FIELDS = frozenset(
    {"status", "run_id", "state", "generation", "diagnostic"}
)
PRUNE_FIELDS = frozenset({"status", "removed", "diagnostic"})
CODER_TRANSITIONS: dict[str, set[str]] = {
    "preparing": {"prepared", "blocked"},
    "prepared": {"dispatching", "blocked"},
    "dispatching": {"reviewing", "blocked"},
    "reviewing": {"dispatching", "integration_pending", "blocked"},
    "integration_pending": {"integrated", "blocked"},
    "integrated": {"dispatching", "blocked"},
    "blocked": {"dispatching"},
}
REVIEWER_TRANSITIONS: dict[str, set[str]] = {
    "dispatching": {"reviewed", "blocked"},
    "reviewed": {"dispatching", "blocked"},
    "blocked": {"dispatching"},
}

_active_probe_cleanup: list[tuple[Path | None, Path]] = []
_active_probe_process: subprocess.Popen[Any] | None = None
_active_review_process: subprocess.Popen[str] | None = None
_active_review_cleanup: list[Path] = []
_active_external_processes: list[subprocess.Popen[Any]] = []
_interrupted = False
_interrupt_signum: int | None = None


class DispatchError(RuntimeError):
    """Raised when adapter preconditions are not met."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def probe_script() -> Path:
    return repo_root() / "scripts" / "probe.sh"


def default_hermes_bin() -> str:
    return os.environ.get("HERMES_BIN") or shutil.which("hermes") or "hermes"


def role_key(role: str) -> str:
    if role == "coder":
        return "cursor-coder"
    if role == "reviewer":
        return "cursor-reviewer"
    raise DispatchError(f"invalid role: {role}")


def _bash_major_version(path: Path, *, timeout_seconds: float | None = None) -> int:
    timeout = BASH_VERSION_TIMEOUT_SECONDS
    if timeout_seconds is not None:
        timeout = min(timeout, timeout_seconds)
    try:
        returncode, stdout, _stderr = _run_external(
            [str(path), "--version"],
            timeout_seconds=timeout,
            label="Bash version probe",
        )
    except DispatchError as exc:
        raise DispatchError(
            f"unable to execute bash candidate {path.name}"
        ) from exc

    if returncode != 0:
        raise DispatchError(f"unable to determine bash version for {path.name}")
    match = re.search(r"version (\d+)", stdout)
    if not match:
        raise DispatchError(f"unable to parse bash version for {path.name}")
    return int(match.group(1))


def _is_repo_local(candidate: Path, repo: Path) -> bool:
    repo_resolved = repo.resolve()

    for path in (candidate, *candidate.parents):
        try:
            path.relative_to(repo)
            return True
        except ValueError:
            pass

    try:
        candidate.resolve().relative_to(repo_resolved)
        return True
    except ValueError:
        return False


def _collect_bash_candidates(path_value: str) -> list[Path]:
    seen: set[Path] = set()
    candidates: list[Path] = []

    def add(candidate: Path) -> None:
        if not candidate.is_file():
            return
        resolved = candidate.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(candidate)

    for trusted in TRUSTED_BASH_PATHS:
        add(trusted)

    for entry in path_value.split(os.pathsep):
        if not entry or entry.startswith("."):
            continue
        path = Path(entry)
        if not path.is_absolute():
            continue
        bash = (path / "bash") if path.is_dir() else path
        if bash.name != "bash":
            continue
        add(bash)

    return candidates


def find_bash5(
    repo: Path,
    path_value: str,
    *,
    deadline: float | None = None,
) -> Path:
    for candidate in _collect_bash_candidates(path_value):
        remaining = (
            _remaining_review_seconds(deadline) if deadline is not None else None
        )
        if _is_repo_local(candidate, repo):
            continue
        if not os.access(candidate, os.X_OK):
            continue
        try:
            major = _bash_major_version(candidate, timeout_seconds=remaining)
        except DispatchError:
            continue
        if major >= 5:
            return candidate.resolve()

    raise RuntimeError(
        "Bash 5 or newer is required. Install Homebrew bash and ensure "
        "/opt/homebrew/bin is on PATH."
    )


def _require_hermes_home(env: dict[str, str]) -> str:
    home = env.get("HERMES_HOME", "")
    if not home or not home.strip():
        raise DispatchError("HERMES_HOME is required")
    path = Path(home)
    if not path.is_absolute() or ".." in path.parts:
        raise DispatchError("HERMES_HOME must be an absolute path")
    return str(path)


def read_model(
    role: str,
    hermes_bin: str,
    env: dict[str, str],
    *,
    timeout_seconds: float | None = None,
) -> str:
    _require_hermes_home(env)
    if role not in {"coder", "reviewer"}:
        raise DispatchError(f"invalid role: {role}")

    key = f"{PLUGIN_SETTINGS_PREFIX}.{role}_model"
    # SECURITY-REVIEW: hermes_bin is a controller-resolved executable, never user text.
    returncode, stdout, _stderr = _run_external(
        [hermes_bin, "config", "get", key],
        env=env,
        timeout_seconds=timeout_seconds,
        label="Hermes configuration lookup",
    )
    if returncode != 0:
        raise DispatchError(f"missing configuration for {role}_model")
    model = stdout.strip()
    if not model:
        raise DispatchError(f"blank configuration for {role}_model")
    if not MODEL_RE.fullmatch(model):
        raise DispatchError(f"invalid model identifier for {role}_model")
    return model


def validate_run_id(value: str) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise DispatchError("run id must match ^[a-f0-9]{16}$")
    return value


def validate_cursor_sandbox(value: str) -> str:
    if value != "enabled":
        raise DispatchError("invalid cursor sandbox mode")
    return value


def validate_verification_command(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DispatchError("non-empty verification command is required")
    return value


def write_pool(role: str, model: str, directory: Path) -> Path:
    if role not in {"coder", "reviewer"}:
        raise DispatchError(f"invalid role: {role}")
    if not MODEL_RE.fullmatch(model):
        raise DispatchError("invalid model identifier")

    key = role_key(role)
    top_key = "coders" if role == "coder" else "reviewers"
    payload = {
        top_key: [
            {
                "key": key,
                "label": "Cursor Coder" if role == "coder" else "Cursor Reviewer",
                "harness": "cursor",
                "model": model,
                "default": True,
            }
        ]
    }

    previous = os.umask(0o077)
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f"pool-{role}-",
            suffix=".json",
            dir=directory,
            delete=False,
        )
        try:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
    finally:
        os.umask(previous)

    return Path(handle.name)


def create_verification_home() -> Path:
    """Create a private verification home and return its physical path."""
    path = Path(tempfile.mkdtemp(prefix="claude-subagents-verify-")).resolve()
    os.chmod(path, 0o700)
    return path


def add_envelope_fields(
    payload: dict[str, Any],
    run_id: str,
    generation: int | None,
) -> dict[str, Any]:
    out = dict(payload)
    out["run_id"] = validate_run_id(run_id)
    if generation is not None:
        out["generation"] = generation
    return out


def build_probe_env(
    *,
    role: str,
    pool_path: Path,
    run_id: str,
    source_env: dict[str, str],
) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in PROBE_ENV_ALLOWLIST:
        value = source_env.get(key)
        if value:
            env[key] = value
    if "TMPDIR" not in env:
        env["TMPDIR"] = tempfile.gettempdir()

    env_key = "CSC_CODERS_JSON" if role == "coder" else "CSC_REVIEWERS_JSON"
    env[env_key] = str(pool_path)
    env["CSC_STREAM_MAX_BYTES"] = str(STREAM_MAX_BYTES)
    env["CSC_RUN_ID"] = run_id
    return env


def blocked_probe_result(
    diagnostic: str,
    *,
    role: str,
    key: str,
    run_id: str,
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "run_id": run_id,
        "role": role,
        "key": key,
        "reason": "",
        "diagnostic": _redact_failure(diagnostic),
    }


def validate_probe_payload(
    payload: dict[str, Any],
    *,
    role: str,
    key: str,
    exit_code: int,
) -> dict[str, Any]:
    if set(payload.keys()) != PROBE_FIELDS:
        raise DispatchError("probe returned unexpected fields")

    status = payload["status"]
    if not isinstance(status, str):
        raise DispatchError("probe status must be a string")
    if status not in {"READY", "FAILED"}:
        raise DispatchError("probe returned invalid status")
    if payload["role"] != role:
        raise DispatchError("probe role mismatch")
    if payload["key"] != key:
        raise DispatchError("probe key mismatch")
    if not isinstance(payload["reason"], str):
        raise DispatchError("probe reason must be a string")
    if not isinstance(payload["diagnostic"], str):
        raise DispatchError("probe diagnostic must be a string")

    if status == "READY" and exit_code != 0:
        raise DispatchError("probe status conflicts with exit code")
    if status == "FAILED" and exit_code != 1:
        raise DispatchError("probe status conflicts with exit code")

    return payload


def _parse_json_stdout(stdout: str, run_id: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise DispatchError("probe returned no output")
    size = len(text.encode("utf-8"))
    if size > MAX_RESULT_BYTES:
        raise DispatchError(f"result exceeds {MAX_RESULT_BYTES} bytes")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise DispatchError("probe stdout must contain exactly one JSON object")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise DispatchError("probe returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise DispatchError("probe returned non-object JSON")
    return payload


def _cleanup_probe_artifacts(pool_path: Path | None, pool_dir: Path) -> None:
    try:
        if pool_path is not None:
            pool_path.unlink(missing_ok=True)
        if pool_dir.exists():
            shutil.rmtree(pool_dir)
    except OSError as exc:
        raise DispatchError("unable to remove probe temporary artifacts") from exc


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while _process_group_exists(pgid) and time.monotonic() < deadline:
        try:
            process.wait(timeout=0.02)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.02)
    if _process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    kill_deadline = time.monotonic() + TERM_GRACE_SECONDS
    while _process_group_exists(pgid) and time.monotonic() < kill_deadline:
        try:
            process.wait(timeout=0.02)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.02)
    try:
        process.wait(timeout=0.1)
    except subprocess.TimeoutExpired:
        pass


def _run_external(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float | None,
    label: str,
) -> tuple[int, str, str]:
    timeout = None
    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            raise DispatchError(f"{label} timed out")
        timeout = max(0.001, float(timeout_seconds))
    # SECURITY-REVIEW: callers pass fixed argv elements and validated paths; no shell.
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise DispatchError(f"unable to launch {label}") from exc
    _active_external_processes.append(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        process.communicate()
        raise DispatchError(f"{label} timed out") from exc
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        if process in _active_external_processes:
            _active_external_processes.remove(process)
    return process.returncode, stdout, stderr


def _handle_lifecycle_signal(signum: int, _frame: Any) -> None:
    global _interrupted, _interrupt_signum
    _interrupted = True
    _interrupt_signum = signum
    for process in list(_active_external_processes):
        _terminate_process_group(process)
    if _active_probe_process is not None:
        _terminate_process_group(_active_probe_process)
    if _active_review_process is not None:
        _terminate_process_group(_active_review_process)
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def lifecycle_signal_handlers() -> Iterator[None]:
    previous: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, _handle_lifecycle_signal)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _handle_review_signal(signum: int, _frame: Any) -> None:
    global _interrupted, _interrupt_signum
    _interrupted = True
    _interrupt_signum = signum
    if _active_review_process is not None and _active_review_process.poll() is None:
        _terminate_process_group(_active_review_process)
    raise SystemExit(128 + signum)


def _handle_probe_signal(signum: int, _frame: Any) -> None:
    global _interrupted, _interrupt_signum
    _interrupted = True
    _interrupt_signum = signum
    if _active_probe_process is not None and _active_probe_process.poll() is None:
        _terminate_process_group(_active_probe_process)
    for pool_path, pool_dir in _active_probe_cleanup:
        try:
            _cleanup_probe_artifacts(pool_path, pool_dir)
        except DispatchError:
            pass
    raise SystemExit(128 + signum)


def run_probe(
    *,
    role: str,
    run_id: str,
    hermes_bin: str,
    env: dict[str, str],
    bash_path: Path,
) -> tuple[int, dict[str, Any]]:
    validate_run_id(run_id)
    if role not in {"coder", "reviewer"}:
        raise DispatchError(f"invalid role: {role}")

    key = role_key(role)
    pool_dir: Path | None = None
    pool_path: Path | None = None
    process: subprocess.Popen[str] | None = None
    previous_handlers: dict[int, Any] = {}
    global _active_probe_process, _interrupted, _interrupt_signum
    _interrupted = False
    _interrupt_signum = None

    previous_handlers[signal.SIGINT] = signal.signal(
        signal.SIGINT, _handle_probe_signal
    )
    previous_handlers[signal.SIGTERM] = signal.signal(
        signal.SIGTERM, _handle_probe_signal
    )

    try:
        model = read_model(role, hermes_bin, env)
        pool_dir = Path(tempfile.mkdtemp(prefix="claude-subagents-pool-"))
        _active_probe_cleanup.append((None, pool_dir))
        pool_path = write_pool(role, model, pool_dir)
        _active_probe_cleanup[-1] = (pool_path, pool_dir)
        probe_env = build_probe_env(
            role=role,
            pool_path=pool_path,
            run_id=run_id,
            source_env=env,
        )

        process = subprocess.Popen(
            [
                str(bash_path),
                str(probe_script()),
                "--role",
                role,
                "--key",
                key,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=probe_env,
            cwd=str(repo_root()),
            start_new_session=True,
        )
        _active_probe_process = process
        stdout, stderr = process.communicate()
        if _interrupted and _interrupt_signum is not None:
            raise SystemExit(128 + _interrupt_signum)
        exit_code = process.returncode

        if exit_code not in {0, 1}:
            return 2, blocked_probe_result(
                stderr.strip() or "probe failed",
                role=role,
                key=key,
                run_id=run_id,
            )

        try:
            payload = _parse_json_stdout(stdout, run_id)
            payload = validate_probe_payload(
                payload,
                role=role,
                key=key,
                exit_code=exit_code,
            )
            payload["diagnostic"] = _redact_failure(payload["diagnostic"])
        except DispatchError as exc:
            return 1, blocked_probe_result(
                str(exc),
                role=role,
                key=key,
                run_id=run_id,
            )

        payload = add_envelope_fields(payload, run_id, None)
        return exit_code, payload
    finally:
        if process is not None and process.poll() is None:
            _terminate_process_group(process)
        _active_probe_process = None
        if pool_dir is not None:
            _cleanup_probe_artifacts(pool_path, pool_dir)
            if _active_probe_cleanup and _active_probe_cleanup[-1][1] == pool_dir:
                _active_probe_cleanup.pop()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def review_delegate_script() -> Path:
    return repo_root() / "scripts" / "review-delegate.sh"


def code_delegate_script() -> Path:
    return repo_root() / "scripts" / "code-delegate.sh"


def worktree_script() -> Path:
    return repo_root() / "scripts" / "worktree.sh"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_timestamp(value: datetime | None = None) -> str:
    current = value or _utc_now()
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise ValueError("timestamp must be RFC3339")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _redact_failure(
    value: str,
    *,
    session_ids: list[str] | tuple[str, ...] = (),
) -> str:
    text = str(value).strip()
    configured_secrets = {
        env_value
        for env_name, env_value in os.environ.items()
        if re.search(
            r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization)",
            env_name,
        )
        and isinstance(env_value, str)
        and len(env_value) >= 4
    }
    for configured_secret in sorted(configured_secrets, key=len, reverse=True):
        text = text.replace(configured_secret, "[REDACTED_CONFIGURED_SECRET]")
    for session_id in session_ids:
        if isinstance(session_id, str) and session_id:
            text = text.replace(session_id, "[REDACTED_SESSION_ID]")
    text = re.sub(
        r"(?i)\b(authorization)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
        r"\1: [REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b([a-z0-9_.-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"token|password|passwd|secret|credential)[a-z0-9_.-]*)"
        r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1=[REDACTED]",
        text,
    )
    if len(text.encode("utf-8")) > FAILURE_MAX_BYTES:
        encoded = text.encode("utf-8")[:FAILURE_MAX_BYTES]
        text = encoded.decode("utf-8", errors="ignore")
    return text


def state_dir(hermes_home: Path) -> Path:
    # SECURITY-REVIEW: HERMES_HOME is external profile input; callers require it
    # to be an absolute controller-owned directory before state operations.
    directory = Path(hermes_home) / "claude-subagents" / "runs"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def review_artifact_dir(hermes_home: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    # SECURITY-REVIEW: HERMES_HOME controls a profile-scoped filesystem path.
    root = Path(hermes_home) / "claude-subagents" / "review-artifacts" / run_id
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root.parent.parent, 0o700)
    os.chmod(root.parent, 0o700)
    os.chmod(root, 0o700)
    return root


def _state_path(hermes_home: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    return state_dir(hermes_home) / f"{run_id}.json"


def _lock_path(hermes_home: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    return state_dir(hermes_home) / f"{run_id}.lock"


@contextlib.contextmanager
def lock_run(hermes_home: Path, run_id: str) -> Iterator[None]:
    validate_run_id(run_id)
    directory = state_dir(hermes_home)
    lock_path = directory / f"{run_id}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    previous = os.umask(0o077)
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            parent_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            os.replace(tmp_path, path)
            os.chmod(path, 0o600)
            parent_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if tmp_path is not None and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    finally:
        os.umask(previous)


def _transitions_for_role(role: str) -> dict[str, set[str]]:
    if role == "coder":
        return CODER_TRANSITIONS
    if role == "reviewer":
        return REVIEWER_TRANSITIONS
    raise DispatchError(f"invalid role: {role}")


def _validate_rfc3339_timestamp(value: str) -> None:
    try:
        _parse_iso_timestamp(value)
    except ValueError as exc:
        raise DispatchError("invalid updated_at timestamp") from exc


def _validate_absolute_path(value: str, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise DispatchError(f"invalid {label}")
    path = Path(value)
    if not path.is_absolute():
        raise DispatchError(f"{label} must be absolute")
    return path


def _validate_optional_git_object(
    value: Any,
    *,
    label: str,
    object_width: int,
) -> None:
    if value is not None and (
        not isinstance(value, str)
        or not re.fullmatch(rf"[0-9a-f]{{{object_width}}}", value)
    ):
        raise DispatchError(f"invalid object format width for {label}")


def _validate_coder_payload(
    coder: Any,
    *,
    run_id: str,
    state: str,
    object_width: int,
) -> dict[str, Any]:
    if not isinstance(coder, dict):
        raise DispatchError("malformed coder state")
    required = {
        "feature_branch",
        "worktree",
        "work_branch",
        "pending_commit",
        "last_integrated_commit",
        "unintegrated_commits",
        "dispatch_baseline",
        "budget",
        "protected_manifest",
    }
    if set(coder.keys()) != required:
        raise DispatchError("malformed coder state")
    def valid_branch(value: Any) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and not value.startswith("-")
            and not value.endswith(("/", ".", ".lock"))
            and ".." not in value
            and "//" not in value
            and "@{" not in value
            and "\\" not in value
            and not any(character.isspace() or ord(character) < 32 for character in value)
        )

    if not valid_branch(coder["feature_branch"]):
        raise DispatchError("invalid feature branch")
    worktree = _validate_absolute_path(coder["worktree"], label="worktree")
    if not valid_branch(coder["work_branch"]):
        raise DispatchError("invalid work branch")
    embedded = re.compile(r"-hermes-([a-f0-9]{16})-work$")
    branch_match = embedded.search(coder["work_branch"])
    path_match = embedded.search(worktree.name)
    if (
        branch_match is None
        or path_match is None
        or branch_match.group(1) != run_id
        or path_match.group(1) != run_id
    ):
        raise DispatchError("coder worktree and branch run id mismatch")
    if coder["pending_commit"] is not None and (
        not isinstance(coder["pending_commit"], str)
        or not re.fullmatch(rf"[0-9a-f]{{{object_width}}}", coder["pending_commit"])
    ):
        raise DispatchError("invalid pending commit")
    if coder["last_integrated_commit"] is not None and (
        not isinstance(coder["last_integrated_commit"], str)
        or not re.fullmatch(
            rf"[0-9a-f]{{{object_width}}}", coder["last_integrated_commit"]
        )
    ):
        raise DispatchError("invalid last integrated commit")
    commits = coder["unintegrated_commits"]
    if not isinstance(commits, list) or not all(
        isinstance(item, str)
        and re.fullmatch(rf"[0-9a-f]{{{object_width}}}", item)
        for item in commits
    ):
        raise DispatchError("invalid unintegrated commits")
    baseline = coder["dispatch_baseline"]
    if not isinstance(baseline, dict):
        raise DispatchError("invalid dispatch baseline")
    if set(baseline) != DISPATCH_BASELINE_FIELDS:
        raise DispatchError("invalid dispatch baseline fields")
    for key in DISPATCH_BASELINE_FIELDS:
        _validate_optional_git_object(
            baseline[key],
            label=key,
            object_width=(64 if key == "verified_worker_index" else object_width),
        )
    if baseline["verified_worker_index"] is not None and not re.fullmatch(
        r"[0-9a-f]{64}", str(baseline["verified_worker_index"])
    ):
        raise DispatchError("invalid dispatch baseline verified worker index")
    object_values = [
        baseline[key]
        for key in (
            "pre_dispatch_work_branch_commit",
            "pre_dispatch_feature_branch_commit",
            "verified_worker_tree",
        )
        if baseline[key] is not None
    ]
    if object_values and len({len(str(value)) for value in object_values}) != 1:
        raise DispatchError("mixed Git object formats in dispatch baseline")
    budget = coder["budget"]
    if not isinstance(budget, dict) or set(budget) != CODER_BUDGET_FIELDS:
        raise DispatchError("invalid coder budget")
    for key, maximum in (
        ("cursor_calls", MAX_CURSOR_CALLS),
        ("controller_review_rounds", MAX_CONTROLLER_REVIEW_ROUNDS),
    ):
        value = budget[key]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > maximum
        ):
            raise DispatchError(f"invalid coder budget {key}")
    active_seconds = budget["active_worker_seconds"]
    if (
        not isinstance(active_seconds, (int, float))
        or isinstance(active_seconds, bool)
        or active_seconds < 0
        or active_seconds > MAX_ACTIVE_WORKER_SECONDS
    ):
        raise DispatchError("invalid coder active worker seconds")
    protected = coder["protected_manifest"]
    if protected is not None:
        if not isinstance(protected, dict) or set(protected) != PROTECTED_MANIFEST_FIELDS:
            raise DispatchError("invalid protected manifest")
        if not all(
            isinstance(protected[key], str)
            and re.fullmatch(r"[0-9a-f]{64}", protected[key])
            for key in PROTECTED_MANIFEST_FIELDS
        ):
            raise DispatchError("invalid protected manifest digest")
    if state in {"dispatching", "reviewing", "integration_pending", "integrated"}:
        if baseline["pre_dispatch_work_branch_commit"] is None:
            raise DispatchError("dispatch baseline is missing work branch commit")
        if baseline["pre_dispatch_feature_branch_commit"] is None:
            raise DispatchError("dispatch baseline is missing feature branch commit")
    if (baseline["verified_worker_tree"] is None) != (
        baseline["verified_worker_index"] is None
    ):
        raise DispatchError("dispatch baseline worker identity is incomplete")
    return coder


def _validate_relative_document(value: Any, *, label: str, optional: bool) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise DispatchError(f"invalid reviewer {label}")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or str(path) in {".", ""}:
        raise DispatchError(f"invalid reviewer {label}")
    return value


def _validate_reviewer_payload(
    reviewer: Any,
    *,
    run_id: str,
) -> dict[str, Any]:
    if not isinstance(reviewer, dict):
        raise DispatchError("malformed reviewer state")
    required = {
        "target",
        "document",
        "specification",
        "lenses",
        "snapshot_outcome",
        "snapshot_path",
    }
    if set(reviewer.keys()) != required:
        raise DispatchError("malformed reviewer state")
    if reviewer["target"] not in {"spec", "plan"}:
        raise DispatchError("invalid reviewer target")
    _validate_relative_document(reviewer["document"], label="document", optional=False)
    _validate_relative_document(
        reviewer["specification"], label="specification", optional=True
    )
    if reviewer["target"] == "spec" and reviewer["specification"] is not None:
        raise DispatchError("spec review must not include a specification")
    lenses = reviewer["lenses"]
    if (
        not isinstance(lenses, list)
        or not all(isinstance(item, str) and item in REVIEW_LENSES for item in lenses)
        or len(lenses) != len(set(lenses))
    ):
        raise DispatchError("invalid reviewer lenses")
    if reviewer["target"] == "plan" and lenses:
        raise DispatchError("plan review must not include lenses")
    outcome = reviewer["snapshot_outcome"]
    if outcome is not None and outcome not in REVIEW_SNAPSHOT_OUTCOMES:
        raise DispatchError("invalid reviewer snapshot outcome")
    snapshot_path = reviewer.get("snapshot_path")
    if snapshot_path is not None:
        _validate_absolute_path(snapshot_path, label="snapshot path")
    if snapshot_path is not None and outcome not in {"changed", "failed"}:
        raise DispatchError("snapshot path requires changed or failed outcome")
    if outcome == "changed" and snapshot_path is None:
        raise DispatchError("changed snapshot requires snapshot path")
    if snapshot_path is not None and run_id not in Path(snapshot_path).parts:
        raise DispatchError("snapshot path run id mismatch")
    return reviewer


def _validate_snapshot_containment(
    hermes_home: Path,
    run_id: str,
    reviewer: dict[str, Any],
) -> None:
    snapshot_path = reviewer.get("snapshot_path")
    if snapshot_path is None:
        return
    snapshot = Path(snapshot_path).resolve()
    artifact_root = (
        Path(hermes_home) / "claude-subagents" / "review-artifacts" / run_id
    ).resolve()
    try:
        snapshot.relative_to(artifact_root)
    except ValueError as exc:
        raise DispatchError("snapshot path is outside review artifact directory") from exc


def _validate_state_record(record: dict[str, Any], *, run_id: str | None = None) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise DispatchError("malformed state record")
    required = {
        "schema_version",
        "git_object_format",
        "run_id",
        "role",
        "state",
        "generation",
        "repository",
        "cursor_session_id",
        "coder",
        "reviewer",
        "failure",
        "updated_at",
    }
    if set(record.keys()) != required:
        raise DispatchError("malformed state record")
    if (
        not isinstance(record["schema_version"], int)
        or isinstance(record["schema_version"], bool)
        or record["schema_version"] != STATE_SCHEMA_VERSION
    ):
        raise DispatchError("unsupported state schema version")
    object_format = record["git_object_format"]
    if object_format not in OBJECT_FORMAT_WIDTHS:
        raise DispatchError("invalid repository object format")
    object_width = OBJECT_FORMAT_WIDTHS[object_format]
    embedded_run_id = validate_run_id(record["run_id"])
    if run_id is not None and record["run_id"] != run_id:
        raise DispatchError("run id mismatch")
    role = record["role"]
    if role not in {"coder", "reviewer"}:
        raise DispatchError("invalid state role")
    state = record["state"]
    transitions = _transitions_for_role(role)
    if state not in transitions and state != "complete":
        raise DispatchError("invalid state value")
    generation = record["generation"]
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise DispatchError("invalid state generation")
    _validate_absolute_path(record["repository"], label="repository")
    session = record["cursor_session_id"]
    if session is not None and (not isinstance(session, str) or not session):
        raise DispatchError("invalid cursor session id")
    if role == "coder":
        if record["reviewer"] is not None:
            raise DispatchError("coder state must not include reviewer payload")
        if record["coder"] is None:
            raise DispatchError("missing coder state")
        _validate_coder_payload(
            record["coder"],
            run_id=embedded_run_id,
            state=state,
            object_width=object_width,
        )
        coder_payload = record["coder"]
        if state in {"reviewing", "integration_pending", "integrated"}:
            if not isinstance(session, str) or not session:
                raise DispatchError("coder review lifecycle requires session id")
            baseline = coder_payload["dispatch_baseline"]
            if (
                baseline["verified_worker_tree"] is None
                or baseline["verified_worker_index"] is None
            ):
                raise DispatchError("coder review lifecycle requires verified identity")
        if state == "reviewing" and not coder_payload["unintegrated_commits"]:
            raise DispatchError("reviewing coder state requires unintegrated commit")
        if state == "integration_pending":
            pending = coder_payload["pending_commit"]
            if pending is None or pending not in coder_payload["unintegrated_commits"]:
                raise DispatchError(
                    "integration pending state requires pending unintegrated commit"
                )
        if state == "integrated":
            integrated = coder_payload["last_integrated_commit"]
            if integrated is None or coder_payload["pending_commit"] is not None:
                raise DispatchError("integrated state requires integrated commit")
            if integrated in coder_payload["unintegrated_commits"]:
                raise DispatchError("integrated commit remains unintegrated")
    if role == "reviewer":
        if record["coder"] is not None:
            raise DispatchError("reviewer state must not include coder payload")
        if record["reviewer"] is None:
            raise DispatchError("missing reviewer state")
        _validate_reviewer_payload(record["reviewer"], run_id=embedded_run_id)
        if state in {"reviewed", "complete"}:
            if not isinstance(session, str) or not session:
                raise DispatchError("reviewed reviewer state requires session id")
            if record["reviewer"]["snapshot_outcome"] not in REVIEW_SNAPSHOT_OUTCOMES:
                raise DispatchError("reviewed reviewer state requires snapshot outcome")
    failure = record["failure"]
    if failure is not None and not isinstance(failure, str):
        raise DispatchError("invalid failure value")
    if state == "blocked" and (not isinstance(failure, str) or not failure):
        raise DispatchError("blocked state requires failure")
    if not isinstance(record["updated_at"], str):
        raise DispatchError("invalid updated_at timestamp")
    _validate_rfc3339_timestamp(record["updated_at"])
    return record


def load_state(hermes_home: Path, run_id: str) -> dict[str, Any]:
    validate_run_id(run_id)
    path = _state_path(hermes_home, run_id)
    if not path.is_file():
        raise DispatchError("missing run state")
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            raise DispatchError("state record exceeds size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except DispatchError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError("malformed state record") from exc
    if not isinstance(payload, dict):
        raise DispatchError("malformed state record")
    record = _validate_state_record(payload, run_id=run_id)
    if record["role"] == "reviewer" and isinstance(record.get("reviewer"), dict):
        _validate_snapshot_containment(hermes_home, run_id, record["reviewer"])
    return record


def _sanitize_state_record(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    session_ids = [
        value
        for value in (payload.get("cursor_session_id"),)
        if isinstance(value, str) and value
    ]
    if payload.get("failure") is not None:
        payload["failure"] = _redact_failure(
            str(payload["failure"]),
            session_ids=session_ids,
        )
    return payload


def _validate_state_for_home(
    hermes_home: Path,
    record: dict[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    validated = _validate_state_record(record, run_id=run_id)
    reviewer = validated.get("reviewer")
    if validated["role"] == "reviewer" and isinstance(reviewer, dict):
        _validate_snapshot_containment(hermes_home, run_id, reviewer)
    return validated


def create_state(hermes_home: Path, record: dict[str, Any]) -> dict[str, Any]:
    run_id = validate_run_id(record["run_id"])
    path = _state_path(hermes_home, run_id)
    if path.exists():
        raise DispatchError("run state already exists")
    payload = _sanitize_state_record(record)
    payload.setdefault("schema_version", STATE_SCHEMA_VERSION)
    payload.setdefault("generation", 1)
    payload.setdefault("updated_at", _iso_timestamp())
    payload = _validate_state_for_home(hermes_home, payload, run_id=run_id)
    with lock_run(hermes_home, run_id):
        if path.exists():
            raise DispatchError("run state already exists")
        atomic_write_json(path, payload)
    return payload


def transition_state(
    hermes_home: Path,
    run_id: str,
    expected_generation: int,
    next_state: str,
    changes: dict[str, Any],
) -> dict[str, Any]:
    validate_run_id(run_id)
    if (
        not isinstance(expected_generation, int)
        or isinstance(expected_generation, bool)
        or expected_generation < 1
    ):
        raise DispatchError("invalid expected generation")
    with lock_run(hermes_home, run_id):
        return _transition_state_unlocked(
            hermes_home,
            run_id,
            expected_generation,
            next_state,
            changes,
        )


def _transition_state_unlocked(
    hermes_home: Path,
    run_id: str,
    expected_generation: int,
    next_state: str,
    changes: dict[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(expected_generation, int)
        or isinstance(expected_generation, bool)
        or expected_generation < 1
    ):
        raise DispatchError("invalid expected generation")
    current = load_state(hermes_home, run_id)
    if current["generation"] != expected_generation:
        raise DispatchError("stale generation")
    role = current["role"]
    allowed = _transitions_for_role(role).get(current["state"], set())
    if next_state not in allowed:
        raise DispatchError(
            f"invalid state transition: {current['state']} -> {next_state}"
        )
    updated = dict(current)
    if role == "coder" and current["state"] == "integrated" and next_state == "dispatching":
        coder = dict(current["coder"])
        coder["budget"] = {
            "cursor_calls": 0,
            "controller_review_rounds": 0,
            "active_worker_seconds": 0.0,
        }
        coder["protected_manifest"] = None
        coder["dispatch_baseline"] = {
            "pre_dispatch_work_branch_commit": None,
            "pre_dispatch_feature_branch_commit": None,
            "verified_worker_tree": None,
            "verified_worker_index": None,
        }
        updated["coder"] = coder
        updated["cursor_session_id"] = None
        updated["failure"] = None
    updated.update(changes)
    if role == "coder" and current["state"] == "integrated" and next_state == "dispatching":
        coder = dict(updated["coder"])
        coder["budget"] = {
            "cursor_calls": 0,
            "controller_review_rounds": 0,
            "active_worker_seconds": 0.0,
        }
        updated["coder"] = coder
        updated["cursor_session_id"] = None
    updated["state"] = next_state
    updated["generation"] = expected_generation + 1
    updated["updated_at"] = _iso_timestamp()
    if next_state == "dispatching" and role == "coder":
        coder = updated.get("coder")
        if isinstance(coder, dict):
            repository = Path(str(updated["repository"]))
            baseline = _capture_dispatch_baseline(
                repository,
                str(coder["work_branch"]),
                str(coder["feature_branch"]),
            )
            updated["coder"] = {**coder, "dispatch_baseline": baseline}
    updated = _sanitize_state_record(updated)
    updated = _validate_state_for_home(hermes_home, updated, run_id=run_id)
    atomic_write_json(_state_path(hermes_home, run_id), updated)
    return updated


def _fsync_directory(path: Path) -> None:
    dir_fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def finalize_complete_state(
    hermes_home: Path,
    run_id: str,
    expected_generation: int,
) -> dict[str, Any]:
    validate_run_id(run_id)
    with lock_run(hermes_home, run_id):
        current = load_state(hermes_home, run_id)
        if current["generation"] != expected_generation:
            raise DispatchError("stale generation")
        if current["state"] == "complete":
            if current["role"] == "coder":
                coder = current["coder"]
                repository = Path(str(current["repository"]))
                if Path(str(coder["worktree"])).exists() or _branch_exists(
                    repository, str(coder["work_branch"])
                ):
                    raise DispatchError("complete coder still has worktree or work branch")
            return current
        if current["role"] == "reviewer":
            if current["state"] != "reviewed":
                raise DispatchError("cannot complete an active reviewer run")
        else:
            coder = current["coder"]
            repository = Path(str(current["repository"]))
            if Path(str(coder["worktree"])).exists():
                raise DispatchError("coder worktree still exists")
            if _branch_exists(repository, str(coder["work_branch"])):
                raise DispatchError("coder work branch still exists")
        record = _persist_complete_unlocked(
            hermes_home, run_id, current["generation"]
        )
        if record["state"] != "complete":
            raise DispatchError("state is not complete")
    return record


def _advance_to_complete_unlocked(
    hermes_home: Path,
    run_id: str,
    generation: int,
    current_state: str,
) -> dict[str, Any]:
    if current_state == "complete":
        return load_state(hermes_home, run_id)
    current = load_state(hermes_home, run_id)
    if current["role"] != "coder":
        if current_state != "reviewed":
            raise DispatchError("cannot complete an active reviewer run")
    else:
        coder = current["coder"]
        repository = Path(str(current["repository"]))
        if Path(str(coder["worktree"])).exists() or _branch_exists(
            repository, str(coder["work_branch"])
        ):
            raise DispatchError("coder worktree or work branch still exists")
    return _persist_complete_unlocked(hermes_home, run_id, generation)


def _persist_complete_unlocked(
    hermes_home: Path,
    run_id: str,
    generation: int,
) -> dict[str, Any]:
    current = load_state(hermes_home, run_id)
    if current["generation"] != generation:
        raise DispatchError("stale generation")
    updated = dict(current)
    updated["state"] = "complete"
    updated["generation"] = generation + 1
    updated["updated_at"] = _iso_timestamp()
    updated = _sanitize_state_record(updated)
    updated = _validate_state_for_home(hermes_home, updated, run_id=run_id)
    atomic_write_json(_state_path(hermes_home, run_id), updated)
    return updated


def delete_state(hermes_home: Path, run_id: str) -> None:
    del hermes_home
    validate_run_id(run_id)
    raise DispatchError("complete state is retained until prune")


def prune_state(hermes_home: Path, now: datetime) -> list[str]:
    directory = state_dir(hermes_home)
    removed: list[str] = []
    for path in sorted(directory.glob("*.json")):
        run_id = path.stem
        if not RUN_ID_RE.fullmatch(run_id):
            continue
        with lock_run(hermes_home, run_id):
            if not path.exists():
                continue
            try:
                record = load_state(hermes_home, run_id)
            except DispatchError:
                continue
            updated_at = _parse_iso_timestamp(str(record["updated_at"]))
            if now - updated_at < PRUNE_AGE:
                continue
            if record["role"] == "coder" and isinstance(record.get("coder"), dict):
                worktree = record["coder"].get("worktree")
                if isinstance(worktree, str) and Path(worktree).exists():
                    continue
            path.unlink()
            _fsync_directory(path.parent)
            removed.append(run_id)
    return removed


def _new_coder_record(
    *,
    run_id: str,
    repository: str,
    feature_branch: str,
    worktree: str,
    work_branch: str,
    git_object_format: str,
) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "git_object_format": git_object_format,
        "run_id": run_id,
        "role": "coder",
        "state": "preparing",
        "generation": 1,
        "repository": repository,
        "cursor_session_id": None,
        "coder": {
            "feature_branch": feature_branch,
            "worktree": worktree,
            "work_branch": work_branch,
            "pending_commit": None,
            "last_integrated_commit": None,
            "unintegrated_commits": [],
            "dispatch_baseline": {
                "pre_dispatch_work_branch_commit": None,
                "pre_dispatch_feature_branch_commit": None,
                "verified_worker_tree": None,
                "verified_worker_index": None,
            },
            "budget": {
                "cursor_calls": 0,
                "controller_review_rounds": 0,
                "active_worker_seconds": 0.0,
            },
            "protected_manifest": None,
        },
        "reviewer": None,
        "failure": None,
        "updated_at": _iso_timestamp(),
    }


def _new_reviewer_record(
    *,
    run_id: str,
    repository: str,
    target: str,
    document: str,
    specification: str | None,
    lenses: list[str],
    git_object_format: str,
) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "git_object_format": git_object_format,
        "run_id": run_id,
        "role": "reviewer",
        "state": "dispatching",
        "generation": 1,
        "repository": repository,
        "cursor_session_id": None,
        "coder": None,
        "reviewer": {
            "target": target,
            "document": document,
            "specification": specification,
            "lenses": lenses,
            "snapshot_outcome": None,
            "snapshot_path": None,
        },
        "failure": None,
        "updated_at": _iso_timestamp(),
    }


def _parse_delegate_json(stdout: str, *, label: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise DispatchError(f"{label} returned no output")
    size = len(text.encode("utf-8"))
    if size > MAX_RESULT_BYTES:
        raise DispatchError(f"result exceeds {MAX_RESULT_BYTES} bytes")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise DispatchError(f"{label} stdout must contain exactly one JSON object")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise DispatchError(f"{label} returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise DispatchError(f"{label} returned non-object JSON")
    return payload


def _run_script_json(
    *,
    bash_path: Path,
    script: Path,
    args: list[str],
    env: dict[str, str],
    cwd: Path,
    label: str,
) -> tuple[int, dict[str, Any]]:
    returncode, stdout, stderr = _run_external(
        [str(bash_path), str(script), *args],
        env=env,
        cwd=cwd,
        timeout_seconds=None,
        label=label,
    )
    if returncode not in {0, 1}:
        raise DispatchError(stderr.strip() or f"{label} failed")
    payload = _parse_delegate_json(stdout, label=label)
    return returncode, payload


def _run_script_json_timed(
    *,
    bash_path: Path,
    script: Path,
    args: list[str],
    env: dict[str, str],
    cwd: Path,
    label: str,
    timeout_seconds: int | float,
    track_review: bool = False,
) -> tuple[int, dict[str, Any]]:
    global _active_review_process, _interrupted, _interrupt_signum
    timeout = max(0.001, float(timeout_seconds))
    # SECURITY-REVIEW: argv is fixed and paths are validated; no shell expansion.
    try:
        process = subprocess.Popen(
            [str(bash_path), str(script), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(cwd),
            start_new_session=True,
        )
    except OSError as exc:
        raise DispatchError(f"unable to launch {label}") from exc
    _active_external_processes.append(process)
    previous_handlers: dict[int, Any] = {}
    if track_review:
        _interrupted = False
        _interrupt_signum = None
        previous_handlers[signal.SIGINT] = signal.signal(
            signal.SIGINT, _handle_review_signal
        )
        previous_handlers[signal.SIGTERM] = signal.signal(
            signal.SIGTERM, _handle_review_signal
        )
        _active_review_process = process
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        process.communicate()
        raise DispatchError(f"{label} timed out after {timeout} seconds")
    finally:
        if process in _active_external_processes:
            _active_external_processes.remove(process)
        if track_review:
            _active_review_process = None
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    if track_review and _interrupted and _interrupt_signum is not None:
        raise SystemExit(128 + _interrupt_signum)
    if process.returncode not in {0, 1}:
        raise DispatchError(stderr.strip() or f"{label} failed")
    payload = _parse_delegate_json(stdout, label=label)
    return process.returncode, payload


def _git_common_dir(
    repository: Path,
    *,
    deadline: float | None = None,
) -> Path:
    returncode, stdout, _stderr = _run_external(
        ["git", "-C", str(repository), "rev-parse", "--git-common-dir"],
        timeout_seconds=(
            _remaining_review_seconds(deadline) if deadline is not None else None
        ),
        label="git common-directory inspection",
    )
    if returncode != 0:
        raise DispatchError("not inside a git repository")
    common = stdout.strip()
    path = Path(common)
    if not path.is_absolute():
        path = (repository / path).resolve()
    return path


def _git_head(repository: Path, *, deadline: float | None = None) -> str:
    returncode, stdout, _stderr = _run_external(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        timeout_seconds=(
            _remaining_review_seconds(deadline) if deadline is not None else None
        ),
        label="git HEAD inspection",
    )
    if returncode != 0:
        raise DispatchError("repository has no valid HEAD")
    return stdout.strip()


def _git_object_format(
    repository: Path,
    *,
    deadline: float | None = None,
) -> str:
    returncode, stdout, _stderr = _run_external(
        ["git", "-C", str(repository), "rev-parse", "--show-object-format"],
        timeout_seconds=(
            _remaining_review_seconds(deadline) if deadline is not None else None
        ),
        label="Git object format inspection",
    )
    value = stdout.strip()
    if returncode != 0 or value not in OBJECT_FORMAT_WIDTHS:
        raise DispatchError("unsupported repository object format")
    return value


def _resolve_branch_head(repository: Path, branch: str) -> str | None:
    returncode, stdout, _stderr = _run_external(
        ["git", "-C", str(repository), "rev-parse", "--verify", f"refs/heads/{branch}"],
        timeout_seconds=None,
        label="git branch inspection",
    )
    if returncode != 0:
        return None
    return stdout.strip()


def _capture_dispatch_baseline(
    repository: Path,
    work_branch: str,
    feature_branch: str,
) -> dict[str, str | None]:
    return {
        "pre_dispatch_work_branch_commit": _resolve_branch_head(
            repository, work_branch
        ),
        "pre_dispatch_feature_branch_commit": _resolve_branch_head(
            repository, feature_branch
        ),
        "verified_worker_tree": None,
        "verified_worker_index": None,
    }


def _git_index_identity(
    worktree: Path,
    *,
    deadline: float | None = None,
) -> str:
    """Return a stable digest for the exact linked-worktree index bytes."""
    # SECURITY-REVIEW: worktree comes from validated, persisted run state.
    returncode, stdout, _stderr = _run_external(
        ["git", "-C", str(worktree), "rev-parse", "--git-path", "index"],
        timeout_seconds=None,
        label="worktree index inspection",
    )
    if returncode != 0:
        raise DispatchError("unable to locate worktree index")
    index_path = Path(stdout.strip())
    if not index_path.is_absolute():
        index_path = (worktree / index_path).resolve()
    try:
        return _hash_file(index_path, deadline=deadline)
    except OSError as exc:
        raise DispatchError("unable to read worktree index") from exc


def _current_branch(repository: Path) -> str:
    returncode, stdout, _stderr = _run_external(
        ["git", "-C", str(repository), "rev-parse", "--abbrev-ref", "HEAD"],
        timeout_seconds=None,
        label="controller branch inspection",
    )
    if returncode != 0:
        raise DispatchError("unable to resolve controller branch")
    return stdout.strip()


def _validate_controller_feature_branch(repository: Path, feature_branch: str) -> None:
    current = _current_branch(repository)
    if current != feature_branch:
        raise DispatchError("controller checkout is not on the feature branch")


def _reject_symlink_components(candidate: Path, repository: Path) -> None:
    repo = repository.resolve()
    current = candidate
    while True:
        if current.exists() or current.is_symlink():
            if stat.S_ISLNK(current.lstat().st_mode):
                raise DispatchError("path must not contain symlinks")
        if current.resolve() == repo:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent


def _remaining_review_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DispatchError("review timed out")
    return remaining


def _validate_coder_worktree_identity(
    state: dict[str, Any],
    *,
    repository: Path,
    run_id: str,
    expected_generation: int,
) -> dict[str, Any]:
    if state["run_id"] != run_id:
        raise DispatchError("run id mismatch")
    if state["role"] != "coder":
        raise DispatchError("role mismatch")
    if state["generation"] != expected_generation:
        raise DispatchError("stale generation")
    if Path(str(state["repository"])).resolve() != repository.resolve():
        raise DispatchError("repository mismatch")
    if state["git_object_format"] != _git_object_format(repository):
        raise DispatchError("repository object format mismatch")
    coder = state.get("coder")
    if not isinstance(coder, dict):
        raise DispatchError("malformed coder state")
    for key in ("feature_branch", "work_branch", "worktree"):
        value = coder.get(key)
        if not isinstance(value, str) or not value:
            raise DispatchError(f"missing persisted {key}")
    return coder


def _resolve_repo_path(repository: Path, candidate: Path) -> Path:
    # SECURITY-REVIEW: candidate is user-controlled and must remain in repository.
    _reject_symlink_components(candidate, repository)
    if stat.S_ISLNK(candidate.lstat().st_mode):
        raise DispatchError("document path must not be a symlink")
    repo = repository.resolve()
    path = candidate.resolve()
    try:
        path.relative_to(repo)
    except ValueError as exc:
        raise DispatchError("document path is outside the repository") from exc
    return path


def _validate_review_document(path: Path) -> Path:
    if not path.exists():
        raise DispatchError("document path is missing")
    if stat.S_ISLNK(path.lstat().st_mode):
        raise DispatchError("document path must not be a symlink")
    if not path.is_file():
        raise DispatchError("document path must be a regular file")
    if not os.access(path, os.R_OK):
        raise DispatchError("document path is unreadable")
    return path


def _validate_clean_checkout(
    repository: Path,
    allowed: set[Path],
    *,
    deadline: float | None = None,
) -> None:
    returncode, stdout, _stderr = _run_external(
        ["git", "-C", str(repository), "status", "--porcelain"],
        timeout_seconds=(
            _remaining_review_seconds(deadline) if deadline is not None else None
        ),
        label="git checkout inspection",
    )
    if returncode != 0:
        raise DispatchError("unable to inspect repository status")
    repo = repository.resolve()
    for line in stdout.splitlines():
        if not line.strip():
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        candidate = (repo / entry).resolve()
        if candidate not in allowed:
            raise DispatchError("controller checkout is not clean")


def _hash_file(path: Path, *, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            if deadline is not None:
                _remaining_review_seconds(deadline)
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_bounded(
    source: Path,
    target: Path,
    *,
    deadline: float | None = None,
) -> None:
    with source.open("rb") as source_handle, target.open("wb") as target_handle:
        while True:
            if deadline is not None:
                _remaining_review_seconds(deadline)
            chunk = source_handle.read(1024 * 1024)
            if not chunk:
                break
            target_handle.write(chunk)
    shutil.copystat(source, target, follow_symlinks=False)
    if deadline is not None:
        _remaining_review_seconds(deadline)


def _manifest_entry(
    path: Path,
    root: Path,
    *,
    deadline: float | None = None,
) -> dict[str, str | int]:
    relative = str(path.relative_to(root))
    if path.is_symlink():
        return {
            "type": "symlink",
            "mode": stat.S_IMODE(path.lstat().st_mode),
            "target": os.readlink(path),
        }
    if path.is_dir():
        return {"type": "directory", "mode": stat.S_IMODE(path.lstat().st_mode)}
    if path.is_file():
        digest = _hash_file(path, deadline=deadline)
        return {
            "type": "file",
            "mode": stat.S_IMODE(path.lstat().st_mode),
            "sha256": digest,
        }
    return {"type": "other", "mode": stat.S_IMODE(path.lstat().st_mode)}


def source_manifest(
    root: Path,
    *,
    deadline: float | None = None,
) -> dict[str, dict[str, str | int]]:
    base = root.resolve()
    manifest: dict[str, dict[str, str | int]] = {}
    for current, dirnames, filenames in os.walk(base, followlinks=False):
        if deadline is not None:
            _remaining_review_seconds(deadline)
        current_path = Path(current)
        rel_current = current_path.relative_to(base)
        if current_path != base:
            manifest[str(rel_current)] = _manifest_entry(
                current_path, base, deadline=deadline
            )
        for name in list(dirnames):
            if name == ".git":
                dirnames.remove(name)
                continue
            child = current_path / name
            rel_child = str(child.relative_to(base))
            if rel_child not in manifest:
                manifest[rel_child] = _manifest_entry(
                    child, base, deadline=deadline
                )
            if child.is_symlink():
                dirnames.remove(name)
        for name in filenames:
            child = current_path / name
            manifest[str(child.relative_to(base))] = _manifest_entry(
                child, base, deadline=deadline
            )
    return manifest


def _rmtree_bounded(path: Path, *, deadline: float | None = None) -> None:
    if not path.exists() and not path.is_symlink():
        return
    # SECURITY-REVIEW: callers provide adapter-created temporary directories.
    for current, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
        if deadline is not None:
            _remaining_review_seconds(deadline)
        current_path = Path(current)
        for name in filenames:
            if deadline is not None:
                _remaining_review_seconds(deadline)
            (current_path / name).unlink()
        for name in dirnames:
            if deadline is not None:
                _remaining_review_seconds(deadline)
            child = current_path / name
            if child.is_symlink():
                child.unlink()
            else:
                child.rmdir()
    path.rmdir()


@dataclass
class ReviewClone:
    path: Path
    repository: Path
    before_manifest: dict[str, dict[str, str | int]]
    documents: list[Path]


def create_review_clone(
    repository: Path,
    run_id: str,
    documents: list[Path],
    *,
    artifact_root: Path | None = None,
    deadline: float | None = None,
) -> ReviewClone:
    # SECURITY-REVIEW: repository/documents are validated user paths; Git runs
    # without a shell in a fresh process group and the clone root is mode 0700.
    validate_run_id(run_id)
    repo = repository.resolve()
    if deadline is not None:
        _remaining_review_seconds(deadline)
    head = _git_head(repo, deadline=deadline)
    root = artifact_root or Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    if artifact_root is not None:
        os.chmod(root, 0o700)
    clone_root = Path(tempfile.mkdtemp(prefix="snapshot-", dir=str(root)))
    try:
        returncode, _stdout, _stderr = _run_external(
            [
                "git",
                "clone",
                "--no-local",
                "--no-hardlinks",
                "--no-checkout",
                str(repo),
                str(clone_root),
            ],
            timeout_seconds=(
                _remaining_review_seconds(deadline) if deadline is not None else None
            ),
            label="review clone",
        )
        if returncode != 0:
            raise DispatchError("unable to create review clone")
        checkout_code, _stdout, _stderr = _run_external(
            ["git", "-C", str(clone_root), "checkout", "--detach", head],
            timeout_seconds=(
                _remaining_review_seconds(deadline) if deadline is not None else None
            ),
            label="review clone checkout",
        )
        if checkout_code != 0:
            raise DispatchError("unable to initialize review clone")
        for document in documents:
            if deadline is not None:
                _remaining_review_seconds(deadline)
            relative = document.resolve().relative_to(repo)
            target = clone_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_file_bounded(document, target, deadline=deadline)
        before_manifest = source_manifest(clone_root, deadline=deadline)
        return ReviewClone(
            path=clone_root,
            repository=repo,
            before_manifest=before_manifest,
            documents=documents,
        )
    except BaseException as primary:
        try:
            if clone_root.exists():
                shutil.rmtree(clone_root)
        except OSError as exc:
            raise DispatchError(
                "unable to remove partial review clone"
            ) from exc
        raise


def cleanup_review_clone(
    clone: ReviewClone,
    changed: bool,
    *,
    deadline: float | None = None,
) -> str | None:
    if changed:
        return str(clone.path)
    if deadline is not None:
        _remaining_review_seconds(deadline)
    try:
        _rmtree_bounded(clone.path, deadline=deadline)
    except OSError as exc:
        raise DispatchError("unable to remove unchanged review clone") from exc
    if clone.path.exists():
        raise DispatchError("unable to remove unchanged review clone")
    if deadline is not None:
        _remaining_review_seconds(deadline)
    return None


def _clone_is_independent(
    controller: Path,
    clone: Path,
    *,
    deadline: float | None = None,
) -> bool:
    controller_common = _git_common_dir(controller, deadline=deadline)
    clone_common = _git_common_dir(clone, deadline=deadline)
    if controller_common == clone_common:
        return False
    controller_objects = (controller_common / "objects").resolve()
    clone_objects = (clone_common / "objects").resolve()
    if controller_objects == clone_objects:
        return False
    for rel in ("config", "hooks", "refs", "worktrees"):
        left = (controller_common / rel).resolve()
        right = (clone_common / rel).resolve()
        if left == right:
            return False
    for object_file in (clone_common / "objects").rglob("*"):
        if deadline is not None:
            _remaining_review_seconds(deadline)
        if (
            object_file.is_file()
            and not object_file.is_symlink()
            and object_file.stat().st_nlink != 1
        ):
            return False
    return True


def _validate_review_delegate_payload(
    payload: dict[str, Any],
    *,
    target: str,
    lenses: list[str],
    exit_code: int,
) -> dict[str, Any]:
    if set(payload.keys()) != REVIEW_FIELDS:
        raise DispatchError("review delegate returned unexpected fields")
    status = payload["status"]
    if not isinstance(status, str) or status not in {"REVIEWED", "BLOCKED"}:
        raise DispatchError("review delegate returned invalid status")
    reviewer = payload["reviewer"]
    if not isinstance(reviewer, str) or reviewer != role_key("reviewer"):
        raise DispatchError("review delegate reviewer mismatch")
    session_id = payload["session_id"]
    if not isinstance(session_id, str):
        raise DispatchError("review delegate session id must be a string")
    target_value = payload["target"]
    if not isinstance(target_value, str) or target_value != target:
        raise DispatchError("review delegate target mismatch")
    lenses_value = payload["lenses"]
    if not isinstance(lenses_value, list) or not all(
        isinstance(item, str) for item in lenses_value
    ):
        raise DispatchError("review delegate lenses must be a string list")
    if lenses_value != lenses:
        raise DispatchError("review delegate lenses mismatch")
    report = payload["report"]
    if not isinstance(report, str):
        raise DispatchError("review delegate report must be a string")
    diagnostic = payload["diagnostic"]
    if not isinstance(diagnostic, str):
        raise DispatchError("review delegate diagnostic must be a string")
    if status == "REVIEWED" and exit_code != 0:
        raise DispatchError("review delegate status conflicts with exit code")
    if status == "BLOCKED" and exit_code != 1:
        raise DispatchError("review delegate status conflicts with exit code")
    return payload


def _review_output(
    *,
    status: str,
    run_id: str,
    generation: int,
    session_id: str,
    target: str,
    lenses: list[str],
    report: str,
    diagnostic: str,
    snapshot_path: str,
) -> dict[str, Any]:
    payload = {
        "status": status,
        "run_id": run_id,
        "generation": generation,
        "reviewer": role_key("reviewer"),
        "session_id": session_id,
        "target": target,
        "lenses": lenses,
        "report": report,
        "diagnostic": diagnostic,
        "snapshot_path": snapshot_path,
    }
    if set(payload.keys()) != REVIEW_OUTPUT_FIELDS:
        raise DispatchError("review output schema mismatch")
    return payload


def _blocked_review(
    *,
    run_id: str,
    generation: int,
    target: str,
    lenses: list[str],
    diagnostic: str,
    snapshot_path: str = "",
    session_ids: list[str] | tuple[str, ...] = (),
    session_id: str = "",
) -> dict[str, Any]:
    return _review_output(
        status="BLOCKED",
        run_id=run_id,
        generation=generation,
        session_id=session_id,
        target=target,
        lenses=lenses,
        report="",
        diagnostic=_redact_failure(diagnostic, session_ids=session_ids),
        snapshot_path=snapshot_path,
    )


def _reviewer_state_payload(state: dict[str, Any]) -> dict[str, Any]:
    reviewer = state.get("reviewer")
    if not isinstance(reviewer, dict):
        raise DispatchError("malformed reviewer state")
    return reviewer


def _finalize_review_clone(
    clone: ReviewClone,
    before_manifest: dict[str, dict[str, str | int]],
    *,
    deadline: float | None = None,
) -> tuple[bool, str]:
    after_manifest = source_manifest(clone.path, deadline=deadline)
    changed = after_manifest != before_manifest
    snapshot_path = cleanup_review_clone(
        clone, changed, deadline=deadline
    ) or ""
    return changed, snapshot_path


def _capture_review_snapshot(
    clone: ReviewClone,
    *,
    deadline: float | None = None,
) -> tuple[str, str | None]:
    try:
        changed, snapshot_path = _finalize_review_clone(
            clone,
            clone.before_manifest,
            deadline=deadline,
        )
    except (DispatchError, OSError):
        return "failed", str(clone.path)
    if changed:
        return "changed", snapshot_path or str(clone.path)
    return "unchanged", None


def _block_review_state(
    hermes_home: Path,
    run_id: str,
    generation: int,
    *,
    failure: str,
    snapshot_outcome: str,
    snapshot_path: str | None,
    cursor_session_id: str | None = None,
) -> dict[str, Any]:
    state = load_state(hermes_home, run_id)
    reviewer = _reviewer_state_payload(state)
    session_ids = [
        value
        for value in (state.get("cursor_session_id"), cursor_session_id)
        if isinstance(value, str) and value
    ]
    changes = {
        "failure": _redact_failure(failure, session_ids=session_ids),
        "reviewer": {
            **reviewer,
            "snapshot_outcome": snapshot_outcome,
            "snapshot_path": snapshot_path,
        },
    }
    if isinstance(cursor_session_id, str) and cursor_session_id:
        changes["cursor_session_id"] = cursor_session_id
    if state["generation"] != generation:
        raise DispatchError("stale generation")
    if state["state"] == "blocked":
        return update_state_fields(
            hermes_home,
            run_id,
            generation,
            changes,
        )
    return transition_state(
        hermes_home,
        run_id,
        generation,
        "blocked",
        changes,
    )


def _validate_rereview_identity(
    state: dict[str, Any],
    *,
    run_id: str,
    repository: Path,
    target: str,
    document: str,
    specification: str | None,
    lenses: list[str],
    session: str,
    expected_generation: int,
) -> None:
    if state["run_id"] != run_id:
        raise DispatchError("run id mismatch")
    if state["role"] != "reviewer":
        raise DispatchError("role mismatch")
    if state["state"] not in {"reviewed", "blocked"}:
        raise DispatchError("invalid state for re-review")
    if state["generation"] != expected_generation:
        raise DispatchError("stale generation")
    if Path(str(state["repository"])).resolve() != repository.resolve():
        raise DispatchError("repository mismatch")
    if state.get("cursor_session_id") != session:
        raise DispatchError("session id mismatch")
    reviewer = _reviewer_state_payload(state)
    if reviewer.get("target") != target:
        raise DispatchError("target mismatch")
    if reviewer.get("document") != document:
        raise DispatchError("document mismatch")
    if reviewer.get("specification") != specification:
        raise DispatchError("specification mismatch")
    if reviewer.get("lenses") != lenses:
        raise DispatchError("lenses mismatch")


def run_review(args: argparse.Namespace) -> dict[str, Any]:
    review_deadline = time.monotonic() + REVIEW_TIMEOUT_SECONDS
    _remaining_review_seconds(review_deadline)
    target = args.target
    if target not in {"spec", "plan"}:
        raise DispatchError("review target must be spec or plan")
    run_id = validate_run_id(args.run_id)
    hermes_home = Path(_require_hermes_home(os.environ))
    repository = Path(args.repo).resolve() if args.repo else None
    doc_path = Path(args.doc_file)
    if repository is None:
        returncode, stdout, _stderr = _run_external(
            ["git", "-C", str(doc_path.parent), "rev-parse", "--show-toplevel"],
            timeout_seconds=_remaining_review_seconds(review_deadline),
            label="review repository discovery",
        )
        if returncode != 0:
            raise DispatchError("not inside a git repository")
        repository = Path(stdout.strip()).resolve()
    _remaining_review_seconds(review_deadline)
    _git_head(repository, deadline=review_deadline)
    doc_resolved = _validate_review_document(
        _resolve_repo_path(repository, doc_path)
    )
    spec_resolved: Path | None = None
    if args.spec_file:
        spec_resolved = _validate_review_document(
            _resolve_repo_path(repository, Path(args.spec_file))
        )
    allowed = {doc_resolved}
    if spec_resolved is not None:
        allowed.add(spec_resolved)
    _validate_clean_checkout(repository, allowed, deadline=review_deadline)
    _remaining_review_seconds(review_deadline)

    lenses: list[str] = []
    if args.lenses is not None and not isinstance(args.lenses, str):
        raise DispatchError("lenses must be a comma-separated string")
    if args.lenses:
        lenses = [part.strip() for part in args.lenses.split(",") if part.strip()]
        for lens in lenses:
            if lens not in {"backend", "frontend", "ui"}:
                raise DispatchError(f"unknown lens: {lens}")
    if target == "plan" and lenses:
        raise DispatchError("plan review must not include lenses")
    if target == "spec" and spec_resolved is not None:
        raise DispatchError("spec review must not include a specification")

    documents = [doc_resolved]
    if spec_resolved is not None:
        documents.append(spec_resolved)
    document_rel = str(doc_resolved.relative_to(repository))
    specification_rel = (
        str(spec_resolved.relative_to(repository)) if spec_resolved is not None else None
    )

    expected_generation = getattr(args, "expected_generation", None)
    raw_session = getattr(args, "session", None)
    if expected_generation is None:
        if raw_session is not None:
            raise DispatchError("initial review must not include session id")
        session: str | None = None
        is_resume = False
    else:
        if (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 1
        ):
            raise DispatchError("invalid expected generation")
        if not isinstance(raw_session, str) or not raw_session:
            raise DispatchError("session id is required for re-review")
        session = raw_session
        is_resume = True
    clone: ReviewClone | None = None
    pool_dir: Path | None = None
    pool_path: Path | None = None
    generation = 0
    try:
        if is_resume:
            assert session is not None
            state = load_state(hermes_home, run_id)
            if state["git_object_format"] != _git_object_format(
                repository, deadline=review_deadline
            ):
                raise DispatchError("repository object format mismatch")
            _validate_rereview_identity(
                state,
                run_id=run_id,
                repository=repository,
                target=target,
                document=document_rel,
                specification=specification_rel,
                lenses=lenses,
                session=session,
                expected_generation=expected_generation,
            )
            updated = transition_state(
                hermes_home,
                run_id,
                expected_generation,
                "dispatching",
                {"cursor_session_id": session, "failure": None},
            )
            generation = updated["generation"]
        else:
            create_state(
                hermes_home,
                _new_reviewer_record(
                    run_id=run_id,
                    repository=str(repository),
                    git_object_format=_git_object_format(
                        repository, deadline=review_deadline
                    ),
                    target=target,
                    document=document_rel,
                    specification=specification_rel,
                    lenses=lenses,
                ),
            )
            generation = 1

        _remaining_review_seconds(review_deadline)
        clone = create_review_clone(
            repository,
            run_id,
            documents,
            artifact_root=review_artifact_dir(hermes_home, run_id),
            deadline=review_deadline,
        )
        _active_review_cleanup.append(clone.path)
        if not _clone_is_independent(
            repository,
            clone.path,
            deadline=review_deadline,
        ):
            snapshot_outcome, persisted_snapshot = _capture_review_snapshot(
                clone, deadline=review_deadline
            )
            if clone.path in _active_review_cleanup:
                _active_review_cleanup.remove(clone.path)
            clone = None
            updated = _block_review_state(
                hermes_home,
                run_id,
                generation,
                failure="review clone is not independent",
                snapshot_outcome=snapshot_outcome,
                snapshot_path=persisted_snapshot,
            )
            return _blocked_review(
                run_id=run_id,
                generation=updated["generation"],
                target=target,
                lenses=lenses,
                diagnostic="review clone is not independent",
                snapshot_path=persisted_snapshot or "",
            )

        env = os.environ.copy()
        hermes_bin = default_hermes_bin()
        _remaining_review_seconds(review_deadline)
        bash_path = find_bash5(
            repo_root(), env.get("PATH", ""), deadline=review_deadline
        )
        model = read_model(
            "reviewer",
            hermes_bin,
            env,
            timeout_seconds=_remaining_review_seconds(review_deadline),
        )
        pool_dir = Path(tempfile.mkdtemp(prefix="claude-subagents-pool-"))
        pool_path = write_pool("reviewer", model, pool_dir)
        _remaining_review_seconds(review_deadline)
        delegate_env = build_probe_env(
            role="reviewer",
            pool_path=pool_path,
            run_id=run_id,
            source_env=env,
        )
        clone_doc = clone.path / doc_resolved.relative_to(repository)
        delegate_args = [
            "--reviewer",
            role_key("reviewer"),
            "--target",
            target,
            "--doc-file",
            str(clone_doc),
        ]
        if spec_resolved is not None:
            delegate_args.extend(
                [
                    "--spec-file",
                    str(clone.path / spec_resolved.relative_to(repository)),
                ]
            )
        if lenses:
            delegate_args.extend(["--lenses", ",".join(lenses)])
        if session:
            delegate_args.extend(["--session", session])

        _remaining_review_seconds(review_deadline)
        exit_code, delegate_payload = _run_script_json_timed(
            bash_path=bash_path,
            script=review_delegate_script(),
            args=delegate_args,
            env=delegate_env,
            cwd=repo_root(),
            label="review delegate",
            timeout_seconds=_remaining_review_seconds(review_deadline),
            track_review=True,
        )
        delegate_payload = _validate_review_delegate_payload(
            delegate_payload,
            target=target,
            lenses=lenses,
            exit_code=exit_code,
        )
        report = delegate_payload["report"]
        session_id = delegate_payload["session_id"]
        _remaining_review_seconds(review_deadline)

        changed, snapshot_path = _finalize_review_clone(
            clone,
            clone.before_manifest,
            deadline=review_deadline,
        )
        if clone.path in _active_review_cleanup:
            _active_review_cleanup.remove(clone.path)
        clone = None
        if changed:
            updated = _block_review_state(
                hermes_home,
                run_id,
                generation,
                failure="review snapshot mutated",
                snapshot_outcome="changed",
                snapshot_path=snapshot_path or None,
                cursor_session_id=(
                    session_id if isinstance(session_id, str) and session_id else None
                ),
            )
            return _blocked_review(
                run_id=run_id,
                generation=updated["generation"],
                target=target,
                lenses=lenses,
                diagnostic="review snapshot mutated",
                snapshot_path=snapshot_path,
            )

        if delegate_payload["status"] == "BLOCKED":
            diagnostic = delegate_payload["diagnostic"] or "review failed"
            known_sessions = [
                value
                for value in (session_id, session)
                if isinstance(value, str) and value
            ]
            diagnostic = _redact_failure(
                diagnostic,
                session_ids=known_sessions,
            )
            updated = _block_review_state(
                hermes_home,
                run_id,
                generation,
                failure=diagnostic,
                snapshot_outcome="unchanged",
                snapshot_path=None,
                cursor_session_id=session_id or None,
            )
            return _blocked_review(
                run_id=run_id,
                generation=updated["generation"],
                target=target,
                lenses=lenses,
                diagnostic=diagnostic,
                session_ids=known_sessions,
                session_id=session_id,
            )
        if (
            delegate_payload["status"] != "REVIEWED"
            or exit_code != 0
            or not isinstance(report, str)
            or not report.strip()
            or not isinstance(session_id, str)
            or not session_id.strip()
        ):
            if not isinstance(report, str) or not report.strip():
                diagnostic = "reviewer returned empty report"
            elif not isinstance(session_id, str) or not session_id.strip():
                diagnostic = "reviewer returned no session id"
            else:
                diagnostic = delegate_payload["diagnostic"] or "review failed"
            updated = _block_review_state(
                hermes_home,
                run_id,
                generation,
                failure=diagnostic,
                snapshot_outcome="unchanged",
                snapshot_path=None,
                cursor_session_id=(
                    session_id if isinstance(session_id, str) and session_id else None
                ),
            )
            return _blocked_review(
                run_id=run_id,
                generation=updated["generation"],
                target=target,
                lenses=lenses,
                diagnostic=diagnostic,
                session_id=(
                    session_id if isinstance(session_id, str) and session_id else ""
                ),
            )

        updated = transition_state(
            hermes_home,
            run_id,
            generation,
            "reviewed",
            {
                "cursor_session_id": session_id,
                "failure": None,
                "reviewer": {
                    **_reviewer_state_payload(load_state(hermes_home, run_id)),
                    "snapshot_outcome": "unchanged",
                    "snapshot_path": None,
                },
            },
        )
        return _review_output(
            status="REVIEWED",
            run_id=run_id,
            generation=updated["generation"],
            session_id=session_id,
            target=target,
            lenses=lenses,
            report=report,
            diagnostic="",
            snapshot_path="",
        )
    except (KeyboardInterrupt, SystemExit):
        outcome = "failed"
        snapshot_path: str | None = None
        if clone is not None:
            outcome, snapshot_path = _capture_review_snapshot(
                clone, deadline=review_deadline
            )
            if clone.path in _active_review_cleanup:
                _active_review_cleanup.remove(clone.path)
            clone = None
        if generation > 0 and _state_path(hermes_home, run_id).exists():
            try:
                current = load_state(hermes_home, run_id)
                _block_review_state(
                    hermes_home,
                    run_id,
                    current["generation"],
                    failure="review interrupted",
                    snapshot_outcome=outcome,
                    snapshot_path=snapshot_path,
                )
            except DispatchError:
                pass
        raise
    except (DispatchError, OSError, RuntimeError) as exc:
        snapshot_outcome = "failed"
        snapshot_path: str | None = None
        if clone is not None:
            snapshot_outcome, snapshot_path = _capture_review_snapshot(
                clone, deadline=review_deadline
            )
            if clone.path in _active_review_cleanup:
                _active_review_cleanup.remove(clone.path)
            clone = None
        if generation > 0 and _state_path(hermes_home, run_id).exists():
            try:
                current = load_state(hermes_home, run_id)
                updated = _block_review_state(
                    hermes_home,
                    run_id,
                    current["generation"],
                    failure=str(exc),
                    snapshot_outcome=snapshot_outcome,
                    snapshot_path=snapshot_path,
                )
                generation = updated["generation"]
            except DispatchError:
                current = load_state(hermes_home, run_id)
                generation = current["generation"]
            return _blocked_review(
                run_id=run_id,
                generation=generation,
                target=target,
                lenses=lenses,
                diagnostic=str(exc),
                snapshot_path=snapshot_path or "",
                session_ids=[
                    value
                    for value in (session,)
                    if isinstance(value, str) and value
                ],
            )
        raise
    finally:
        cleanup_error: DispatchError | None = None
        if clone is not None:
            try:
                _capture_review_snapshot(clone, deadline=review_deadline)
            except (DispatchError, OSError) as exc:
                cleanup_error = DispatchError(
                    "unable to finalize review snapshot cleanup"
                )
                cleanup_error.__cause__ = exc
            if clone.path in _active_review_cleanup:
                _active_review_cleanup.remove(clone.path)
        try:
            if pool_path is not None:
                pool_path.unlink(missing_ok=True)
            if pool_dir is not None and pool_dir.exists():
                shutil.rmtree(pool_dir)
        except OSError as exc:
            cleanup_error = DispatchError(
                "unable to remove review temporary artifacts"
            )
            cleanup_error.__cause__ = exc
        if cleanup_error is not None:
            try:
                if _state_path(hermes_home, run_id).exists():
                    current = load_state(hermes_home, run_id)
                    if current["state"] != "blocked":
                        _block_review_state(
                            hermes_home,
                            run_id,
                            current["generation"],
                            failure=str(cleanup_error),
                            snapshot_outcome="failed",
                            snapshot_path=(
                                str(clone.path)
                                if clone is not None and clone.path.exists()
                                else None
                            ),
                        )
            except DispatchError:
                pass
            if sys.exc_info()[0] is None:
                raise cleanup_error


def _git_text(
    repository: Path,
    arguments: list[str],
    *,
    label: str,
    deadline: float | None = None,
    allow_failure: bool = False,
) -> str:
    returncode, stdout, stderr = _run_external(
        ["git", "-C", str(repository), *arguments],
        timeout_seconds=(
            _remaining_review_seconds(deadline) if deadline is not None else None
        ),
        label=label,
    )
    if returncode != 0 and not allow_failure:
        raise DispatchError(_redact_failure(stderr or f"{label} failed"))
    return stdout


def _registered_worktrees(
    repository: Path,
    *,
    deadline: float | None = None,
) -> str:
    return _git_text(
        repository,
        ["worktree", "list", "--porcelain"],
        label="worktree registration inspection",
        deadline=deadline,
    )


def _effective_hooks_path(
    repository: Path,
    *,
    deadline: float | None = None,
) -> Path:
    output = _git_text(
        repository,
        ["rev-parse", "--path-format=absolute", "--git-path", "hooks"],
        label="effective Git hooks path inspection",
        deadline=deadline,
    ).strip()
    path = Path(output)
    if not path.is_absolute():
        path = (repository / path).resolve()
    return path.resolve()


def _worktree_is_registered(
    repository: Path,
    worktree: Path,
    branch: str,
    *,
    deadline: float | None = None,
) -> bool:
    expected_path = str(worktree.resolve())
    expected_branch = f"refs/heads/{branch}"
    current_path: str | None = None
    for line in _registered_worktrees(repository, deadline=deadline).splitlines():
        if line.startswith("worktree "):
            current_path = str(Path(line[9:]).resolve())
        elif line.startswith("branch ") and current_path == expected_path:
            return line[7:] == expected_branch
    return False


def controller_source_manifest(
    repository: Path,
    *,
    deadline: float | None = None,
) -> dict[str, dict[str, str | int]]:
    output = _git_text(
        repository,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        label="controller source listing",
        deadline=deadline,
    )
    result: dict[str, dict[str, str | int]] = {}
    root = repository.resolve()
    for relative in output.split("\0"):
        if not relative:
            continue
        if deadline is not None:
            _remaining_review_seconds(deadline)
        path = root / relative
        if not path.exists() and not path.is_symlink():
            result[relative] = {"type": "missing", "mode": 0}
            continue
        result[relative] = _manifest_entry(path, root, deadline=deadline)
    return result


def git_control_manifest(
    repository: Path,
    worktree: Path,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    common = _git_common_dir(repository, deadline=deadline)
    hooks = _effective_hooks_path(worktree, deadline=deadline)
    config = common / "config"
    config_worktree = common / "config.worktree"
    manifest: dict[str, Any] = {
        "common_dir": str(common.resolve()),
        "refs": _git_text(
            repository,
            ["for-each-ref", "--format=%(refname)%00%(objectname)"],
            label="Git ref inspection",
            deadline=deadline,
        ),
        "repository_config": _git_text(
            repository,
            ["config", "--local", "--show-origin", "--list"],
            label="repository configuration inspection",
            deadline=deadline,
        ),
        "worktree_config": _git_text(
            worktree,
            ["config", "--worktree", "--show-origin", "--list"],
            label="worktree configuration inspection",
            deadline=deadline,
            allow_failure=True,
        ),
        "worktrees": _registered_worktrees(repository, deadline=deadline),
        "controller_branch": _current_branch(repository),
        "controller_head": _git_head(repository, deadline=deadline),
        "work_branch": _current_branch(worktree),
        "work_head": _git_head(worktree, deadline=deadline),
        "effective_hooks_path": str(hooks),
        "hooks": source_manifest(hooks, deadline=deadline) if hooks.is_dir() else {},
        "config_file": (
            _manifest_entry(config, common, deadline=deadline)
            if config.exists()
            else None
        ),
        "config_worktree_file": (
            _manifest_entry(config_worktree, common, deadline=deadline)
            if config_worktree.exists()
            else None
        ),
    }
    return manifest


def validate_coder_workspace(
    repository: Path,
    worktree: Path,
    state: dict[str, Any],
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    repo = repository.resolve()
    cwd = worktree.resolve()
    coder = state.get("coder")
    if state.get("role") != "coder" or not isinstance(coder, dict):
        raise DispatchError("coder run state is required")
    if Path(str(state["repository"])).resolve() != repo:
        raise DispatchError("repository mismatch")
    if state["git_object_format"] != _git_object_format(repo, deadline=deadline):
        raise DispatchError("repository object format mismatch")
    if Path(str(coder["worktree"])).resolve() != cwd:
        raise DispatchError("worktree mismatch")
    if repo == cwd:
        raise DispatchError("controller checkout cannot be the coder worktree")
    if _git_common_dir(repo, deadline=deadline) != _git_common_dir(
        cwd, deadline=deadline
    ):
        raise DispatchError("coder worktree belongs to another repository")
    if not _worktree_is_registered(
        repo, cwd, str(coder["work_branch"]), deadline=deadline
    ):
        raise DispatchError("coder worktree is not registered on the expected branch")
    if _current_branch(repo) != coder["feature_branch"]:
        raise DispatchError("controller checkout is not on the persisted feature branch")
    if _current_branch(cwd) != coder["work_branch"]:
        raise DispatchError("coder worktree branch mismatch")
    status = _git_text(
        repo,
        ["status", "--porcelain", "--untracked-files=all"],
        label="controller checkout inspection",
        deadline=deadline,
    )
    if status.strip():
        raise DispatchError("controller checkout is not clean")
    return coder


def _validate_code_delegate_payload(
    payload: dict[str, Any],
    *,
    exit_code: int,
) -> dict[str, Any]:
    if set(payload) != CODE_FIELDS:
        raise DispatchError("code delegate returned unexpected fields")
    if payload["status"] not in {"DONE", "BLOCKED"}:
        raise DispatchError("code delegate returned invalid status")
    if payload["coder"] != role_key("coder"):
        raise DispatchError("code delegate coder mismatch")
    if not isinstance(payload["session_id"], str):
        raise DispatchError("code delegate session id must be a string")
    if (
        not isinstance(payload["attempts"], int)
        or isinstance(payload["attempts"], bool)
        or payload["attempts"] < 0
        or payload["attempts"] > 3
    ):
        raise DispatchError("code delegate attempts must be between zero and three")
    for field in ("verified", "changed"):
        if not isinstance(payload[field], bool):
            raise DispatchError(f"code delegate {field} must be a boolean")
    for field in ("commit_id", "result", "verify_output"):
        if not isinstance(payload[field], str):
            raise DispatchError(f"code delegate {field} must be a string")
    if payload["commit_id"]:
        raise DispatchError("external coder must not return a commit")
    if payload["status"] == "DONE" and exit_code != 0:
        raise DispatchError("code delegate status conflicts with exit code")
    if payload["status"] == "BLOCKED" and exit_code != 1:
        raise DispatchError("code delegate status conflicts with exit code")
    return payload


def _code_output(
    payload: dict[str, Any],
    *,
    run_id: str,
    generation: int,
    diagnostic: str,
) -> dict[str, Any]:
    output = {
        **payload,
        "run_id": run_id,
        "generation": generation,
        "diagnostic": diagnostic,
    }
    if set(output) != CODE_OUTPUT_FIELDS:
        raise DispatchError("code output schema mismatch")
    return output


def _blocked_code(
    *,
    run_id: str,
    generation: int,
    diagnostic: str,
    payload: dict[str, Any] | None = None,
    session_ids: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    base = payload or {
        "status": "BLOCKED",
        "coder": role_key("coder"),
        "session_id": "",
        "attempts": 0,
        "verified": False,
        "changed": False,
        "commit_id": "",
        "result": "",
        "verify_output": "",
    }
    base = dict(base)
    base["status"] = "BLOCKED"
    base["verified"] = False
    base["commit_id"] = ""
    return _code_output(
        base,
        run_id=run_id,
        generation=generation,
        diagnostic=_redact_failure(diagnostic, session_ids=session_ids),
    )


def _manifest_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def protected_manifest(
    repository: Path,
    worktree: Path,
    *,
    deadline: float | None = None,
) -> dict[str, str]:
    return {
        "git_control_sha256": _manifest_digest(
            git_control_manifest(repository, worktree, deadline=deadline)
        ),
        "controller_source_sha256": _manifest_digest(
            controller_source_manifest(repository, deadline=deadline)
        ),
    }


def validate_coder_budget(
    state: dict[str, Any],
    *,
    max_retries: int,
    correction: bool,
) -> None:
    coder = state.get("coder")
    if state.get("role") != "coder" or not isinstance(coder, dict):
        raise DispatchError("coder state is required")
    if correction:
        if max_retries != 0:
            raise DispatchError("correction dispatch requires max retries zero")
    elif max_retries < 0 or max_retries > 3:
        raise DispatchError("initial dispatch permits at most three retries")
    budget = coder["budget"]
    requested_calls = max_retries + 1
    if budget["cursor_calls"] + requested_calls > MAX_CURSOR_CALLS:
        raise DispatchError("Cursor call budget exceeded")
    if budget["controller_review_rounds"] >= MAX_CONTROLLER_REVIEW_ROUNDS:
        raise DispatchError("controller review round budget exceeded")
    if budget["active_worker_seconds"] >= MAX_ACTIVE_WORKER_SECONDS:
        raise DispatchError("active worker time budget exceeded")


def _update_coder_budget(
    hermes_home: Path,
    run_id: str,
    *,
    cursor_calls: int = 0,
    active_worker_seconds: float = 0.0,
) -> dict[str, Any]:
    with lock_run(hermes_home, run_id):
        state = load_state(hermes_home, run_id)
        coder = dict(state["coder"])
        budget = dict(coder["budget"])
        budget["cursor_calls"] += cursor_calls
        budget["active_worker_seconds"] = min(
            MAX_ACTIVE_WORKER_SECONDS,
            budget["active_worker_seconds"] + max(0.0, active_worker_seconds),
        )
        coder["budget"] = budget
        return _update_state_fields_unlocked(
            hermes_home,
            run_id,
            state["generation"],
            {"coder": coder},
        )


def reserve_controller_review_round(
    hermes_home: Path,
    run_id: str,
    expected_generation: int,
) -> dict[str, Any]:
    with lock_run(hermes_home, run_id):
        state = load_state(hermes_home, run_id)
        if state["generation"] != expected_generation:
            raise DispatchError("stale generation")
        coder = dict(state["coder"])
        budget = dict(coder["budget"])
        if budget["controller_review_rounds"] >= MAX_CONTROLLER_REVIEW_ROUNDS:
            raise DispatchError("controller review round budget exceeded")
        budget["controller_review_rounds"] += 1
        coder["budget"] = budget
        return _update_state_fields_unlocked(
            hermes_home,
            run_id,
            state["generation"],
            {"coder": coder},
        )


def run_coder(args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + CODE_TIMEOUT_SECONDS
    run_id = validate_run_id(args.run_id)
    hermes_home = Path(_require_hermes_home(os.environ))
    repository = Path(args.repo).resolve()
    worktree = Path(args.cwd).resolve()
    verify_command = validate_verification_command(args.verify_cmd)
    expected_generation = args.expected_generation
    if (
        not isinstance(expected_generation, int)
        or isinstance(expected_generation, bool)
        or expected_generation < 1
    ):
        raise DispatchError("invalid expected generation")
    if not isinstance(args.max_retries, int) or args.max_retries not in range(0, 4):
        raise DispatchError("max retries must be between 0 and 3")
    task_file = Path(args.task_file)
    if task_file.is_symlink() or not task_file.is_file() or not os.access(task_file, os.R_OK):
        raise DispatchError("task file must be a readable regular file")
    state = load_state(hermes_home, run_id)
    if state["generation"] != expected_generation:
        raise DispatchError("stale generation")
    coder = validate_coder_workspace(
        repository, worktree, state, deadline=deadline
    )
    prior_session = state.get("cursor_session_id")
    new_task = state["state"] == "integrated"
    previous_task_session = prior_session if new_task else None
    supplied_session = args.session
    if new_task and supplied_session is not None:
        raise DispatchError("new task must not reuse the prior task session")
    if supplied_session is not None:
        if not isinstance(supplied_session, str) or not supplied_session:
            raise DispatchError("invalid session id")
        if prior_session != supplied_session:
            raise DispatchError("session id mismatch")
    if (
        state["state"] == "reviewing"
        or (state["state"] == "blocked" and isinstance(prior_session, str) and prior_session)
    ) and supplied_session is None:
        raise DispatchError("review correction requires the prior session id")
    if state["state"] not in {"prepared", "reviewing", "integrated", "blocked"}:
        raise DispatchError("invalid state for coder dispatch")
    if state["state"] == "blocked" and coder["pending_commit"] is not None:
        raise DispatchError("blocked integration cannot return to coder dispatch")
    if new_task:
        coder = {
            **coder,
            "budget": {
                "cursor_calls": 0,
                "controller_review_rounds": 0,
                "active_worker_seconds": 0.0,
            },
            "protected_manifest": None,
            "dispatch_baseline": {
                "pre_dispatch_work_branch_commit": None,
                "pre_dispatch_feature_branch_commit": None,
                "verified_worker_tree": None,
                "verified_worker_index": None,
            },
        }
        state = {**state, "coder": coder, "cursor_session_id": None}
        prior_session = None
    correction = state["state"] == "reviewing" or (
        state["state"] == "blocked"
        and isinstance(prior_session, str)
        and bool(prior_session)
    )
    validate_coder_budget(
        state,
        max_retries=args.max_retries,
        correction=correction,
    )
    before_control = git_control_manifest(repository, worktree, deadline=deadline)
    before_controller = controller_source_manifest(repository, deadline=deadline)
    current_protected = {
        "git_control_sha256": _manifest_digest(before_control),
        "controller_source_sha256": _manifest_digest(before_controller),
    }
    if state["state"] == "blocked":
        persisted_protected = coder["protected_manifest"]
        if persisted_protected is not None and persisted_protected != current_protected:
            raise DispatchError("protected state differs from original dispatch baseline")
        if (
            persisted_protected is None
            and isinstance(prior_session, str)
            and prior_session
        ):
            raise DispatchError("resumable coder run is missing protected baseline")
    dispatch_coder = dict(coder)
    dispatch_coder["protected_manifest"] = current_protected
    dispatching = transition_state(
        hermes_home,
        run_id,
        expected_generation,
        "dispatching",
        {"coder": dispatch_coder, "failure": None},
    )
    dispatching = _update_coder_budget(hermes_home, run_id, cursor_calls=1)
    generation = dispatching["generation"]
    pool_dir: Path | None = None
    pool_path: Path | None = None
    verify_home: Path | None = None
    delegate_payload: dict[str, Any] | None = None
    worker_started: float | None = None
    worker_accounted = False
    try:
        source_env = os.environ.copy()
        bash_path = find_bash5(
            repo_root(), source_env.get("PATH", ""), deadline=deadline
        )
        model = read_model(
            "coder",
            default_hermes_bin(),
            source_env,
            timeout_seconds=_remaining_review_seconds(deadline),
        )
        pool_dir = Path(tempfile.mkdtemp(prefix="claude-subagents-pool-"))
        pool_path = write_pool("coder", model, pool_dir)
        verify_home = create_verification_home()
        delegate_env = build_probe_env(
            role="coder",
            pool_path=pool_path,
            run_id=run_id,
            source_env=source_env,
        )
        delegate_env["CSC_CURSOR_SANDBOX"] = validate_cursor_sandbox("enabled")
        delegate_env["CSC_VERIFY_HOME"] = str(verify_home)
        delegate_args = [
            "--coder",
            role_key("coder"),
            "--cwd",
            str(worktree),
            "--task-file",
            str(task_file),
            "--verify-cmd",
            verify_command,
            "--max-retries",
            str(args.max_retries),
        ]
        if supplied_session is not None:
            delegate_args.extend(["--session", supplied_session])
        worker_started = time.monotonic()
        active_remaining = (
            MAX_ACTIVE_WORKER_SECONDS
            - dispatching["coder"]["budget"]["active_worker_seconds"]
        )
        exit_code, delegate_payload = _run_script_json_timed(
            bash_path=bash_path,
            script=code_delegate_script(),
            args=delegate_args,
            env=delegate_env,
            cwd=repo_root(),
            label="code delegate",
            timeout_seconds=min(
                _remaining_review_seconds(deadline),
                active_remaining,
            ),
        )
        delegate_payload = _validate_code_delegate_payload(
            delegate_payload, exit_code=exit_code
        )
        if delegate_payload["attempts"] > args.max_retries:
            raise DispatchError("code delegate exceeded retry budget")
        accounted = _update_coder_budget(
            hermes_home,
            run_id,
            cursor_calls=delegate_payload["attempts"],
            active_worker_seconds=time.monotonic() - worker_started,
        )
        generation = accounted["generation"]
        worker_accounted = True
        after_control = git_control_manifest(repository, worktree, deadline=deadline)
        after_controller = controller_source_manifest(repository, deadline=deadline)
        if after_control != before_control:
            raise DispatchError("external coder changed protected Git control-plane state")
        if after_controller != before_controller:
            raise DispatchError("external coder changed the controller checkout")
        fsck_code, _stdout, fsck_stderr = _run_external(
            ["git", "-C", str(repository), "fsck", "--full"],
            timeout_seconds=_remaining_review_seconds(deadline),
            label="Git object integrity check",
        )
        if fsck_code != 0:
            raise DispatchError(
                _redact_failure(fsck_stderr or "Git object integrity check failed")
            )
        session_id = delegate_payload["session_id"]
        if (
            new_task
            and isinstance(previous_task_session, str)
            and previous_task_session
            and session_id == previous_task_session
        ):
            raise DispatchError("new task returned the prior task session id")
        if supplied_session is not None and session_id != supplied_session:
            raise DispatchError("resumed coder returned a different session id")
        if (
            delegate_payload["status"] != "DONE"
            or exit_code != 0
            or delegate_payload["verified"] is not True
            or not session_id
        ):
            diagnostic = (
                delegate_payload["verify_output"]
                or delegate_payload["result"]
                or "coder or verification failed"
            )
            blocked = transition_state(
                hermes_home,
                run_id,
                generation,
                "blocked",
                {"failure": diagnostic, "cursor_session_id": session_id or prior_session},
            )
            return _blocked_code(
                run_id=run_id,
                generation=blocked["generation"],
                diagnostic=diagnostic,
                payload=delegate_payload,
                session_ids=[
                    value
                    for value in (session_id, supplied_session)
                    if isinstance(value, str) and value
                ],
            )
        _git_text(
            worktree,
            ["add", "-A"],
            label="verified worker index preparation",
            deadline=deadline,
        )
        tree = _git_text(
            worktree,
            ["write-tree"],
            label="verified worker tree inspection",
            deadline=deadline,
        ).strip()
        index_identity = _git_index_identity(worktree, deadline=deadline)
        current = load_state(hermes_home, run_id)
        current_coder = dict(current["coder"])
        current_coder["dispatch_baseline"] = {
            **current_coder["dispatch_baseline"],
            "verified_worker_tree": tree,
            "verified_worker_index": index_identity,
        }
        updated = update_state_fields(
            hermes_home,
            run_id,
            current["generation"],
            {
                "cursor_session_id": session_id,
                "coder": current_coder,
                "failure": None,
            },
        )
        return _code_output(
            delegate_payload,
            run_id=run_id,
            generation=updated["generation"],
            diagnostic="",
        )
    except (DispatchError, OSError, RuntimeError) as exc:
        if worker_started is not None and not worker_accounted:
            try:
                _update_coder_budget(
                    hermes_home,
                    run_id,
                    active_worker_seconds=time.monotonic() - worker_started,
                )
            except DispatchError:
                pass
        current = load_state(hermes_home, run_id)
        returned_session = (
            delegate_payload.get("session_id")
            if isinstance(delegate_payload, dict)
            and isinstance(delegate_payload.get("session_id"), str)
            and delegate_payload.get("session_id")
            else prior_session
        )
        if new_task and returned_session == previous_task_session:
            returned_session = None
        if current["state"] != "blocked":
            current = transition_state(
                hermes_home,
                run_id,
                current["generation"],
                "blocked",
                {
                    "failure": str(exc),
                    "cursor_session_id": returned_session,
                },
            )
        return _blocked_code(
            run_id=run_id,
            generation=current["generation"],
            diagnostic=str(exc),
            payload=delegate_payload,
            session_ids=[
                value
                for value in (returned_session, supplied_session)
                if isinstance(value, str) and value
            ],
        )
    finally:
        cleanup_error: DispatchError | None = None
        try:
            if pool_path is not None:
                pool_path.unlink(missing_ok=True)
            if pool_dir is not None and pool_dir.exists():
                _rmtree_bounded(pool_dir, deadline=deadline)
            if verify_home is not None and verify_home.exists():
                _rmtree_bounded(verify_home, deadline=deadline)
        except OSError as exc:
            cleanup_error = DispatchError(
                "unable to remove coder temporary artifacts"
            )
            cleanup_error.__cause__ = exc
        if cleanup_error is not None:
            try:
                current = load_state(hermes_home, run_id)
                if current["state"] != "blocked":
                    transition_state(
                        hermes_home,
                        run_id,
                        current["generation"],
                        "blocked",
                        {"failure": str(cleanup_error)},
                    )
            except DispatchError:
                pass
            if sys.exc_info()[0] is None:
                raise cleanup_error


def _validate_worktree_prepare_payload(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "status",
        "worktree",
        "work_branch",
        "feature_branch",
        "copied",
        "skipped",
        "neutralized_symlinks",
        "purged_secrets",
        "clone_supported",
    }
    keys = set(payload)
    if keys != required and keys != required | {"diagnostic"}:
        raise DispatchError("worktree prepare returned unexpected fields")
    if payload["status"] != "READY":
        raise DispatchError(
            str(payload.get("diagnostic") or "worktree prepare failed")
        )
    for key in ("worktree", "work_branch", "feature_branch"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DispatchError(f"worktree prepare returned invalid {key}")
    for key in ("copied", "skipped"):
        if not isinstance(payload[key], list) or not all(
            isinstance(item, str) for item in payload[key]
        ):
            raise DispatchError(f"worktree prepare returned invalid {key}")
    for key in ("neutralized_symlinks", "purged_secrets"):
        if (
            not isinstance(payload[key], int)
            or isinstance(payload[key], bool)
            or payload[key] < 0
        ):
            raise DispatchError(f"worktree prepare returned invalid {key}")
    if not isinstance(payload["clone_supported"], bool):
        raise DispatchError("worktree prepare returned invalid clone_supported")
    if "diagnostic" in payload and not isinstance(payload["diagnostic"], str):
        raise DispatchError("worktree prepare returned invalid diagnostic")
    return payload


def _validate_worktree_remove_payload(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"status", "work_branch", "unmerged"}
    keys = set(payload)
    if keys != required and keys != required | {"diagnostic"}:
        raise DispatchError("worktree remove returned unexpected fields")
    if payload["status"] not in {"REMOVED", "REFUSED"}:
        raise DispatchError("worktree remove returned invalid status")
    if not isinstance(payload["work_branch"], str):
        raise DispatchError("worktree remove returned invalid work branch")
    if not isinstance(payload["unmerged"], list) or not all(
        isinstance(item, str) for item in payload["unmerged"]
    ):
        raise DispatchError("worktree remove returned invalid unmerged commits")
    if "diagnostic" in payload and not isinstance(payload["diagnostic"], str):
        raise DispatchError("worktree remove returned invalid diagnostic")
    return payload


def _worktree_prepare_identity(
    repository: Path,
    run_id: str,
) -> tuple[str, Path, str]:
    top_level = _git_text(
        repository,
        ["rev-parse", "--show-toplevel"],
        label="repository root inspection",
    ).strip()
    if Path(top_level).resolve() != repository.resolve():
        raise DispatchError("repository must be its Git top-level checkout")
    feature_branch = _current_branch(repository)
    if feature_branch in {"main", "master"} or feature_branch.endswith("-work"):
        raise DispatchError("worktree preparation requires a feature branch")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", feature_branch)
    slug = re.sub(r"-+", "-", slug).strip("._-") or "branch"
    slug = slug[:80]
    work_branch = f"{slug}-hermes-{run_id}-work"
    worktree = (
        repository.parent
        / f"{repository.name}-{slug}-hermes-{run_id}-work"
    ).resolve()
    return feature_branch, worktree, work_branch


def _write_origin_feature_marker(worktree: Path, feature_branch: str) -> None:
    git_dir = Path(
        _git_text(
            worktree,
            ["rev-parse", "--absolute-git-dir"],
            label="worktree private Git directory inspection",
        ).strip()
    ).resolve()
    marker = git_dir / "csc-origin-feature"
    temporary = git_dir / f".csc-origin-feature.{os.getpid()}.tmp"
    # SECURITY-REVIEW: both paths are derived from Git's private worktree directory.
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(feature_branch + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
        _fsync_directory(git_dir)
    finally:
        temporary.unlink(missing_ok=True)


def _reconcile_preparing_worktree_unlocked(
    repository: Path,
    coder: dict[str, Any],
) -> None:
    feature_branch = str(coder["feature_branch"])
    work_branch = str(coder["work_branch"])
    worktree = Path(str(coder["worktree"]))
    feature_head = _resolve_branch_head(repository, feature_branch)
    work_head = _resolve_branch_head(repository, work_branch)
    have_branch = work_head is not None
    have_path = worktree.exists()
    if not have_branch and not have_path:
        return
    if have_branch and have_path:
        if not _worktree_is_registered(repository, worktree, work_branch):
            raise DispatchError(
                "preparing worktree path is not the registered recovery worktree"
            )
        if work_head != feature_head:
            raise DispatchError("preparing work branch moved beyond the feature branch")
        marker = Path(
            _git_text(
                worktree,
                ["rev-parse", "--absolute-git-dir"],
                label="worktree private Git directory inspection",
            ).strip()
        ) / "csc-origin-feature"
        if not marker.is_file():
            _write_origin_feature_marker(worktree, feature_branch)
        return
    if have_branch and not have_path:
        if work_head != feature_head:
            raise DispatchError("orphaned preparing branch contains unexpected commits")
        prune_code, _stdout, prune_stderr = _run_external(
            ["git", "-C", str(repository), "worktree", "prune"],
            timeout_seconds=None,
            label="preparing worktree metadata prune",
        )
        if prune_code != 0:
            raise DispatchError(
                _redact_failure(
                    prune_stderr or "unable to prune preparing worktree metadata"
                )
            )
        delete_code, _stdout, delete_stderr = _run_external(
            ["git", "-C", str(repository), "branch", "-d", work_branch],
            timeout_seconds=None,
            label="orphaned preparing branch removal",
        )
        if delete_code != 0:
            raise DispatchError(
                _redact_failure(
                    delete_stderr or "unable to remove orphaned preparing branch"
                )
            )
        return
    raise DispatchError("preparing worktree path exists without its recovery branch")


def run_worktree_prepare(
    *,
    repository: Path,
    run_id: str,
    bash_path: Path,
    hermes_home: Path,
) -> dict[str, Any]:
    # SECURITY-REVIEW: repository is caller-controlled; worktree.sh receives no
    # interpolated shell command and CSC_RUN_ID is strictly validated.
    validate_run_id(run_id)
    repo = repository.resolve()
    feature_branch, worktree, work_branch = _worktree_prepare_identity(repo, run_id)
    expected_identity = {
        "repository": str(repo),
        "feature_branch": feature_branch,
        "worktree": str(worktree),
        "work_branch": work_branch,
    }
    env = os.environ.copy()
    env["CSC_RUN_ID"] = run_id
    env["CSC_EXPECTED_FEATURE"] = feature_branch
    env["CSC_EXPECTED_WORKTREE"] = str(worktree)
    env["CSC_EXPECTED_WORK_BRANCH"] = work_branch
    initial = _new_coder_record(
        run_id=run_id,
        repository=str(repo),
        git_object_format=_git_object_format(repo),
        feature_branch=feature_branch,
        worktree=str(worktree),
        work_branch=work_branch,
    )
    with lock_run(hermes_home, run_id):
        state_path = _state_path(hermes_home, run_id)
        if state_path.exists():
            state = load_state(hermes_home, run_id)
            coder = state.get("coder")
            if (
                state["role"] != "coder"
                or state["state"] not in {"preparing", "prepared"}
                or not isinstance(coder, dict)
                or state["repository"] != expected_identity["repository"]
                or coder["feature_branch"] != expected_identity["feature_branch"]
                or coder["worktree"] != expected_identity["worktree"]
                or coder["work_branch"] != expected_identity["work_branch"]
            ):
                raise DispatchError("run id already has different recovery identity")
        else:
            state = _validate_state_for_home(hermes_home, initial, run_id=run_id)
            atomic_write_json(state_path, state)
        try:
            _reconcile_preparing_worktree_unlocked(repo, state["coder"])
            exit_code, payload = _run_script_json(
                bash_path=bash_path,
                script=worktree_script(),
                args=["prepare"],
                env=env,
                cwd=repo,
                label="worktree prepare",
            )
            payload = _validate_worktree_prepare_payload(payload)
            if exit_code != 0:
                raise DispatchError("worktree prepare status conflicts with exit code")
            if (
                payload["feature_branch"] != feature_branch
                or Path(payload["worktree"]).resolve() != worktree
                or payload["work_branch"] != work_branch
            ):
                raise DispatchError("worktree prepare returned mismatched recovery identity")
            current = load_state(hermes_home, run_id)
            if current["state"] == "preparing":
                state = _transition_state_unlocked(
                    hermes_home,
                    run_id,
                    current["generation"],
                    "prepared",
                    {"failure": None},
                )
            else:
                state = current
        except (DispatchError, OSError, RuntimeError) as exc:
            current = load_state(hermes_home, run_id)
            if current["state"] == "preparing":
                _update_state_fields_unlocked(
                    hermes_home,
                    run_id,
                    current["generation"],
                    {"failure": str(exc)},
                )
            raise
    return {
        "status": "READY",
        "run_id": run_id,
        "generation": state["generation"],
        "worktree": payload["worktree"],
        "work_branch": payload["work_branch"],
        "feature_branch": payload["feature_branch"],
        "copied": payload.get("copied", []),
        "skipped": payload.get("skipped", []),
        "neutralized_symlinks": payload.get("neutralized_symlinks", 0),
        "purged_secrets": payload.get("purged_secrets", 0),
        "clone_supported": payload.get("clone_supported", False),
        "diagnostic": "",
    }


def _worktree_already_removed(repository: Path, coder: dict[str, Any]) -> bool:
    worktree = Path(str(coder["worktree"]))
    work_branch = str(coder["work_branch"])
    return not worktree.exists() and not _branch_exists(repository, work_branch)


def _finalize_worktree_removal_unlocked(
    hermes_home: Path,
    run_id: str,
    generation: int,
    current_state: str,
) -> dict[str, Any]:
    record = _advance_to_complete_unlocked(
        hermes_home,
        run_id,
        generation,
        current_state,
    )
    return record


def _remove_orphaned_work_branch_unlocked(
    *,
    repository: Path,
    coder: dict[str, Any],
) -> tuple[bool, list[str], str]:
    work_branch = str(coder["work_branch"])
    feature_branch = str(coder["feature_branch"])
    ancestor_code, _stdout, stderr = _run_external(
        [
            "git",
            "-C",
            str(repository),
            "merge-base",
            "--is-ancestor",
            work_branch,
            feature_branch,
        ],
        timeout_seconds=None,
        label="orphaned work-branch ancestry check",
    )
    if ancestor_code == 1:
        return (
            False,
            _commits_ahead(repository, work_branch, feature_branch),
            "work branch has commits not on the feature branch; nothing was deleted",
        )
    if ancestor_code != 0:
        raise DispatchError(
            _redact_failure(stderr or "unable to validate orphaned work branch")
        )
    prune_code, _stdout, prune_stderr = _run_external(
        ["git", "-C", str(repository), "worktree", "prune"],
        timeout_seconds=None,
        label="worktree metadata prune",
    )
    if prune_code != 0:
        raise DispatchError(
            _redact_failure(prune_stderr or "unable to prune worktree metadata")
        )
    delete_code, _stdout, delete_stderr = _run_external(
        ["git", "-C", str(repository), "branch", "-d", work_branch],
        timeout_seconds=None,
        label="orphaned work-branch removal",
    )
    if delete_code != 0:
        raise DispatchError(
            _redact_failure(delete_stderr or "unable to remove orphaned work branch")
        )
    return True, [], ""


def run_worktree_remove(
    *,
    repository: Path,
    run_id: str,
    expected_generation: int,
    bash_path: Path,
    hermes_home: Path,
) -> dict[str, Any]:
    # SECURITY-REVIEW: persisted paths and branch identity are validated while
    # holding the stable per-run lock before the subprocess can remove anything.
    validate_run_id(run_id)
    if (
        not isinstance(expected_generation, int)
        or isinstance(expected_generation, bool)
        or expected_generation < 1
    ):
        raise DispatchError("invalid expected generation")
    repo = repository.resolve()

    with lock_run(hermes_home, run_id):
        state_path = _state_path(hermes_home, run_id)
        if not state_path.exists():
            raise DispatchError("missing run state")

        state = load_state(hermes_home, run_id)
        coder = _validate_coder_worktree_identity(
            state,
            repository=repo,
            run_id=run_id,
            expected_generation=expected_generation,
        )
        _validate_controller_feature_branch(repo, str(coder["feature_branch"]))
        generation = state["generation"]
        current_state = str(state["state"])
        if current_state == "complete":
            if not _worktree_already_removed(repo, coder):
                raise DispatchError(
                    "complete coder tombstone still has a worktree or work branch"
                )
            return {
                "status": "REMOVED",
                "run_id": run_id,
                "work_branch": str(coder["work_branch"]),
                "unmerged": [],
                "generation": generation,
                "diagnostic": "",
            }

        if _worktree_already_removed(repo, coder):
            record = _finalize_worktree_removal_unlocked(
                hermes_home,
                run_id,
                generation,
                current_state,
            )
            return {
                "status": "REMOVED",
                "run_id": run_id,
                "work_branch": str(coder["work_branch"]),
                "unmerged": [],
                "generation": record["generation"],
                "diagnostic": "",
            }

        if not Path(str(coder["worktree"])).exists() and _branch_exists(
            repo, str(coder["work_branch"])
        ):
            removed, unmerged, diagnostic = _remove_orphaned_work_branch_unlocked(
                repository=repo,
                coder=coder,
            )
            if not removed:
                updated = _transition_state_unlocked(
                    hermes_home,
                    run_id,
                    generation,
                    "blocked",
                    {"failure": diagnostic},
                )
                return {
                    "status": "BLOCKED",
                    "run_id": run_id,
                    "work_branch": str(coder["work_branch"]),
                    "unmerged": unmerged,
                    "generation": updated["generation"],
                    "diagnostic": diagnostic,
                }
            record = _finalize_worktree_removal_unlocked(
                hermes_home,
                run_id,
                generation,
                current_state,
            )
            return {
                "status": "REMOVED",
                "run_id": run_id,
                "work_branch": str(coder["work_branch"]),
                "unmerged": [],
                "generation": record["generation"],
                "diagnostic": "",
            }

        env = os.environ.copy()
        env["CSC_RUN_ID"] = run_id
        exit_code, payload = _run_script_json(
            bash_path=bash_path,
            script=worktree_script(),
            args=["remove"],
            env=env,
            cwd=repo,
            label="worktree remove",
        )
        payload = _validate_worktree_remove_payload(payload)
        status = payload["status"]
        if status == "REFUSED":
            if exit_code != 1:
                raise DispatchError("worktree remove status conflicts with exit code")
            known_sessions = [
                value
                for value in (state.get("cursor_session_id"),)
                if isinstance(value, str) and value
            ]
            diagnostic = _redact_failure(
                str(payload.get("diagnostic") or "worktree remove refused"),
                session_ids=known_sessions,
            )
            updated = _update_state_fields_unlocked(
                hermes_home,
                run_id,
                expected_generation,
                {"failure": diagnostic},
            )
            return {
                "status": "BLOCKED",
                "run_id": run_id,
                "work_branch": payload.get("work_branch", ""),
                "unmerged": payload.get("unmerged", []),
                "generation": updated["generation"],
                "diagnostic": diagnostic,
            }
        if status != "REMOVED":
            raise DispatchError(str(payload.get("diagnostic") or "worktree remove failed"))
        if exit_code != 0:
            raise DispatchError("worktree remove status conflicts with exit code")
        record = _finalize_worktree_removal_unlocked(
            hermes_home,
            run_id,
            generation,
            current_state,
        )
        return {
            "status": "REMOVED",
            "run_id": run_id,
            "work_branch": payload.get("work_branch", ""),
            "unmerged": payload.get("unmerged", []),
            "generation": record["generation"],
            "diagnostic": _redact_failure(
                str(payload.get("diagnostic") or ""),
                session_ids=[
                    value
                    for value in (state.get("cursor_session_id"),)
                    if isinstance(value, str) and value
                ],
            ),
        }


def _update_state_fields_unlocked(
    hermes_home: Path,
    run_id: str,
    expected_generation: int,
    changes: dict[str, Any],
) -> dict[str, Any]:
    current = load_state(hermes_home, run_id)
    if current["generation"] != expected_generation:
        raise DispatchError("stale generation")
    updated = dict(current)
    updated.update(changes)
    updated["generation"] = expected_generation + 1
    updated["updated_at"] = _iso_timestamp()
    updated = _sanitize_state_record(updated)
    updated = _validate_state_for_home(hermes_home, updated, run_id=run_id)
    atomic_write_json(_state_path(hermes_home, run_id), updated)
    return updated


def update_state_fields(
    hermes_home: Path,
    run_id: str,
    expected_generation: int,
    changes: dict[str, Any],
) -> dict[str, Any]:
    validate_run_id(run_id)
    if (
        not isinstance(expected_generation, int)
        or isinstance(expected_generation, bool)
        or expected_generation < 1
    ):
        raise DispatchError("invalid expected generation")
    with lock_run(hermes_home, run_id):
        return _update_state_fields_unlocked(
            hermes_home, run_id, expected_generation, changes
        )


def reconcile_state(hermes_home: Path, run_id: str) -> dict[str, Any]:
    state = load_state(hermes_home, run_id)
    if state["role"] != "coder":
        return state
    return reconcile_coder_state(hermes_home, run_id)


def reconcile_coder_state(hermes_home: Path, run_id: str) -> dict[str, Any]:
    validate_run_id(run_id)
    with lock_run(hermes_home, run_id):
        return _reconcile_coder_state_unlocked(hermes_home, run_id)


def record_reviewing_state(
    hermes_home: Path,
    run_id: str,
    expected_generation: int,
) -> dict[str, Any]:
    with lock_run(hermes_home, run_id):
        state = load_state(hermes_home, run_id)
        if state["generation"] != expected_generation:
            raise DispatchError("stale generation")
        if state["state"] == "reviewing":
            return state
        if state["state"] != "dispatching":
            raise DispatchError("invalid state for record-reviewing")
        record = _reconcile_coder_state_unlocked(hermes_home, run_id)
        if record["state"] != "reviewing":
            raise DispatchError(
                str(record.get("failure") or "controller commit is unauthorized")
            )
        return record


def _commit_parent_and_tree(
    repository: Path,
    commit: str,
) -> tuple[str | None, str | None]:
    parent_code, parent_stdout, _stderr = _run_external(
        ["git", "-C", str(repository), "rev-list", "--parents", "-n", "1", commit],
        timeout_seconds=None,
        label="controller commit parent inspection",
    )
    tree_code, tree_stdout, _stderr = _run_external(
        ["git", "-C", str(repository), "rev-parse", f"{commit}^{{tree}}"],
        timeout_seconds=None,
        label="controller commit tree inspection",
    )
    if parent_code != 0 or tree_code != 0:
        return None, None
    parts = parent_stdout.strip().split()
    if len(parts) != 2:
        return None, tree_stdout.strip()
    return parts[1], tree_stdout.strip()


def _block_coder_unlocked(
    hermes_home: Path,
    run_id: str,
    state: dict[str, Any],
    diagnostic: str,
) -> dict[str, Any]:
    return _transition_state_unlocked(
        hermes_home,
        run_id,
        state["generation"],
        "blocked",
        {
            "failure": _redact_failure(
                diagnostic,
                session_ids=[
                    value
                    for value in (state.get("cursor_session_id"),)
                    if isinstance(value, str)
                ],
            )
        },
    )


def _authorized_controller_commit(
    repository: Path,
    commit: str,
    baseline: dict[str, Any],
) -> tuple[bool, str]:
    expected_parent = baseline["pre_dispatch_work_branch_commit"]
    expected_tree = baseline["verified_worker_tree"]
    expected_index = baseline["verified_worker_index"]
    if expected_tree is None or expected_index is None:
        return False, "unauthorized worker commit: verified worker identity is missing"
    parent, tree = _commit_parent_and_tree(repository, commit)
    if parent != expected_parent:
        return False, "unauthorized controller commit parent"
    if tree != expected_tree:
        return False, "unauthorized controller commit tree"
    return True, ""


def _reconcile_coder_state_unlocked(
    hermes_home: Path,
    run_id: str,
) -> dict[str, Any]:
    state = load_state(hermes_home, run_id)
    if state["role"] != "coder":
        raise DispatchError("role mismatch")
    coder = state.get("coder")
    if not isinstance(coder, dict):
        raise DispatchError("malformed coder state")
    worktree = coder.get("worktree")
    work_branch = coder.get("work_branch")
    feature_branch = str(coder.get("feature_branch", ""))
    repository = Path(str(state["repository"]))
    generation = state["generation"]
    current_state = str(state["state"])
    if current_state == "preparing":
        return state
    baseline = coder["dispatch_baseline"]
    expected_feature = baseline["pre_dispatch_feature_branch_commit"]
    current_feature = _resolve_branch_head(repository, feature_branch)

    if current_state == "reviewing":
        if coder.get("pending_commit"):
            return _block_coder_unlocked(
                hermes_home, run_id, state, "pending commit set while reviewing"
            )
        if expected_feature != current_feature:
            return _block_coder_unlocked(
                hermes_home,
                run_id,
                state,
                "unauthorized feature branch movement",
            )
        current_work = _resolve_branch_head(repository, str(work_branch))
        if not isinstance(current_work, str):
            return _block_coder_unlocked(
                hermes_home, run_id, state, "work branch disappeared during review"
            )
        authorized, diagnostic = _authorized_controller_commit(
            repository, current_work, baseline
        )
        if not authorized:
            return _block_coder_unlocked(hermes_home, run_id, state, diagnostic)
        return state

    if current_state == "dispatching":
        if expected_feature != current_feature:
            return _block_coder_unlocked(
                hermes_home,
                run_id,
                state,
                "unauthorized feature branch movement",
            )
        pre_dispatch = baseline["pre_dispatch_work_branch_commit"]
        current_work = _resolve_branch_head(repository, str(work_branch))
        if current_work == pre_dispatch:
            return _block_coder_unlocked(
                hermes_home,
                run_id,
                state,
                "dispatch interrupted before an authorized controller commit",
            )
        if not isinstance(current_work, str):
            return _block_coder_unlocked(
                hermes_home, run_id, state, "work branch disappeared during dispatch"
            )
        authorized, diagnostic = _authorized_controller_commit(
            repository, current_work, baseline
        )
        if not authorized:
            return _block_coder_unlocked(hermes_home, run_id, state, diagnostic)
        reviewing_coder = dict(coder)
        unintegrated = list(reviewing_coder["unintegrated_commits"])
        if current_work not in unintegrated:
            unintegrated.append(current_work)
        reviewing_coder["unintegrated_commits"] = unintegrated
        budget = dict(reviewing_coder["budget"])
        if budget["controller_review_rounds"] >= MAX_CONTROLLER_REVIEW_ROUNDS:
            return _block_coder_unlocked(
                hermes_home,
                run_id,
                state,
                "controller review round budget exceeded",
            )
        budget["controller_review_rounds"] += 1
        reviewing_coder["budget"] = budget
        return _transition_state_unlocked(
            hermes_home,
            run_id,
            generation,
            "reviewing",
            {"coder": reviewing_coder},
        )

    if current_state == "integration_pending":
        pending = coder.get("pending_commit")
        if isinstance(pending, str) and pending:
            authorized, diagnostic = _authorized_controller_commit(
                repository, pending, baseline
            )
            if not authorized:
                return _block_coder_unlocked(
                    hermes_home, run_id, state, diagnostic
                )
            current_work = _resolve_branch_head(repository, str(work_branch))
            if current_work != pending:
                return _block_coder_unlocked(
                    hermes_home,
                    run_id,
                    state,
                    "integration candidate is not the exact work branch tip",
                )
            if current_feature == pending:
                return _transition_state_unlocked(
                    hermes_home,
                    run_id,
                    generation,
                    "integrated",
                    {
                        "coder": {
                            **coder,
                            "last_integrated_commit": pending,
                            "pending_commit": None,
                            "unintegrated_commits": _commits_ahead(
                                repository,
                                str(work_branch),
                                feature_branch,
                            ),
                        }
                    },
                )
            if current_feature != expected_feature:
                return _block_coder_unlocked(
                    hermes_home,
                    run_id,
                    state,
                    "integration divergence detected",
                )
            return state
        return _block_coder_unlocked(
            hermes_home,
            run_id,
            state,
            "integration pending without candidate commit",
        )

    if isinstance(worktree, str) and not Path(worktree).exists():
        if isinstance(work_branch, str) and not _branch_exists(repository, work_branch):
            return _advance_to_complete_unlocked(
                hermes_home,
                run_id,
                generation,
                current_state,
            )

    if current_state == "prepared":
        ahead = _commits_ahead(repository, str(work_branch), feature_branch)
        if ahead:
            return _block_coder_unlocked(
                hermes_home,
                run_id,
                state,
                "ambiguous worktree edits before dispatch",
            )

    return state


def _branch_exists(repository: Path, branch: str) -> bool:
    returncode, _stdout, _stderr = _run_external(
        ["git", "-C", str(repository), "rev-parse", "--verify", f"refs/heads/{branch}"],
        timeout_seconds=None,
        label="git branch existence check",
    )
    return returncode == 0


def _commits_ahead(repository: Path, work_branch: str, feature_branch: str) -> list[str]:
    returncode, stdout, _stderr = _run_external(
        [
            "git",
            "-C",
            str(repository),
            "log",
            "--format=%H",
            f"{feature_branch}..{work_branch}",
        ],
        timeout_seconds=None,
        label="work-branch commit inspection",
    )
    if returncode != 0:
        return []
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def _emit(payload: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0 if payload.get("status") in {"READY", "DONE", "REVIEWED"} else 1


def _emit_review_cli_failure(
    diagnostic: str,
    *,
    run_id: str | None = None,
    target: str = "spec",
    lenses: list[str] | None = None,
) -> int:
    resolved_run_id = (
        run_id
        if isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id)
        else "0000000000000000"
    )
    generation = 0
    session_ids: list[str] = []
    hermes_home_value = os.environ.get("HERMES_HOME")
    if hermes_home_value and resolved_run_id != "0000000000000000":
        try:
            state = load_state(Path(hermes_home_value), resolved_run_id)
            generation = state["generation"]
            session = state.get("cursor_session_id")
            if isinstance(session, str) and session:
                session_ids.append(session)
        except (DispatchError, OSError):
            pass
    payload = _blocked_review(
        run_id=resolved_run_id,
        generation=generation,
        target=target,
        lenses=lenses or [],
        diagnostic=diagnostic,
        session_ids=session_ids,
    )
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 2


def _emit_cli_failure(
    diagnostic: str,
    *,
    run_id: str | None = None,
    role: str | None = None,
    command: str = "probe",
    action: str | None = None,
) -> int:
    resolved_run_id = (
        run_id
        if isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id)
        else "0000000000000000"
    )
    resolved_role = role if role in {"coder", "reviewer"} else "coder"
    session_ids: list[str] = []
    generation = 0
    state_record: dict[str, Any] | None = None
    hermes_home_value = os.environ.get("HERMES_HOME")
    if (
        hermes_home_value
        and isinstance(resolved_run_id, str)
        and RUN_ID_RE.fullmatch(resolved_run_id)
    ):
        try:
            state_record = load_state(Path(hermes_home_value), resolved_run_id)
            generation = int(state_record["generation"])
            session = state_record.get("cursor_session_id")
            if isinstance(session, str) and session:
                session_ids.append(session)
        except (DispatchError, OSError):
            pass
    redacted = _redact_failure(diagnostic, session_ids=session_ids)
    if command == "worktree":
        coder_state = (
            state_record.get("coder")
            if isinstance(state_record, dict)
            and isinstance(state_record.get("coder"), dict)
            else {}
        )
        if action == "prepare":
            payload = {
                "status": "BLOCKED",
                "run_id": resolved_run_id,
                "generation": generation,
                "worktree": str(coder_state.get("worktree") or ""),
                "work_branch": str(coder_state.get("work_branch") or ""),
                "feature_branch": str(coder_state.get("feature_branch") or ""),
                "copied": [],
                "skipped": [],
                "neutralized_symlinks": 0,
                "purged_secrets": 0,
                "clone_supported": False,
                "diagnostic": redacted,
            }
        else:
            payload = {
                "status": "BLOCKED",
                "run_id": resolved_run_id,
                "work_branch": str(coder_state.get("work_branch") or ""),
                "unmerged": [],
                "generation": generation,
                "diagnostic": redacted,
            }
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        return 2
    if command == "state":
        if action == "show":
            payload = {
                "status": "BLOCKED",
                "run_id": resolved_run_id,
                "generation": generation,
                "record": None,
                "diagnostic": redacted,
            }
        elif action == "prune":
            payload = {"status": "BLOCKED", "removed": [], "diagnostic": redacted}
        else:
            payload = {
                "status": "BLOCKED",
                "run_id": resolved_run_id,
                "state": "blocked",
                "generation": generation,
                "diagnostic": redacted,
            }
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        return 2
    if command == "code":
        payload = {
            "status": "BLOCKED",
            "run_id": resolved_run_id,
            "generation": generation,
            "coder": role_key("coder"),
            "session_id": "",
            "attempts": 0,
            "verified": False,
            "changed": False,
            "commit_id": "",
            "result": "",
            "verify_output": "",
            "diagnostic": redacted,
        }
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        return 2
    payload = blocked_probe_result(
        redacted,
        role=resolved_role,
        key=role_key(resolved_role),
        run_id=resolved_run_id,
    )
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Cursor dispatch adapter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    runtime = subparsers.add_parser("runtime")
    runtime.add_argument("--bash-path", action="store_true")
    runtime.set_defaults(handler="_runtime_bash_path")

    probe = subparsers.add_parser("probe")
    probe.add_argument("--role", required=True, choices=["coder", "reviewer"])
    probe.add_argument("--run-id", required=True)
    probe.set_defaults(handler="_cmd_probe")

    worktree = subparsers.add_parser("worktree")
    worktree.add_argument("--action", required=True, choices=["prepare", "remove"])
    worktree.add_argument("--repo", required=True)
    worktree.add_argument("--run-id", required=True)
    worktree.add_argument("--expected-generation", type=int)
    worktree.set_defaults(handler="_cmd_worktree")

    state = subparsers.add_parser("state")
    state.add_argument(
        "--action",
        required=True,
        choices=[
            "show",
            "record-reviewing",
            "record-integration",
            "block",
            "complete",
            "prune",
        ],
    )
    state.add_argument("--run-id")
    state.add_argument("--expected-generation", type=int)
    state.add_argument("--commit")
    state.set_defaults(handler="_cmd_state")

    code = subparsers.add_parser("code")
    code.add_argument("--repo", required=True)
    code.add_argument("--cwd", required=True)
    code.add_argument("--run-id", required=True)
    code.add_argument("--task-file", required=True)
    code.add_argument("--expected-generation", required=True, type=int)
    code.add_argument("--verify-cmd", required=True)
    code.add_argument("--session")
    code.add_argument("--max-retries", type=int, choices=range(0, 4), default=3)
    code.set_defaults(handler="_cmd_code")

    review = subparsers.add_parser("review")
    review.add_argument("--target", required=True, choices=["spec", "plan"])
    review.add_argument("--doc-file", required=True)
    review.add_argument("--spec-file")
    review.add_argument("--lenses")
    review.add_argument("--run-id", required=True)
    review.add_argument("--expected-generation", type=int)
    review.add_argument("--session")
    review.add_argument("--repo")
    review.set_defaults(handler="_cmd_review")

    return parser


def _runtime_bash_path(args: argparse.Namespace) -> int:
    if not args.bash_path:
        raise DispatchError("runtime requires --bash-path")
    bash = find_bash5(repo_root(), os.environ.get("PATH", ""))
    sys.stdout.write(f"{bash}\n")
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    validate_run_id(args.run_id)
    env = os.environ.copy()
    hermes_bin = default_hermes_bin()
    bash_path = find_bash5(repo_root(), env.get("PATH", ""))
    try:
        code, payload = run_probe(
            role=args.role,
            run_id=args.run_id,
            hermes_bin=hermes_bin,
            env=env,
            bash_path=bash_path,
        )
    except SystemExit:
        raise
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return code


def _cmd_worktree(args: argparse.Namespace) -> int:
    validate_run_id(args.run_id)
    hermes_home = Path(_require_hermes_home(os.environ))
    bash_path = find_bash5(repo_root(), os.environ.get("PATH", ""))
    repository = Path(args.repo).resolve()
    if args.action == "prepare":
        payload = run_worktree_prepare(
            repository=repository,
            run_id=args.run_id,
            bash_path=bash_path,
            hermes_home=hermes_home,
        )
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        return 0
    if args.expected_generation is None:
        raise DispatchError("expected generation is required for worktree remove")
    payload = run_worktree_remove(
        repository=repository,
        run_id=args.run_id,
        expected_generation=args.expected_generation,
        bash_path=bash_path,
        hermes_home=hermes_home,
    )
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0 if payload["status"] == "REMOVED" else 1


def _cmd_state(args: argparse.Namespace) -> int:
    hermes_home = Path(_require_hermes_home(os.environ))
    if args.action == "prune":
        removed = prune_state(hermes_home, _utc_now())
        payload = {"status": "PRUNED", "removed": removed, "diagnostic": ""}
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        return 0
    if not args.run_id:
        raise DispatchError("run id is required")
    run_id = validate_run_id(args.run_id)
    if args.action == "show":
        record = reconcile_state(hermes_home, run_id)
        payload = {
            "status": "OK",
            "run_id": run_id,
            "generation": record["generation"],
            "record": record,
            "diagnostic": "",
        }
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        return 0
    if args.expected_generation is None:
        raise DispatchError("expected generation is required")
    if (
        not isinstance(args.expected_generation, int)
        or isinstance(args.expected_generation, bool)
        or args.expected_generation < 1
    ):
        raise DispatchError("invalid expected generation")
    changes: dict[str, Any] = {}
    if args.action == "record-reviewing":
        record = record_reviewing_state(
            hermes_home, run_id, args.expected_generation
        )
    elif args.action == "record-integration":
        next_state = "integration_pending"
        if not isinstance(args.commit, str) or not GIT_OBJECT_RE.fullmatch(args.commit):
            raise DispatchError("commit is required for record-integration")
        state = load_state(hermes_home, run_id)
        if state["generation"] != args.expected_generation:
            raise DispatchError("stale generation")
        if state["state"] != "reviewing" or state["role"] != "coder":
            raise DispatchError("invalid state for record-integration")
        coder = dict(state["coder"])
        authorized, diagnostic = _authorized_controller_commit(
            Path(state["repository"]),
            args.commit,
            coder["dispatch_baseline"],
        )
        if not authorized:
            raise DispatchError(diagnostic)
        if _resolve_branch_head(
            Path(state["repository"]), str(coder["work_branch"])
        ) != args.commit:
            raise DispatchError("integration commit is not the current work branch")
        coder["pending_commit"] = args.commit
        changes["coder"] = coder
    elif args.action == "block":
        next_state = "blocked"
        changes["failure"] = "blocked by controller"
    elif args.action == "complete":
        next_state = "complete"
    else:
        raise DispatchError("unsupported state action")
    if args.action == "complete":
        record = finalize_complete_state(
            hermes_home,
            run_id,
            args.expected_generation,
        )
    elif args.action != "record-reviewing":
        record = transition_state(
            hermes_home,
            run_id,
            args.expected_generation,
            next_state,
            changes,
        )
    payload = {
        "status": "OK",
        "run_id": run_id,
        "state": record["state"],
        "generation": record["generation"],
        "diagnostic": "",
    }
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    payload = run_review(args)
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0 if payload["status"] == "REVIEWED" else 1


def _cmd_code(args: argparse.Namespace) -> int:
    payload = run_coder(args)
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0 if payload["status"] == "DONE" else 1


def _structured_command(argv: list[str] | None) -> str | None:
    tokens = argv if argv is not None else sys.argv[1:]
    return tokens[0] if tokens else None


def _cli_tokens(argv: list[str] | None) -> dict[str, str | None]:
    tokens = argv if argv is not None else sys.argv[1:]
    run_id: str | None = None
    role: str | None = None
    action: str | None = None
    for index, token in enumerate(tokens):
        if token == "--run-id" and index + 1 < len(tokens):
            run_id = tokens[index + 1]
        if token == "--role" and index + 1 < len(tokens):
            role = tokens[index + 1]
        if token == "--action" and index + 1 < len(tokens):
            action = tokens[index + 1]
    return {"run_id": run_id, "role": role, "action": action}


def _validate_operation_options(args: argparse.Namespace) -> None:
    if args.command == "worktree":
        if args.action == "prepare" and args.expected_generation is not None:
            raise DispatchError("worktree prepare forbids expected generation")
        if args.action == "remove" and args.expected_generation is None:
            raise DispatchError("worktree remove requires expected generation")
        return
    if args.command != "state":
        return
    action = args.action
    run_id = args.run_id
    generation = args.expected_generation
    commit = args.commit
    if action == "prune":
        if run_id is not None or generation is not None or commit is not None:
            raise DispatchError("state prune forbids run id, generation, and commit")
        return
    if run_id is None:
        raise DispatchError(f"state {action} requires run id")
    if action == "show":
        if generation is not None or commit is not None:
            raise DispatchError("state show forbids generation and commit")
        return
    if generation is None:
        raise DispatchError(f"state {action} requires expected generation")
    if action == "record-integration":
        if commit is None:
            raise DispatchError("state record-integration requires commit")
        return
    if commit is not None:
        raise DispatchError(f"state {action} forbids commit")


def main(argv: list[str] | None = None) -> int:
    if _interrupted:
        return 130

    parser = _build_parser()
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(argv)
        _validate_operation_options(args)
        handler = globals()[args.handler]
        with lifecycle_signal_handlers():
            return handler(args)
    except SystemExit as exc:
        if isinstance(exc.code, int) and exc.code >= 128:
            raise
        command = _structured_command(argv)
        if command in STRUCTURED_COMMANDS and exc.code not in (0, None):
            tokens = _cli_tokens(argv)
            if command == "review":
                raw_tokens = argv if argv is not None else sys.argv[1:]
                target = "spec"
                lenses: list[str] = []
                for index, token in enumerate(raw_tokens):
                    if token == "--target" and index + 1 < len(raw_tokens):
                        target = raw_tokens[index + 1]
                    if token == "--lenses" and index + 1 < len(raw_tokens):
                        lenses = [
                            part.strip()
                            for part in raw_tokens[index + 1].split(",")
                            if part.strip()
                        ]
                return _emit_review_cli_failure(
                    "invalid arguments",
                    run_id=tokens["run_id"],
                    target=target if target in {"spec", "plan"} else "spec",
                    lenses=lenses,
                )
            return _emit_cli_failure(
                "invalid arguments",
                run_id=tokens["run_id"],
                role=tokens["role"],
                command=command,
                action=tokens["action"],
            )
        raise
    except (DispatchError, RuntimeError, FileNotFoundError, OSError) as exc:
        if args is not None and args.command == "runtime":
            sys.stderr.write(f"{exc}\n")
            return 2
        if args is not None and args.command == "review":
            run_id = getattr(args, "run_id", None)
            target = getattr(args, "target", "spec")
            lenses_raw = getattr(args, "lenses", None)
            lenses = (
                [part.strip() for part in lenses_raw.split(",") if part.strip()]
                if lenses_raw
                else []
            )
            return _emit_review_cli_failure(
                str(exc),
                run_id=run_id,
                target=target,
                lenses=lenses,
            )
        run_id = getattr(args, "run_id", None) if args is not None else None
        role = getattr(args, "role", None) if args is not None else None
        return _emit_cli_failure(
            str(exc),
            run_id=run_id,
            role=role,
            command=getattr(args, "command", "probe") if args is not None else "probe",
            action=getattr(args, "action", None) if args is not None else None,
        )


if __name__ == "__main__":
    raise SystemExit(main())
