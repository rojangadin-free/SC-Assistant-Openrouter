"""Offline CLI, SSH command and persistence-workflow contracts. No network calls."""

import argparse
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401

from rag.dummy_students import DummyStudentDatabase, create_database
from tools import deploy_student_database as cli

ROOT = Path(__file__).resolve().parents[1]


class DeploymentCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sc_student_ssh_")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "current # source.sqlite3"
        create_database(self.source, start_id=100000, end_id=100005, batch_size=2)
        DummyStudentDatabase(self.source).link_account("pool", "first-owner", "100000")
        self.destination = self.directory / "host" / "dummy_students.sqlite3"
        self.common = ["--pool-id", "pool", "--expect-end-id", "100005"]

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_export_install_repeat_preserves_late_host_claims(self):
        exported = self.directory / "snapshot.sqlite3"
        code, output, error = self.invoke(["export", "--source", str(self.source), "--output", str(exported), *self.common])
        self.assertEqual((code, error), (0, ""))
        manifest = json.loads(output)
        arguments = ["install", "--destination", str(self.destination), "--source", str(exported),
                     "--sha256", manifest["sha256"], "--size", str(manifest["size_bytes"]), *self.common]
        code, output, error = self.invoke(arguments)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["status"], "installed")
        database = DummyStudentDatabase(self.destination)
        self.assertEqual(database.get_by_uid("pool", "first-owner")["student_number"], "100000")
        database.link_account("pool", "late-owner", "100001")
        code, output, error = self.invoke(["install", "--destination", str(self.destination),
                                          "--source", str(self.directory / "missing.sqlite3"),
                                          "--sha256", "invalid", "--size", "-1", *self.common])
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["status"], "preserved")
        self.assertEqual(database.get_by_uid("pool", "late-owner")["student_number"], "100001")

    def test_missing_check_and_install_never_generate_a_database(self):
        self.assertEqual(self.invoke(["prepare-directory", "--directory", str(self.destination.parent)])[0], 0)
        args = ["check", "--destination", str(self.destination), *self.common]
        self.assertEqual(self.invoke(args)[0], 1)
        code, output, _ = self.invoke([*args, "--allow-missing"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "missing")
        self.assertEqual(self.invoke(["install", "--destination", str(self.destination), *self.common])[0], 1)
        self.assertFalse(self.destination.exists())

    def test_bad_checksum_does_not_publish(self):
        code, _, _ = self.invoke(["install", "--destination", str(self.destination), "--source", str(self.source),
                                  "--sha256", "0" * 64, "--size", str(self.source.stat().st_size), *self.common])
        self.assertEqual(code, 1)
        self.assertFalse(self.destination.exists())

    def test_prepare_rejects_parent_traversal_and_missing_ancestors(self):
        with self.assertRaises(ValueError):
            cli.prepare_directory(self.directory / ".." / "escape")
        with self.assertRaises(FileNotFoundError):
            cli.prepare_directory(self.directory / "missing-parent" / "leaf")
        self.assertFalse((self.directory / "missing-parent").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and mode enforcement")
    def test_existing_permissions_are_not_changed(self):
        self.destination.parent.mkdir(mode=0o755)
        self.destination.parent.chmod(0o755)
        with self.assertRaises(PermissionError):
            cli.prepare_directory(self.destination.parent)
        self.assertEqual(stat.S_IMODE(self.destination.parent.stat().st_mode), 0o755)

    def test_orphan_recovery_files_are_preserved(self):
        cli.prepare_directory(self.destination.parent)
        orphan = self.destination.with_name(self.destination.name + "-wal")
        orphan.write_bytes(b"recoverable-state")
        orphan.chmod(0o600)
        code, _, _ = self.invoke(["check", "--destination", str(self.destination), "--allow-missing", *self.common])
        self.assertEqual(code, 1)
        self.assertEqual(orphan.read_bytes(), b"recoverable-state")
        self.assertFalse(self.destination.exists())

    def test_installer_bundle_is_whitelisted_and_runs_without_repository(self):
        bundle = self.directory / "installer.pyz"
        cli._bundle(bundle)
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(set(archive.namelist()), {
                "__main__.py", "student_database_cli.py", "rag/__init__.py", "rag/dummy_students.py",
                "rag/student_subjects.py", "rag/student_database_deploy.py",
            })
        command = [sys.executable, "-B", str(bundle), "install", "--source", str(self.source),
                   "--destination", str(self.destination), *self.common]
        result = subprocess.run(command, check=False, cwd=self.directory, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "installed")
        self.assertEqual(DummyStudentDatabase(self.destination).get_by_uid("pool", "first-owner")["student_number"], "100000")

    def test_help_does_not_load_storage_or_run_commands(self):
        with patch.object(cli, "_storage") as storage, patch.object(cli, "_run") as run, \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exited:
            cli.main(["--help"])
        self.assertEqual(exited.exception.code, 0)
        storage.assert_not_called()
        run.assert_not_called()


class SSHTransportTests(unittest.TestCase):
    def setUp(self):
        self.arguments = argparse.Namespace(
            host="ubuntu@example.invalid", port=2222, identity_file=None, remote_python="python3", no_sudo=False,
            source=Path("not-consulted-until-needed.sqlite3"), destination=cli.DEFAULT_DESTINATION,
            pool_id="pool", expect_start_id=100000, expect_end_id=100005,
        )
        self.commands = []
        self.remote_directory = "/tmp/sc-assistant-student-AbC123xyZ9"
        self.status = "missing"
        self.fail_upload = False

    def fake_run(self, arguments, **kwargs):
        self.commands.append(arguments)
        if arguments[0] == "scp":
            if self.fail_upload and arguments[-1].endswith("/snapshot.sqlite3"):
                raise RuntimeError("SCP failed")
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        command = arguments[-1]
        if "mktemp -d" in command:
            output = self.remote_directory
        elif "prepare-directory" in command:
            output = json.dumps({"status": "prepared"})
        elif " check " in command:
            output = json.dumps({"status": self.status})
        elif " install " in command:
            output = json.dumps({"status": "installed"})
        else:
            output = ""
        return SimpleNamespace(stdout=output, stderr="", returncode=0)

    def snapshot(self, _source, destination, **kwargs):
        destination.write_bytes(b"fake sealed snapshot")
        return {"sha256": "a" * 64, "size_bytes": 20}

    def invoke(self, snapshot=None):
        with patch.object(cli, "_run", side_effect=self.fake_run), \
                patch.object(cli, "_bundle", side_effect=lambda path: path.write_bytes(b"installer")), \
                redirect_stderr(io.StringIO()):
            return cli.transfer(self.arguments, snapshot or self.snapshot)

    def test_transfer_checks_before_export_and_uploads_with_strict_host_keys(self):
        snapshot = Mock(side_effect=self.snapshot)
        result = self.invoke(snapshot)
        self.assertEqual(result["status"], "installed")
        snapshot.assert_called_once()
        scp = [command for command in self.commands if command[0] == "scp"]
        self.assertEqual(len(scp), 2)
        self.assertTrue(scp[0][-1].endswith("/installer.pyz"))
        self.assertTrue(scp[1][-1].endswith("/snapshot.sqlite3"))
        for command in self.commands:
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("BatchMode=yes", command)
            self.assertIn("ForwardAgent=no", command)
            self.assertIn("2222", command)
        install = next(shlex.split(command[-1]) for command in self.commands if " install " in command[-1])
        self.assertEqual(install[:4], ["sudo", "-n", "--", "python3"])
        self.assertIn("--require-root", install)
        self.assertEqual(install[install.index("--sha256") + 1], "a" * 64)
        self.assertEqual(install[install.index("--size") + 1], "20")
        self.assertEqual(shlex.split(self.commands[-1][-1]), ["rm", "-rf", "--", self.remote_directory])

    def test_existing_host_skips_local_source_and_snapshot_transfer(self):
        self.status = "preserved"
        snapshot = Mock(side_effect=AssertionError("Existing host must skip local data"))
        self.assertEqual(self.invoke(snapshot)["status"], "preserved")
        snapshot.assert_not_called()
        self.assertEqual(len([command for command in self.commands if command[0] == "scp"]), 1)
        self.assertFalse(any(" install " in command[-1] for command in self.commands))

    def test_failed_upload_never_runs_installer_and_cleans_own_stage(self):
        self.fail_upload = True
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertFalse(any(" install " in command[-1] for command in self.commands))
        self.assertEqual(shlex.split(self.commands[-1][-1]), ["rm", "-rf", "--", self.remote_directory])

    def test_export_failure_keeps_host_unmodified_and_cleans_own_stage(self):
        with self.assertRaises(ValueError):
            self.invoke(Mock(side_effect=ValueError("Invalid local source")))
        self.assertEqual(len([command for command in self.commands if command[0] == "scp"]), 1)
        self.assertFalse(any(" install " in command[-1] for command in self.commands))
        self.assertEqual(shlex.split(self.commands[-1][-1]), ["rm", "-rf", "--", self.remote_directory])

    def test_ssh_authentication_failure_does_not_export_or_stage(self):
        snapshot = Mock()
        with patch.object(cli, "_run", side_effect=RuntimeError("Host key verification failed")), \
                patch.object(cli, "_bundle") as bundle, self.assertRaises(RuntimeError):
            cli.transfer(self.arguments, snapshot)
        snapshot.assert_not_called()
        bundle.assert_not_called()

    def test_identity_and_remote_python_paths_remain_single_arguments(self):
        with tempfile.TemporaryDirectory(prefix="sc_ssh_key_test_") as temporary:
            key = Path(temporary) / "test key.pem"
            key.write_bytes(b"fake fixture, not a credential")
            self.arguments.identity_file = key
            self.arguments.remote_python = "/opt/python with spaces/python3"
            self.arguments.no_sudo = True
            self.invoke()
            for command in self.commands:
                self.assertEqual(command[command.index("-i") + 1], str(key.absolute()))
            install = next(shlex.split(command[-1]) for command in self.commands if " install " in command[-1])
            self.assertEqual(install[0], self.arguments.remote_python)
            self.assertNotIn("sudo", install)
            self.assertIn("--require-root", install)

    def test_remote_arguments_are_shell_quoted_not_interpolated(self):
        self.arguments.destination = "/var/lib/name;not-a-command/dummy_students.sqlite3"
        self.invoke()
        install = next(shlex.split(command[-1]) for command in self.commands if " install " in command[-1])
        self.assertEqual(install[install.index("--destination") + 1], self.arguments.destination)

    def test_host_option_injection_is_rejected_before_network_calls(self):
        for host in ("-oProxyCommand=bad", "user@host;bad", "host\ncommand", "host with spaces"):
            with self.subTest(host=host), patch.object(cli, "_run") as run:
                self.arguments.host = host
                with self.assertRaises(ValueError):
                    cli.transfer(self.arguments, self.snapshot)
                run.assert_not_called()

    def test_untrusted_remote_stage_is_not_used_or_removed(self):
        self.remote_directory = "/var/lib/important"
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertFalse(any(command[0] == "scp" for command in self.commands))
        self.assertFalse(any(command[-1].startswith("rm ") for command in self.commands))


class DeploymentWorkflowTests(unittest.TestCase):
    def test_workflow_shell_blocks_have_valid_syntax(self):
        bash = os.getenv("SC_TEST_BASH") or shutil.which("bash")
        if bash is None:
            self.skipTest("Bash is not available for syntax validation")
        workflow = (ROOT / ".github/workflows/cicd.yaml").read_text(encoding="utf-8")
        blocks = re.findall(r"(?m)^        run: \|\n((?:(?:          [^\n]*)?\n)+)", workflow)
        self.assertGreaterEqual(len(blocks), 4)
        environment = dict(os.environ)
        environment.pop("BASH_ENV", None)
        for index, block in enumerate(blocks):
            with self.subTest(block=index):
                script = "\n".join(line[10:] for line in block.splitlines())
                script = re.sub(r"\$\{\{.*?\}\}", "test-value", script)
                result = subprocess.run([bash, "-n"], input=script, text=True, capture_output=True,
                                        check=False, timeout=10, env=environment)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_database_preflight_precedes_app_only_replacement(self):
        workflow = (ROOT / ".github/workflows/cicd.yaml").read_text(encoding="utf-8")
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("runs-on: [self-hosted, linux]", workflow)
        self.assertIn("tests/test_student_database_deploy.py", workflow)
        self.assertIn("tests/test_student_database_ssh.py", workflow)
        self.assertLess(workflow.index("tools/deploy_student_database.py check"), workflow.index('docker stop --time 130 "$CONTAINER_NAME"'))
        self.assertIn("--mount type=bind,source=/var/lib/sc-assistant,target=/var/lib/sc-assistant", workflow)
        self.assertIn("-e STUDENT_RECORDS_BACKEND=sqlite_demo", workflow)
        self.assertIn("-e DUMMY_STUDENT_DB_PATH=/var/lib/sc-assistant/dummy_students.sqlite3", workflow)
        self.assertIn("-e WEB_INDEXING_ENABLED=true", workflow)
        for destructive in ("docker ps -aq", "docker volume prune", "docker system prune", "docker network prune", "docker rmi"):
            self.assertNotIn(destructive, workflow)


if __name__ == "__main__":
    unittest.main()
