#!/usr/bin/env python3
"""Hermes dispatch adapter tests."""

from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import stat
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


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class BashDiscoveryTests(unittest.TestCase):
    def test_rejects_repository_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            local_bash = repo / "bash"
            write_executable(
                local_bash,
                "#!/bin/sh\necho 'GNU bash, version 5.2.0'\n",
            )
            with patch.object(dispatch, "TRUSTED_BASH_PATHS", (local_bash,)):
                with self.assertRaises(RuntimeError):
                    dispatch.find_bash5(repo, "")

    def test_rejects_repository_local_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            outside = Path(tmp) / "outside-bash"
            write_executable(
                outside,
                "#!/bin/sh\necho 'GNU bash, version 5.2.0'\n",
            )
            local_link = repo / "bash"
            local_link.symlink_to(outside)
            with patch.object(dispatch, "TRUSTED_BASH_PATHS", (local_link,)):
                with self.assertRaises(RuntimeError):
                    dispatch.find_bash5(repo, "")

    def test_rejects_relative_path_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            trusted = (Path("/bin/nonexistent-bash"),)
            with patch.object(dispatch, "TRUSTED_BASH_PATHS", trusted):
                with self.assertRaises(RuntimeError):
                    dispatch.find_bash5(repo, "relative/bin")

    def test_deduplicates_trusted_and_path_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            bash_dir = Path(tmp) / "bin"
            bash_dir.mkdir()
            bash = bash_dir / "bash"
            write_executable(
                bash,
                "#!/bin/sh\necho 'GNU bash, version 5.2.0'\n",
            )
            resolved = bash.resolve()
            with patch.object(dispatch, "TRUSTED_BASH_PATHS", (bash,)):
                found = dispatch.find_bash5(repo, str(bash_dir))
            self.assertEqual(found, resolved)

    def test_skips_non_executable_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            bad = Path(tmp) / "bad-bash"
            bad.write_text("#!/bin/sh\n", encoding="utf-8")
            good = Path(tmp) / "good-bash"
            write_executable(
                good,
                "#!/bin/sh\necho 'GNU bash, version 5.2.0'\n",
            )
            with patch.object(dispatch, "TRUSTED_BASH_PATHS", (bad, good)):
                self.assertEqual(dispatch.find_bash5(repo, ""), good.resolve())

    def test_skips_unparseable_version_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            bad = Path(tmp) / "bad-bash"
            write_executable(bad, "#!/bin/sh\necho 'not bash'\n")
            good = Path(tmp) / "good-bash"
            write_executable(
                good,
                "#!/bin/sh\necho 'GNU bash, version 5.2.0'\n",
            )
            with patch.object(dispatch, "TRUSTED_BASH_PATHS", (bad, good)):
                self.assertEqual(dispatch.find_bash5(repo, ""), good.resolve())

    def test_skips_bash_older_than_five(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            old = Path(tmp) / "old-bash"
            write_executable(
                old,
                "#!/bin/sh\necho 'GNU bash, version 3.2.0'\n",
            )
            new = Path(tmp) / "new-bash"
            write_executable(
                new,
                "#!/bin/sh\necho 'GNU bash, version 5.2.0'\n",
            )
            with patch.object(dispatch, "TRUSTED_BASH_PATHS", (old, new)):
                self.assertEqual(dispatch.find_bash5(repo, ""), new.resolve())

    def test_prefers_homebrew_bash_on_macos(self) -> None:
        brew = Path("/opt/homebrew/bin/bash")
        if not brew.is_file():
            self.skipTest("Homebrew bash not installed")
        found = dispatch.find_bash5(REPO_ROOT, os.environ.get("PATH", ""))
        self.assertEqual(found, brew.resolve())

    def test_skips_launch_failure_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            bad = Path(tmp) / "bad-bash"
            write_executable(bad, "#!/bin/sh\necho 'GNU bash, version 5.2.0'\n")
            good = Path(tmp) / "good-bash"
            write_executable(
                good,
                "#!/bin/sh\necho 'GNU bash, version 5.2.0'\n",
            )
            with patch.object(dispatch, "TRUSTED_BASH_PATHS", (bad, good)):
                with patch.object(
                    dispatch,
                    "_run_external",
                    side_effect=[
                        dispatch.DispatchError("launch failed"),
                        (0, "GNU bash, version 5.2.0\n", ""),
                    ],
                ):
                    self.assertEqual(dispatch.find_bash5(repo, ""), good.resolve())

    def test_skips_timeout_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            bad = Path(tmp) / "bad-bash"
            write_executable(bad, "#!/bin/sh\necho 'GNU bash, version 5.2.0'\n")
            good = Path(tmp) / "good-bash"
            write_executable(
                good,
                "#!/bin/sh\necho 'GNU bash, version 5.2.0'\n",
            )
            with patch.object(dispatch, "TRUSTED_BASH_PATHS", (bad, good)):
                with patch.object(
                    dispatch,
                    "_run_external",
                    side_effect=[
                        dispatch.DispatchError("timed out"),
                        (0, "GNU bash, version 5.2.0\n", ""),
                    ],
                ):
                    self.assertEqual(dispatch.find_bash5(repo, ""), good.resolve())


class ModelConfigTests(unittest.TestCase):
    def test_missing_hermes_home_fails_closed(self) -> None:
        env = dict(os.environ)
        env.pop("HERMES_HOME", None)
        with self.assertRaises(dispatch.DispatchError):
            dispatch.read_model("coder", "hermes", env)

    def test_blank_hermes_home_fails_closed(self) -> None:
        env = dict(os.environ)
        env["HERMES_HOME"] = "   "
        with self.assertRaises(dispatch.DispatchError):
            dispatch.read_model("coder", "hermes", env)

    def test_invalid_role_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HERMES_HOME": tmp}
            with self.assertRaises(dispatch.DispatchError):
                dispatch.read_model("worker", "hermes", env)

    def test_missing_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "hermes"
            fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake.chmod(0o755)
            env = {"HERMES_HOME": tmp}
            with self.assertRaises(dispatch.DispatchError):
                dispatch.read_model("coder", str(fake), env)

    def test_blank_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "hermes"
            fake.write_text("#!/bin/sh\necho\n", encoding="utf-8")
            fake.chmod(0o755)
            env = {"HERMES_HOME": tmp}
            with self.assertRaises(dispatch.DispatchError):
                dispatch.read_model("reviewer", str(fake), env)

    def test_reads_configured_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "hermes"
            script = textwrap.dedent(
                """\
                #!/bin/sh
                key="$3"
                case "$key" in
                  *coder_model*) echo "composer-2.5" ;;
                  *reviewer_model*) echo "gpt-5.6-sol-high" ;;
                  *) exit 1 ;;
                esac
                """
            )
            fake.write_text(script, encoding="utf-8")
            fake.chmod(0o755)
            env = {"HERMES_HOME": tmp}
            self.assertEqual(
                dispatch.read_model("coder", str(fake), env),
                "composer-2.5",
            )
            self.assertEqual(
                dispatch.read_model("reviewer", str(fake), env),
                "gpt-5.6-sol-high",
            )


class PoolTests(unittest.TestCase):
    def test_write_pool_mode_and_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            coder_path = dispatch.write_pool("coder", "composer-2.5", directory)
            reviewer_path = dispatch.write_pool(
                "reviewer", "gpt-5.6-sol-high", directory
            )
            self.assertTrue(coder_path.is_file())
            self.assertTrue(reviewer_path.is_file())
            self.assertEqual(stat.S_IMODE(coder_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(reviewer_path.stat().st_mode), 0o600)

            coder_payload = json.loads(coder_path.read_text(encoding="utf-8"))
            reviewer_payload = json.loads(reviewer_path.read_text(encoding="utf-8"))
            self.assertEqual(coder_payload["coders"][0]["key"], "cursor-coder")
            self.assertEqual(reviewer_payload["reviewers"][0]["key"], "cursor-reviewer")

    def test_rejects_invalid_model_characters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(dispatch.DispatchError):
                dispatch.write_pool("coder", "bad model", Path(tmp))


class RunIdTests(unittest.TestCase):
    def test_accepts_valid_run_id(self) -> None:
        self.assertEqual(
            dispatch.validate_run_id("0123456789abcdef"),
            "0123456789abcdef",
        )

    def test_rejects_invalid_run_id(self) -> None:
        for bad in ("", "short", "0123456789abcdeg", "G1234567890abcdef"):
            with self.subTest(run_id=bad):
                with self.assertRaises(dispatch.DispatchError):
                    dispatch.validate_run_id(bad)


class EnvelopeTests(unittest.TestCase):
    def test_adds_run_id_and_generation(self) -> None:
        payload = {
            "status": "READY",
            "role": "coder",
            "key": "cursor-coder",
            "reason": "",
            "diagnostic": "",
        }
        out = dispatch.add_envelope_fields(payload, "0123456789abcdef", 2)
        self.assertEqual(out["run_id"], "0123456789abcdef")
        self.assertEqual(out["generation"], 2)
        self.assertEqual(out["status"], "READY")


class SandboxModeTests(unittest.TestCase):
    def test_accepts_enabled(self) -> None:
        self.assertEqual(dispatch.validate_cursor_sandbox("enabled"), "enabled")

    def test_rejects_invalid_mode(self) -> None:
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate_cursor_sandbox("disabled")


class ProbeEnvironmentTests(unittest.TestCase):
    def test_allows_only_specified_variables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool.json"
            pool.write_text("{}", encoding="utf-8")
            source = {
                "PATH": "/bin:/usr/bin",
                "HOME": "/Users/tester",
                "TMPDIR": "/tmp",
                "USER": "tester",
                "SHELL": "/bin/zsh",
                "LANG": "C",
                "HERMES_HOME": "/secret/profile",
                "OPENAI_API_KEY": "secret",
                "CSC_CODERS_JSON": "/should-not-forward",
            }
            env = dispatch.build_probe_env(
                role="coder",
                pool_path=pool,
                run_id="0123456789abcdef",
                source_env=source,
            )
            self.assertEqual(env["PATH"], "/bin:/usr/bin")
            self.assertEqual(env["HOME"], "/Users/tester")
            self.assertEqual(env["CSC_CODERS_JSON"], str(pool))
            self.assertEqual(env["CSC_STREAM_MAX_BYTES"], str(dispatch.STREAM_MAX_BYTES))
            self.assertEqual(env["CSC_RUN_ID"], "0123456789abcdef")
            self.assertNotIn("HERMES_HOME", env)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("CSC_REVIEWERS_JSON", env)


class ProbeValidationTests(unittest.TestCase):
    def test_accepts_ready_payload(self) -> None:
        payload = {
            "status": "READY",
            "role": "coder",
            "key": "cursor-coder",
            "reason": "",
            "diagnostic": "",
        }
        out = dispatch.validate_probe_payload(
            payload,
            role="coder",
            key="cursor-coder",
            exit_code=0,
        )
        self.assertEqual(out["status"], "READY")

    def test_rejects_extra_field(self) -> None:
        payload = {
            "status": "READY",
            "role": "coder",
            "key": "cursor-coder",
            "reason": "",
            "diagnostic": "",
            "extra": "nope",
        }
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate_probe_payload(
                payload,
                role="coder",
                key="cursor-coder",
                exit_code=0,
            )

    def test_rejects_role_mismatch(self) -> None:
        payload = {
            "status": "READY",
            "role": "reviewer",
            "key": "cursor-coder",
            "reason": "",
            "diagnostic": "",
        }
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate_probe_payload(
                payload,
                role="coder",
                key="cursor-coder",
                exit_code=0,
            )

    def test_rejects_contradictory_exit_code(self) -> None:
        payload = {
            "status": "READY",
            "role": "coder",
            "key": "cursor-coder",
            "reason": "",
            "diagnostic": "",
        }
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate_probe_payload(
                payload,
                role="coder",
                key="cursor-coder",
                exit_code=1,
            )

    def test_rejects_non_string_status(self) -> None:
        for bad_status in (0, None, ["READY"]):
            with self.subTest(status=bad_status):
                payload = {
                    "status": bad_status,
                    "role": "coder",
                    "key": "cursor-coder",
                    "reason": "",
                    "diagnostic": "",
                }
                with self.assertRaises(dispatch.DispatchError):
                    dispatch.validate_probe_payload(
                        payload,
                        role="coder",
                        key="cursor-coder",
                        exit_code=0,
                    )

    def test_blocked_wrapper_has_complete_probe_fields(self) -> None:
        payload = dispatch.blocked_probe_result(
            "bad output",
            role="reviewer",
            key="cursor-reviewer",
            run_id="0123456789abcdef",
        )
        self.assertEqual(set(payload.keys()), dispatch.PROBE_FIELDS | {"run_id"})


class ProbeRoutingTests(unittest.TestCase):
    def _fake_probe_setup(self, *, bash_body: str, role: str = "coder") -> tuple[Path, Path, Path, Path, dict[str, str]]:
        tmp_path = Path(tempfile.mkdtemp())
        fake_hermes = tmp_path / "hermes"
        fake_bash = tmp_path / "bash5"
        fake_probe = tmp_path / "probe.sh"
        env_log = tmp_path / "env.log"

        write_executable(fake_hermes, "#!/bin/sh\necho composer-2.5\n")
        write_executable(
            fake_bash,
            textwrap.dedent(
                f"""\
                #!/bin/sh
                env > "{env_log}"
                {bash_body}
                """
            ),
        )
        write_executable(fake_probe, "#!/bin/sh\n")
        env = {
            "HERMES_HOME": str(tmp_path),
            "PATH": str(tmp_path),
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "USER": "tester",
            "SHELL": "/bin/sh",
            "OPENAI_API_KEY": "secret",
        }
        return tmp_path, fake_hermes, fake_bash, env_log, env

    def tearDown(self) -> None:
        for path in getattr(self, "_cleanup_dirs", []):
            if path.exists():
                import shutil

                shutil.rmtree(path, ignore_errors=True)

    def test_probe_routes_with_minimal_environment(self) -> None:
        tmp_path, fake_hermes, fake_bash, _, env = self._fake_probe_setup(
            bash_body=(
                'echo \'{"status":"READY","role":"coder","key":"cursor-coder",'
                '"reason":"","diagnostic":""}\''
            )
        )
        self._cleanup_dirs = [tmp_path]
        captured: dict[str, object] = {}
        real_popen = subprocess.Popen

        def tracking_popen(*args, **kwargs):
            captured["env"] = dict(kwargs.get("env", {}))
            return real_popen(*args, **kwargs)

        with patch.object(dispatch, "repo_root", return_value=REPO_ROOT), patch.object(
            dispatch, "find_bash5", return_value=fake_bash
        ), patch.object(dispatch, "probe_script", return_value=tmp_path / "probe.sh"), patch.object(
            dispatch.subprocess, "Popen", side_effect=tracking_popen
        ):
            code, payload = dispatch.run_probe(
                role="coder",
                run_id="0123456789abcdef",
                hermes_bin=str(fake_hermes),
                env=env,
                bash_path=fake_bash,
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "READY")
        probe_env = captured["env"]
        assert isinstance(probe_env, dict)
        self.assertEqual(probe_env["CSC_RUN_ID"], "0123456789abcdef")
        self.assertIn("CSC_CODERS_JSON", probe_env)
        self.assertNotIn("HERMES_HOME", probe_env)
        self.assertNotIn("OPENAI_API_KEY", probe_env)

    def test_probe_worker_failure(self) -> None:
        tmp_path, fake_hermes, fake_bash, _, env = self._fake_probe_setup(
            bash_body=(
                'echo \'{"status":"FAILED","role":"coder","key":"cursor-coder",'
                '"reason":"auth","diagnostic":"nope"}\'; exit 1'
            )
        )
        self._cleanup_dirs = [tmp_path]
        with patch.object(dispatch, "repo_root", return_value=REPO_ROOT), patch.object(
            dispatch, "find_bash5", return_value=fake_bash
        ), patch.object(dispatch, "probe_script", return_value=tmp_path / "probe.sh"):
            code, payload = dispatch.run_probe(
                role="coder",
                run_id="0123456789abcdef",
                hermes_bin=str(fake_hermes),
                env=env,
                bash_path=fake_bash,
            )

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "FAILED")

    def test_probe_cleans_up_pool_on_failure(self) -> None:
        tmp_path, fake_hermes, fake_bash, _, env = self._fake_probe_setup(
            bash_body=(
                'echo \'{"status":"FAILED","role":"coder","key":"cursor-coder",'
                '"reason":"auth","diagnostic":"nope"}\'; exit 1'
            )
        )
        self._cleanup_dirs = [tmp_path]
        with patch.object(dispatch, "repo_root", return_value=REPO_ROOT), patch.object(
            dispatch, "find_bash5", return_value=fake_bash
        ), patch.object(dispatch, "probe_script", return_value=tmp_path / "probe.sh"):
            dispatch.run_probe(
                role="coder",
                run_id="0123456789abcdef",
                hermes_bin=str(fake_hermes),
                env=env,
                bash_path=fake_bash,
            )

        self.assertFalse(list(tmp_path.glob("pool-*.json")))

    def test_probe_rejects_malformed_json(self) -> None:
        tmp_path, fake_hermes, fake_bash, _, env = self._fake_probe_setup(
            bash_body='echo "not-json"'
        )
        self._cleanup_dirs = [tmp_path]
        with patch.object(dispatch, "repo_root", return_value=REPO_ROOT), patch.object(
            dispatch, "find_bash5", return_value=fake_bash
        ), patch.object(dispatch, "probe_script", return_value=tmp_path / "probe.sh"):
            code, payload = dispatch.run_probe(
                role="coder",
                run_id="0123456789abcdef",
                hermes_bin=str(fake_hermes),
                env=env,
                bash_path=fake_bash,
            )

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["role"], "coder")
        self.assertEqual(payload["key"], "cursor-coder")

    def test_probe_rejects_exact_size_boundary_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_hermes = tmp_path / "hermes"
            fake_bash = tmp_path / "bash5"
            fake_probe = tmp_path / "probe.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho composer-2.5\n")

            prefix = (
                '{"status":"READY","role":"coder","key":"cursor-coder",'
                '"reason":"","diagnostic":"'
            )
            suffix = '"}'
            exact_blob_len = (
                dispatch.MAX_RESULT_BYTES
                - len(prefix.encode("utf-8"))
                - len(suffix.encode("utf-8"))
            )
            exact_json = prefix + ("x" * exact_blob_len) + suffix
            self.assertEqual(len(exact_json.encode("utf-8")), dispatch.MAX_RESULT_BYTES)
            exact_file = tmp_path / "exact.json"
            exact_file.write_text(exact_json + "\n", encoding="utf-8")
            overflow_file = tmp_path / "overflow.json"
            overflow_file.write_text(
                prefix + ("x" * (exact_blob_len + 1)) + suffix + "\n",
                encoding="utf-8",
            )

            write_executable(fake_bash, f"#!/bin/sh\ncat '{exact_file}'\n")
            write_executable(fake_probe, "#!/bin/sh\n")
            env = {"HERMES_HOME": str(tmp_path), "PATH": f"/bin:/usr/bin:{tmp_path}"}
            with patch.object(dispatch, "repo_root", return_value=REPO_ROOT), patch.object(
                dispatch, "find_bash5", return_value=fake_bash
            ), patch.object(dispatch, "probe_script", return_value=fake_probe):
                code, payload = dispatch.run_probe(
                    role="coder",
                    run_id="0123456789abcdef",
                    hermes_bin=str(fake_hermes),
                    env=env,
                    bash_path=fake_bash,
                )
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "READY")

            write_executable(
                fake_bash,
                f"#!/bin/sh\ncat '{overflow_file}'\n",
            )
            with patch.object(dispatch, "repo_root", return_value=REPO_ROOT), patch.object(
                dispatch, "find_bash5", return_value=fake_bash
            ), patch.object(dispatch, "probe_script", return_value=fake_probe):
                code, payload = dispatch.run_probe(
                    role="coder",
                    run_id="0123456789abcdef",
                    hermes_bin=str(fake_hermes),
                    env=env,
                    bash_path=fake_bash,
                )
            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertIn("result exceeds", payload["diagnostic"])

    def test_probe_returns_exit_two_before_schema_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_hermes = tmp_path / "hermes"
            fake_bash = tmp_path / "bash5"
            fake_probe = tmp_path / "probe.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho composer-2.5\n")
            write_executable(
                fake_bash,
                '#!/bin/sh\n'
                'echo \'{"status":"READY","role":"coder","key":"cursor-coder",'
                '"reason":"","diagnostic":""}\'; exit 2\n',
            )
            write_executable(fake_probe, "#!/bin/sh\n")
            env = {"HERMES_HOME": str(tmp_path), "PATH": f"/bin:/usr/bin:{tmp_path}"}
            with patch.object(dispatch, "repo_root", return_value=REPO_ROOT), patch.object(
                dispatch, "find_bash5", return_value=fake_bash
            ), patch.object(dispatch, "probe_script", return_value=fake_probe):
                code, payload = dispatch.run_probe(
                    role="coder",
                    run_id="0123456789abcdef",
                    hermes_bin=str(fake_hermes),
                    env=env,
                    bash_path=fake_bash,
                )
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertEqual(payload["diagnostic"], "probe failed")

    def test_probe_rejects_invalid_status_type_from_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_hermes = tmp_path / "hermes"
            fake_bash = tmp_path / "bash5"
            fake_probe = tmp_path / "probe.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho composer-2.5\n")
            write_executable(
                fake_bash,
                '#!/bin/sh\n'
                'echo \'{"status":0,"role":"coder","key":"cursor-coder",'
                '"reason":"","diagnostic":""}\'\n',
            )
            write_executable(fake_probe, "#!/bin/sh\n")
            env = {"HERMES_HOME": str(tmp_path), "PATH": f"/bin:/usr/bin:{tmp_path}"}
            with patch.object(dispatch, "repo_root", return_value=REPO_ROOT), patch.object(
                dispatch, "find_bash5", return_value=fake_bash
            ), patch.object(dispatch, "probe_script", return_value=fake_probe):
                code, payload = dispatch.run_probe(
                    role="coder",
                    run_id="0123456789abcdef",
                    hermes_bin=str(fake_hermes),
                    env=env,
                    bash_path=fake_bash,
                )
            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertIn("probe status must be a string", payload["diagnostic"])


