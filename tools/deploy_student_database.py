"""Export, SSH-transfer and provision a private student database without app imports."""

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

DEFAULT_DIRECTORY = "/var/lib/sc-assistant"
DEFAULT_DESTINATION = DEFAULT_DIRECTORY + "/dummy_students.sqlite3"
_REMOTE_STAGE = re.compile(r"/tmp/sc-assistant-student-[A-Za-z0-9]{10}")
_HOST = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]*")


def _storage():
    # A transferred .pyz already contains its own rag package; never add /tmp to
    # its import path. Direct repository execution needs the project root only.
    if not getattr(globals().get("__loader__"), "archive", None):
        root = str(Path(__file__).resolve().parents[1])
        if root not in sys.path:
            sys.path.insert(0, root)
    from rag.dummy_students import inspect_database, snapshot_database
    from rag.student_database_deploy import provision_database

    return inspect_database, snapshot_database, provision_database


def _plain_path(value):
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise ValueError("Parent traversal is not permitted in deployment paths.")
    path = Path(os.path.abspath(path))
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            if cursor != path:
                raise
            break
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Deployment paths must not contain symlinks or reparse points.")
        if cursor != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("A deployment path ancestor is not a directory.")
        if os.name == "posix" and cursor != path:
            if info.st_uid not in (0, os.geteuid()):
                raise PermissionError("Deployment path ancestors must be owned by root or the operator.")
            if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise PermissionError("Deployment path ancestors must not be group/world writable.")
    return path


def _private_entry(path, *, directory=False):
    info = path.lstat()
    valid_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_kind or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Expected a regular private database file or directory, not a link.")
    if os.name == "posix":
        expected = 0o700 if directory else 0o600
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != expected:
            raise PermissionError(
                f"Existing storage must be owned by uid {os.geteuid()} with mode {expected:o}; "
                "existing permissions were not changed."
            )
    return info


