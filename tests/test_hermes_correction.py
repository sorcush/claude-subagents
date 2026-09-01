#!/usr/bin/env python3
"""Correction-3 behavioral contracts for Hermes durable state and review."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes.scripts import dispatch


RUN_ID = "0123456789abcdef"
OTHER_RUN_ID = "fedcba9876543210"
BASELINE_KEYS = {
    "pre_dispatch_work_branch_commit",
    "pre_dispatch_feature_branch_commit",
    "verified_worker_tree",
    "verified_worker_index",
}


def run_git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def init_repository(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir()
    run_git(repository, "init", "-b", "feature/test")
    run_git(repository, "config", "user.email", "test@example.com")
    run_git(repository, "config", "user.name", "Test User")
    (repository / "README.md").write_text("initial\n", encoding="utf-8")
    run_git(repository, "add", "README.md")
    run_git(repository, "commit", "-m", "initial")
    return repository


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def coder_record(
    root: Path,
    *,
    state: str = "prepared",
    generation: int = 1,
    repository: Path | None = None,
    worktree: Path | None = None,
    work_branch: str | None = None,
) -> dict[str, object]:
    repo = repository or root / "repo"
    work = worktree or root / f"repo-feature-test-hermes-{RUN_ID}-work"
    branch = work_branch or f"feature-test-hermes-{RUN_ID}-work"
    return {
        "schema_version": 1,
        "git_object_format": "sha1",
        "run_id": RUN_ID,
        "role": "coder",
        "state": state,
        "generation": generation,
        "repository": str(repo),
        "cursor_session_id": None,
        "coder": {
            "feature_branch": "feature/test",
            "worktree": str(work),
            "work_branch": branch,
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
        "failure": "blocked" if state == "blocked" else None,
        "updated_at": "2026-08-31T12:00:00Z",
    }


def reviewer_record(
    root: Path,
    *,
    state: str = "dispatching",
    generation: int = 1,
    snapshot_outcome: str | None = None,
    snapshot_path: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "git_object_format": "sha1",
        "run_id": RUN_ID,
        "role": "reviewer",
        "state": state,
        "generation": generation,
        "repository": str(root / "repo"),
        "cursor_session_id": (
            "review-session-1" if state in {"reviewed", "complete"} else None
        ),
        "coder": None,
        "reviewer": {
            "target": "spec",
            "document": "docs/spec.md",
            "specification": None,
            "lenses": ["backend"],
            "snapshot_outcome": (
                snapshot_outcome
                if snapshot_outcome is not None
                else "unchanged"
                if state in {"reviewed", "complete"}
                else None
            ),
            "snapshot_path": snapshot_path,
        },
        "failure": "blocked" if state == "blocked" else None,
        "updated_at": "2026-08-31T12:00:00+00:00",
    }


def review_args(repository: Path, document: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "target": "spec",
        "run_id": RUN_ID,
        "doc_file": str(document),
        "spec_file": None,
        "lenses": "backend",
        "expected_generation": None,
        "session": None,
        "repo": str(repository),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class CompleteTombstoneTests(unittest.TestCase):
    def test_complete_retains_tombstone_and_repeat_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = reviewer_record(home, state="reviewed")
            dispatch.create_state(home, record)

            first = dispatch.finalize_complete_state(home, RUN_ID, 1)
            second = dispatch.finalize_complete_state(
                home, RUN_ID, first["generation"]
            )

            self.assertEqual(first["state"], "complete")
            self.assertEqual(second, first)
            self.assertTrue(dispatch._state_path(home, RUN_ID).is_file())
            self.assertTrue(dispatch._lock_path(home, RUN_ID).is_file())

    def test_remove_without_state_refuses_to_fabricate_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repository = init_repository(Path(tmp))
            with self.assertRaisesRegex(dispatch.DispatchError, "missing run state"):
                dispatch.run_worktree_remove(
                    repository=repository,
                    run_id=RUN_ID,
                    expected_generation=9,
                    bash_path=Path("/bin/false"),
                    hermes_home=home,
                )

    def test_prune_removes_old_tombstone_but_keeps_stable_lock_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = reviewer_record(home, state="complete")
            record["updated_at"] = "2026-07-01T12:00:00Z"
            dispatch.create_state(home, record)
            lock_path = dispatch._lock_path(home, RUN_ID)
            before = lock_path.stat().st_ino

            removed = dispatch.prune_state(
                home, datetime(2026, 8, 31, tzinfo=timezone.utc)
            )

            self.assertEqual(removed, [RUN_ID])
            self.assertFalse(dispatch._state_path(home, RUN_ID).exists())
            self.assertEqual(lock_path.stat().st_ino, before)


class ExactStateValidationTests(unittest.TestCase):
    def test_rejects_boolean_generation_and_expected_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = coder_record(home)
            record["generation"] = True
            with self.assertRaisesRegex(dispatch.DispatchError, "generation"):
                dispatch.create_state(home, record)

            valid = coder_record(home)
            dispatch.create_state(home, valid)
            with self.assertRaisesRegex(dispatch.DispatchError, "generation"):
                dispatch.transition_state(
                    home, RUN_ID, True, "dispatching", {}
                )

    def test_rejects_naive_and_non_rfc3339_timestamps(self) -> None:
        for timestamp in ("2026-08-31T12:00:00", "2026-08-31", "not-a-date"):
            with self.subTest(timestamp=timestamp), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                record = coder_record(home)
                record["updated_at"] = timestamp
                with self.assertRaisesRegex(dispatch.DispatchError, "timestamp"):
                    dispatch.create_state(home, record)

    def test_requires_exact_dispatch_baseline_schema_and_sha_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = coder_record(home)
            baseline = record["coder"]["dispatch_baseline"]  # type: ignore[index]
            self.assertEqual(set(baseline), BASELINE_KEYS)
            baseline["extra"] = None
            with self.assertRaisesRegex(
                dispatch.DispatchError, "baseline|object format"
            ):
                dispatch.create_state(home, record)

            record = coder_record(home)
            record["coder"]["dispatch_baseline"][  # type: ignore[index]
                "verified_worker_tree"
            ] = "not-a-sha"
            with self.assertRaisesRegex(
                dispatch.DispatchError, "baseline|object format"
            ):
                dispatch.create_state(home, record)

    def test_requires_run_id_in_coder_branch_and_worktree_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record = coder_record(
                home,
                worktree=home / f"repo-hermes-{OTHER_RUN_ID}-work",
                work_branch=f"feature-hermes-{OTHER_RUN_ID}-work",
            )
            with self.assertRaisesRegex(dispatch.DispatchError, "run id"):
                dispatch.create_state(home, record)

    def test_rejects_malformed_coder_commit_and_branch_fields(self) -> None:
        mutations = [
            lambda coder: coder.__setitem__("pending_commit", "not-a-sha"),
            lambda coder: coder.__setitem__("last_integrated_commit", ""),
            lambda coder: coder.__setitem__("unintegrated_commits", ["bad"]),
            lambda coder: coder.__setitem__("feature_branch", "-unsafe"),
        ]
        for mutate in mutations:
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                record = coder_record(home)
                mutate(record["coder"])  # type: ignore[arg-type]
                with self.assertRaises(dispatch.DispatchError):
                    dispatch.create_state(home, record)

    def test_reviewer_field_combinations_fail_closed(self) -> None:
        mutations = [
            ("bad outcome", lambda p: p.__setitem__("snapshot_outcome", "maybe")),
            ("path without change", lambda p: p.__setitem__("snapshot_path", str(Path("/tmp/x")))),
            ("absolute document", lambda p: p.__setitem__("document", "/tmp/spec.md")),
            ("unknown lens", lambda p: p.__setitem__("lenses", ["database"])),
            ("spec with specification", lambda p: p.__setitem__("specification", "docs/base.md")),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                record = reviewer_record(home)
                mutate(record["reviewer"])  # type: ignore[arg-type]
                with self.assertRaises(dispatch.DispatchError):
                    dispatch.create_state(home, record)

    def test_rejects_snapshot_path_outside_profile_artifact_root_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            outside = Path(tmp) / RUN_ID / "snapshot-outside"
            record = reviewer_record(
                home,
                snapshot_outcome="changed",
                snapshot_path=str(outside),
            )
            with self.assertRaisesRegex(dispatch.DispatchError, "artifact"):
                dispatch.create_state(home, record)
            self.assertFalse(dispatch._state_path(home, RUN_ID).exists())


class RedactionTests(unittest.TestCase):
    def test_redacts_credentials_and_exact_known_session_before_truncation(self) -> None:
        session = "cursor-session-sensitive-123"
        diagnostic = (
            "api_key=super-secret token: bearer-token password=hunter2 "
            "Authorization: Bearer abc.def.ghi "
            f"session={session} exact={session}"
        )
        redacted = dispatch._redact_failure(diagnostic, session_ids=[session])
        for secret in (
            "super-secret",
            "bearer-token",
            "hunter2",
            "abc.def.ghi",
            session,
        ):
            self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED_SESSION_ID]", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_delegate_blocked_result_and_state_never_return_known_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            document = repository / "spec.md"
            document.write_text("# Spec\n", encoding="utf-8")
            run_git(repository, "add", "spec.md")
            run_git(repository, "commit", "-m", "spec")
            home = root / "home"
            fake_hermes = root / "hermes"
            fake_bash = root / "bash"
            fake_delegate = root / "review-delegate.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho reviewer-model\n")
            write_executable(fake_bash, '#!/bin/sh\nexec "$@"\n')
            session = "cursor-session-sensitive-123"
            write_executable(
                fake_delegate,
                "#!/bin/sh\n"
                "echo '{\"status\":\"BLOCKED\",\"reviewer\":\"cursor-reviewer\","
                "\"session_id\":\"cursor-session-sensitive-123\","
                "\"target\":\"spec\",\"lenses\":[\"backend\"],\"report\":\"\","
                "\"diagnostic\":\"Authorization: Bearer raw-token "
                "cursor-session-sensitive-123\"}'\n"
                "exit 1\n",
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch.object(
                dispatch, "default_hermes_bin", return_value=str(fake_hermes)
            ), patch.object(dispatch, "find_bash5", return_value=fake_bash), patch.object(
                dispatch, "review_delegate_script", return_value=fake_delegate
            ):
                result = dispatch.run_review(
                    review_args(repository, document)
                )
            state = dispatch.load_state(home, RUN_ID)
            self.assertNotIn(session, result["diagnostic"])
            self.assertNotIn("raw-token", result["diagnostic"])
            self.assertNotIn(session, state["failure"])


class ReconciliationIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = init_repository(self.root)
        self.work_branch = f"feature-test-hermes-{RUN_ID}-work"
        run_git(self.repository, "branch", self.work_branch)
        self.worktree = self.root / f"repo-feature-test-hermes-{RUN_ID}-work"
        run_git(
            self.repository,
            "worktree",
            "add",
            str(self.worktree),
            self.work_branch,
        )
        self.home = self.root / "home"
        record = coder_record(
            self.home,
            state="prepared",
            repository=self.repository,
            worktree=self.worktree,
            work_branch=self.work_branch,
        )
        dispatch.create_state(self.home, record)
        self.dispatching = dispatch.transition_state(
            self.home, RUN_ID, 1, "dispatching", {}
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.temp.cleanup()

    def _record_verified_worker_state(self) -> dict[str, object]:
        (self.worktree / "worker.txt").write_text("worker\n", encoding="utf-8")
        run_git(self.worktree, "add", "worker.txt")
        tree = run_git(self.worktree, "write-tree")
        index = dispatch._git_index_identity(self.worktree)
        state = dispatch.load_state(self.home, RUN_ID)
        coder = dict(state["coder"])
        coder["dispatch_baseline"] = {
            **coder["dispatch_baseline"],
            "verified_worker_tree": tree,
            "verified_worker_index": index,
        }
        return dispatch.update_state_fields(
            self.home,
            RUN_ID,
            state["generation"],
            {"coder": coder, "cursor_session_id": "session-1"},
        )

    def test_authorized_controller_commit_requires_exact_parent_and_tree(self) -> None:
        verified = self._record_verified_worker_state()
        run_git(self.worktree, "commit", "-m", "controller commit")

        result = dispatch.reconcile_coder_state(self.home, RUN_ID)

        self.assertEqual(result["state"], "reviewing")
        self.assertGreater(result["generation"], verified["generation"])

    def test_arbitrary_worker_commit_blocks(self) -> None:
        (self.worktree / "worker.txt").write_text("worker\n", encoding="utf-8")
        run_git(self.worktree, "add", "worker.txt")
        run_git(self.worktree, "commit", "-m", "unauthorized worker commit")

        result = dispatch.reconcile_coder_state(self.home, RUN_ID)

        self.assertEqual(result["state"], "blocked")
        self.assertIn("unauthorized", result["failure"])

    def test_controller_commit_with_wrong_tree_blocks(self) -> None:
        self._record_verified_worker_state()
        (self.worktree / "extra.txt").write_text("extra\n", encoding="utf-8")
        run_git(self.worktree, "add", "extra.txt")
        run_git(self.worktree, "commit", "-m", "wrong tree")

        result = dispatch.reconcile_coder_state(self.home, RUN_ID)

        self.assertEqual(result["state"], "blocked")
        self.assertIn("tree", result["failure"])

    def test_integration_pending_blocks_feature_branch_divergence(self) -> None:
        self._record_verified_worker_state()
        run_git(self.worktree, "commit", "-m", "controller commit")
        commit = run_git(self.worktree, "rev-parse", "HEAD")
        state = dispatch.reconcile_coder_state(self.home, RUN_ID)
        coder = dict(state["coder"])
        coder["pending_commit"] = commit
        pending = dispatch.transition_state(
            self.home,
            RUN_ID,
            state["generation"],
            "integration_pending",
            {"coder": coder},
        )
        (self.repository / "diverged.txt").write_text("diverged\n", encoding="utf-8")
        run_git(self.repository, "add", "diverged.txt")
        run_git(self.repository, "commit", "-m", "diverged feature")

        result = dispatch.reconcile_coder_state(self.home, RUN_ID)

        self.assertEqual(pending["state"], "integration_pending")
        self.assertEqual(result["state"], "blocked")
        self.assertIn("diverg", result["failure"])

    def test_integration_pending_reconciles_exact_fast_forward(self) -> None:
        self._record_verified_worker_state()
        run_git(self.worktree, "commit", "-m", "controller commit")
        commit = run_git(self.worktree, "rev-parse", "HEAD")
        reviewed = dispatch.reconcile_coder_state(self.home, RUN_ID)
        coder = dict(reviewed["coder"])
        coder["pending_commit"] = commit
        dispatch.transition_state(
            self.home,
            RUN_ID,
            reviewed["generation"],
            "integration_pending",
            {"coder": coder},
        )
        run_git(self.repository, "merge", "--ff-only", commit)

        result = dispatch.reconcile_coder_state(self.home, RUN_ID)

        self.assertEqual(result["state"], "integrated")
        self.assertEqual(result["coder"]["last_integrated_commit"], commit)
        self.assertIsNone(result["coder"]["pending_commit"])

    def test_integrating_corrected_tip_clears_all_reachable_ancestors(self) -> None:
        self._record_verified_worker_state()
        run_git(self.worktree, "commit", "-m", "first controller commit")
        first = run_git(self.worktree, "rev-parse", "HEAD")
        reviewing = dispatch.reconcile_coder_state(self.home, RUN_ID)
        dispatch.transition_state(
            self.home,
            RUN_ID,
            reviewing["generation"],
            "dispatching",
            {},
        )
        (self.worktree / "correction.txt").write_text("corrected\n", encoding="utf-8")
        run_git(self.worktree, "add", "correction.txt")
        state = dispatch.load_state(self.home, RUN_ID)
        coder = dict(state["coder"])
        coder["dispatch_baseline"] = {
            **coder["dispatch_baseline"],
            "verified_worker_tree": run_git(self.worktree, "write-tree"),
            "verified_worker_index": dispatch._git_index_identity(self.worktree),
        }
        dispatch.update_state_fields(
            self.home,
            RUN_ID,
            state["generation"],
            {"coder": coder},
        )
        run_git(self.worktree, "commit", "-m", "corrected controller commit")
        tip = run_git(self.worktree, "rev-parse", "HEAD")
        reviewing = dispatch.reconcile_coder_state(self.home, RUN_ID)
        self.assertEqual(
            reviewing["coder"]["unintegrated_commits"], [first, tip]
        )
        coder = dict(reviewing["coder"])
        coder["pending_commit"] = tip
        dispatch.transition_state(
            self.home,
            RUN_ID,
            reviewing["generation"],
            "integration_pending",
            {"coder": coder},
        )
        run_git(self.repository, "merge", "--ff-only", tip)
        integrated = dispatch.reconcile_coder_state(self.home, RUN_ID)
        self.assertEqual(integrated["state"], "integrated")
        self.assertEqual(integrated["coder"]["unintegrated_commits"], [])


class WorktreeRemovalLockTests(unittest.TestCase):
    def test_prepare_rejects_ready_payload_with_failure_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            payload = {
                "status": "READY",
                "worktree": str(root / f"repo-hermes-{RUN_ID}-work"),
                "work_branch": f"feature-hermes-{RUN_ID}-work",
                "feature_branch": "feature/test",
                "copied": [],
                "skipped": [],
                "neutralized_symlinks": 0,
                "purged_secrets": 0,
                "clone_supported": False,
            }
            with patch.object(
                dispatch, "_run_script_json", return_value=(1, payload)
            ):
                with self.assertRaisesRegex(
                    dispatch.DispatchError, "status conflicts"
                ):
                    dispatch.run_worktree_prepare(
                        repository=repository,
                        run_id=RUN_ID,
                        bash_path=Path("/bin/false"),
                        hermes_home=root / "home",
                    )

    def test_completed_worktree_removal_retry_returns_same_tombstone_generation(self) -> None:
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
            first = dispatch.run_worktree_remove(
                repository=repository,
                run_id=RUN_ID,
                expected_generation=prepared["generation"],
                bash_path=bash,
                hermes_home=home,
            )
            second = dispatch.run_worktree_remove(
                repository=repository,
                run_id=RUN_ID,
                expected_generation=first["generation"],
                bash_path=bash,
                hermes_home=home,
            )
            self.assertEqual(second, first)

    def test_remove_reconciles_absent_worktree_with_remaining_branch(self) -> None:
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
            run_git(
                repository,
                "worktree",
                "remove",
                prepared["worktree"],
            )
            self.assertFalse(Path(prepared["worktree"]).exists())
            self.assertEqual(
                run_git(
                    repository,
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{prepared['work_branch']}",
                ),
                run_git(repository, "rev-parse", "HEAD"),
            )

            result = dispatch.run_worktree_remove(
                repository=repository,
                run_id=RUN_ID,
                expected_generation=prepared["generation"],
                bash_path=bash,
                hermes_home=home,
            )

            self.assertEqual(result["status"], "REMOVED")
            self.assertEqual(dispatch.load_state(home, RUN_ID)["state"], "complete")
            self.assertNotEqual(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repository),
                        "rev-parse",
                        "--verify",
                        f"refs/heads/{prepared['work_branch']}",
                    ],
                    check=False,
                    capture_output=True,
                ).returncode,
                0,
            )

    def test_remove_holds_same_run_lock_across_side_effect_and_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            home = root / "home"
            worktree = root / f"repo-feature-test-hermes-{RUN_ID}-work"
            worktree.mkdir()
            work_branch = f"feature-test-hermes-{RUN_ID}-work"
            run_git(repository, "branch", work_branch)
            record = coder_record(
                home,
                state="integrated",
                repository=repository,
                worktree=worktree,
                work_branch=work_branch,
            )
            head = run_git(repository, "rev-parse", "HEAD")
            record["cursor_session_id"] = "session-1"
            record["coder"]["last_integrated_commit"] = head  # type: ignore[index]
            record["coder"]["dispatch_baseline"] = {  # type: ignore[index]
                "pre_dispatch_work_branch_commit": head,
                "pre_dispatch_feature_branch_commit": head,
                "verified_worker_tree": run_git(repository, "rev-parse", "HEAD^{tree}"),
                "verified_worker_index": "a" * 64,
            }
            dispatch.create_state(home, record)
            entered = threading.Event()
            release = threading.Event()
            transition_finished = threading.Event()

            def fake_remove(**_kwargs: object) -> tuple[int, dict[str, object]]:
                entered.set()
                release.wait(timeout=5)
                shutil.rmtree(worktree)
                run_git(repository, "branch", "-D", work_branch)
                return 0, {
                    "status": "REMOVED",
                    "work_branch": work_branch,
                    "unmerged": [],
                }

            def remove() -> None:
                dispatch.run_worktree_remove(
                    repository=repository,
                    run_id=RUN_ID,
                    expected_generation=1,
                    bash_path=Path("/bin/false"),
                    hermes_home=home,
                )

            def competing_transition() -> None:
                entered.wait(timeout=5)
                try:
                    dispatch.transition_state(
                        home, RUN_ID, 1, "dispatching", {}
                    )
                except dispatch.DispatchError:
                    pass
                transition_finished.set()

            with patch.object(dispatch, "_run_script_json", side_effect=fake_remove):
                remove_thread = threading.Thread(target=remove)
                competing_thread = threading.Thread(target=competing_transition)
                remove_thread.start()
                competing_thread.start()
                self.assertTrue(entered.wait(timeout=5))
                time.sleep(0.1)
                self.assertFalse(transition_finished.is_set())
                release.set()
                remove_thread.join(timeout=5)
                competing_thread.join(timeout=5)

            state = dispatch.load_state(home, RUN_ID)
            self.assertEqual(state["state"], "complete")
            self.assertTrue(transition_finished.is_set())


class ReviewerEvidenceAndContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = init_repository(self.root)
        self.document = self.repository / "spec.md"
        self.document.write_text("# Spec\n", encoding="utf-8")
        run_git(self.repository, "add", "spec.md")
        run_git(self.repository, "commit", "-m", "spec")
        self.home = self.root / "home"
        self.fake_hermes = self.root / "hermes"
        self.fake_bash = self.root / "bash"
        write_executable(self.fake_hermes, "#!/bin/sh\necho reviewer-model\n")
        write_executable(self.fake_bash, '#!/bin/sh\nexec "$@"\n')

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run_with_delegate(self, body: str) -> dict[str, object]:
        delegate = self.root / "review-delegate.sh"
        write_executable(delegate, body)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=False), patch.object(
            dispatch, "default_hermes_bin", return_value=str(self.fake_hermes)
        ), patch.object(dispatch, "find_bash5", return_value=self.fake_bash), patch.object(
            dispatch, "review_delegate_script", return_value=delegate
        ):
            return dispatch.run_review(review_args(self.repository, self.document))

    def test_parser_failure_after_mutation_persists_durable_changed_snapshot(self) -> None:
        result = self._run_with_delegate(
            textwrap.dedent(
                """\
                #!/bin/sh
                doc=""
                while [ "$#" -gt 0 ]; do
                  if [ "$1" = "--doc-file" ]; then doc="$2"; break; fi
                  shift
                done
                printf 'mutated\n' > "$doc"
                printf 'not-json\n'
                """
            )
        )

        state = dispatch.load_state(self.home, RUN_ID)
        snapshot = Path(result["snapshot_path"])
        artifact_root = (
            self.home / "claude-subagents" / "review-artifacts" / RUN_ID
        ).resolve()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(state["reviewer"]["snapshot_outcome"], "changed")
        self.assertEqual(state["reviewer"]["snapshot_path"], str(snapshot))
        self.assertTrue(snapshot.is_dir())
        self.assertTrue(snapshot.resolve().is_relative_to(artifact_root))

    def test_raw_delegate_null_list_and_numeric_contract_values_are_rejected(self) -> None:
        base = {
            "status": "REVIEWED",
            "reviewer": "cursor-reviewer",
            "session_id": "session",
            "target": "spec",
            "lenses": ["backend"],
            "report": "report",
            "diagnostic": "",
        }
        for field, value in (
            ("session_id", None),
            ("session_id", []),
            ("session_id", 12),
            ("report", None),
            ("report", []),
            ("report", 12),
        ):
            with self.subTest(field=field, value=value):
                payload = {**base, field: value}
                with self.assertRaises(dispatch.DispatchError):
                    dispatch._validate_review_delegate_payload(
                        payload,
                        target="spec",
                        lenses=["backend"],
                        exit_code=0,
                    )

    def test_pre_lifecycle_cli_failure_is_reviewer_shaped(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = dispatch.main(["review", "--target", "spec"])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(set(payload), dispatch.REVIEW_OUTPUT_FIELDS)
        self.assertEqual(payload["status"], "BLOCKED")

    def test_existing_state_cli_failure_returns_actual_generation(self) -> None:
        dispatch.create_state(self.home, reviewer_record(self.home))
        buffer = StringIO()
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=False), redirect_stdout(
            buffer
        ):
            code = dispatch.main(
                [
                    "review",
                    "--target",
                    "spec",
                    "--doc-file",
                    str(self.repository / "missing.md"),
                    "--run-id",
                    RUN_ID,
                    "--repo",
                    str(self.repository),
                ]
            )
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["generation"], 1)

    def test_initial_review_rejects_raw_session_and_generation_values(self) -> None:
        for field, value in (
            ("session", 0),
            ("session", []),
            ("expected_generation", False),
        ):
            with self.subTest(field=field, value=value), patch.dict(
                os.environ, {"HERMES_HOME": str(self.home)}, clear=False
            ):
                with self.assertRaises(dispatch.DispatchError):
                    dispatch.run_review(
                        review_args(
                            self.repository,
                            self.document,
                            **{field: value},
                        )
                    )

    def test_total_deadline_starts_before_repository_preflight(self) -> None:
        with patch.dict(
            os.environ, {"HERMES_HOME": str(self.home)}, clear=False
        ), patch.object(
            dispatch.time, "monotonic", side_effect=[10.0, 1811.0]
        ), patch.object(dispatch, "create_review_clone") as create_clone:
            with self.assertRaisesRegex(dispatch.DispatchError, "timed out"):
                dispatch.run_review(review_args(self.repository, self.document))
        create_clone.assert_not_called()

    def test_delegate_timeout_terminates_process_group_and_blocks_state(self) -> None:
        pid_file = self.root / "descendant.pid"
        delegate = self.root / "review-delegate.sh"
        write_executable(
            delegate,
            "#!/bin/sh\n"
            "sleep 30 &\n"
            f"echo $! > {str(pid_file)!r}\n"
            "wait\n",
        )
        with patch.dict(
            os.environ, {"HERMES_HOME": str(self.home)}, clear=False
        ), patch.object(
            dispatch, "default_hermes_bin", return_value=str(self.fake_hermes)
        ), patch.object(
            dispatch, "find_bash5", return_value=self.fake_bash
        ), patch.object(
            dispatch, "review_delegate_script", return_value=delegate
        ), patch.object(
            dispatch, "REVIEW_TIMEOUT_SECONDS", 2
        ), patch.object(
            dispatch, "TERM_GRACE_SECONDS", 0.05
        ):
            result = dispatch.run_review(
                review_args(self.repository, self.document)
            )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("timed out", result["diagnostic"])
        state = dispatch.load_state(self.home, RUN_ID)
        self.assertEqual(state["state"], "blocked")
        self.assertTrue(pid_file.is_file())
        descendant = int(pid_file.read_text(encoding="utf-8").strip())
        for _ in range(50):
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("review delegate descendant survived timeout")


class ManifestAndCloneContractTests(unittest.TestCase):
    def test_manifest_detects_all_named_path_changes_and_excludes_git(self) -> None:
        def fixture(root: Path) -> None:
            (root / ".git").mkdir()
            (root / ".git" / "ignored").write_text("x", encoding="utf-8")
            (root / "regular").write_text("one", encoding="utf-8")
            (root / "directory").mkdir()
            os.symlink("regular", root / "file-link")
            os.symlink("directory", root / "dir-link")

        def add_path(root: Path) -> str:
            (root / "added").write_text("x", encoding="utf-8")
            return "added"

        def remove_path(root: Path) -> str:
            (root / "regular").unlink()
            return "regular"

        def change_content(root: Path) -> str:
            (root / "regular").write_text("two", encoding="utf-8")
            return "regular"

        def change_mode(root: Path) -> str:
            (root / "regular").chmod(0o755)
            return "regular"

        def file_to_directory(root: Path) -> str:
            (root / "regular").unlink()
            (root / "regular").mkdir()
            return "regular"

        def file_to_symlink(root: Path) -> str:
            (root / "regular").unlink()
            os.symlink("directory", root / "regular")
            return "regular"

        def directory_to_file(root: Path) -> str:
            (root / "directory").rmdir()
            (root / "directory").write_text("now a file", encoding="utf-8")
            return "directory"

        def change_file_symlink_target(root: Path) -> str:
            (root / "file-link").unlink()
            os.symlink("directory", root / "file-link")
            return "file-link"

        def change_directory_symlink_target(root: Path) -> str:
            (root / "dir-link").unlink()
            os.symlink(".", root / "dir-link")
            return "dir-link"

        cases = {
            "added": add_path,
            "removed": remove_path,
            "content": change_content,
            "executable mode": change_mode,
            "file to directory": file_to_directory,
            "file to symlink": file_to_symlink,
            "directory to file": directory_to_file,
            "file symlink target": change_file_symlink_target,
            "directory symlink target": change_directory_symlink_target,
        }
        for label, mutate in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture(root)
                before = dispatch.source_manifest(root)
                self.assertFalse(
                    any(path == ".git" or path.startswith(".git/") for path in before)
                )
                changed_path = mutate(root)
                after = dispatch.source_manifest(root)
                if label == "added":
                    self.assertNotIn(changed_path, before)
                    self.assertIn(changed_path, after)
                elif label == "removed":
                    self.assertIn(changed_path, before)
                    self.assertNotIn(changed_path, after)
                else:
                    self.assertNotEqual(before[changed_path], after[changed_path])

    def test_clone_has_independent_storage_refs_config_hooks_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            document = repository / "spec.md"
            document.write_text("# Spec\n", encoding="utf-8")
            clone = dispatch.create_review_clone(
                repository,
                RUN_ID,
                [document],
                artifact_root=root / "artifacts",
                deadline=time.monotonic() + 30,
            )
            self.addCleanup(lambda: shutil.rmtree(clone.path, ignore_errors=True))
            controller_git = dispatch._git_common_dir(repository)
            clone_git = dispatch._git_common_dir(clone.path)
            self.assertNotEqual(controller_git, clone_git)
            for relative in ("objects", "refs", "config", "hooks", "worktrees"):
                self.assertNotEqual(
                    (controller_git / relative).resolve(),
                    (clone_git / relative).resolve(),
                )
            for object_file in (clone_git / "objects").rglob("*"):
                if object_file.is_file() and not object_file.is_symlink():
                    self.assertEqual(object_file.stat().st_nlink, 1)

    def test_clone_overlay_and_initial_manifest_failures_remove_partial_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            document = repository / "spec.md"
            document.write_text("# Spec\n", encoding="utf-8")
            artifacts = root / "artifacts"

            with patch.object(
                dispatch, "_copy_file_bounded", side_effect=OSError("overlay failed")
            ):
                with self.assertRaises(OSError):
                    dispatch.create_review_clone(
                        repository,
                        RUN_ID,
                        [document],
                        artifact_root=artifacts,
                        deadline=time.monotonic() + 30,
                    )
            self.assertEqual(list(artifacts.glob("snapshot-*")), [])

            with patch.object(
                dispatch, "source_manifest", side_effect=OSError("manifest failed")
            ):
                with self.assertRaises(OSError):
                    dispatch.create_review_clone(
                        repository,
                        RUN_ID,
                        [document],
                        artifact_root=artifacts,
                        deadline=time.monotonic() + 30,
                    )
            self.assertEqual(list(artifacts.glob("snapshot-*")), [])


class GenuineConcurrentContractTests(unittest.TestCase):
    def test_adapter_contract_concurrent_worktrees_are_created_simultaneously(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = init_repository(root)
            home = root / "home"
            barrier = threading.Barrier(2)
            results: list[dict[str, object]] = []
            errors: list[BaseException] = []

            def prepare(run_id: str) -> None:
                try:
                    barrier.wait(timeout=5)
                    results.append(
                        dispatch.run_worktree_prepare(
                            repository=repository,
                            run_id=run_id,
                            bash_path=bash,
                            hermes_home=home,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=prepare, args=(RUN_ID,)),
                threading.Thread(target=prepare, args=(OTHER_RUN_ID,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(len({result["worktree"] for result in results}), 2)
            self.assertEqual(len({result["work_branch"] for result in results}), 2)


if __name__ == "__main__":
    unittest.main()
