#!/usr/bin/env python3
"""Regression tests for the final system-review correction."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes.scripts import dispatch
from tests.test_hermes_correction import (
    RUN_ID,
    coder_record,
    init_repository,
    review_args,
    reviewer_record,
    run_git,
    write_executable,
)


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class CoderProcessGroupTests(unittest.TestCase):
    def test_global_signal_handler_terminates_coder_child_and_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "coder.sh"
            direct_pid = root / "direct.pid"
            descendant_pid = root / "descendant.pid"
            write_executable(
                script,
                f"""#!/bin/sh
echo $$ > {str(direct_pid)!r}
sh -c 'echo $$ > {str(descendant_pid)!r}; sleep 60' &
wait
""",
            )
            error: list[BaseException] = []

            def run() -> None:
                try:
                    dispatch._run_script_json_timed(
                        bash_path=Path("/bin/sh"),
                        script=script,
                        args=[],
                        env=os.environ.copy(),
                        cwd=root,
                        label="code delegate",
                        timeout_seconds=30,
                    )
                except BaseException as exc:
                    error.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            for _ in range(100):
                if direct_pid.exists() and descendant_pid.exists():
                    break
                time.sleep(0.02)
            direct = int(direct_pid.read_text())
            descendant = int(descendant_pid.read_text())
            with self.assertRaises(SystemExit):
                dispatch._handle_lifecycle_signal(signal.SIGTERM, None)
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertFalse(pid_exists(direct))
            self.assertFalse(pid_exists(descendant))
            self.assertTrue(error)
            dispatch._interrupted = False
            dispatch._interrupt_signum = None

    def test_coder_timeout_terminates_direct_child_and_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "coder.sh"
            direct_pid = root / "direct.pid"
            descendant_pid = root / "descendant.pid"
            write_executable(
                script,
                f"""#!/bin/sh
echo $$ > {str(direct_pid)!r}
sh -c 'echo $$ > {str(descendant_pid)!r}; sleep 60' &
wait
""",
            )
            with self.assertRaisesRegex(dispatch.DispatchError, "timed out"):
                dispatch._run_script_json_timed(
                    bash_path=Path("/bin/sh"),
                    script=script,
                    args=[],
                    env=os.environ.copy(),
                    cwd=root,
                    label="code delegate",
                    timeout_seconds=0.1,
                )
            self.assertFalse(pid_exists(int(direct_pid.read_text())))
            self.assertFalse(pid_exists(int(descendant_pid.read_text())))

    def test_cleanup_uses_recorded_group_after_leader_exits(self) -> None:
        process = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 60 & echo $!"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert process.stdout is not None
        descendant = int(process.stdout.readline().strip())
        process.wait(timeout=5)
        self.assertIsNotNone(process.poll())
        dispatch._terminate_process_group(process)
        process.communicate(timeout=1)
        self.assertFalse(pid_exists(descendant))


class ProtectedManifestTests(unittest.TestCase):
    def test_effective_custom_hooks_path_content_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            worktree = root / "worker"
            run_git(repository, "worktree", "add", "-b", "worker", str(worktree))
            hooks = root / "custom-hooks"
            hooks.mkdir()
            run_git(repository, "config", "core.hooksPath", str(hooks))
            before = dispatch.git_control_manifest(repository, worktree)
            (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            after = dispatch.git_control_manifest(repository, worktree)
            self.assertNotEqual(before, after)

    def test_relative_custom_hooks_path_content_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            worktree = root / "worker"
            run_git(repository, "worktree", "add", "-b", "worker", str(worktree))
            hooks = worktree / ".custom-hooks"
            hooks.mkdir()
            run_git(repository, "config", "core.hooksPath", ".custom-hooks")
            before = dispatch.git_control_manifest(repository, worktree)
            (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            after = dispatch.git_control_manifest(repository, worktree)
            self.assertNotEqual(before, after)

    def test_prepared_state_contains_durable_budget_and_protected_fields(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            prepared = dispatch.run_worktree_prepare(
                repository=repository,
                run_id=RUN_ID,
                bash_path=bash,
                hermes_home=root / "home",
            )
            state = dispatch.load_state(root / "home", RUN_ID)
            self.assertEqual(state["git_object_format"], "sha1")
            self.assertEqual(
                set(state["coder"]["budget"]),
                {"cursor_calls", "controller_review_rounds", "active_worker_seconds"},
            )
            self.assertIsNone(state["coder"]["protected_manifest"])
            self.assertEqual(prepared["generation"], 2)

    def test_custom_hook_mutation_blocks_without_staging_or_baseline_recapture(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            home = root / "home"
            prepared = dispatch.run_worktree_prepare(
                repository=repository,
                run_id=RUN_ID,
                bash_path=bash,
                hermes_home=home,
            )
            worktree = Path(prepared["worktree"])
            hooks = root / "custom-hooks"
            hooks.mkdir()
            run_git(repository, "config", "core.hooksPath", str(hooks))
            task = root / "task.md"
            task.write_text("bounded\n", encoding="utf-8")
            fake_hermes = root / "hermes"
            delegate = root / "code-delegate.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho coder-model\n")
            write_executable(
                delegate,
                f"""#!/bin/sh
