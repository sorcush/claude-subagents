#!/usr/bin/env python3
"""Sensitive contracts for the completed Hermes vertical slice."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tests.test_hermes_correction import (
    RUN_ID,
    coder_record,
    init_repository,
    reviewer_record,
    run_git,
    write_executable,
)
from hermes.scripts import dispatch


class VerificationHomeTests(unittest.TestCase):
    def test_created_verification_home_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_parent = root / "real"
            real_parent.mkdir()
            logical_parent = root / "logical"
            logical_parent.symlink_to(real_parent, target_is_directory=True)
            created = real_parent / "verify"
            created.mkdir()
            with patch.object(
                dispatch.tempfile,
                "mkdtemp",
                return_value=str(logical_parent / "verify"),
            ):
                verify_home = dispatch.create_verification_home()
            self.assertEqual(verify_home, created.resolve())
            self.assertFalse(any(part.is_symlink() for part in [verify_home, *verify_home.parents]))


class CompletionSafetyTests(unittest.TestCase):
    def test_reviewer_completion_rejects_dispatching_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dispatch.create_state(home, reviewer_record(home, state="dispatching"))
            with self.assertRaisesRegex(dispatch.DispatchError, "active"):
                dispatch.finalize_complete_state(home, RUN_ID, 1)
            self.assertEqual(dispatch.load_state(home, RUN_ID)["state"], "dispatching")

    def test_coder_completion_requires_absent_worktree_and_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = coder_record(home, state="integrated")
            record["coder"]["dispatch_baseline"] = {
                "pre_dispatch_work_branch_commit": "a" * 40,
                "pre_dispatch_feature_branch_commit": "b" * 40,
                "verified_worker_tree": "c" * 40,
                "verified_worker_index": "d" * 64,
            }
            record["cursor_session_id"] = "session-1"
            record["coder"]["last_integrated_commit"] = "e" * 40
            Path(record["coder"]["worktree"]).mkdir(parents=True)
            dispatch.create_state(home, record)
            with self.assertRaisesRegex(dispatch.DispatchError, "worktree"):
                dispatch.finalize_complete_state(home, RUN_ID, 1)
            self.assertEqual(dispatch.load_state(home, RUN_ID)["state"], "integrated")


class ExactStateContractTests(unittest.TestCase):
    def test_git_identity_widths_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = coder_record(home, state="dispatching")
            baseline = record["coder"]["dispatch_baseline"]
            baseline["pre_dispatch_work_branch_commit"] = "a" * 41
            baseline["pre_dispatch_feature_branch_commit"] = "b" * 40
            with self.assertRaisesRegex(
                dispatch.DispatchError, "baseline|object format"
            ):
                dispatch.create_state(home, record)

            record = coder_record(home, state="dispatching")
            baseline = record["coder"]["dispatch_baseline"]
            baseline["pre_dispatch_work_branch_commit"] = "a" * 40
            baseline["pre_dispatch_feature_branch_commit"] = "b" * 40
            baseline["verified_worker_tree"] = "c" * 40
            baseline["verified_worker_index"] = "d" * 40
            with self.assertRaisesRegex(dispatch.DispatchError, "index"):
                dispatch.create_state(home, record)

    def test_reviewed_state_requires_session_and_snapshot_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = reviewer_record(home, state="reviewed")
            record["cursor_session_id"] = None
            with self.assertRaisesRegex(dispatch.DispatchError, "session"):
                dispatch.create_state(home, record)

    def test_coder_review_lifecycle_requires_verified_session_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = coder_record(home, state="reviewing")
            record["coder"]["dispatch_baseline"] = {
                "pre_dispatch_work_branch_commit": "a" * 40,
                "pre_dispatch_feature_branch_commit": "a" * 40,
                "verified_worker_tree": "b" * 40,
                "verified_worker_index": "c" * 64,
            }
            with self.assertRaisesRegex(dispatch.DispatchError, "session"):
                dispatch.create_state(home, record)


class RedactionContractTests(unittest.TestCase):
    def test_redacts_configured_secret_values_without_key_assignment(self) -> None:
        with patch.dict(
            os.environ,
            {"PRIVATE_ACCESS_TOKEN": "configured-secret-value"},
            clear=False,
        ):
            redacted = dispatch._redact_failure(
                "worker echoed configured-secret-value in prose"
            )
        self.assertNotIn("configured-secret-value", redacted)


class ExactCliFailureTests(unittest.TestCase):
    def test_worktree_failure_uses_worktree_remove_schema_and_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = coder_record(home)
            dispatch.create_state(home, record)
            output = StringIO()
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch.object(
                dispatch, "find_bash5", return_value=Path("/bin/false")
            ), redirect_stdout(output):
                rc = dispatch.main(
                    [
                        "worktree",
                        "--action",
                        "remove",
                        "--repo",
                        str(record["repository"]),
                        "--run-id",
                        RUN_ID,
                        "--expected-generation",
                        "2",
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 2)
            self.assertEqual(set(payload), dispatch.WORKTREE_REMOVE_FIELDS)
            self.assertEqual(payload["generation"], 1)


class ReviewCleanupTests(unittest.TestCase):
    def test_unchanged_clone_cleanup_failure_is_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clone = dispatch.ReviewClone(
                path=Path(tmp) / "snapshot",
                repository=Path(tmp),
                before_manifest={},
                documents=[],
            )
            clone.path.mkdir()
            with patch.object(dispatch, "_rmtree_bounded", side_effect=OSError("busy")):
                with self.assertRaisesRegex(dispatch.DispatchError, "remove"):
                    dispatch.cleanup_review_clone(clone, changed=False)


class CoderCliTests(unittest.TestCase):
    def test_code_parser_exposes_complete_interface(self) -> None:
        parser = dispatch._build_parser()
        args = parser.parse_args(
            [
                "code",
                "--repo",
                "/repo",
                "--cwd",
                "/worktree",
                "--run-id",
                RUN_ID,
                "--task-file",
                "/task",
                "--expected-generation",
                "1",
                "--verify-cmd",
                "python3 -m unittest",
                "--max-retries",
                "3",
            ]
        )
        self.assertEqual(args.handler, "_cmd_code")
        self.assertEqual(args.max_retries, 3)

    def test_code_dispatch_uses_worktree_and_records_verified_identity(self) -> None:
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
            task = root / "task.md"
            task.write_text("Create worker.txt\n", encoding="utf-8")
            fake_hermes = root / "hermes"
            delegate = root / "code-delegate.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho coder-model\n")
            write_executable(
                delegate,
                """#!/bin/sh
