#!/usr/bin/env python3
"""Run a command with timeout and bounded stdout capture."""

import argparse
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

TIMEOUT_EXIT = 124
OVERFLOW_EXIT = 125
TERM_GRACE_SECONDS = 5
CHUNK_SIZE = 64 * 1024


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--max-stdout-bytes", type=int, required=True)
    parser.add_argument("--stdout-file", type=Path, required=True)
    parser.add_argument("--stderr-file", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.max_stdout_bytes <= 0:
        parser.error("--max-stdout-bytes must be positive")
    return args


def terminate_process_group(process):
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


def shell_status(returncode):
    return returncode if returncode >= 0 else 128 - returncode


def run(args):
    process = subprocess.Popen(
        args.command,
        cwd=args.cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    assert stdout_pipe is not None and stderr_pipe is not None
    selector.register(stdout_pipe, selectors.EVENT_READ, "stdout")
    selector.register(stderr_pipe, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + args.timeout_seconds
    stdout_bytes = 0
    outcome = None

    try:
        with args.stdout_file.open("wb") as stdout_file, args.stderr_file.open("ab") as stderr_file:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outcome = TIMEOUT_EXIT
                    terminate_process_group(process)
                    break

                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, CHUNK_SIZE)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stdout":
                        stdout_bytes += len(chunk)
                        if stdout_bytes > args.max_stdout_bytes:
                            outcome = OVERFLOW_EXIT
                            terminate_process_group(process)
                            break
                        stdout_file.write(chunk)
                    else:
                        stderr_file.write(chunk)
                if outcome is not None:
                    break

            if outcome is None:
                outcome = shell_status(process.wait())
    finally:
        selector.close()
        if process.poll() is None:
            terminate_process_group(process)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    if outcome == OVERFLOW_EXIT:
        args.stdout_file.unlink(missing_ok=True)
    return outcome


def main():
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
