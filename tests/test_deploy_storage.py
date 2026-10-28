"""Storage cleanup policy tests with no real Docker commands or file deletion."""

import _bootstrap  # noqa: F401
import contextlib
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

from tools import deploy_storage as storage

REPOSITORY = "registry.example:5000/school/assistant"
TARGET = REPOSITORY + ":new"


def image(identity, tags=(), digests=(), labels=None):
    return {"id": identity, "tags": list(tags), "digests": list(digests),
            "labels": labels or {}, "size": 1024}


class StorageTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"SC_DEPLOY_IMAGE_BYTES": str(256 * 1024**2),
                                "SC_DEPLOY_RESERVE_MB": "1024", "SC_DEPLOY_MIN_DISK_MB": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def test_budget_uses_measured_image_plus_download_allowance_and_reserve(self):
        self.assertEqual(storage.required_free_mb(600 * 1024**2, 1024), 2224)
        self.assertEqual(storage.required_free_mb(2 * 1024**3, 1024), 5120)
        self.assertEqual(storage.required_free_mb(600 * 1024**2, 1024, 6144), 6144)
        with self.assertRaises(ValueError):
            storage.required_free_mb(0, 1024)
        with self.assertRaises(ValueError):
            storage.required_free_mb(1024, 0)

    def test_build_measurement_prints_only_size_and_cannot_clean(self):
        with patch.object(storage, "docker", return_value=json.dumps([{"Size": 987654321}])), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(storage.main(["--image", TARGET, "--measure"]), 0)
        self.assertEqual(output.getvalue().strip(), "987654321")
        with patch.object(storage, "docker", return_value=json.dumps([{"Size": 0}])):
            with self.assertRaises(ValueError):
                storage.main(["--image", TARGET, "--measure"])

    def test_cached_immutable_target_needs_only_reserve_not_a_second_download(self):
        target = REPOSITORY + "@sha256:" + "b" * 64
        cached = dict(image("cached", digests=[target]), size=256 * 1024**2, materialized=True)
        with patch.object(storage, "images", return_value=[cached]), \
                patch.object(storage, "protected_images", return_value=set()), \
                patch.object(storage, "filesystem_report", return_value=[{"sufficient": True}]) as report, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(storage.main(["--image", target, "--apply"]), 0)
        report.assert_called_once_with(1024)

    def test_retirement_is_limited_to_stopped_named_project_containers(self):
        active = "sc-assistant-web-blue"
        inactive = "sc-assistant-web-green"
        backup = "sc-assistant-indexer-backup"

        def record(name, running=False, labelled=True):
            return {"id": "id-" + name, "name": name, "image": REPOSITORY + ":old",
                    "labels": {"sc.project": "sc-assistant"} if labelled else {},
                    "running": running, "status": "running" if running else "exited"}

        records = {active: record(active, True), inactive: record(inactive), backup: record(backup)}
        records["rag-app-container"] = record("rag-app-container", labelled=False)
        listed = "\n".join([active, inactive, backup, "sc-assistant-indexer", "rag-app-container", "unrelated"])
        calls = []

        def docker(*args):
            calls.append(args)
            return listed if args[:3] == ("container", "ls", "--all") else "OK"

        with patch.object(storage, "container_info", side_effect=lambda name: records[name]), \
                patch.object(storage, "docker", side_effect=docker):
            retired = storage.retire_stopped(active, TARGET, apply=True)
        self.assertEqual(set(retired), {inactive, backup, "rag-app-container"})
        removals = [call for call in calls if call[:2] == ("container", "rm")]
        self.assertEqual({call[2] for call in removals}, {"id-" + inactive, "id-" + backup, "id-rag-app-container"})
        self.assertTrue(all(len(call) == 3 for call in removals))
        records[inactive] = record(inactive, running=True)
        records[backup] = record(backup, labelled=False)
        records["rag-app-container"]["image"] = "foreign.example/app:old"
        calls.clear()
        with patch.object(storage, "container_info", side_effect=lambda name: records[name]), \
                patch.object(storage, "docker", side_effect=docker):
            self.assertEqual(storage.retire_stopped(active, TARGET, apply=True), [])
        self.assertFalse(any(call[:2] == ("container", "rm") for call in calls))

    def test_retirement_requires_active_readiness_and_preview_does_not_remove(self):
        active = "sc-assistant-web-blue"
        active_record = {"id": "active", "name": active, "image": REPOSITORY + ":current",
                         "labels": {"sc.project": "sc-assistant"}, "status": "running", "running": True}
        failure = subprocess.CalledProcessError(1, ["docker", "exec"], stderr="not ready")
        with patch.object(storage, "container_info", return_value=active_record), \
                patch.object(storage, "docker", side_effect=failure) as docker:
            with self.assertRaises(subprocess.CalledProcessError):
                storage.retire_stopped(active, TARGET, apply=True)
        self.assertTrue(all(call.args[0] == "exec" for call in docker.call_args_list))
        stopped = dict(active_record, name="sc-assistant-web-green", id="stopped", running=False, status="exited")
        with patch.object(storage, "container_info", side_effect=[active_record, stopped]), \
                patch.object(storage, "docker", side_effect=["OK", "sc-assistant-web-green"]) as docker:
            self.assertEqual(storage.retire_stopped(active, TARGET), ["sc-assistant-web-green"])
        self.assertFalse(any(call.args[:2] == ("container", "rm") for call in docker.call_args_list))

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
        self.assertLess(script.index('deploy_storage.py" "${storage_args[@]}"'), script.index('docker pull "$image"'))
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
        self.assertIn('ENV_FILE="$(mktemp /dev/shm/sc-assistant-env.XXXXXX)"', workflow)
        self.assertIn("SC_DEPLOY_IMAGE_BYTES: ${{ needs.build.outputs.image_bytes }}", workflow)
        self.assertIn("SC_KEEP_ROLLBACK: ${{ vars.SC_KEEP_ROLLBACK || 'false' }}", workflow)
        self.assertNotIn("SC_DEPLOY_MIN_DISK_MB || '6144'", workflow)

    def test_completed_rollout_is_the_only_point_current_version_is_retired(self):
        with open("tools/deploy.sh", encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn('--retire-stopped --active-container "$old"', script)
        self.assertIn('--retire-stopped --active-container "$candidate" --cleanup-only', script)
        self.assertLess(script.index('running "$indexer"\ntrap - ERR'), script.index('--cleanup-only'))
        self.assertNotIn('docker rm -f "$candidate"', script)
        self.assertIn('if running "$candidate"; then', script)

    def test_real_rollout_script_success_and_failures_with_fake_docker(self):
        bash = (str(Path(os.environ.get("ProgramFiles", "C:/Program Files"), "Git/bin/bash.exe"))
                if os.name == "nt" else shutil.which("bash"))
        if not bash or not Path(bash).exists():
            self.skipTest("Bash is required for the isolated rollout simulation")
        source = Path(__file__).resolve().parent.parent
        stub = source / "tests/fixtures/deploy_stub.py"
        target = REPOSITORY + "@sha256:" + "b" * 64
        current = REPOSITORY + "@sha256:" + "a" * 64
        old = REPOSITORY + "@sha256:" + "c" * 64
        legacy = REPOSITORY + "@sha256:" + "d" * 64
        for scenario in ("success", "pull-failure", "candidate-failure", "indexer-failure"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(prefix="sc_rollout_") as temporary:
                root = Path(temporary)
                (root / "tools").mkdir()
                (root / "bin").mkdir()
                (root / "state/proxy").mkdir(parents=True)
                for name in ("deploy.sh", "deploy_storage.py", "proxy_config.py"):
                    shutil.copyfile(source / "tools" / name, root / "tools" / name)
                # Keep the real control flow, but shorten polling windows in
                # the failure simulation. Shell fixtures must use LF on Windows.
                script_path = root / "tools/deploy.sh"
                script_path.write_text(script_path.read_text().replace("seq 1 60", "seq 1 2")
                                       .replace("seq 1 30", "seq 1 2"), newline="\n")
                (root / "state/proxy/nginx.conf").write_text("# sc-active:sc-assistant-web-blue\n")
                (root / "environment").write_text("OFFLINE_TEST=true\n")
                for command, mode in (("docker", "--docker"), ("python3", "--python")):
                    script = root / "bin" / command
                    script.write_text('#!/usr/bin/env sh\nexec "$SC_TEST_PYTHON" "$SC_TEST_STUB" ' + mode + ' "$@"\n', newline="\n")
                    script.chmod(0o755)
                for command, body in (("sleep", "exit 0"), ("flock", "exit 0"), ("awk", "echo 999999")):
                    script = root / "bin" / command
                    script.write_text("#!/usr/bin/env sh\n" + body + "\n", newline="\n")
                    script.chmod(0o755)
                containers = {}
                for name, reference, identity, running in (
                    ("sc-assistant-web-blue", current, "current-image", True),
                    ("sc-assistant-web-green", old, "old-image", False),
                    ("sc-assistant-indexer", current, "current-image", True),
                    ("sc-assistant-indexer-backup", old, "old-image", False),
                    ("rag-app-container", legacy, "legacy-image", False),
                    ("unrelated", "other.example/app:v1", "foreign-image", True),
                ):
                    containers[name] = {"Id": "id-" + name, "Name": "/" + name, "Image": identity,
                        "Config": {"Image": reference, "Labels": {"sc.project": "sc-assistant" if name != "unrelated" else "other"}},
                        "State": {"Running": running, "Status": "running" if running else "exited"}, "workers": 3}
                    if name == "rag-app-container":
                        containers[name]["Config"]["Labels"] = {}
                data = {"containers": containers, "target": target, "scenario": scenario, "events": [], "images": {}}
                for identity, reference in (("current-image", current), ("old-image", old), ("legacy-image", legacy), ("foreign-image", "other.example/app:v1")):
                    data["images"][identity] = {"Id": identity, "RepoTags": [], "RepoDigests": [reference], "Config": {}, "Size": 1024}
                state_file = root / "docker-state.json"
                state_file.write_text(json.dumps(data))
                environment = dict(os.environ, PATH=str(root / "bin") + os.pathsep + os.environ["PATH"],
                    SC_TEST_PYTHON=Path(sys.executable).as_posix(), SC_TEST_STUB=stub.as_posix(),
                    SC_TEST_STATE=str(state_file), SC_TEST_ROOT=str(root), SC_DEPLOY_DIR=str(root / "state"),
                    SC_CONTAINERD_ROOT=str(root), SC_DEPLOY_IMAGE_BYTES="1024", SC_DEPLOY_RESERVE_MB="512",
                    SC_DEPLOY_MIN_DISK_MB="", SC_DEPLOY_MIN_FREE_MB="0", SC_KEEP_ROLLBACK="false", GUNICORN_WORKERS="3", PYTHONDONTWRITEBYTECODE="1")
                result = subprocess.run([bash, "-x", (root / "tools/deploy.sh").as_posix(), "--apply", target,
                                         (root / "environment").as_posix()], env=environment, capture_output=True, text=True, timeout=90)
                final = json.loads(state_file.read_text())
                self.assertTrue(final["containers"]["unrelated"]["State"]["Running"])
                if scenario == "success":
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("sc-assistant-web-blue", final["containers"])
                    self.assertNotIn("current-image", final["images"])
                    self.assertEqual(final["containers"]["sc-assistant-web-green"]["workers"], 3)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(final["containers"]["sc-assistant-web-blue"]["State"]["Running"])
                    self.assertTrue(final["containers"]["sc-assistant-indexer"]["State"]["Running"])
                    self.assertIn("current-image", final["images"])
                    self.assertTrue((root / "state/proxy/nginx.conf").read_text().startswith("# sc-active:sc-assistant-web-blue"))
                self.assertNotIn("old-image", final["images"], result.stdout + result.stderr)
                self.assertNotIn("legacy-image", final["images"], result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