cwd=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--cwd" ]; then cwd="$2"; shift 2; continue; fi
  shift
done
printf 'worker\n' > "$cwd/worker.txt"
printf '%s\n' '{"status":"DONE","coder":"cursor-coder","session_id":"session-1","attempts":0,"verified":true,"changed":true,"commit_id":"","result":"done","verify_output":"ok"}'
""",
            )
            args = argparse.Namespace(
                repo=str(repository),
                cwd=prepared["worktree"],
                run_id=RUN_ID,
                task_file=str(task),
                expected_generation=prepared["generation"],
                verify_cmd="test -f worker.txt",
                session=None,
                max_retries=3,
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch.object(
                dispatch, "default_hermes_bin", return_value=str(fake_hermes)
            ), patch.object(dispatch, "find_bash5", return_value=bash), patch.object(
                dispatch, "code_delegate_script", return_value=delegate
            ):
                result = dispatch.run_coder(args)
            self.assertEqual(result["status"], "DONE")
            self.assertTrue(result["verified"])
            state = dispatch.load_state(home, RUN_ID)
            self.assertEqual(state["state"], "dispatching")
            self.assertEqual(state["cursor_session_id"], "session-1")
            self.assertEqual(
                state["coder"]["dispatch_baseline"]["verified_worker_tree"],
                run_git(Path(prepared["worktree"]), "write-tree"),
            )
            self.assertEqual(
                len(state["coder"]["dispatch_baseline"]["verified_worker_index"]), 64
            )
            run_git(Path(prepared["worktree"]), "commit", "-m", "controller commit")
            reviewing = dispatch.reconcile_coder_state(home, RUN_ID)
            self.assertEqual(reviewing["state"], "reviewing")
            args_log = root / "args.log"
            write_executable(
                delegate,
                f"""#!/bin/sh
