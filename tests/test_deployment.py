"""Server configuration and deployment safety checks without Docker/AWS."""

import _bootstrap  # noqa: F401
import os
import runpy
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.proxy_config import render
from sc_assistant import readiness


class DeploymentTests(unittest.TestCase):
    def test_server_defaults_and_persisted_worker_count(self):
        with patch.dict(os.environ, {}, clear=True):
            config = runpy.run_path("gunicorn.conf.py")
            self.assertEqual((config["workers"], config["threads"], config["worker_class"]), (3, 12, "gthread"))
            self.assertFalse(config["preload_app"])
            self.assertGreater(config["max_requests_jitter"], 0)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "workers")
            with open(path, "w") as handle:
                handle.write("1")
            with patch.dict(os.environ, {"GUNICORN_WORKERS_FILE": path, "GUNICORN_WORKERS": "3"}):
                self.assertEqual(runpy.run_path("gunicorn.conf.py")["workers"], 1)
                with open(path, "w") as handle:
                    handle.write("3")
                self.assertEqual(runpy.run_path("gunicorn.conf.py")["workers"], 3)

    def test_proxy_does_not_buffer_or_accept_config_injection(self):
        config = render("sc-assistant-web-blue")
        self.assertIn("proxy_buffering off", config)
        self.assertIn("proxy_request_buffering off", config)
        self.assertIn("proxy_pass_header X-Accel-Buffering;", config)
        self.assertIn('proxy_set_header Accept-Encoding "";', config)
        self.assertIn("worker_shutdown_timeout", config)
        with self.assertRaises(ValueError):
            render("bad; upstream injected")

    def test_deployment_never_prunes_other_services(self):
        with open("tools/deploy.sh", encoding="utf-8") as handle:
            script = handle.read()
        with open(".github/workflows/cicd.yaml", encoding="utf-8") as handle:
            workflow = handle.read()
        for forbidden in ("docker system prune", "docker volume prune", "docker ps -aq", "docker images -q"):
            self.assertNotIn(forbidden, script + workflow)
        self.assertIn("candidate_started=0", script)
        self.assertIn('ready "$candidate"', script)
        self.assertIn("--signal=TTIN", script)
        self.assertIn("sleep 2", script)
        self.assertIn("$GITHUB_SHA", workflow)
        self.assertNotIn("::set-output", workflow)
        self.assertIn("cancel-in-progress: false", workflow)

    def test_readiness_checks_shared_storage_and_hides_configuration(self):
        readiness._checked, readiness._ok = 0, False
        fake = SimpleNamespace(FLASK_SECRET_KEY="nondefault-secret", OPENROUTER_API_KEY="test", PINECONE_API_KEY="test")
        with patch.dict(os.environ, {"SC_PRODUCTION": "true", "STORE_BACKEND": "dynamodb"}), \
                patch.dict(sys.modules, {"config": fake}), \
                patch("rag.artifacts._manifest", return_value=SimpleNamespace(mutate=lambda fn: {})):
            self.assertTrue(readiness.probe())
        readiness._checked, readiness._ok = 0, False
        with patch.dict(os.environ, {"SC_PRODUCTION": "true", "STORE_BACKEND": "file"}), \
                patch.dict(sys.modules, {"config": fake}):
            self.assertFalse(readiness.probe())

    def test_image_reference_does_not_cross_secret_filtered_job_outputs(self):
        with open(".github/workflows/cicd.yaml", encoding="utf-8") as handle:
            workflow = handle.read()
        build, deploy = workflow.split("\n  deploy:\n", 1)
        for job in (build, deploy):
            self.assertIn("id: ecr", job)
            self.assertIn("REGISTRY: ${{ steps.ecr.outputs.registry }}", job)
            self.assertIn("REPOSITORY: ${{ secrets.ECR_REPO }}", job)
            self.assertIn('IMAGE="$REGISTRY/$REPOSITORY:$GITHUB_SHA"', job)
            self.assertIn('${REPOSITORY:?Set the ECR_REPO GitHub secret}', job)
        self.assertNotIn("needs.build.outputs.image", workflow)
        self.assertNotIn("$GITHUB_OUTPUT", workflow)
        self.assertIn("needs: build", deploy)
        self.assertLess(deploy.index('IMAGE="$REGISTRY/$REPOSITORY:$GITHUB_SHA"'),
                        deploy.index('bash tools/deploy.sh --apply "$IMAGE" "$ENV_FILE"'))


if __name__ == "__main__":
    unittest.main()