class CliTests(unittest.TestCase):
    def test_runtime_bash_path(self) -> None:
        brew = Path("/opt/homebrew/bin/bash")
        if not brew.is_file():
            self.skipTest("Homebrew bash not installed")
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "hermes/scripts/dispatch.py"), "runtime", "--bash-path"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.stdout.strip(), str(brew.resolve()))
        self.assertFalse(proc.stdout.strip().startswith("{"))

    def test_probe_cli_emits_single_json_object_with_fake_executables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_hermes = tmp_path / "hermes"
            fake_bash = tmp_path / "bash"
            fake_probe = tmp_path / "probe.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho composer-2.5\n")
            write_executable(
                fake_bash,
                '#!/bin/sh\nexec "$@"\n',
            )
            write_executable(
                fake_probe,
                '#!/bin/sh\n'
                'echo \'{"status":"READY","role":"reviewer","key":"cursor-reviewer",'
                '"reason":"","diagnostic":""}\'\n',
            )

            env = os.environ.copy()
            env["HERMES_HOME"] = str(tmp_path)
            env["HERMES_BIN"] = str(fake_hermes)
            env["PATH"] = f"{tmp_path}:{env.get('PATH', '')}"
            env["DISPATCH_PROBE_SCRIPT"] = str(fake_probe)
            env["DISPATCH_BASH_PATH"] = str(fake_bash)

            wrapper = textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import os
                import sys
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from hermes.scripts import dispatch
                dispatch.probe_script = lambda: __import__('pathlib').Path(os.environ['DISPATCH_PROBE_SCRIPT'])
                dispatch.find_bash5 = lambda repo, path: __import__('pathlib').Path(os.environ['DISPATCH_BASH_PATH'])
                dispatch.default_hermes_bin = lambda: os.environ['HERMES_BIN']
                raise SystemExit(dispatch.main())
                """
            )
            launcher = tmp_path / "launcher.py"
            launcher.write_text(wrapper, encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "probe",
                    "--role",
                    "reviewer",
                    "--run-id",
                    "0123456789abcdef",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT),
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = [line for line in proc.stdout.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["run_id"], "0123456789abcdef")
            self.assertEqual(payload["status"], "READY")

    def test_probe_cli_missing_hermes_home_emits_redacted_json(self) -> None:
        env = os.environ.copy()
        env.pop("HERMES_HOME", None)
        env["PATH"] = f"/opt/homebrew/bin:{env.get('PATH', '')}"
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""\
                    import os
                    import sys
                    sys.path.insert(0, {str(REPO_ROOT)!r})
                    os.environ.pop('HERMES_HOME', None)
                    from pathlib import Path
                    from hermes.scripts import dispatch
                    dispatch.find_bash5 = lambda repo, path: Path('/opt/homebrew/bin/bash')
                    raise SystemExit(dispatch.main([
                        'probe', '--role', 'coder', '--run-id', '0123456789abcdef'
                    ]))
                    """
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
        )

        self.assertEqual(proc.returncode, 2)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("HERMES_HOME", payload["diagnostic"])
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_probe_missing_arguments_emit_structured_json(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = dispatch.main(["probe"])
        self.assertEqual(code, 2)
        payload = json.loads(buffer.getvalue().strip())
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["diagnostic"], "invalid arguments")

    def test_probe_invalid_role_emit_structured_json(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = dispatch.main(
                ["probe", "--role", "worker", "--run-id", "0123456789abcdef"]
            )
        self.assertEqual(code, 2)
        payload = json.loads(buffer.getvalue().strip())
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["diagnostic"], "invalid arguments")

    def test_probe_malformed_run_id_emit_structured_json(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            with patch.object(
                dispatch,
                "find_bash5",
                return_value=Path("/opt/homebrew/bin/bash"),
            ):
                code = dispatch.main(
                    ["probe", "--role", "coder", "--run-id", "not-valid"]
                )
        self.assertEqual(code, 2)
        payload = json.loads(buffer.getvalue().strip())
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("run id must match", payload["diagnostic"])


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class InterruptionTests(unittest.TestCase):
    def test_probe_terminates_descendants_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_hermes = tmp_path / "hermes"
            fake_bash = tmp_path / "bash"
            fake_probe = tmp_path / "probe.sh"
            pid_file = tmp_path / "pids.txt"
            write_executable(fake_hermes, "#!/bin/sh\necho composer-2.5\n")
            write_executable(
                fake_bash,
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    /bin/sleep 30 &
                    descendant=$!
                    printf '%s\\n%s\\n' "$$" "$descendant" > "{pid_file}"
                    wait
                    """
                ),
            )
            write_executable(fake_probe, "#!/bin/sh\n")

            launcher = textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import os
                import sys
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from pathlib import Path
                from hermes.scripts import dispatch
                dispatch.probe_script = lambda: Path({str(fake_probe)!r})
                dispatch.find_bash5 = lambda repo, path: Path({str(fake_bash)!r})
                dispatch.default_hermes_bin = lambda: {str(fake_hermes)!r}
                raise SystemExit(dispatch.main([
                    "probe", "--role", "coder", "--run-id", "0123456789abcdef"
                ]))
                """
            )
            launcher_path = tmp_path / "launcher.py"
            launcher_path.write_text(launcher, encoding="utf-8")

            env = os.environ.copy()
            env["HERMES_HOME"] = str(tmp_path)
            env["PATH"] = f"/bin:/usr/bin:{tmp_path}"
            proc = subprocess.Popen(
                [sys.executable, str(launcher_path)],
                env=env,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            deadline = time.time() + 5
            while not pid_file.is_file() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.is_file(), "probe child did not record process ids")
            child_pid, descendant_pid = (
                int(line) for line in pid_file.read_text(encoding="utf-8").splitlines() if line.strip()
            )
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=15)

            self.assertEqual(proc.returncode, 143)
            self.assertEqual(stdout.strip(), "")
            self.assertFalse(pid_alive(child_pid))
            self.assertFalse(pid_alive(descendant_pid))
            self.assertFalse(list(tmp_path.glob("pool-*.json")))


