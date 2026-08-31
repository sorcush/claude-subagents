#!/usr/bin/env python3
"""Regressions for the second final Slice B correction."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes.scripts import dispatch
from tests.test_hermes_correction import (
    RUN_ID,
    coder_record,
    init_repository,
    run_git,
    write_executable,
)


def integrated_record(
    root: Path,
    repository: Path,
    worktree: Path,
    work_branch: str,
) -> dict[str, object]:
    record = coder_record(
        root,
        state="integrated",
        repository=repository,
        worktree=worktree,
        work_branch=work_branch,
    )
    head = run_git(repository, "rev-parse", "HEAD")
    tree = run_git(repository, "rev-parse", "HEAD^{tree}")
    record["cursor_session_id"] = "first-task-session"
    coder = record["coder"]
    assert isinstance(coder, dict)
    coder["last_integrated_commit"] = head
    coder["dispatch_baseline"] = {
        "pre_dispatch_work_branch_commit": head,
        "pre_dispatch_feature_branch_commit": head,
        "verified_worker_tree": tree,
        "verified_worker_index": "c" * 64,
    }
    coder["budget"] = {
        "cursor_calls": 9,
        "controller_review_rounds": 5,
        "active_worker_seconds": 5 * 60 * 60,
    }
    coder["protected_manifest"] = {
        "git_control_sha256": "d" * 64,
        "controller_source_sha256": "e" * 64,
    }
    return record


class ReviewedTipReconciliationTests(unittest.TestCase):
    def test_integrated_pending_tip_blocks_if_work_branch_has_newer_commit(self) -> None:
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
            (worktree / "reviewed.txt").write_text("reviewed\n", encoding="utf-8")
            run_git(worktree, "add", "reviewed.txt")
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
            run_git(worktree, "commit", "-m", "reviewed commit")
            pending = run_git(worktree, "rev-parse", "HEAD")
            reviewing = dispatch.reconcile_coder_state(home, RUN_ID)
            coder = dict(reviewing["coder"])
            coder["pending_commit"] = pending
            dispatch.transition_state(
                home,
                RUN_ID,
                reviewing["generation"],
                "integration_pending",
                {"coder": coder},
            )
            (worktree / "unreviewed.txt").write_text("unreviewed\n", encoding="utf-8")
            run_git(worktree, "add", "unreviewed.txt")
            run_git(worktree, "commit", "-m", "unreviewed extra commit")
            run_git(repository, "merge", "--ff-only", pending)

            blocked = dispatch.reconcile_coder_state(home, RUN_ID)
            self.assertEqual(blocked["state"], "blocked")
            self.assertIn("work branch", blocked["failure"])
            self.assertEqual(blocked["coder"]["pending_commit"], pending)

            task = root / "task.md"
            task.write_text("must not dispatch\n", encoding="utf-8")
            args = argparse.Namespace(
                repo=str(repository),
                cwd=str(worktree),
                run_id=RUN_ID,
                task_file=str(task),
                expected_generation=blocked["generation"],
                verify_cmd="true",
                session="session-1",
                max_retries=0,
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                with self.assertRaisesRegex(dispatch.DispatchError, "integration"):
                    dispatch.run_coder(args)
            with self.assertRaises(dispatch.DispatchError):
                dispatch.transition_state(
                    home,
                    RUN_ID,
                    blocked["generation"],
                    "integration_pending",
                    {},
                )


class PerTaskResetTests(unittest.TestCase):
    def test_integrated_to_dispatching_resets_task_scoped_identity_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            worktree = root / f"repository-feature-test-hermes-{RUN_ID}-work"
            work_branch = f"feature-test-hermes-{RUN_ID}-work"
            run_git(repository, "worktree", "add", "-b", work_branch, str(worktree))
            home = root / "home"
            dispatch.create_state(
                home,
                integrated_record(root, repository, worktree, work_branch),
            )
            reset = dispatch.transition_state(
                home, RUN_ID, 1, "dispatching", {}
            )
            self.assertIsNone(reset["cursor_session_id"])
            self.assertEqual(
                reset["coder"]["budget"],
                {
                    "cursor_calls": 0,
                    "controller_review_rounds": 0,
                    "active_worker_seconds": 0.0,
                },
            )
            self.assertIsNone(reset["coder"]["protected_manifest"])
            self.assertIsNone(
                reset["coder"]["dispatch_baseline"]["verified_worker_tree"]
            )
            self.assertIsNone(
                reset["coder"]["dispatch_baseline"]["verified_worker_index"]
            )

    def test_new_task_rejects_prior_task_session_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            worktree = root / f"repository-feature-test-hermes-{RUN_ID}-work"
            work_branch = f"feature-test-hermes-{RUN_ID}-work"
            run_git(repository, "worktree", "add", "-b", work_branch, str(worktree))
            home = root / "home"
            dispatch.create_state(
                home,
                integrated_record(root, repository, worktree, work_branch),
            )
            task = root / "task.md"
            task.write_text("second task\n", encoding="utf-8")
            args = argparse.Namespace(
                repo=str(repository),
                cwd=str(worktree),
                run_id=RUN_ID,
                task_file=str(task),
                expected_generation=1,
                verify_cmd="true",
                session="first-task-session",
                max_retries=0,
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                with self.assertRaisesRegex(dispatch.DispatchError, "must not reuse"):
                    dispatch.run_coder(args)
            self.assertEqual(dispatch.load_state(home, RUN_ID)["state"], "integrated")

    def test_exhausted_first_task_budget_does_not_block_second_task(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            worktree = root / f"repository-feature-test-hermes-{RUN_ID}-work"
            work_branch = f"feature-test-hermes-{RUN_ID}-work"
            run_git(repository, "worktree", "add", "-b", work_branch, str(worktree))
            home = root / "home"
            dispatch.create_state(
                home,
                integrated_record(root, repository, worktree, work_branch),
            )
            task = root / "task.md"
            task.write_text("create second.txt\n", encoding="utf-8")
            fake_hermes = root / "hermes"
            delegate = root / "code-delegate.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho coder-model\n")
            write_executable(
                delegate,
                f"""#!/bin/sh
