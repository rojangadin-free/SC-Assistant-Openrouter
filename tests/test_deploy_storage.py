"""Storage cleanup policy tests with no real Docker commands or file deletion."""

import _bootstrap  # noqa: F401
import contextlib
import io
import os
import subprocess
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
            self.assertEqual(args[:4], ("container", "inspect", "--format", "{{.Image}}"))
            return {"running": "active-image", "stopped": "rollback-image"}[args[-1]]
        with patch.object(storage, "docker", side_effect=docker):
            self.assertEqual(storage.protected_images(), {"active-image", "rollback-image"})

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
        self.assertIn("--index-url https://download.pytorch.org/whl/cpu torch==2.6.0", dockerfile)
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