def cleanup_hermes_state(hermes_home: Path) -> None:
    runs = dispatch.state_dir(hermes_home)
    for path in runs.glob("*.json"):
        path.unlink(missing_ok=True)
    for path in runs.glob("*.lock"):
        path.unlink(missing_ok=True)


def init_git_repo(path: Path, *, branch: str = "feature/test") -> None:
    subprocess.run(["git", "init", "-b", branch], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )


class StateTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.hermes_home = self.tmp / "hermes-home"
        self.hermes_home.mkdir()
        self.run_id = "0123456789abcdef"
        self.repository = self.tmp / "repo"
        self.repository.mkdir()
        init_git_repo(self.repository)
        subprocess.run(
            [
                "git",
                "branch",
                f"feature-test-hermes-{self.run_id}-work",
            ],
            cwd=self.repository,
            check=True,
            capture_output=True,
        )

    def tearDown(self) -> None:
        cleanup_hermes_state(self.hermes_home)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _coder_record(self, state: str = "prepared") -> dict:
        pre_dispatch = None if state in {"prepared", "blocked", "complete"} else "a" * 40
        review_lifecycle = state in {"reviewing", "integration_pending", "integrated"}
        commit = "d" * 40
        return {
            "schema_version": dispatch.STATE_SCHEMA_VERSION,
            "git_object_format": "sha1",
            "run_id": self.run_id,
            "role": "coder",
            "state": state,
            "generation": 1,
            "repository": str(self.repository),
            "cursor_session_id": "session-1" if review_lifecycle else None,
            "coder": {
                "feature_branch": "feature/test",
                "worktree": str(
                    self.tmp / f"repo-feature-test-hermes-{self.run_id}-work"
                ),
                "work_branch": f"feature-test-hermes-{self.run_id}-work",
                "pending_commit": commit if state == "integration_pending" else None,
                "last_integrated_commit": commit if state == "integrated" else None,
                "unintegrated_commits": (
                    [commit] if state in {"reviewing", "integration_pending"} else []
                ),
                "dispatch_baseline": {
                    "pre_dispatch_work_branch_commit": pre_dispatch,
                    "pre_dispatch_feature_branch_commit": pre_dispatch,
                    "verified_worker_tree": "b" * 40 if review_lifecycle else None,
                    "verified_worker_index": "c" * 64 if review_lifecycle else None,
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
            "updated_at": dispatch._iso_timestamp(),
        }

    def _reviewer_record(self, state: str = "dispatching") -> dict:
        return {
            "schema_version": dispatch.STATE_SCHEMA_VERSION,
            "git_object_format": "sha1",
            "run_id": self.run_id,
            "role": "reviewer",
            "state": state,
            "generation": 1,
            "repository": "/tmp/repo",
            "cursor_session_id": (
                "session-1" if state in {"reviewed", "complete"} else None
            ),
            "coder": None,
            "reviewer": {
                "target": "spec",
                "document": "docs/spec.md",
                "specification": None,
                "lenses": ["backend"],
                "snapshot_outcome": (
                    "unchanged" if state in {"reviewed", "complete"} else None
                ),
                "snapshot_path": None,
            },
            "failure": "blocked" if state == "blocked" else None,
            "updated_at": dispatch._iso_timestamp(),
        }

    def test_all_allowed_coder_transitions(self) -> None:
        for current, targets in dispatch.CODER_TRANSITIONS.items():
            for target in targets:
                with self.subTest(current=current, target=target):
                    dispatch.create_state(self.hermes_home, self._coder_record(current))
                    changes = {"failure": "blocked"} if target == "blocked" else {}
                    if target in {"reviewing", "integration_pending", "integrated"}:
                        target_record = self._coder_record(target)
                        changes = {
                            "cursor_session_id": target_record["cursor_session_id"],
                            "coder": target_record["coder"],
                        }
                    updated = dispatch.transition_state(
                        self.hermes_home,
                        self.run_id,
                        1,
                        target,
                        changes,
                    )
                    self.assertEqual(updated["state"], target)
                    self.assertEqual(updated["generation"], 2)
                    cleanup_hermes_state(self.hermes_home)

    def test_all_allowed_reviewer_transitions(self) -> None:
        for current, targets in dispatch.REVIEWER_TRANSITIONS.items():
            for target in targets:
                with self.subTest(current=current, target=target):
                    dispatch.create_state(
                        self.hermes_home, self._reviewer_record(current)
                    )
                    updated = dispatch.transition_state(
                        self.hermes_home,
                        self.run_id,
                        1,
                        target,
                        (
                            {
                                "cursor_session_id": "session-1",
                                "reviewer": {
                                    **self._reviewer_record(current)["reviewer"],
                                    "snapshot_outcome": "unchanged",
                                },
                            }
                            if target == "reviewed"
                            else {"failure": "blocked"}
                            if target == "blocked"
                            else {}
                        ),
                    )
                    self.assertEqual(updated["state"], target)
                    cleanup_hermes_state(self.hermes_home)

    def test_rejects_invalid_coder_transition(self) -> None:
        dispatch.create_state(self.hermes_home, self._coder_record("prepared"))
        with self.assertRaises(dispatch.DispatchError):
            dispatch.transition_state(
                self.hermes_home, self.run_id, 1, "integrated", {}
            )

    def test_rejects_invalid_reviewer_transition(self) -> None:
        dispatch.create_state(self.hermes_home, self._reviewer_record("dispatching"))
        with self.assertRaises(dispatch.DispatchError):
            dispatch.transition_state(
                self.hermes_home, self.run_id, 1, "integrated", {}
            )

    def test_rejects_all_invalid_coder_transitions(self) -> None:
        all_states = set(dispatch.CODER_TRANSITIONS) | {"complete"}
        for current in dispatch.CODER_TRANSITIONS:
            for target in all_states:
                if target == current:
                    continue
                if target in dispatch.CODER_TRANSITIONS[current]:
                    continue
                with self.subTest(current=current, target=target):
                    dispatch.create_state(
                        self.hermes_home, self._coder_record(current)
                    )
                    with self.assertRaises(dispatch.DispatchError):
                        dispatch.transition_state(
                            self.hermes_home, self.run_id, 1, target, {}
                        )
                    cleanup_hermes_state(self.hermes_home)

    def test_rejects_all_invalid_reviewer_transitions(self) -> None:
        all_states = set(dispatch.REVIEWER_TRANSITIONS) | {"complete"}
        for current in dispatch.REVIEWER_TRANSITIONS:
            for target in all_states:
                if target == current:
                    continue
                if target in dispatch.REVIEWER_TRANSITIONS[current]:
                    continue
                with self.subTest(current=current, target=target):
                    dispatch.create_state(
                        self.hermes_home, self._reviewer_record(current)
                    )
                    with self.assertRaises(dispatch.DispatchError):
                        dispatch.transition_state(
                            self.hermes_home, self.run_id, 1, target, {}
                        )
                    cleanup_hermes_state(self.hermes_home)

    def test_dispatch_baseline_captured_on_dispatching(self) -> None:
        dispatch.create_state(self.hermes_home, self._coder_record())
        updated = dispatch.transition_state(
            self.hermes_home, self.run_id, 1, "dispatching", {}
        )
        baseline = updated["coder"]["dispatch_baseline"]
        self.assertEqual(set(baseline), dispatch.DISPATCH_BASELINE_FIELDS)

    def test_rejects_embedded_run_id_mismatch(self) -> None:
        record = self._coder_record()
        record["run_id"] = "fedcba9876543210"
        runs = dispatch.state_dir(self.hermes_home)
        dispatch.atomic_write_json(runs / f"{self.run_id}.json", record)
        with self.assertRaises(dispatch.DispatchError):
            dispatch.load_state(self.hermes_home, self.run_id)

    def test_rejects_stale_generation(self) -> None:
        dispatch.create_state(self.hermes_home, self._coder_record())
        with self.assertRaises(dispatch.DispatchError):
            dispatch.transition_state(
                self.hermes_home, self.run_id, 99, "dispatching", {}
            )

    def test_rejects_role_mismatch_payload(self) -> None:
        record = self._coder_record()
        record["reviewer"] = {"target": "spec"}
        with self.assertRaises(dispatch.DispatchError):
            dispatch.create_state(self.hermes_home, record)

    def test_state_file_and_directory_permissions(self) -> None:
        dispatch.create_state(self.hermes_home, self._coder_record())
        runs = dispatch.state_dir(self.hermes_home)
        state_path = runs / f"{self.run_id}.json"
        self.assertEqual(stat.S_IMODE(runs.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)

    def test_malformed_state_fails_closed(self) -> None:
        runs = dispatch.state_dir(self.hermes_home)
        (runs / f"{self.run_id}.json").write_text('{"bad":true}', encoding="utf-8")
        with self.assertRaises(dispatch.DispatchError):
            dispatch.load_state(self.hermes_home, self.run_id)

    def test_concurrent_writer_rejects_stale_generation(self) -> None:
        dispatch.create_state(self.hermes_home, self._coder_record())
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def worker(generation: int) -> None:
            try:
                barrier.wait(timeout=5)
                dispatch.transition_state(
                    self.hermes_home,
                    self.run_id,
                    generation,
                    "dispatching",
                    {},
                )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(1,)),
            threading.Thread(target=worker, args=(1,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], dispatch.DispatchError)


class PruneStateTests(unittest.TestCase):
    def test_prune_removes_old_record_without_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            run_id = "0123456789abcdef"
            old = datetime.now(timezone.utc) - timedelta(days=31)
            record = {
                "schema_version": 1,
                "git_object_format": "sha1",
                "run_id": run_id,
                "role": "coder",
                "state": "blocked",
                "generation": 1,
                "repository": "/tmp/repo",
                "cursor_session_id": None,
                "coder": {
                    "feature_branch": "feature/test",
                    "worktree": str(Path(tmp) / f"repo-hermes-{run_id}-work"),
                    "work_branch": f"test-hermes-{run_id}-work",
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
                "failure": "stale",
                "updated_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            dispatch.atomic_write_json(dispatch._state_path(hermes_home, run_id), record)
            removed = dispatch.prune_state(hermes_home, datetime.now(timezone.utc))
            self.assertEqual(removed, [run_id])
            self.assertFalse(dispatch._state_path(hermes_home, run_id).exists())
            lock_path = dispatch._lock_path(hermes_home, run_id)
            lock_path.touch()
            self.assertTrue(lock_path.exists())

    def test_prune_retains_existing_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            run_id = "0123456789abcdef"
            lock_path = dispatch._lock_path(hermes_home, run_id)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.touch()
            old = datetime.now(timezone.utc) - timedelta(days=31)
            record = {
                "schema_version": 1,
                "git_object_format": "sha1",
                "run_id": run_id,
                "role": "coder",
                "state": "blocked",
                "generation": 1,
                "repository": "/tmp/repo",
                "cursor_session_id": None,
                "coder": {
                    "feature_branch": "feature/test",
                    "worktree": str(Path(tmp) / f"repo-hermes-{run_id}-work"),
                    "work_branch": f"test-hermes-{run_id}-work",
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
                "failure": "stale",
                "updated_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            dispatch.atomic_write_json(dispatch._state_path(hermes_home, run_id), record)
            dispatch.prune_state(hermes_home, datetime.now(timezone.utc))
            self.assertTrue(lock_path.exists())

    def test_prune_refuses_live_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            run_id = "0123456789abcdef"
            worktree = Path(tmp) / f"live-hermes-{run_id}-work"
            worktree.mkdir()
            old = datetime.now(timezone.utc) - timedelta(days=31)
            record = {
                "schema_version": 1,
                "git_object_format": "sha1",
                "run_id": run_id,
                "role": "coder",
                "state": "blocked",
                "generation": 1,
                "repository": "/tmp/repo",
                "cursor_session_id": None,
                "coder": {
                    "feature_branch": "feature/test",
                    "worktree": str(worktree),
                    "work_branch": f"test-hermes-{run_id}-work",
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
                "failure": "stale",
                "updated_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            dispatch.atomic_write_json(dispatch._state_path(hermes_home, run_id), record)
            removed = dispatch.prune_state(hermes_home, datetime.now(timezone.utc))
            self.assertEqual(removed, [])
            self.assertTrue(dispatch._state_path(hermes_home, run_id).exists())


class WorktreeDispatchTests(unittest.TestCase):
    def test_prepare_transitions_preparing_record_to_generation_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo)
            hermes_home = Path(tmp) / "hermes-home"
            hermes_home.mkdir()
            run_id = "0123456789abcdef"
            bash = Path("/opt/homebrew/bin/bash")
            if not bash.is_file():
                self.skipTest("Homebrew bash not installed")
            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}, clear=False):
                payload = dispatch.run_worktree_prepare(
                    repository=repo,
                    run_id=run_id,
                    bash_path=bash,
                    hermes_home=hermes_home,
                )
            self.assertEqual(payload["status"], "READY")
            self.assertEqual(payload["generation"], 2)
            state = dispatch.load_state(hermes_home, run_id)
            self.assertEqual(state["state"], "prepared")
            self.assertEqual(state["role"], "coder")

    def test_remove_is_idempotent_when_already_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo)
            hermes_home = Path(tmp) / "hermes-home"
            hermes_home.mkdir()
            run_id = "0123456789abcdef"
            bash = Path("/opt/homebrew/bin/bash")
            if not bash.is_file():
                self.skipTest("Homebrew bash not installed")
            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}, clear=False):
                dispatch.run_worktree_prepare(
                    repository=repo,
                    run_id=run_id,
                    bash_path=bash,
                    hermes_home=hermes_home,
                )
                state = dispatch.load_state(hermes_home, run_id)
                payload = dispatch.run_worktree_remove(
                    repository=repo,
                    run_id=run_id,
                    expected_generation=state["generation"],
                    bash_path=bash,
                    hermes_home=hermes_home,
                )
            self.assertEqual(payload["status"], "REMOVED")
            self.assertEqual(
                dispatch.load_state(hermes_home, run_id)["state"], "complete"
            )
            self.assertTrue(dispatch._lock_path(hermes_home, run_id).exists())

    def test_remove_rejects_controller_not_on_feature_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo)
            hermes_home = Path(tmp) / "hermes-home"
            run_id = "0123456789abcdef"
            bash = Path("/opt/homebrew/bin/bash")
            if not bash.is_file():
                self.skipTest("Homebrew bash not installed")
            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}, clear=False):
                dispatch.run_worktree_prepare(
                    repository=repo,
                    run_id=run_id,
                    bash_path=bash,
                    hermes_home=hermes_home,
                )
                state = dispatch.load_state(hermes_home, run_id)
                subprocess.run(
                    ["git", "checkout", "-b", "other-branch"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                )
                with self.assertRaises(dispatch.DispatchError):
                    dispatch.run_worktree_remove(
                        repository=repo,
                        run_id=run_id,
                        expected_generation=state["generation"],
                        bash_path=bash,
                        hermes_home=hermes_home,
                    )


class ReviewCloneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        init_git_repo(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/app.py"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add app"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        self.doc = self.repo / "docs" / "spec.md"
        self.doc.parent.mkdir()
        self.doc.write_text("# Spec\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rejects_non_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "not-git"
            bad.mkdir()
            doc = bad / "spec.md"
            doc.write_text("x", encoding="utf-8")
            args = argparse.Namespace(
                target="spec",
                run_id="0123456789abcdef",
                doc_file=str(doc),
                spec_file=None,
                lenses=None,
                expected_generation=None,
                session=None,
                repo=str(bad),
            )
            with patch.dict(os.environ, {"HERMES_HOME": tmp}, clear=False):
                with self.assertRaises(dispatch.DispatchError):
                    dispatch.run_review(args)

    def test_rejects_out_of_repository_document(self) -> None:
        outside = self.tmp / "outside.md"
        outside.write_text("x", encoding="utf-8")
        args = argparse.Namespace(
            target="spec",
            run_id="0123456789abcdef",
            doc_file=str(outside),
            spec_file=None,
            lenses=None,
            expected_generation=None,
            session=None,
            repo=str(self.repo),
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.tmp)}, clear=False):
            with self.assertRaises(dispatch.DispatchError):
                dispatch.run_review(args)

    def test_rejects_unrelated_dirty_files(self) -> None:
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        args = argparse.Namespace(
            target="spec",
            run_id="0123456789abcdef",
            doc_file=str(self.doc),
            spec_file=None,
            lenses=None,
            expected_generation=None,
            session=None,
            repo=str(self.repo),
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.tmp)}, clear=False):
            with self.assertRaises(dispatch.DispatchError):
                dispatch.run_review(args)

    def test_rejects_parent_symlink_component(self) -> None:
        real = self.repo / "real"
        real.mkdir()
        (real / "spec.md").write_text("# Spec\n", encoding="utf-8")
        linked = self.repo / "linked"
        os.symlink(real, linked)
        args = argparse.Namespace(
            target="spec",
            run_id="0123456789abcdef",
            doc_file=str(linked / "spec.md"),
            spec_file=None,
            lenses=None,
            expected_generation=None,
            session=None,
            repo=str(self.repo),
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.tmp)}, clear=False):
            with self.assertRaises(dispatch.DispatchError):
                dispatch.run_review(args)

    def test_manifest_includes_directory_symlink(self) -> None:
        target = self.repo / "target-dir"
        target.mkdir()
        os.symlink(target, self.repo / "linked-dir")
        manifest = dispatch.source_manifest(self.repo)
        self.assertEqual(manifest["linked-dir"]["type"], "symlink")

    def test_clone_has_no_hardlinked_object_files(self) -> None:
        clone = dispatch.create_review_clone(
            self.repo,
            "0123456789abcdef",
            [self.doc],
        )
        self.addCleanup(lambda: dispatch.cleanup_review_clone(clone, False))
        controller_objects = dispatch._git_common_dir(self.repo) / "objects"
        clone_objects = dispatch._git_common_dir(clone.path) / "objects"
        self.assertNotEqual(controller_objects.resolve(), clone_objects.resolve())
        for path in clone_objects.rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertEqual(path.stat().st_nlink, 1)

    def test_clone_is_independent_and_overlays_documents(self) -> None:
        clone = dispatch.create_review_clone(
            self.repo,
            "0123456789abcdef",
            [self.doc],
        )
        self.addCleanup(lambda: dispatch.cleanup_review_clone(clone, False))
        self.assertTrue(dispatch._clone_is_independent(self.repo, clone.path))
        copied = clone.path / "docs" / "spec.md"
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_text(encoding="utf-8"), self.doc.read_text(encoding="utf-8"))

    def test_manifest_detects_content_and_mode_changes(self) -> None:
        before = dispatch.source_manifest(self.repo)
        target = self.repo / "src" / "app.py"
        target.write_text("print('changed')\n", encoding="utf-8")
        after = dispatch.source_manifest(self.repo)
        self.assertNotEqual(before["src/app.py"], after["src/app.py"])
        target.chmod(0o755)
        after_mode = dispatch.source_manifest(self.repo)
        self.assertNotEqual(after["src/app.py"], after_mode["src/app.py"])

    def test_cleanup_preserves_changed_snapshot(self) -> None:
        clone = dispatch.create_review_clone(
            self.repo,
            "0123456789abcdef",
            [self.doc],
        )
        (clone.path / "docs" / "spec.md").write_text("# mutated\n", encoding="utf-8")
        preserved = dispatch.cleanup_review_clone(clone, True)
        self.assertIsNotNone(preserved)
        assert preserved is not None
        self.assertTrue(Path(preserved).exists())
        shutil.rmtree(preserved, ignore_errors=True)

    def test_cleanup_removes_unchanged_snapshot(self) -> None:
        clone = dispatch.create_review_clone(
            self.repo,
            "0123456789abcdef",
            [self.doc],
        )
        preserved = dispatch.cleanup_review_clone(clone, False)
        self.assertIsNone(preserved)
        self.assertFalse(clone.path.exists())


class ReviewRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        init_git_repo(self.repo)
        self.doc = self.repo / "spec.md"
        self.doc.write_text("# Spec\n", encoding="utf-8")
        subprocess.run(["git", "add", "spec.md"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add spec"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        self.hermes_home = self.tmp / "hermes-home"
        self.hermes_home.mkdir()
        self.run_id = "0123456789abcdef"

    def tearDown(self) -> None:
        cleanup_hermes_state(self.hermes_home)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_review_env(self) -> tuple[Path, Path, Path]:
        fake_hermes = self.tmp / "hermes"
        fake_bash = self.tmp / "bash"
        fake_delegate = self.tmp / "review-delegate.sh"
        write_executable(fake_hermes, "#!/bin/sh\necho gpt-5.6-sol-high\n")
        write_executable(fake_bash, '#!/bin/sh\nexec "$@"\n')
        write_executable(
            fake_delegate,
            textwrap.dedent(
                """\
                #!/bin/sh
                echo '{"status":"REVIEWED","reviewer":"cursor-reviewer","session_id":"sess-1","target":"spec","lenses":["backend"],"report":"good review","diagnostic":""}'
                """
            ),
        )
        return fake_hermes, fake_bash, fake_delegate

    def test_initial_review_returns_exact_schema(self) -> None:
        fake_hermes, fake_bash, fake_delegate = self._fake_review_env()
        args = argparse.Namespace(
            target="spec",
            run_id=self.run_id,
            doc_file=str(self.doc),
            spec_file=None,
            lenses="backend",
            expected_generation=None,
            session=None,
            repo=str(self.repo),
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home)}, clear=False), patch.object(
            dispatch, "default_hermes_bin", return_value=str(fake_hermes)
        ), patch.object(dispatch, "find_bash5", return_value=fake_bash), patch.object(
            dispatch, "review_delegate_script", return_value=fake_delegate
        ):
            payload = dispatch.run_review(args)
        self.assertEqual(set(payload.keys()), dispatch.REVIEW_OUTPUT_FIELDS)
        self.assertEqual(payload["status"], "REVIEWED")
        self.assertEqual(payload["generation"], 2)
        self.assertEqual(payload["session_id"], "sess-1")
        self.assertEqual(payload["report"], "good review")
        state = dispatch.load_state(self.hermes_home, self.run_id)
        self.assertEqual(state["state"], "reviewed")
        self.assertEqual(state["cursor_session_id"], "sess-1")

    def test_initial_review_rejects_session(self) -> None:
        args = argparse.Namespace(
            target="spec",
            run_id=self.run_id,
            doc_file=str(self.doc),
            spec_file=None,
            lenses="backend",
            expected_generation=None,
            session="sess-existing",
            repo=str(self.repo),
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home)}, clear=False):
            with self.assertRaises(dispatch.DispatchError):
                dispatch.run_review(args)

    def test_preserves_delegate_blocked_diagnostic(self) -> None:
        fake_hermes = self.tmp / "hermes"
        fake_bash = self.tmp / "bash"
        fake_delegate = self.tmp / "review-delegate.sh"
        write_executable(fake_hermes, "#!/bin/sh\necho gpt-5.6-sol-high\n")
        write_executable(fake_bash, '#!/bin/sh\nexec "$@"\n')
        write_executable(
            fake_delegate,
            '#!/bin/sh\n'
            'echo \'{"status":"BLOCKED","reviewer":"cursor-reviewer",'
            '"session_id":"review-session-1","target":"spec","lenses":["backend"],'
            '"report":"","diagnostic":"auth token expired for review-session-1"}\'\n'
            "exit 1\n",
        )
        args = argparse.Namespace(
            target="spec",
            run_id=self.run_id,
            doc_file=str(self.doc),
            spec_file=None,
            lenses="backend",
            expected_generation=None,
            session=None,
            repo=str(self.repo),
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home)}, clear=False), patch.object(
            dispatch, "default_hermes_bin", return_value=str(fake_hermes)
        ), patch.object(dispatch, "find_bash5", return_value=fake_bash), patch.object(
            dispatch, "review_delegate_script", return_value=fake_delegate
        ):
            payload = dispatch.run_review(args)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("auth token expired", payload["diagnostic"])
        self.assertNotIn("review-session-1", payload["diagnostic"])
        self.assertEqual(payload["session_id"], "review-session-1")
        state = dispatch.load_state(self.hermes_home, self.run_id)
        self.assertEqual(state["cursor_session_id"], "review-session-1")
        self.assertNotIn("review-session-1", state["failure"])
        self.assertEqual(set(payload.keys()), dispatch.REVIEW_OUTPUT_FIELDS)

    def test_rejects_empty_session_id(self) -> None:
        fake_hermes = self.tmp / "hermes"
        fake_bash = self.tmp / "bash"
        fake_delegate = self.tmp / "review-delegate.sh"
        write_executable(fake_hermes, "#!/bin/sh\necho gpt-5.6-sol-high\n")
        write_executable(fake_bash, '#!/bin/sh\nexec "$@"\n')
        write_executable(
            fake_delegate,
            '#!/bin/sh\n'
            'echo \'{"status":"REVIEWED","reviewer":"cursor-reviewer",'
            '"session_id":"","target":"spec","lenses":[],"report":"report","diagnostic":""}\'\n',
        )
        args = argparse.Namespace(
            target="spec",
            run_id=self.run_id,
            doc_file=str(self.doc),
            spec_file=None,
            lenses=None,
            expected_generation=None,
            session=None,
            repo=str(self.repo),
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home)}, clear=False), patch.object(
            dispatch, "default_hermes_bin", return_value=str(fake_hermes)
        ), patch.object(dispatch, "find_bash5", return_value=fake_bash), patch.object(
            dispatch, "review_delegate_script", return_value=fake_delegate
        ):
            payload = dispatch.run_review(args)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("session id", payload["diagnostic"])

    def test_resume_requires_session_and_advances_generation(self) -> None:
        fake_hermes, fake_bash, fake_delegate = self._fake_review_env()
        initial = argparse.Namespace(
            target="spec",
            run_id=self.run_id,
            doc_file=str(self.doc),
            spec_file=None,
            lenses="backend",
            expected_generation=None,
            session=None,
            repo=str(self.repo),
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home)}, clear=False), patch.object(
            dispatch, "default_hermes_bin", return_value=str(fake_hermes)
        ), patch.object(dispatch, "find_bash5", return_value=fake_bash), patch.object(
            dispatch, "review_delegate_script", return_value=fake_delegate
        ):
            first = dispatch.run_review(initial)
            resume = argparse.Namespace(
                target="spec",
                run_id=self.run_id,
                doc_file=str(self.doc),
                spec_file=None,
                lenses="backend",
                expected_generation=first["generation"],
                session=first["session_id"],
                repo=str(self.repo),
            )
            second = dispatch.run_review(resume)
        self.assertEqual(second["status"], "REVIEWED")
        self.assertEqual(second["session_id"], "sess-1")
        self.assertGreater(second["generation"], first["generation"])


