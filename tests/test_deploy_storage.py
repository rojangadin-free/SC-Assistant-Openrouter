"""Storage cleanup policy tests with no real Docker commands or file deletion."""

import _bootstrap  # noqa: F401
import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools import deploy_storage as storage

REPOSITORY = "registry.example:5000/school/assistant"
TARGET = REPOSITORY + ":new"


def image(identity, tags=(), digests=(), labels=None):
    return {"id": identity, "tags": list(tags), "digests": list(digests),
            "labels": labels or {}, "size": 1024}


class StorageTests(unittest.TestCase):
    def test_only_unreferenced_project_images_are_eligible(self):
        candidates = storage.cleanup_candidates([
            image("active", [REPOSITORY + ":active"]),
            image("rollback", [REPOSITORY + ":rollback"]),
            image("target", [TARGET]),
            image("old", [REPOSITORY + ":old"]),
            image("digest", digests=[REPOSITORY + "@sha256:old"]),
            image("dangling-project", labels={"sc.project": "sc-assistant"}),
            image("dangling-unknown"),
            image("foreign", ["another.example/app:old"]),
            image("shared", [REPOSITORY + ":shared", "another.example/app:shared"], labels={"sc.project": "sc-assistant"}),
        ], {"active", "rollback"}, TARGET)
        self.assertEqual({item["id"] for item in candidates}, {"old", "digest", "dangling-project"})

    def test_container_references_include_stopped_rollback_without_reading_secrets(self):
        def docker(*args):
            if args == ("container", "ls", "--all", "--quiet"):
                return "running\nstopped"
            self.assertEqual(args[:3], ("container", "inspect", "--format"))
            self.assertEqual(args[3], '{{.Image}}{{with .Config}}\n{{.Image}}{{end}}')
            return {"running": "active-image\n" + REPOSITORY + ":active",
                    "stopped": "rollback-image\n" + REPOSITORY + ":rollback"}[args[-1]]
        with patch.object(storage, "docker", side_effect=docker):
            self.assertEqual(storage.protected_images(), {"active-image", "rollback-image",
                             REPOSITORY + ":active", REPOSITORY + ":rollback"})

    def test_oci_parent_reference_protects_different_platform_image_id(self):
        parent = image("parent-index", [REPOSITORY + ":rollback"])
        self.assertEqual(storage.cleanup_candidates([parent],
                         {"child-platform-id", REPOSITORY + ":rollback"}, TARGET), [])

    def test_inspection_parses_null_and_missing_configs_without_a_go_template(self):
        records = {
            "index": {"Id": "index", "Config": None, "RepoTags": [REPOSITORY + ":old"], "RepoDigests": None},
            "attestation": {"Id": "attestation", "RepoTags": None, "RepoDigests": None},
            "normal": {"Id": "normal", "Config": {"Labels": {"sc.project": "sc-assistant"},
                                                      "Env": ["SECRET=do-not-log"]}, "Size": 1024},
        }

        def docker(*args):
            if args[:2] == ("image", "ls"):
                return "\n".join(records)
            self.assertEqual(args[:2], ("image", "inspect"))
            self.assertNotIn("--format", args)
            return json.dumps([records[args[-1]]])

        with patch.object(storage, "docker", side_effect=docker), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            found = storage.images()
        self.assertEqual(len(found), 3)
        self.assertEqual(next(row for row in found if row["id"] == "index")["labels"], {})
        self.assertNotIn("SECRET", output.getvalue() + errors.getvalue() + repr(found))
        eligible = storage.cleanup_candidates(found, set(), TARGET)
        self.assertEqual({row["id"] for row in eligible}, {"index", "normal"})

    def test_uninspectable_image_is_preserved_and_engine_error_is_visible(self):
        def docker(*args):
            if args[:2] == ("image", "ls"):
                return "missing\ngood"
            if args[-1] == "missing":
                raise subprocess.CalledProcessError(1, ["docker"], output="SECRET=never-print",
                                                    stderr="Error response from daemon: content digest not found")
            return json.dumps([{"Id": "good", "RepoTags": [REPOSITORY + ":old"]}])
        with patch.object(storage, "docker", side_effect=docker), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            found = storage.images()
        self.assertEqual([row["id"] for row in found], ["good"])
        self.assertIn("content digest not found", output.getvalue())
        self.assertNotIn("SECRET", output.getvalue())

    def test_malformed_ownership_metadata_is_never_a_cleanup_candidate(self):
        malformed = ({"Id": "bad", "RepoTags": REPOSITORY + ":old"},
                     {"Id": "bad", "Config": {"Labels": "not-an-object"}},
                     {"RepoTags": [REPOSITORY + ":old"]})
        for record in malformed:
            with patch.object(storage, "docker", side_effect=["bad", json.dumps([record])]), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(storage.images(), [])

    def test_container_inventory_failure_still_blocks_deletion(self):
        error = subprocess.CalledProcessError(1, ["docker"], stderr="Cannot connect to the Docker daemon")
        with patch.object(storage, "images", return_value=[image("old", [REPOSITORY + ":old"])]), \
                patch.object(storage, "protected_images", side_effect=error), \
                patch.object(storage, "docker") as docker:
            with self.assertRaises(subprocess.CalledProcessError):
                storage.main(["--image", TARGET, "--apply"])
        docker.assert_not_called()

    def test_skipping_uninspectable_images_does_not_skip_disk_check(self):
        error = subprocess.CalledProcessError(1, ["docker"], stderr="No such image")
        with patch.object(storage, "docker", side_effect=["uninspectable", error]) as docker, \
                patch.object(storage, "protected_images", return_value=set()), \
                patch.object(storage, "filesystem_report", return_value=[{"sufficient": False}]) as report, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(storage.main(["--image", TARGET, "--apply"]), 2)
        report.assert_called_once()
        self.assertTrue(all(call.args[:2] != ("image", "rm") for call in docker.call_args_list))

    def test_command_line_reports_engine_stderr_without_dumping_stdout(self):
        error = subprocess.CalledProcessError(1, ["docker"], output="SECRET=never-print",
                                              stderr="Cannot connect to the Docker daemon")
        with patch.object(sys, "argv", ["deploy_storage.py", "--image", TARGET, "--apply"]), \
                patch.object(subprocess, "run", side_effect=error), \
                contextlib.redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as raised:
            runpy.run_path("tools/deploy_storage.py", run_name="__main__")
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Cannot connect to the Docker daemon", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        self.assertNotIn("SECRET", output.getvalue())

    def test_preview_never_removes_images(self):
        with patch.object(storage, "images", return_value=[image("old", [REPOSITORY + ":old"])]), \
                patch.object(storage, "protected_images", return_value=set()), \
                patch.object(storage, "filesystem_report", return_value=[{"sufficient": False}]), \
                patch.object(storage, "docker") as docker, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(storage.main(["--image", TARGET]), 0)
        docker.assert_not_called()

    def test_apply_removes_by_id_without_force_and_refuses_insufficient_disk(self):
        with patch.object(storage, "images", return_value=[image("old", [REPOSITORY + ":old"])]), \
                patch.object(storage, "protected_images", return_value=set()), \
                patch.object(storage, "filesystem_report", return_value=[{"sufficient": False}]), \
                patch.object(storage, "docker") as docker, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(storage.main(["--image", TARGET, "--apply"]), 2)
        docker.assert_called_once_with("image", "rm", "old")
        self.assertIn("No services were stopped", output.getvalue())

    def test_image_that_becomes_referenced_is_preserved(self):
        with patch.object(storage, "images", return_value=[image("old", [REPOSITORY + ":old"])]), \
                patch.object(storage, "protected_images", side_effect=[set(), {"old"}]), \
                patch.object(storage, "filesystem_report", return_value=[{"sufficient": True}]), \
                patch.object(storage, "docker") as docker, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(storage.main(["--image", TARGET, "--apply"]), 0)
        docker.assert_not_called()

    def test_docker_refusal_does_not_trigger_forced_deletion(self):
        with patch.object(storage, "images", return_value=[image("old", [REPOSITORY + ":old"])]), \
                patch.object(storage, "protected_images", return_value=set()), \
                patch.object(storage, "filesystem_report", return_value=[{"sufficient": True}]), \
                patch.object(storage, "docker", side_effect=subprocess.CalledProcessError(1, ["docker"])) as docker, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(storage.main(["--image", TARGET, "--apply"]), 0)
        docker.assert_called_once_with("image", "rm", "old")

    def test_docker_and_containerd_filesystems_are_both_checked(self):
        with tempfile.TemporaryDirectory() as docker_root, tempfile.TemporaryDirectory() as containerd_root:
            with patch.dict(os.environ, {"SC_CONTAINERD_ROOT": containerd_root}), \
                    patch.object(storage, "docker", return_value=docker_root), \
                    patch.object(storage.shutil, "disk_usage", side_effect=[
                        SimpleNamespace(free=8 * 1024**3), SimpleNamespace(free=100 * 1024**2)
                    ]), \
                    patch.object(storage.os, "statvfs", return_value=SimpleNamespace(f_favail=50000, f_files=100000), create=True):
                report = storage.filesystem_report(6144)
        self.assertEqual([row["path"] for row in report], [docker_root, containerd_root])
        self.assertTrue(report[0]["sufficient"])
        self.assertFalse(report[1]["sufficient"])

    def test_inode_exhaustion_is_not_mistaken_for_free_capacity(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(storage, "docker", return_value=root), \
                patch.dict(os.environ, {"SC_CONTAINERD_ROOT": root}), \
                patch.object(storage.shutil, "disk_usage", return_value=SimpleNamespace(free=20 * 1024**3)), \
                patch.object(storage.os, "statvfs", return_value=SimpleNamespace(f_favail=0, f_files=100000), create=True):
            self.assertFalse(storage.filesystem_report(6144)[0]["sufficient"])

    def test_image_pull_precedes_service_changes_and_rollback_trap(self):
        with open("tools/deploy.sh", encoding="utf-8") as handle:
            script = handle.read()
        self.assertLess(script.index('deploy_storage.py" --image "$image" --apply'), script.index('docker pull "$image"'))
        self.assertLess(script.index('docker pull "$image"'), script.index("trap restore ERR"))
        self.assertLess(script.index('docker pull "$image"'), script.index('if running "$indexer"; then'))
        self.assertIn('if ! python3 "$root/tools/proxy_config.py"', script)
        self.assertGreaterEqual(script.count("--log-driver local"), 3)

    def test_runtime_image_uses_cpu_torch_and_cached_build_layers(self):
        with open("Dockerfile", encoding="utf-8") as handle:
            dockerfile = handle.read()
        with open("requirements.txt", encoding="utf-8") as handle:
            requirements = handle.read()
        with open(".github/workflows/cicd.yaml", encoding="utf-8") as handle:
            workflow = handle.read()
        for source in (dockerfile, workflow):
            self.assertIn("--index-url https://pypi.org/simple", source)
            self.assertIn("--extra-index-url https://download.pytorch.org/whl/cpu", source)
            self.assertIn("'torch==2.6.0+cpu'", source)
            self.assertNotIn("--index-url https://download.pytorch.org/whl/cpu", source)
        self.assertIn("python3 -m pip install --no-cache-dir --upgrade pip", dockerfile)
        self.assertIn("assert torch.version.cuda is None", dockerfile)
        self.assertIn("torch==2.6.0", requirements)
        self.assertIn('LABEL sc.project="sc-assistant"', dockerfile)
        self.assertIn("docker/build-push-action@v6", workflow)
        self.assertIn("cache-from: type=gha", workflow)
        self.assertIn("cache-to: type=gha", workflow)
        self.assertLess(workflow.index('python3 tools/deploy_storage.py --image "$IMAGE" --apply'),
                        workflow.index('ENV_FILE="$(mktemp'))


if __name__ == "__main__":
    unittest.main()
