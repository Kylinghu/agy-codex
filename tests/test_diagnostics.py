"""Environment diagnostics without real Codex authentication or model calls."""
import os
import subprocess
import sys
import unittest
from unittest import mock

import test_protocol

sa = test_protocol.sa
CODEX_DOUBLE = "agy-codex-diagnostic-test-double"


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.f = test_protocol.ProtocolTests("test_pass_has_independent_tests_diff_and_persistent_session")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        task_home = self.f.repo / ".git" / "diagnostic-codex-home"
        task_home.mkdir()
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(task_home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.real_run = subprocess.run
        self.real_which = sa.shutil.which

    def run_command(self, argv, *args, **kwargs):
        if argv[0] == CODEX_DOUBLE:
            return subprocess.CompletedProcess(argv, 0,
                "TEST DOUBLE: ChatGPT --json --output-last-message --output-schema\n", "")
        return self.real_run(argv, *args, **kwargs)

    def diagnose(self):
        return sa.doctor(self.f.repo, codex_cmd=[CODEX_DOUBLE])

    def test_reports_current_interpreter_and_callable_git(self):
        with mock.patch.object(sa.subprocess, "run", side_effect=self.run_command):
            report = self.diagnose()
        self.assertEqual(report["python_executable"], sys.executable)
        self.assertEqual(report["git_executable"], self.real_which("git"))
        self.assertTrue(report["checks"]["git_on_path"])
        self.assertTrue(report["checks"]["git_invocation"])
        self.assertTrue(report["checks"]["git_repository"])
        self.assertTrue(report["static_checks_passed"], report)
        self.assertTrue(report["git_version"].startswith("git version"))
        self.assertEqual(report["live_exec"], "NOT_RUN")
        self.assertFalse(self.f.controller.root.exists())

    def test_missing_git_is_distinguished_from_an_invalid_repository(self):
        def which(name):
            return None if name == "git" else self.real_which(name)
        with mock.patch.object(sa.shutil, "which", side_effect=which), \
                mock.patch.object(sa.subprocess, "run", side_effect=self.run_command), \
                mock.patch.object(sa, "Controller") as controller:
            report = self.diagnose()
        controller.assert_not_called()
        self.assertIsNone(report["git_executable"])
        self.assertFalse(report["checks"]["git_on_path"])
        self.assertFalse(report["checks"]["git_invocation"])
        self.assertFalse(report["static_checks_passed"])
        self.assertIn("not found on PATH", report["git_error"])
        self.assertIn("Not checked", report["git_repository_error"])

    def test_discovered_git_must_run_before_repository_checks(self):
        git_executable = self.real_which("git")
        failures = [OSError("Executable could not start"),
                    subprocess.TimeoutExpired([git_executable, "--version"], 10),
                    subprocess.CompletedProcess([git_executable], 1, "", "Executable failed")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                def run(argv, *args, **kwargs):
                    if argv == [git_executable, "--version"]:
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    return self.run_command(argv, *args, **kwargs)
                with mock.patch.object(sa.subprocess, "run", side_effect=run), \
                        mock.patch.object(sa, "Controller") as controller:
                    report = self.diagnose()
                controller.assert_not_called()
                self.assertTrue(report["checks"]["git_on_path"])
                self.assertFalse(report["checks"]["git_invocation"])
                self.assertFalse(report["static_checks_passed"])
                self.assertTrue(report["git_error"])
                self.assertIn("Not checked", report["git_repository_error"])


if __name__ == "__main__":
    unittest.main()