def prepare_directory(directory):
    """Create one private leaf; never recursively chmod/chown existing host data."""
    path = _plain_path(directory)
    try:
        _private_entry(path, directory=True)
    except FileNotFoundError:
        if os.name == "posix":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open(path.parent, flags)
            try:
                try:
                    os.mkdir(path.name, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            finally:
                os.close(descriptor)
        else:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        _private_entry(path, directory=True)
    return {"status": "prepared", "directory": str(path)}


def _private_storage(destination):
    path = _plain_path(destination)
    _private_entry(path.parent, directory=True)
    present = False
    sidecars = False
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = path.with_name(path.name + suffix)
        try:
            _private_entry(candidate)
        except FileNotFoundError:
            continue
        if suffix:
            sidecars = True
        else:
            present = True
    if sidecars and not present:
        raise ValueError("Orphan SQLite recovery files exist; recover them instead of replacing the database.")
    return path, present


def _run(arguments, *, timeout=900):
    try:
        return subprocess.run(arguments, check=True, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() or "Remote command failed."
        raise RuntimeError(message) from None


def _bundle(destination):
    if getattr(globals().get("__loader__"), "archive", None):
        raise ValueError("Run transfer from the repository script, not a transferred installer.")
    root = Path(__file__).resolve().parents[1]
    files = {
        "student_database_cli.py": root / "tools/deploy_student_database.py",
        "rag/dummy_students.py": root / "rag/dummy_students.py",
        "rag/student_subjects.py": root / "rag/student_subjects.py",
        "rag/student_database_deploy.py": root / "rag/student_database_deploy.py",
    }
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", "from student_database_cli import main\nraise SystemExit(main())\n")
        archive.writestr("rag/__init__.py", "")
        for name, source in files.items():
            if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root):
                raise ValueError("Installer code must be regular repository files.")
            archive.write(source, name)


def transfer(args, snapshot_database):
    """Use host-key-verified SSH/SCP; no credentials or data are placed in Git."""
    if not _HOST.fullmatch(args.host):
        raise ValueError("Use a DNS/IP host or an SSH-config alias, optionally prefixed by user@.")
    if not 1 <= args.port <= 65535:
        raise ValueError("SSH port must be within 1..65535.")
    destination = PurePosixPath(args.destination)
    if not destination.is_absolute() or ".." in destination.parts or destination.name != "dummy_students.sqlite3":
        raise ValueError("Remote destination must be an absolute path ending in dummy_students.sqlite3, without '..'.")
    if not args.remote_python or args.remote_python.startswith("-") or any(c in args.remote_python for c in "\r\n\0"):
        raise ValueError("Specify a single remote Python executable, not shell arguments.")
    options = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ForwardAgent=no",
               "-o", "ConnectTimeout=15"]
    if args.identity_file is not None:
        identity = args.identity_file.expanduser()
        if not identity.is_file():
            raise ValueError("The SSH identity file does not exist.")
        options += ["-o", "IdentitiesOnly=yes", "-i", str(identity.absolute())]
    ssh = ["ssh", "-T", *options, "-p", str(args.port), args.host]
    scp = ["scp", "-q", *options, "-P", str(args.port)]
    privilege = [] if args.no_sudo else ["sudo", "-n", "--"]
    runtime = [*privilege, args.remote_python]
    check_python = "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 'Python 3.10 or later is required')"
    _run([*ssh, shlex.join([*runtime, "-c", check_python])], timeout=60)

    remote_stage = None
    try:
        result = _run([*ssh, "umask 077; mktemp -d /tmp/sc-assistant-student-XXXXXXXXXX"], timeout=60)
        remote_stage = result.stdout.strip()
        if not _REMOTE_STAGE.fullmatch(remote_stage):
            remote_stage = None
            raise RuntimeError("The remote host returned an unexpected temporary directory.")
        with tempfile.TemporaryDirectory(prefix="sc-student-transfer-") as temporary:
            bundle = Path(temporary) / "installer.pyz"
            _bundle(bundle)
            remote_bundle = remote_stage + "/installer.pyz"
            _run([*scp, "--", str(bundle), f"{args.host}:{remote_bundle}"])
            command = [*runtime, remote_bundle]

            def remote(arguments):
                response = _run([*ssh, shlex.join([*command, *arguments])])
                try:
                    data = json.loads(response.stdout)
                except ValueError:
                    raise RuntimeError("The remote installer did not return JSON; check noninteractive shell output.") from None
                if not isinstance(data, dict):
                    raise TypeError("The remote installer response must be a JSON object.")
                return data

            remote(["prepare-directory", "--directory", str(destination.parent), "--require-root"])
            common = ["--destination", str(destination), "--pool-id", args.pool_id,
                      "--expect-start-id", str(args.expect_start_id), "--expect-end-id", str(args.expect_end_id),
                      "--require-root"]
            state = remote(["check", *common, "--allow-missing"])
            if state.get("status") == "preserved":
                return state
            if state.get("status") != "missing":
                raise RuntimeError("Remote database state is not safe for bootstrap.")

            print("Exporting a consistent current SQLite snapshot...", file=sys.stderr)
            exported = Path(temporary) / "snapshot.sqlite3"
            manifest = snapshot_database(args.source, exported, pool_id=args.pool_id,
                                         expected_start=args.expect_start_id, expected_end=args.expect_end_id)
            remote_snapshot = remote_stage + "/snapshot.sqlite3"
            print("Uploading the private snapshot with SCP...", file=sys.stderr)
            _run([*scp, "--", str(exported), f"{args.host}:{remote_snapshot}"])
            installed = remote(["install", *common, "--source", remote_snapshot,
                                "--sha256", manifest["sha256"], "--size", str(manifest["size_bytes"])])
            if installed.get("status") not in ("installed", "preserved"):
                raise RuntimeError("The remote installer did not confirm a preserved or installed database.")
            return installed
    finally:
        if remote_stage is not None:
            try:
                _run([*ssh, shlex.join(["rm", "-rf", "--", remote_stage])], timeout=60)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                print("Warning: the invocation's private remote staging directory could not be cleaned up.", file=sys.stderr)


