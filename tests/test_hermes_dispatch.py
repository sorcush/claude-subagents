#!/usr/bin/env python3
"""Hermes dispatch adapter tests."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout
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
                    dispatch.subprocess,
                    "run",
                    side_effect=[
                        OSError("launch failed"),
                        subprocess.CompletedProcess(
                            args=[str(good), "--version"],
                            returncode=0,
                            stdout="GNU bash, version 5.2.0\n",
                            stderr="",
                        ),
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
                    dispatch.subprocess,
                    "run",
                    side_effect=[
                        subprocess.TimeoutExpired(
                            cmd=[str(bad), "--version"],
                            timeout=dispatch.BASH_VERSION_TIMEOUT_SECONDS,
                        ),
                        subprocess.CompletedProcess(
                            args=[str(good), "--version"],
                            returncode=0,
                            stdout="GNU bash, version 5.2.0\n",
                            stderr="",
                        ),
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


if __name__ == "__main__":
    unittest.main()