printf '%s\n' "$@" > {str(args_log)!r}
cwd=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--cwd" ]; then cwd="$2"; shift 2; continue; fi
  shift
done
printf 'correction\n' > "$cwd/correction.txt"
printf '%s\n' '{{"status":"DONE","coder":"cursor-coder","session_id":"session-1","attempts":0,"verified":true,"changed":true,"commit_id":"","result":"corrected","verify_output":"ok"}}'
""",
            )
            args.expected_generation = reviewing["generation"]
            args.session = "session-1"
            args.max_retries = 0
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch.object(
                dispatch, "default_hermes_bin", return_value=str(fake_hermes)
            ), patch.object(dispatch, "find_bash5", return_value=bash), patch.object(
                dispatch, "code_delegate_script", return_value=delegate
            ):
                correction = dispatch.run_coder(args)
            self.assertEqual(correction["session_id"], "session-1")
            corrected_state = dispatch.load_state(home, RUN_ID)
            self.assertEqual(corrected_state["coder"]["budget"]["cursor_calls"], 2)
            self.assertEqual(
                corrected_state["coder"]["budget"]["controller_review_rounds"], 1
            )
            self.assertGreater(
                corrected_state["coder"]["budget"]["active_worker_seconds"], 0
            )
            logged = args_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(logged[logged.index("--session") + 1], "session-1")
            self.assertEqual(logged[logged.index("--max-retries") + 1], "0")

    def test_code_dispatch_blocks_protected_git_and_controller_mutations(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        actions = {
            "autonomous commit": 'git -C "$cwd" commit --allow-empty -m worker >/dev/null',
            "new ref": 'git -C "$cwd" branch worker-created-ref',
            "repository config": 'git -C "$cwd" config worker.changed true',
            "hook": 'common=$(git -C "$cwd" rev-parse --git-common-dir); printf x > "$common/hooks/pre-commit"',
            "worktree registration": 'git -C "$cwd" worktree add -q -b worker-extra "$cwd-extra"',
            "controller write": (
                'controller=$(git -C "$cwd" worktree list --porcelain | '
                'awk \'/^worktree / { print substr($0, 10); exit }\'); '
                'printf changed > "$controller/controller-write.txt"'
            ),
            "reachable object": (
                'head=$(git -C "$cwd" rev-parse HEAD); '
                'common=$(git -C "$cwd" rev-parse --git-common-dir); '
                'prefix=$(printf %s "$head" | cut -c1-2); '
                'suffix=$(printf %s "$head" | cut -c3-); '
                'rm -f "$common/objects/$prefix/$suffix"'
            ),
        }
        for label, action in actions.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repository = init_repository(root)
                home = root / "home"
                prepared = dispatch.run_worktree_prepare(
                    repository=repository,
                    run_id=RUN_ID,
                    bash_path=bash,
                    hermes_home=home,
                )
                task = root / "task.md"
                task.write_text("bounded\n", encoding="utf-8")
                fake_hermes = root / "hermes"
                delegate = root / "code-delegate.sh"
                write_executable(fake_hermes, "#!/bin/sh\necho coder-model\n")
                write_executable(
                    delegate,
                    f"""#!/bin/sh
cwd=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--cwd" ]; then cwd="$2"; shift 2; continue; fi
  shift
done
{action}
printf '%s\n' '{{"status":"DONE","coder":"cursor-coder","session_id":"session-1","attempts":0,"verified":true,"changed":true,"commit_id":"","result":"done","verify_output":"ok"}}'
""",
                )
                args = argparse.Namespace(
                    repo=str(repository),
                    cwd=prepared["worktree"],
                    run_id=RUN_ID,
                    task_file=str(task),
                    expected_generation=prepared["generation"],
                    verify_cmd="true",
                    session=None,
                    max_retries=3,
                )
                with patch.dict(
                    os.environ, {"HERMES_HOME": str(home)}, clear=False
                ), patch.object(
                    dispatch, "default_hermes_bin", return_value=str(fake_hermes)
                ), patch.object(
                    dispatch, "find_bash5", return_value=bash
                ), patch.object(
                    dispatch, "code_delegate_script", return_value=delegate
                ):
                    result = dispatch.run_coder(args)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(dispatch.load_state(home, RUN_ID)["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