printf '#!/bin/sh\nexit 1\n' > {str(hooks / 'pre-commit')!r}
printf '%s\n' '{{"status":"DONE","coder":"cursor-coder","session_id":"session-1","attempts":0,"verified":true,"changed":true,"commit_id":"","result":"done","verify_output":"ok"}}'
""",
            )
            args = argparse.Namespace(
                repo=str(repository),
                cwd=str(worktree),
                run_id=RUN_ID,
                task_file=str(task),
                expected_generation=prepared["generation"],
                verify_cmd="true",
                session=None,
                max_retries=3,
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch.object(
                dispatch, "default_hermes_bin", return_value=str(fake_hermes)
            ), patch.object(dispatch, "find_bash5", return_value=bash), patch.object(
                dispatch, "code_delegate_script", return_value=delegate
            ):
                result = dispatch.run_coder(args)
            self.assertEqual(result["status"], "BLOCKED")
            state = dispatch.load_state(home, RUN_ID)
            self.assertEqual(state["cursor_session_id"], "session-1")
            self.assertIsNotNone(state["coder"]["protected_manifest"])
            self.assertEqual(run_git(worktree, "diff", "--cached", "--name-only"), "")
            args.expected_generation = state["generation"]
            args.session = "session-1"
            args.max_retries = 0
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                with self.assertRaisesRegex(dispatch.DispatchError, "protected state"):
                    dispatch.run_coder(args)
            self.assertEqual(
                dispatch.load_state(home, RUN_ID)["coder"]["protected_manifest"],
                state["coder"]["protected_manifest"],
            )


class ExactObjectFormatTests(unittest.TestCase):
    def test_sha256_repository_state_rejects_sha1_width_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = coder_record(home, state="dispatching")
            record["git_object_format"] = "sha256"
            record["coder"]["dispatch_baseline"][
                "pre_dispatch_work_branch_commit"
            ] = "a" * 40
            record["coder"]["dispatch_baseline"][
                "pre_dispatch_feature_branch_commit"
            ] = "b" * 40
            with self.assertRaisesRegex(dispatch.DispatchError, "object format"):
                dispatch.create_state(home, record)

    def test_prepare_persists_actual_sha256_repository_format(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            try:
                run_git(repository, "init", "--object-format=sha256", "-b", "feature/test")
            except AssertionError:
                self.skipTest("Git SHA-256 repositories are unavailable")
            run_git(repository, "config", "user.name", "Test User")
            run_git(repository, "config", "user.email", "test@example.invalid")
            (repository / "README.md").write_text("initial\n", encoding="utf-8")
            run_git(repository, "add", "README.md")
            run_git(repository, "commit", "-m", "initial")
            home = root / "home"
            dispatch.run_worktree_prepare(
                repository=repository,
                run_id=RUN_ID,
                bash_path=bash,
                hermes_home=home,
            )
            state = dispatch.load_state(home, RUN_ID)
            self.assertEqual(state["git_object_format"], "sha256")
            self.assertEqual(
                len(run_git(repository, "rev-parse", "HEAD")), 64
            )

    def test_diagnostics_redact_credential_assignments_before_truncation(self) -> None:
        session = "cursor-session-sensitive"
        diagnostic = (
            "AWS_SECRET_ACCESS_KEY=not-safe Authorization: Bearer bearer-secret "
            f"session={session} " + ("x" * dispatch.FAILURE_MAX_BYTES)
        )
        redacted = dispatch._redact_failure(diagnostic, session_ids=[session])
        self.assertNotIn("not-safe", redacted)
        self.assertNotIn("bearer-secret", redacted)
        self.assertNotIn(session, redacted)
        self.assertLessEqual(
            len(redacted.encode("utf-8")), dispatch.FAILURE_MAX_BYTES
        )


class ExactCliContractTests(unittest.TestCase):
    def test_state_parser_failure_uses_state_schema_and_actual_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dispatch.create_state(home, coder_record(home, generation=4))
            output = StringIO()
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), redirect_stdout(output):
                rc = dispatch.main(["state", "--run-id", RUN_ID])
            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 2)
            self.assertEqual(
                set(payload),
                {"status", "run_id", "state", "generation", "diagnostic"},
            )
            self.assertEqual(payload["generation"], 4)

    def test_worktree_prepare_runtime_failure_uses_prepare_schema_and_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dispatch.create_state(home, coder_record(home, generation=4))
            output = StringIO()
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch.object(
                dispatch, "find_bash5", return_value=Path("/bin/sh")
            ), redirect_stdout(output):
                rc = dispatch.main(
                    [
                        "worktree",
                        "--action",
                        "prepare",
                        "--repo",
                        str(home / "missing"),
                        "--run-id",
                        RUN_ID,
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 2)
            self.assertEqual(
                set(payload),
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
                },
            )
            self.assertEqual(payload["generation"], 4)


class ReviewerBlockedResumeTests(unittest.TestCase):
    def test_blocked_reviewer_with_matching_session_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            document = repository / "spec.md"
            document.write_text("# Spec\n", encoding="utf-8")
            run_git(repository, "add", "spec.md")
            run_git(repository, "commit", "-m", "spec")
            home = root / "home"
            record = reviewer_record(home, state="blocked", generation=3)
            record["repository"] = str(repository)
            record["cursor_session_id"] = "review-session-1"
            record["failure"] = "temporary failure"
            record["git_object_format"] = "sha1"
            record["reviewer"]["document"] = "spec.md"
            dispatch.create_state(home, record)
            args = review_args(
                repository,
                document,
                expected_generation=3,
                session="review-session-1",
            )
            fake_hermes = root / "hermes"
            fake_bash = root / "bash"
            fake_delegate = root / "review-delegate.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho reviewer-model\n")
            write_executable(fake_bash, '#!/bin/sh\nexec "$@"\n')
            write_executable(
                fake_delegate,
                "#!/bin/sh\n"
                "echo '{\"status\":\"REVIEWED\",\"reviewer\":\"cursor-reviewer\","
                "\"session_id\":\"review-session-1\",\"target\":\"spec\","
                "\"lenses\":[\"backend\"],\"report\":\"recovered\","
                "\"diagnostic\":\"\"}'\n",
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch.object(
                dispatch, "default_hermes_bin", return_value=str(fake_hermes)
            ), patch.object(
                dispatch, "find_bash5", return_value=fake_bash
            ), patch.object(
                dispatch, "review_delegate_script", return_value=fake_delegate
            ):
                result = dispatch.run_review(args)
            self.assertEqual(result["status"], "REVIEWED")
            self.assertGreater(result["generation"], 3)
            state = dispatch.load_state(home, RUN_ID)
            self.assertEqual(state["cursor_session_id"], "review-session-1")
            self.assertIsNone(state["failure"])


class BudgetTests(unittest.TestCase):
    def test_coder_budget_rejects_tenth_cursor_call_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = coder_record(home)
            record["git_object_format"] = "sha1"
            record["coder"]["budget"] = {
                "cursor_calls": 9,
                "controller_review_rounds": 0,
                "active_worker_seconds": 0.0,
            }
            record["coder"]["protected_manifest"] = None
            dispatch.create_state(home, record)
            with self.assertRaisesRegex(dispatch.DispatchError, "Cursor call"):
                dispatch.validate_coder_budget(
                    dispatch.load_state(home, RUN_ID),
                    max_retries=0,
                    correction=True,
                )

    def test_sixth_controller_review_round_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = coder_record(home, state="reviewing")
            record["git_object_format"] = "sha1"
            record["cursor_session_id"] = "session-1"
            record["coder"]["dispatch_baseline"] = {
                "pre_dispatch_work_branch_commit": "a" * 40,
                "pre_dispatch_feature_branch_commit": "a" * 40,
                "verified_worker_tree": "b" * 40,
                "verified_worker_index": "c" * 64,
            }
            record["coder"]["unintegrated_commits"] = ["d" * 40]
            record["coder"]["budget"] = {
                "cursor_calls": 5,
                "controller_review_rounds": 5,
                "active_worker_seconds": 1.0,
            }
            record["coder"]["protected_manifest"] = None
            dispatch.create_state(home, record)
            with self.assertRaisesRegex(dispatch.DispatchError, "review round"):
                dispatch.reserve_controller_review_round(home, RUN_ID, 1)

    def test_five_active_worker_hours_are_rejected_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = coder_record(home)
            record["coder"]["budget"]["active_worker_seconds"] = 5 * 60 * 60
            dispatch.create_state(home, record)
            with self.assertRaisesRegex(dispatch.DispatchError, "active worker"):
                dispatch.validate_coder_budget(
                    dispatch.load_state(home, RUN_ID),
                    max_retries=3,
                    correction=False,
                )

    def test_record_reviewing_generation_check_is_atomic(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            home = root / "home"
            prepared = dispatch.run_worktree_prepare(
                repository=repository,
                run_id=RUN_ID,
                bash_path=bash,
                hermes_home=home,
            )
            worktree = Path(prepared["worktree"])
            state = dispatch.transition_state(
                home, RUN_ID, prepared["generation"], "dispatching", {}
            )
            (worktree / "worker.txt").write_text("worker\n", encoding="utf-8")
            run_git(worktree, "add", "worker.txt")
            coder = dict(state["coder"])
            coder["dispatch_baseline"] = {
                **coder["dispatch_baseline"],
                "verified_worker_tree": run_git(worktree, "write-tree"),
                "verified_worker_index": dispatch._git_index_identity(worktree),
            }
            state = dispatch.update_state_fields(
                home,
                RUN_ID,
                state["generation"],
                {"coder": coder, "cursor_session_id": "session-1"},
            )
            run_git(worktree, "commit", "-m", "controller commit")
            barrier = threading.Barrier(2)
            results: list[dict[str, object]] = []
            errors: list[BaseException] = []

            def record() -> None:
                barrier.wait(timeout=5)
                try:
                    results.append(
                        dispatch.record_reviewing_state(
                            home, RUN_ID, state["generation"]
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=record) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["state"], "reviewing")
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], dispatch.DispatchError)


if __name__ == "__main__":
    unittest.main()