class CrashReconciliationTests(unittest.TestCase):
    def test_dispatching_reconciles_to_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            run_id = "0123456789abcdef"
            dispatch.create_state(
                hermes_home,
                {
                    "schema_version": 1,
                    "git_object_format": "sha1",
                    "run_id": run_id,
                    "role": "coder",
                    "state": "dispatching",
                    "generation": 1,
                    "repository": "/tmp/repo",
                    "cursor_session_id": None,
                    "coder": {
                        "feature_branch": "feature/test",
                        "worktree": f"/tmp/missing-hermes-{run_id}-work",
                        "work_branch": f"test-hermes-{run_id}-work",
                        "pending_commit": None,
                        "last_integrated_commit": None,
                        "unintegrated_commits": [],
                        "dispatch_baseline": {
                            "pre_dispatch_work_branch_commit": "a" * 40,
                            "pre_dispatch_feature_branch_commit": "a" * 40,
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
                    "updated_at": dispatch._iso_timestamp(),
                },
            )
            record = dispatch.reconcile_coder_state(hermes_home, run_id)
            self.assertEqual(record["state"], "blocked")

    def test_dispatching_with_unverified_commit_reconciles_to_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo, branch="feature/test")
            work_branch = f"feature-test-hermes-0123456789abcdef-work"
            pre_dispatch = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "branch", work_branch],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", work_branch],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            (repo / "worker.txt").write_text("worker\n", encoding="utf-8")
            subprocess.run(["git", "add", "worker.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "worker"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", "feature/test"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            hermes_home = Path(tmp) / "hermes-home"
            run_id = "0123456789abcdef"
            dispatch.create_state(
                hermes_home,
                {
                    "schema_version": 1,
                    "git_object_format": "sha1",
                    "run_id": run_id,
                    "role": "coder",
                    "state": "dispatching",
                    "generation": 1,
                    "repository": str(repo),
                    "cursor_session_id": None,
                    "coder": {
                        "feature_branch": "feature/test",
                        "worktree": str(
                            Path(tmp)
                            / "repo-feature-test-hermes-0123456789abcdef-work"
                        ),
                        "work_branch": work_branch,
                        "pending_commit": None,
                        "last_integrated_commit": None,
                        "unintegrated_commits": [],
                        "dispatch_baseline": {
                            "pre_dispatch_work_branch_commit": pre_dispatch,
                            "pre_dispatch_feature_branch_commit": pre_dispatch,
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
                    "updated_at": dispatch._iso_timestamp(),
                },
            )
            record = dispatch.reconcile_coder_state(hermes_home, run_id)
            self.assertEqual(record["state"], "blocked")

    def test_reviewing_state_is_left_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            run_id = "0123456789abcdef"
            dispatch.create_state(
                hermes_home,
                {
                    "schema_version": 1,
                    "git_object_format": "sha1",
                    "run_id": run_id,
                    "role": "coder",
                    "state": "reviewing",
                    "generation": 2,
                    "repository": "/tmp/repo",
                    "cursor_session_id": "session-1",
                    "coder": {
                        "feature_branch": "feature/test",
                        "worktree": f"/tmp/missing-hermes-{run_id}-work",
                        "work_branch": f"test-hermes-{run_id}-work",
                        "pending_commit": None,
                        "last_integrated_commit": None,
                        "unintegrated_commits": ["a" * 40],
                        "dispatch_baseline": {
                            "pre_dispatch_work_branch_commit": "a" * 40,
                            "pre_dispatch_feature_branch_commit": "a" * 40,
                            "verified_worker_tree": "b" * 40,
                            "verified_worker_index": "c" * 64,
                        },
                        "budget": {
                            "cursor_calls": 1,
                            "controller_review_rounds": 1,
                            "active_worker_seconds": 1.0,
                        },
                        "protected_manifest": None,
                    },
                    "reviewer": None,
                    "failure": None,
                    "updated_at": dispatch._iso_timestamp(),
                },
            )
            with patch.object(
                dispatch, "_resolve_branch_head", return_value="a" * 40
            ), patch.object(
                dispatch, "_authorized_controller_commit", return_value=(True, "")
            ):
                record = dispatch.reconcile_coder_state(hermes_home, run_id)
            self.assertEqual(record["state"], "reviewing")
            self.assertEqual(record["generation"], 2)


class WorktreeIdentityTests(unittest.TestCase):
    def test_remove_rejects_unrelated_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            other = Path(tmp) / "other"
            repo.mkdir()
            other.mkdir()
            init_git_repo(repo)
            hermes_home = Path(tmp) / "hermes-home"
            run_id = "0123456789abcdef"
            bash = Path("/opt/homebrew/bin/bash")
            if not bash.is_file():
                self.skipTest("Homebrew bash not installed")
            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}, clear=False):
                dispatch.run_worktree_prepare(
                    repository=repo,
                    run_id=run_id,
                    bash_path=bash,
                    hermes_home=hermes_home,
                )
                state = dispatch.load_state(hermes_home, run_id)
                with self.assertRaises(dispatch.DispatchError):
                    dispatch.run_worktree_remove(
                        repository=other,
                        run_id=run_id,
                        expected_generation=state["generation"],
                        bash_path=bash,
                        hermes_home=hermes_home,
                    )

    def test_remove_uses_transitioned_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo)
            hermes_home = Path(tmp) / "hermes-home"
            run_id = "0123456789abcdef"
            bash = Path("/opt/homebrew/bin/bash")
            if not bash.is_file():
                self.skipTest("Homebrew bash not installed")
            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}, clear=False):
                dispatch.run_worktree_prepare(
                    repository=repo,
                    run_id=run_id,
                    bash_path=bash,
                    hermes_home=hermes_home,
                )
                state = dispatch.load_state(hermes_home, run_id)
                payload = dispatch.run_worktree_remove(
                    repository=repo,
                    run_id=run_id,
                    expected_generation=state["generation"],
                    bash_path=bash,
                    hermes_home=hermes_home,
                )
            self.assertEqual(payload["status"], "REMOVED")
            self.assertGreater(payload["generation"], state["generation"])
            self.assertEqual(
                dispatch.load_state(hermes_home, run_id)["state"], "complete"
            )