def main(argv=None):
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Create one private, current, self-contained SQLite snapshot")
    export.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / "dummy_students.sqlite3")
    export.add_argument("--output", type=Path, required=True)
    prepare = commands.add_parser("prepare-directory", help="Create/validate one private host directory without changing existing permissions")
    prepare.add_argument("--directory", type=Path, default=Path(DEFAULT_DIRECTORY))
    check = commands.add_parser("check", help="Validate an existing writable current deployment database")
    check.add_argument("--allow-missing", action="store_true", help="Report a missing bootstrap target without creating it")
    install = commands.add_parser("install", help="Install only if missing; an existing valid host database always wins")
    install.add_argument("--source", type=Path)
    install.add_argument("--sha256")
    install.add_argument("--size", type=int)
    upload = commands.add_parser("transfer", help="Export, SCP, and install over verified SSH; preserve an existing host database")
    upload.add_argument("--host", required=True, help="user@host or an SSH-config alias; Linux host")
    upload.add_argument("--port", type=int, default=22)
    upload.add_argument("--identity-file", type=Path)
    upload.add_argument("--remote-python", default="python3", help="Remote Python 3.10+ executable")
    upload.add_argument("--no-sudo", action="store_true", help="Only when the SSH account already runs as root")
    upload.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / "dummy_students.sqlite3")
    upload.add_argument("--destination", default=DEFAULT_DESTINATION)
    for command in (check, install):
        command.add_argument("--destination", type=Path, default=Path(DEFAULT_DESTINATION))
    for command in (prepare, check, install):
        command.add_argument("--require-root", action="store_true", help="Require Linux root for the current Docker image")
    for command in (export, check, install, upload):
        command.add_argument("--pool-id", default=os.getenv("COGNITO_USER_POOL_ID"), help="Expected deployed Cognito user pool")
        command.add_argument("--expect-start-id", type=int, default=100000, help="Required complete range, not a generation option")
        command.add_argument("--expect-end-id", type=int, default=3000000, help="Use a smaller range only for explicit isolated tests")
    args = parser.parse_args(argv)
    if args.command != "prepare-directory":
        if not args.pool_id or any(char.isspace() for char in args.pool_id) or len(args.pool_id) > 256:
            parser.error("Provide --pool-id or COGNITO_USER_POOL_ID matching the deployed account pool.")
        if not 100000 <= args.expect_start_id <= args.expect_end_id <= 3000000:
            parser.error("Expected range must be within 100000..3000000.")
    if getattr(args, "require_root", False) and (not sys.platform.startswith("linux") or os.geteuid() != 0):
        parser.error("This deployment operation requires Linux root; use an authorized sudo account.")

    try:
        if args.command == "prepare-directory":
            result = prepare_directory(args.directory)
        else:
            inspect_database, snapshot_database, provision_database = _storage()
            options = {"pool_id": args.pool_id, "expected_start": args.expect_start_id, "expected_end": args.expect_end_id}
            if args.command == "export":
                result = snapshot_database(args.source, args.output, **options)
            elif args.command == "transfer":
                result = transfer(args, snapshot_database)
            else:
                if args.command == "install":
                    prepare_directory(args.destination.parent)
                destination, present = _private_storage(args.destination)
                if args.command == "check":
                    if not present and args.allow_missing:
                        result = {"status": "missing", "path": str(destination)}
                    else:
                        result = {**inspect_database(destination, writable=True, **options), "status": "preserved"}
                else:
                    result = provision_database(destination, source=args.source, expected_sha256=args.sha256,
                                                expected_size=args.size, **options)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