printf 'second\n' > {str(worktree / 'second.txt')!r}
printf '%s\n' '{{"status":"DONE","coder":"cursor-coder","session_id":"second-task-session","attempts":0,"verified":true,"changed":true,"commit_id":"","result":"done","verify_output":"ok"}}'
""",
            )
            args = argparse.Namespace(
                repo=str(repository),
                cwd=str(worktree),
                run_id=RUN_ID,
                task_file=str(task),
                expected_generation=1,
                verify_cmd="test -f second.txt",
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
            self.assertEqual(result["session_id"], "second-task-session")
            state = dispatch.load_state(home, RUN_ID)
            self.assertEqual(state["coder"]["budget"]["cursor_calls"], 1)
            self.assertEqual(state["coder"]["budget"]["controller_review_rounds"], 0)

    def test_rejected_prior_session_does_not_poison_new_task_retry(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            worktree = root / f"repository-feature-test-hermes-{RUN_ID}-work"
            work_branch = f"feature-test-hermes-{RUN_ID}-work"
            run_git(repository, "worktree", "add", "-b", work_branch, str(worktree))
            home = root / "home"
            dispatch.create_state(
                home,
                integrated_record(root, repository, worktree, work_branch),
            )
            task = root / "task.md"
            task.write_text("second task\n", encoding="utf-8")
            fake_hermes = root / "hermes"
            delegate = root / "code-delegate.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho coder-model\n")
            write_executable(
                delegate,
                "#!/bin/sh\n"
                "printf '%s\\n' '{\"status\":\"DONE\",\"coder\":\"cursor-coder\","
                "\"session_id\":\"first-task-session\",\"attempts\":0,"
                "\"verified\":true,\"changed\":false,\"commit_id\":\"\","
                "\"result\":\"done\",\"verify_output\":\"ok\"}'\n",
            )
            args = argparse.Namespace(
                repo=str(repository),
                cwd=str(worktree),
                run_id=RUN_ID,
                task_file=str(task),
                expected_generation=1,
                verify_cmd="true",
                session=None,
                max_retries=3,
            )
            patches = (
                patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False),
                patch.object(dispatch, "default_hermes_bin", return_value=str(fake_hermes)),
                patch.object(dispatch, "find_bash5", return_value=bash),
                patch.object(dispatch, "code_delegate_script", return_value=delegate),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                blocked = dispatch.run_coder(args)
            self.assertEqual(blocked["status"], "BLOCKED")
            state = dispatch.load_state(home, RUN_ID)
            self.assertEqual(state["state"], "blocked")
            self.assertIsNone(state["cursor_session_id"])

            write_executable(
                delegate,
                "#!/bin/sh\n"
                "printf '%s\\n' '{\"status\":\"DONE\",\"coder\":\"cursor-coder\","
                "\"session_id\":\"second-task-session\",\"attempts\":0,"
                "\"verified\":true,\"changed\":false,\"commit_id\":\"\","
                "\"result\":\"done\",\"verify_output\":\"ok\"}'\n",
            )
            args.expected_generation = state["generation"]
            with patches[0], patches[1], patches[2], patches[3]:
                retried = dispatch.run_coder(args)
            self.assertEqual(retried["status"], "DONE")
            self.assertEqual(retried["session_id"], "second-task-session")


class TransactionalPrepareTests(unittest.TestCase):
    def test_preparing_identity_is_durable_before_shell_side_effect(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            home = root / "home"

            def inspect_then_fail(**kwargs):
                state = dispatch.load_state(home, RUN_ID)
                self.assertEqual(state["state"], "preparing")
                self.assertTrue(state["coder"]["worktree"])
                self.assertTrue(state["coder"]["work_branch"])
                self.assertEqual(
                    kwargs["env"]["CSC_EXPECTED_FEATURE"],
                    state["coder"]["feature_branch"],
                )
                self.assertEqual(
                    kwargs["env"]["CSC_EXPECTED_WORKTREE"],
                    state["coder"]["worktree"],
                )
                self.assertEqual(
                    kwargs["env"]["CSC_EXPECTED_WORK_BRANCH"],
                    state["coder"]["work_branch"],
                )
                raise dispatch.DispatchError("simulated pre-create crash")

            with patch.object(
                dispatch, "_run_script_json", side_effect=inspect_then_fail
            ):
                with self.assertRaisesRegex(dispatch.DispatchError, "simulated"):
                    dispatch.run_worktree_prepare(
                        repository=repository,
                        run_id=RUN_ID,
                        bash_path=bash,
                        hermes_home=home,
                    )
            state = dispatch.load_state(home, RUN_ID)
            self.assertEqual(state["state"], "preparing")
            self.assertGreater(state["generation"], 1)
            self.assertFalse(Path(state["coder"]["worktree"]).exists())
            self.assertEqual(
                dispatch.reconcile_state(home, RUN_ID)["state"], "preparing"
            )
            recovered = dispatch.run_worktree_prepare(
                repository=repository,
                run_id=RUN_ID,
                bash_path=bash,
                hermes_home=home,
            )
            self.assertEqual(recovered["status"], "READY")
            self.assertEqual(dispatch.load_state(home, RUN_ID)["state"], "prepared")

    def test_branch_only_crash_reconciles_to_prepared(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            home = root / "home"

            def create_branch_then_fail(**_kwargs):
                state = dispatch.load_state(home, RUN_ID)
                run_git(
                    repository,
                    "branch",
                    state["coder"]["work_branch"],
                    state["coder"]["feature_branch"],
                )
                raise dispatch.DispatchError("branch-created crash")

            with patch.object(
                dispatch, "_run_script_json", side_effect=create_branch_then_fail
            ):
                with self.assertRaises(dispatch.DispatchError):
                    dispatch.run_worktree_prepare(
                        repository=repository,
                        run_id=RUN_ID,
                        bash_path=bash,
                        hermes_home=home,
                    )
            recovered = dispatch.run_worktree_prepare(
                repository=repository,
                run_id=RUN_ID,
                bash_path=bash,
                hermes_home=home,
            )
            self.assertEqual(recovered["status"], "READY")
            self.assertEqual(dispatch.load_state(home, RUN_ID)["state"], "prepared")
            self.assertTrue(Path(recovered["worktree"]).is_dir())

    def test_worktree_created_before_origin_marker_reconciles_to_prepared(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            home = root / "home"

            def create_worktree_then_fail(**_kwargs):
                state = dispatch.load_state(home, RUN_ID)
                run_git(
                    repository,
                    "worktree",
                    "add",
                    "-b",
                    state["coder"]["work_branch"],
                    state["coder"]["worktree"],
                    state["coder"]["feature_branch"],
                )
                raise dispatch.DispatchError("worktree-created crash")

            with patch.object(
                dispatch, "_run_script_json", side_effect=create_worktree_then_fail
            ):
                with self.assertRaises(dispatch.DispatchError):
                    dispatch.run_worktree_prepare(
                        repository=repository,
                        run_id=RUN_ID,
                        bash_path=bash,
                        hermes_home=home,
                    )
            state = dispatch.load_state(home, RUN_ID)
            git_dir = Path(
                run_git(
                    Path(state["coder"]["worktree"]),
                    "rev-parse",
                    "--absolute-git-dir",
                )
            )
            self.assertFalse((git_dir / "csc-origin-feature").exists())
            recovered = dispatch.run_worktree_prepare(
                repository=repository,
                run_id=RUN_ID,
                bash_path=bash,
                hermes_home=home,
            )
            self.assertEqual(recovered["status"], "READY")
            self.assertEqual(
                (git_dir / "csc-origin-feature").read_text(encoding="utf-8").strip(),
                "feature/test",
            )

    def test_crash_after_shell_creation_reuses_durable_identity(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            home = root / "home"
            real_transition = dispatch._transition_state_unlocked
            failed = False

            def fail_prepared_transition(*args, **kwargs):
                nonlocal failed
                if not failed and args[3] == "prepared":
                    failed = True
                    raise dispatch.DispatchError("before prepared update")
                return real_transition(*args, **kwargs)

            with patch.object(
                dispatch,
                "_transition_state_unlocked",
                side_effect=fail_prepared_transition,
            ):
                with self.assertRaisesRegex(dispatch.DispatchError, "prepared update"):
                    dispatch.run_worktree_prepare(
                        repository=repository,
                        run_id=RUN_ID,
                        bash_path=bash,
                        hermes_home=home,
                    )
            state = dispatch.load_state(home, RUN_ID)
            self.assertEqual(state["state"], "preparing")
            self.assertTrue(Path(state["coder"]["worktree"]).is_dir())
            recovered = dispatch.run_worktree_prepare(
                repository=repository,
                run_id=RUN_ID,
                bash_path=bash,
                hermes_home=home,
            )
            self.assertEqual(recovered["worktree"], state["coder"]["worktree"])
            self.assertEqual(recovered["work_branch"], state["coder"]["work_branch"])
            self.assertEqual(dispatch.load_state(home, RUN_ID)["state"], "prepared")

    def test_created_worktree_cli_failure_returns_durable_recovery_identity(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            home = root / "home"
            real_transition = dispatch._transition_state_unlocked

            def fail_prepared_transition(*args, **kwargs):
                if args[3] == "prepared":
                    raise dispatch.DispatchError("before prepared update")
                return real_transition(*args, **kwargs)

            output = StringIO()
            with patch.dict(
                os.environ, {"HERMES_HOME": str(home)}, clear=False
            ), patch.object(
                dispatch, "find_bash5", return_value=bash
            ), patch.object(
                dispatch,
                "_transition_state_unlocked",
                side_effect=fail_prepared_transition,
            ), redirect_stdout(output):
                rc = dispatch.main(
                    [
                        "worktree",
                        "--action",
                        "prepare",
                        "--repo",
                        str(repository),
                        "--run-id",
                        RUN_ID,
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 2)
            self.assertTrue(payload["worktree"])
            self.assertTrue(payload["work_branch"])
            self.assertTrue(payload["feature_branch"])
            self.assertTrue(Path(payload["worktree"]).is_dir())
            self.assertEqual(
                payload["generation"], dispatch.load_state(home, RUN_ID)["generation"]
            )


class ExactCliOptionsTests(unittest.TestCase):
    def test_invalid_action_option_combinations_are_rejected_with_exact_schema(self) -> None:
        state_fields = {"status", "run_id", "state", "generation", "diagnostic"}
        show_fields = {"status", "run_id", "record", "generation", "diagnostic"}
        prune_fields = {"status", "removed", "diagnostic"}
        prepare_fields = {
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
        remove_fields = {
            "status",
            "run_id",
            "work_branch",
            "unmerged",
            "generation",
            "diagnostic",
        }
        cases = [
            (
                ["worktree", "--action", "prepare", "--repo", "/repo", "--run-id", RUN_ID, "--expected-generation", "4"],
                prepare_fields,
            ),
            (
                ["worktree", "--action", "remove", "--repo", "/repo", "--run-id", RUN_ID],
                remove_fields,
            ),
            (["state", "--action", "prune", "--run-id", RUN_ID], prune_fields),
            (["state", "--action", "prune", "--expected-generation", "4"], prune_fields),
            (["state", "--action", "prune", "--commit", "a" * 40], prune_fields),
            (["state", "--action", "show"], show_fields),
            (["state", "--action", "show", "--run-id", RUN_ID, "--expected-generation", "4"], show_fields),
            (["state", "--action", "show", "--run-id", RUN_ID, "--commit", "a" * 40], show_fields),
            (["state", "--action", "record-reviewing", "--expected-generation", "4"], state_fields),
            (["state", "--action", "record-reviewing", "--run-id", RUN_ID], state_fields),
            (["state", "--action", "record-reviewing", "--run-id", RUN_ID, "--expected-generation", "4", "--commit", "a" * 40], state_fields),
            (["state", "--action", "record-integration", "--expected-generation", "4", "--commit", "a" * 40], state_fields),
            (["state", "--action", "record-integration", "--run-id", RUN_ID, "--expected-generation", "4"], state_fields),
            (["state", "--action", "record-integration", "--run-id", RUN_ID, "--commit", "a" * 40], state_fields),
            (["state", "--action", "block", "--expected-generation", "4"], state_fields),
            (["state", "--action", "block", "--run-id", RUN_ID], state_fields),
            (["state", "--action", "block", "--run-id", RUN_ID, "--expected-generation", "4", "--commit", "a" * 40], state_fields),
            (["state", "--action", "complete", "--expected-generation", "4"], state_fields),
            (["state", "--action", "complete", "--run-id", RUN_ID], state_fields),
            (["state", "--action", "complete", "--run-id", RUN_ID, "--expected-generation", "4", "--commit", "a" * 40], state_fields),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dispatch.create_state(home, coder_record(home, generation=4))
            for argv, fields in cases:
                with self.subTest(argv=argv):
                    output = StringIO()
                    with patch.dict(
                        os.environ, {"HERMES_HOME": str(home)}, clear=False
                    ), redirect_stdout(output):
                        rc = dispatch.main(argv)
                    self.assertEqual(rc, 2)
                    self.assertEqual(set(json.loads(output.getvalue())), fields)


if __name__ == "__main__":
    unittest.main()
