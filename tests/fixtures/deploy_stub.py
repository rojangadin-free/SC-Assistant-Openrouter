"""Private subprocess fixture for deploy.sh; never calls Docker or the network."""

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace


def state():
    return json.loads(Path(os.environ["SC_TEST_STATE"]).read_text())


def docker(args):
    data = state()
    data["events"].append(args)
    containers, images = data["containers"], data["images"]

    def container(reference):
        return next((c for c in containers.values() if reference in (c["Id"], c["Name"].lstrip("/"))), None)

    def inspected(c, template):
        if template == "{{.State.Running}}":
            return str(c["State"]["Running"]).lower()
        if "Labels" in template:
            return c["Config"]["Labels"].get("sc.project", "")
        if "with .Config" in template:
            return c["Image"] + "\n" + c["Config"]["Image"]
        if template == "{{.Config.Image}}":
            return c["Config"]["Image"]
        raise AssertionError("Unexpected inspection format")

    output, error = "", False
    if args[:2] == ["image", "ls"]:
        output = "\n".join(images)
    elif args[:2] == ["image", "inspect"]:
        value = next((v for v in images.values() if args[-1] in [v["Id"], *v["RepoDigests"]]), None)
        output, error = json.dumps([value]), value is None
    elif args[:2] == ["image", "rm"]:
        error = any(c["Image"] == args[-1] for c in containers.values())
        if not error:
            images.pop(args[-1])
    elif args[:2] == ["container", "ls"]:
        output = "\n".join(containers if "--format" in args else [c["Id"] for c in containers.values()])
    elif args[:2] == ["container", "inspect"] or args[0] == "inspect":
        value = container(args[-1])
        error = value is None
        if value:
            if "--format" in args or "-f" in args:
                flag = "--format" if "--format" in args else "-f"
                output = inspected(value, args[args.index(flag) + 1])
            else:
                output = json.dumps([value])
    elif args[:2] == ["container", "rm"] or args[0] == "rm":
        value = container(args[-1])
        error = value is None or value["State"]["Running"]
        if not error:
            del containers[value["Name"].lstrip("/")]
    elif args[0] == "info":
        output = os.environ["SC_TEST_ROOT"]
    elif args[0] == "pull":
        error = data["scenario"] == "pull-failure" and args[-1] == data["target"]
        if not error and args[-1] == data["target"]:
            images["new-image"] = {"Id": "new-image", "RepoTags": [], "RepoDigests": [data["target"]],
                                   "Config": {"Labels": {"sc.project": "sc-assistant"}}, "Size": 1024}
    elif args[0] == "network":
        pass
    elif args[0] == "exec":
        name = args[1]
        error = data["scenario"] == "candidate-failure" and name == "sc-assistant-web-green"
        if "children" in " ".join(args):
            output = str(container(name).get("workers", 1))
    elif args[0] in ("stop", "start"):
        value = container(args[-1])
        error = value is None
        if value:
            running = args[0] == "start"
            value["State"] = {"Running": running, "Status": "running" if running else "exited"}
    elif args[0] == "rename":
        value = containers.pop(args[1])
        value["Name"] = "/" + args[2]
        containers[args[2]] = value
    elif args[0] == "kill":
        container(args[-1])["workers"] += 1
    elif args[0] == "run":
        name = args[args.index("--name") + 1]
        reference = data["target"] if data["target"] in args else "nginx:stable-alpine"
        error = data["scenario"] == "indexer-failure" and name == "sc-assistant-indexer"
        if not error:
            containers[name] = {"Id": "new-" + name, "Name": "/" + name, "Image": "new-image",
                                "Config": {"Image": reference, "Labels": {"sc.project": "sc-assistant"}},
                                "State": {"Running": True, "Status": "running"}, "workers": 1}
    else:
        raise AssertionError("Unexpected Docker command: " + repr(args))
    Path(os.environ["SC_TEST_STATE"]).write_text(json.dumps(data))
    if error:
        print("Simulated Docker failure", file=sys.stderr)
        return 1
    print(output)
    return 0


def python(args):
    if args[0] == "-c":
        assert "X-SC-Backend" in args[1], "Unexpected host Python command"
        config = Path(os.environ["SC_DEPLOY_DIR"], "proxy", "nginx.conf").read_text()
        return 0 if config.startswith("# sc-active:" + args[2]) else 1
    filename = args[0]
    assert Path(filename).name in ("deploy_storage.py", "proxy_config.py")
    original_run = subprocess.run

    def redirect(command, **kwargs):
        assert command[0] == "docker", "Unexpected external command in preflight"
        return original_run([sys.executable, __file__, "--docker", *command[1:]], **kwargs)

    subprocess.run = redirect
    sys.argv = args
    if Path(filename).name == "deploy_storage.py":
        code = runpy.run_path(filename)
        code["main"].__globals__["shutil"].disk_usage = lambda _: SimpleNamespace(free=10 * 1024**3)
        return code["main"]()
    runpy.run_path(filename, run_name="__main__")
    return 0


if __name__ == "__main__":
    mode, *arguments = sys.argv[1:]
    raise SystemExit(docker(arguments) if mode == "--docker" else python(arguments))
