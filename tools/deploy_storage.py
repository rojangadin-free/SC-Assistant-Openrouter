"""Preview or remove unused project images; never stop containers or prune volumes."""

import argparse
import json
import os
import shutil
import subprocess
import sys


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=60).stdout.strip()


def docker_error(error):
    # Inspection stdout can contain image environment values. Report stderr
    # only, rather than dumping the inspected document or hiding the engine error.
    message = error.stderr or "Docker exited with status " + str(error.returncode)
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    return " ".join(message.split())[:1000]


def repository(reference):
    reference = reference.split("@", 1)[0]
    if ":" in reference.rsplit("/", 1)[-1]:
        reference = reference.rsplit(":", 1)[0]
    return reference


def protected_images():
    ids = docker("container", "ls", "--all", "--quiet").splitlines()
    protected = set()
    for container_id in ids:
        # An OCI index ID can differ from the container's platform image ID.
        # Protect its original reference too, without reading environment values.
        template = '{{.Image}}{{with .Config}}\n{{.Image}}{{end}}'
        protected.update(docker("container", "inspect", "--format", template, container_id).splitlines())
    protected.discard("")
    return protected


def images():
    result = []
    for image_id in sorted(set(docker("image", "ls", "--all", "--quiet", "--no-trunc").splitlines())):
        try:
            # OCI indexes/attestations may have no Config. Parse the API's JSON
            # instead of dereferencing .Config.Labels in a fragile Go template.
            # Never print this payload; retain only ownership metadata.
            records = json.loads(docker("image", "inspect", image_id))
            if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
                raise ValueError("Unexpected image metadata shape")
            record = records[0]
            config = record.get("Config") or {}
            if not isinstance(config, dict):
                raise ValueError("Invalid image configuration metadata")
            identity = record.get("Id")
            tags, digests = record.get("RepoTags") or [], record.get("RepoDigests") or []
            labels = config.get("Labels") or {}
            if (not isinstance(identity, str) or not identity
                    or not isinstance(tags, list) or not isinstance(digests, list)
                    or not all(isinstance(ref, str) and ref for ref in tags + digests)
                    or not isinstance(labels, dict)):
                raise ValueError("Incomplete image ownership metadata")
            result.append({"id": identity, "tags": tags, "digests": digests,
                           "labels": labels, "size": record.get("Size") or 0})
        except subprocess.CalledProcessError as error:
            print(f"Preserving uninspectable image {image_id}: {docker_error(error)}", file=sys.stderr)
        except ValueError:
            print(f"Preserving image {image_id}: invalid inspection metadata", file=sys.stderr)
    return result


def cleanup_candidates(items, protected, target):
    project_repository = repository(target)
    candidates = []
    for item in items:
        refs = item["tags"] + item["digests"]
        if item["id"] in protected or protected.intersection(refs) or target in refs:
            continue
        if refs:
            # An image tagged by another project is not exclusively ours, even
            # when one of its tags or its original label says SC-Assistant.
            ours = all(repository(ref) == project_repository for ref in refs)
        else:
            ours = item["labels"].get("sc.project") == "sc-assistant"
        if ours:
            candidates.append(item)
    return candidates


def filesystem_report(minimum_mb):
    root = docker("info", "--format", "{{.DockerRootDir}}")
    if not root or not os.path.isdir(root):
        raise RuntimeError("Docker storage is not on this host; inspect it directly on the deployment server")
    paths = [root]
    containerd = os.getenv("SC_CONTAINERD_ROOT", "/var/lib/containerd")
    if os.path.isdir(containerd) and containerd not in paths:
        paths.append(containerd)
    result = []
    for path in paths:
        usage = shutil.disk_usage(path)
        stat = os.statvfs(path) if hasattr(os, "statvfs") else None
        inodes = stat.f_favail if stat and stat.f_files else None
        free_mb = usage.free // (1024 * 1024)
        result.append({"path": path, "free_mb": free_mb, "required_mb": minimum_mb,
                       "free_inodes": inodes,
                       "sufficient": free_mb >= minimum_mb and (inodes is None or inodes >= 10000)})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Target image including its registry/repository and tag")
    parser.add_argument("--apply", action="store_true", help="Delete eligible unused images, then enforce disk headroom")
    parser.add_argument("--min-free-mb", type=int, default=int(os.getenv("SC_DEPLOY_MIN_DISK_MB", "6144")))
    args = parser.parse_args(argv)
    if not args.image or args.image.startswith("-") or "/" not in args.image or any(c.isspace() for c in args.image):
        parser.error("Use a fully qualified registry/repository image reference")
    if args.min_free_mb < 1024:
        parser.error("Reserve at least 1024 MiB of free space")
    candidates = cleanup_candidates(images(), protected_images(), args.image)
    print(json.dumps({"apply": args.apply, "unused_project_image_ids": [item["id"] for item in candidates],
                      "note": "Active and stopped-container images are protected; shared layers may limit reclaimed space."}))
    if args.apply:
        for item in candidates:
            if not cleanup_candidates([item], protected_images(), args.image):
                continue
            try:
                # No --force: Docker also refuses deletion if a container or
                # multiple tags now reference the image. No raw storage deletion.
                docker("image", "rm", item["id"])
            except subprocess.CalledProcessError as error:
                print(f"Image preserved {item['id']}: {docker_error(error)}", file=sys.stderr)
    report = filesystem_report(args.min_free_mb)
    print(json.dumps({"storage": report}, indent=2))
    if args.apply and not all(row["sufficient"] for row in report):
        print("Insufficient Docker/containerd space or inodes. No services were stopped. "
              "Expand the backing EBS volume/filesystem or inspect retained images; "
              "do not delete /var/lib/containerd or Docker volumes.")
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        print("Docker storage preflight failed: " + docker_error(error), file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Storage preflight failed: {error}", file=sys.stderr)
        raise SystemExit(1)