class AdapterContractTests(unittest.TestCase):
    HERMES_SCENARIOS = {
        "missing-configuration": "test_scenario_missing_configuration",
        "authentication-failure": "test_scenario_authentication_failure",
        "missing-session-id": "test_scenario_missing_session_id",
        "empty-verification": "test_scenario_empty_verification",
        "failed-verification": "test_scenario_failed_verification",
        "session-resume": "test_scenario_session_resume",
        "dirty-controller-checkout": "test_scenario_dirty_controller_checkout",
        "concurrent-worktrees": "test_scenario_concurrent_worktrees",
        "unintegrated-cleanup": "test_scenario_unintegrated_cleanup",
    }

    @classmethod
    def setUpClass(cls) -> None:
        contract = json.loads(
            (REPO_ROOT / "tests" / "adapter-contract.json").read_text(encoding="utf-8")
        )
        hermes_ids = {
            scenario["id"]
            for scenario in contract["scenarios"]
            if "hermes" in scenario["applies_to"]
        }
        missing = hermes_ids - set(cls.HERMES_SCENARIOS)
        if missing:
            raise AssertionError(f"missing executable mappings for: {sorted(missing)}")
        for method_name in cls.HERMES_SCENARIOS.values():
            if not hasattr(cls, method_name):
                raise AssertionError(f"missing test method: {method_name}")

    def test_scenario_missing_configuration(self) -> None:
        output = StringIO()
        env = dict(os.environ)
        env.pop("HERMES_HOME", None)
        with patch.dict(os.environ, env, clear=True), redirect_stdout(output):
            rc = dispatch.main(
                [
                    "probe",
                    "--role",
                    "reviewer",
                    "--run-id",
                    "0123456789abcdef",
                ]
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(rc, 2)
        self.assertEqual(set(payload), dispatch.PROBE_FIELDS | {"run_id"})
        self.assertEqual(payload["status"], "BLOCKED")

    def test_scenario_authentication_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_hermes = Path(tmp) / "hermes"
            fake_bash = Path(tmp) / "bash"
            fake_probe = Path(tmp) / "probe.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho gpt-5.6-sol-high\n")
            write_executable(fake_bash, '#!/bin/sh\nexec "$@"\n')
            write_executable(
                fake_probe,
                '#!/bin/sh\n'
                'echo \'{"status":"FAILED","role":"reviewer","key":"cursor-reviewer",'
                '"reason":"auth","diagnostic":"auth failed"}\'\n'
                "exit 1\n",
            )
            env = {
                "HERMES_HOME": tmp,
                "PATH": str(Path(tmp)),
                "HOME": tmp,
                "TMPDIR": tmp,
                "USER": "tester",
                "SHELL": "/bin/sh",
            }
            with patch.object(dispatch, "probe_script", return_value=fake_probe):
                exit_code, payload = dispatch.run_probe(
                    role="reviewer",
                    run_id="0123456789abcdef",
                    hermes_bin=str(fake_hermes),
                    env=env,
                    bash_path=fake_bash,
                )
            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["status"], "FAILED")
            self.assertEqual(payload["diagnostic"], "auth failed")

    def test_scenario_missing_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_git_repo(repo)
            doc = repo / "spec.md"
            doc.write_text("# Spec\n", encoding="utf-8")
            subprocess.run(["git", "add", "spec.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "spec"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            fake_hermes = root / "hermes"
            fake_bash = root / "bash"
            fake_delegate = root / "review-delegate.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho reviewer-model\n")
            write_executable(fake_bash, '#!/bin/sh\nexec "$@"\n')
            write_executable(
                fake_delegate,
                '#!/bin/sh\n'
                'echo \'{"status":"REVIEWED","reviewer":"cursor-reviewer",'
                '"session_id":"","target":"spec","lenses":["backend"],'
                '"report":"report","diagnostic":""}\'\n',
            )
            args = argparse.Namespace(
                target="spec",
                run_id="0123456789abcdef",
                doc_file=str(doc),
                spec_file=None,
                lenses="backend",
                expected_generation=None,
                session=None,
                repo=str(repo),
            )
            with patch.dict(
                os.environ, {"HERMES_HOME": str(root / "home")}, clear=False
            ), patch.object(
                dispatch, "default_hermes_bin", return_value=str(fake_hermes)
            ), patch.object(
                dispatch, "find_bash5", return_value=fake_bash
            ), patch.object(
                dispatch, "review_delegate_script", return_value=fake_delegate
            ):
                payload = dispatch.run_review(args)
            self.assertEqual(set(payload), dispatch.REVIEW_OUTPUT_FIELDS)
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertIn("session id", payload["diagnostic"])

    def test_scenario_empty_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with patch.dict(os.environ, {"HERMES_HOME": tmp}, clear=False), redirect_stdout(output):
                rc = dispatch.main(
                    [
                        "code",
                        "--repo",
                        "/repo",
                        "--cwd",
                        "/worktree",
                        "--run-id",
                        "0123456789abcdef",
                        "--task-file",
                        "/task",
                        "--expected-generation",
                        "1",
                        "--verify-cmd",
                        "",
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 2)
            self.assertEqual(set(payload), dispatch.CODE_OUTPUT_FIELDS)
            self.assertIn("verification", payload["diagnostic"])

    def test_scenario_failed_verification(self) -> None:
        bash = Path("/opt/homebrew/bin/bash")
        if not bash.is_file():
            self.skipTest("Homebrew Bash 5 is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_git_repo(repo)
            home = root / "home"
            prepared = dispatch.run_worktree_prepare(
                repository=repo,
                run_id="0123456789abcdef",
                bash_path=bash,
                hermes_home=home,
            )
            task = root / "task"
            task.write_text("bounded task\n", encoding="utf-8")
            fake_hermes = root / "hermes"
            fake_delegate = root / "code-delegate.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho coder-model\n")
            write_executable(
                fake_delegate,
                "#!/bin/sh\n"
                "echo '{\"status\":\"BLOCKED\",\"coder\":\"cursor-coder\","
                "\"session_id\":\"session-1\",\"attempts\":3,\"verified\":false,"
                "\"changed\":false,\"commit_id\":\"\",\"result\":\"failed\","
                "\"verify_output\":\"verification failed\"}'\n"
                "exit 1\n",
            )
            args = argparse.Namespace(
                repo=str(repo),
                cwd=prepared["worktree"],
                run_id="0123456789abcdef",
                task_file=str(task),
                expected_generation=prepared["generation"],
                verify_cmd="false",
                session=None,
                max_retries=3,
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch.object(
                dispatch, "default_hermes_bin", return_value=str(fake_hermes)
            ), patch.object(dispatch, "find_bash5", return_value=bash), patch.object(
                dispatch, "code_delegate_script", return_value=fake_delegate
            ):
                payload = dispatch.run_coder(args)
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertFalse(payload["verified"])
            failed_state = dispatch.load_state(home, args.run_id)
            self.assertEqual(failed_state["state"], "blocked")
            self.assertEqual(failed_state["coder"]["budget"]["cursor_calls"], 4)
            self.assertGreater(
                failed_state["coder"]["budget"]["active_worker_seconds"], 0
            )

    def test_scenario_session_resume(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            repo = tmp / "repo"
            repo.mkdir()
            init_git_repo(repo)
            doc = repo / "spec.md"
            doc.write_text("# Spec\n", encoding="utf-8")
            subprocess.run(["git", "add", "spec.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add spec"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            hermes_home = tmp / "hermes-home"
            run_id = "0123456789abcdef"
            fake_hermes = tmp / "hermes"
            fake_bash = tmp / "bash"
            fake_delegate = tmp / "review-delegate.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho gpt-5.6-sol-high\n")
            write_executable(fake_bash, '#!/bin/sh\nexec "$@"\n')
            write_executable(
                fake_delegate,
                '#!/bin/sh\n'
                'echo \'{"status":"REVIEWED","reviewer":"cursor-reviewer",'
                '"session_id":"sess-1","target":"spec","lenses":["backend"],'
                '"report":"ok","diagnostic":""}\'\n',
            )
            initial = argparse.Namespace(
                target="spec",
                run_id=run_id,
                doc_file=str(doc),
                spec_file=None,
                lenses="backend",
                expected_generation=None,
                session=None,
                repo=str(repo),
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}, clear=False), patch.object(
                dispatch, "default_hermes_bin", return_value=str(fake_hermes)
            ), patch.object(dispatch, "find_bash5", return_value=fake_bash), patch.object(
                dispatch, "review_delegate_script", return_value=fake_delegate
            ):
                first = dispatch.run_review(initial)
                resume = argparse.Namespace(
                    target="spec",
                    run_id=run_id,
                    doc_file=str(doc),
                    spec_file=None,
                    lenses="backend",
                    expected_generation=first["generation"],
                    session=first["session_id"],
                    repo=str(repo),
                )
                second = dispatch.run_review(resume)
            self.assertEqual(second["session_id"], "sess-1")
            self.assertGreater(second["generation"], first["generation"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_scenario_dirty_controller_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo)
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            args = argparse.Namespace(
                target="spec",
                run_id="0123456789abcdef",
                doc_file=str(repo / "README.md"),
                spec_file=None,
                lenses=None,
                expected_generation=None,
                session=None,
                repo=str(repo),
            )
            with patch.dict(os.environ, {"HERMES_HOME": tmp}, clear=False):
                with self.assertRaises(dispatch.DispatchError):
                    dispatch.run_review(args)

    def test_scenario_concurrent_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo)
            hermes_home = Path(tmp) / "hermes-home"
            bash = Path("/opt/homebrew/bin/bash")
            if not bash.is_file():
                self.skipTest("Homebrew bash not installed")
            run_a = "0123456789abcdef"
            run_b = "fedcba9876543210"
            barrier = threading.Barrier(2)
            results: list[dict] = []
            errors: list[BaseException] = []

            def prepare(run_id: str) -> None:
                try:
                    barrier.wait(timeout=5)
                    results.append(
                        dispatch.run_worktree_prepare(
                            repository=repo,
                            run_id=run_id,
                            bash_path=bash,
                            hermes_home=hermes_home,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}, clear=False):
                threads = [
                    threading.Thread(target=prepare, args=(run_a,)),
                    threading.Thread(target=prepare, args=(run_b,)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(len({result["worktree"] for result in results}), 2)
            self.assertEqual(len({result["work_branch"] for result in results}), 2)

    def test_scenario_unintegrated_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo)
            hermes_home = Path(tmp) / "hermes-home"
            run_id = "0123456789abcdef"
            bash = Path("/opt/homebrew/bin/bash")
            if not bash.is_file():
                self.skipTest("Homebrew bash not installed")
            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}, clear=False):
                prepared = dispatch.run_worktree_prepare(
                    repository=repo,
                    run_id=run_id,
                    bash_path=bash,
                    hermes_home=hermes_home,
                )
                worktree = Path(prepared["worktree"])
                (worktree / "unintegrated.txt").write_text(
                    "unintegrated\n", encoding="utf-8"
                )
                subprocess.run(
                    ["git", "add", "unintegrated.txt"],
                    cwd=worktree,
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "unintegrated"],
                    cwd=worktree,
                    check=True,
                    capture_output=True,
                )
                state = dispatch.load_state(hermes_home, run_id)
                payload = dispatch.run_worktree_remove(
                    repository=repo,
                    run_id=run_id,
                    expected_generation=state["generation"],
                    bash_path=bash,
                    hermes_home=hermes_home,
                )
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertTrue(Path(prepared["worktree"]).exists())
            preserved = dispatch.load_state(hermes_home, run_id)
            self.assertEqual(preserved["state"], "prepared")
            self.assertEqual(preserved["generation"], state["generation"] + 1)
            self.assertIn("commits not on the feature branch", preserved["failure"])


class ReviewDelegatePathTests(unittest.TestCase):
    def test_passes_clone_document_path_to_delegate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo)
            doc = repo / "spec.md"
            doc.write_text("# Spec\n", encoding="utf-8")
            subprocess.run(["git", "add", "spec.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add spec"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            hermes_home = Path(tmp) / "hermes-home"
            captured: dict[str, str] = {}
            fake_hermes = Path(tmp) / "hermes"
            fake_bash = Path(tmp) / "bash"
            fake_delegate = Path(tmp) / "review-delegate.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho gpt-5.6-sol-high\n")
            write_executable(fake_bash, '#!/bin/sh\nexec "$@"\n')
            write_executable(
                fake_delegate,
                '#!/bin/sh\n'
                'echo \'{"status":"REVIEWED","reviewer":"cursor-reviewer",'
                '"session_id":"sess-1","target":"spec","lenses":["backend"],'
                '"report":"ok","diagnostic":""}\'\n',
            )
            real_timed = dispatch._run_script_json_timed

            def tracking_timed(**kwargs):
                captured["doc"] = kwargs["args"][kwargs["args"].index("--doc-file") + 1]
                return real_timed(**kwargs)

            args = argparse.Namespace(
                target="spec",
                run_id="0123456789abcdef",
                doc_file=str(doc),
                spec_file=None,
                lenses="backend",
                expected_generation=None,
                session=None,
                repo=str(repo),
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}, clear=False), patch.object(
                dispatch, "default_hermes_bin", return_value=str(fake_hermes)
            ), patch.object(dispatch, "find_bash5", return_value=fake_bash), patch.object(
                dispatch, "review_delegate_script", return_value=fake_delegate
            ), patch.object(dispatch, "_run_script_json_timed", side_effect=tracking_timed):
                dispatch.run_review(args)
            self.assertIn("/claude-subagents/review-artifacts/", captured["doc"])
            self.assertTrue(captured["doc"].endswith("/spec.md"))

    def test_two_reviews_use_distinct_clone_document_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo)
            doc = repo / "spec.md"
            doc.write_text("# Spec\n", encoding="utf-8")
            subprocess.run(["git", "add", "spec.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add spec"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            hermes_home = Path(tmp) / "hermes-home"
            captured: list[tuple[str, list[str]]] = []
            fake_hermes = Path(tmp) / "hermes"
            fake_bash = Path(tmp) / "bash"
            fake_delegate = Path(tmp) / "review-delegate.sh"
            write_executable(fake_hermes, "#!/bin/sh\necho gpt-5.6-sol-high\n")
            write_executable(fake_bash, '#!/bin/sh\nexec "$@"\n')
            write_executable(
                fake_delegate,
                '#!/bin/sh\n'
                'echo \'{"status":"REVIEWED","reviewer":"cursor-reviewer",'
                '"session_id":"sess-1","target":"spec","lenses":["backend"],'
                '"report":"ok","diagnostic":""}\'\n',
            )
            real_timed = dispatch._run_script_json_timed

            def tracking_timed(**kwargs):
                captured.append(
                    (
                        kwargs["args"][kwargs["args"].index("--doc-file") + 1],
                        list(kwargs["args"]),
                    )
                )
                return real_timed(**kwargs)

            args = argparse.Namespace(
                target="spec",
                run_id="0123456789abcdef",
                doc_file=str(doc),
                spec_file=None,
                lenses="backend",
                expected_generation=None,
                session=None,
                repo=str(repo),
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}, clear=False), patch.object(
                dispatch, "default_hermes_bin", return_value=str(fake_hermes)
            ), patch.object(dispatch, "find_bash5", return_value=fake_bash), patch.object(
                dispatch, "review_delegate_script", return_value=fake_delegate
            ), patch.object(dispatch, "_run_script_json_timed", side_effect=tracking_timed):
                first = dispatch.run_review(args)
                resume = argparse.Namespace(
                    target="spec",
                    run_id="0123456789abcdef",
                    doc_file=str(doc),
                    spec_file=None,
                    lenses="backend",
                    expected_generation=first["generation"],
                    session=first["session_id"],
                    repo=str(repo),
                )
                dispatch.run_review(resume)
            self.assertEqual(len(captured), 2)
            self.assertNotEqual(captured[0][0], captured[1][0])
            for path, _args in captured:
                self.assertIn("/claude-subagents/review-artifacts/", path)
            self.assertIn("--session", captured[1][1])
            self.assertEqual(
                captured[1][1][captured[1][1].index("--session") + 1],
                first["session_id"],
            )


if __name__ == "__main__":
    unittest.main()
