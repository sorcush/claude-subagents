#!/usr/bin/env python3
"""Hermes profile-aware adapter for Cursor probe and delegate routing."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MAX_RESULT_BYTES = 2 * 1024 * 1024
STREAM_MAX_BYTES = 67108864
TERM_GRACE_SECONDS = 5
BASH_VERSION_TIMEOUT_SECONDS = 3
STRUCTURED_COMMANDS = frozenset({"probe", "worktree", "state", "code", "review"})
RUN_ID_RE = re.compile(r"^[a-f0-9]{16}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
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

_active_probe_cleanup: list[tuple[Path | None, Path]] = []
_active_probe_process: subprocess.Popen[Any] | None = None
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
    return "cursor-coder" if role == "coder" else "cursor-reviewer"


def _bash_major_version(path: Path) -> int:
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=BASH_VERSION_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        raise DispatchError(
            f"unable to execute bash candidate {path.name}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DispatchError(
            f"timed out probing bash candidate {path.name}"
        ) from exc

    if proc.returncode != 0:
        raise DispatchError(f"unable to determine bash version for {path.name}")
    match = re.search(r"version (\d+)", proc.stdout)
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


def find_bash5(repo: Path, path_value: str) -> Path:
    for candidate in _collect_bash_candidates(path_value):
        if _is_repo_local(candidate, repo):
            continue
        if not os.access(candidate, os.X_OK):
            continue
        try:
            major = _bash_major_version(candidate)
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
    return home


def read_model(role: str, hermes_bin: str, env: dict[str, str]) -> str:
    _require_hermes_home(env)
    if role not in {"coder", "reviewer"}:
        raise DispatchError(f"invalid role: {role}")

    key = f"{PLUGIN_SETTINGS_PREFIX}.{role}_model"
    proc = subprocess.run(
        [hermes_bin, "config", "get", key],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise DispatchError(f"missing configuration for {role}_model")
    model = proc.stdout.strip()
    if not model:
        raise DispatchError(f"blank configuration for {role}_model")
    if not MODEL_RE.fullmatch(model):
        raise DispatchError(f"invalid model identifier for {role}_model")
    return model


def validate_run_id(value: str) -> str:
    if not RUN_ID_RE.fullmatch(value):
        raise DispatchError("run id must match ^[a-f0-9]{16}$")
    return value


def validate_cursor_sandbox(value: str) -> str:
    if value != "enabled":
        raise DispatchError("invalid cursor sandbox mode")
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
        "diagnostic": diagnostic,
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
    if pool_path is not None:
        pool_path.unlink(missing_ok=True)
    shutil.rmtree(pool_dir, ignore_errors=True)


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    time.sleep(TERM_GRACE_SECONDS)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.wait()


def _handle_probe_signal(signum: int, _frame: Any) -> None:
    global _interrupted, _interrupt_signum
    _interrupted = True
    _interrupt_signum = signum
    if _active_probe_process is not None and _active_probe_process.poll() is None:
        _terminate_process_group(_active_probe_process)
    for pool_path, pool_dir in _active_probe_cleanup:
        if pool_path is not None:
            pool_path.unlink(missing_ok=True)
        shutil.rmtree(pool_dir, ignore_errors=True)
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


def _emit(payload: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0 if payload.get("status") in {"READY", "DONE", "REVIEWED"} else 1


def _emit_cli_failure(
    diagnostic: str,
    *,
    run_id: str | None = None,
    role: str | None = None,
) -> int:
    resolved_run_id = run_id or "0000000000000000"
    resolved_role = role if role in {"coder", "reviewer"} else "coder"
    payload = blocked_probe_result(
        diagnostic,
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
    worktree.add_argument("--action", required=True)
    worktree.add_argument("--repo")
    worktree.add_argument("--run-id")
    worktree.set_defaults(handler="_cmd_unimplemented")

    state = subparsers.add_parser("state")
    state.add_argument("--action", required=True)
    state.add_argument("--run-id")
    state.set_defaults(handler="_cmd_unimplemented")

    code = subparsers.add_parser("code")
    code.set_defaults(handler="_cmd_unimplemented")

    review = subparsers.add_parser("review")
    review.set_defaults(handler="_cmd_unimplemented")

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


def _cmd_unimplemented(args: argparse.Namespace) -> int:
    del args
    payload = {
        "status": "BLOCKED",
        "diagnostic": "operation not implemented",
    }
    sys.stdout.write(json.dumps(payload) + "\n")
    return 2


def _structured_command(argv: list[str] | None) -> str | None:
    tokens = argv if argv is not None else sys.argv[1:]
    return tokens[0] if tokens else None


def _cli_tokens(argv: list[str] | None) -> dict[str, str | None]:
    tokens = argv if argv is not None else sys.argv[1:]
    run_id: str | None = None
    role: str | None = None
    for index, token in enumerate(tokens):
        if token == "--run-id" and index + 1 < len(tokens):
            run_id = tokens[index + 1]
        if token == "--role" and index + 1 < len(tokens):
            role = tokens[index + 1]
    return {"run_id": run_id, "role": role}


def main(argv: list[str] | None = None) -> int:
    if _interrupted:
        return 130

    parser = _build_parser()
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(argv)
        handler = globals()[args.handler]
        return handler(args)
    except SystemExit as exc:
        if isinstance(exc.code, int) and exc.code >= 128:
            raise
        command = _structured_command(argv)
        if command in STRUCTURED_COMMANDS and exc.code not in (0, None):
            tokens = _cli_tokens(argv)
            return _emit_cli_failure(
                "invalid arguments",
                run_id=tokens["run_id"],
                role=tokens["role"],
            )
        raise
    except (DispatchError, RuntimeError, FileNotFoundError, OSError) as exc:
        if args is not None and args.command == "runtime":
            sys.stderr.write(f"{exc}\n")
            return 2
        run_id = getattr(args, "run_id", None) if args is not None else None
        role = getattr(args, "role", None) if args is not None else None
        return _emit_cli_failure(str(exc), run_id=run_id, role=role)


if __name__ == "__main__":
    raise SystemExit(main())
