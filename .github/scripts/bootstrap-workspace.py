"""Keep bootstrap generations small, with one shared release toolchain."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess


def env_path(root, path, active=False):
    root = root.resolve(strict=True)
    if not (root / "release_pkg/tools").is_dir():
        raise ValueError("Workspace has no release toolchain directory")
    path = Path(os.path.abspath(path))
    names = ("boot_env", "release_pkg") if active else ("boot_env",)
    if path.parent.resolve() != root or not (path.name in names or re.fullmatch(r"build_[0-9a-f]{7,40}", path.name)):
        raise ValueError("Not a bootstrap environment directly inside this workspace: " + str(path))
    if linked(path):
        raise ValueError("Bootstrap environment itself must not be a link: " + str(path))
    if path.resolve().parent != root:
        raise ValueError("Bootstrap environment resolves outside the workspace: " + str(path))
    return path


def junction(path):
    return os.name == "nt" and path.is_junction()


def linked(path):
    return path.is_symlink() or junction(path)


def unlink_tools(path):
    if junction(path):
        os.rmdir(path)
    else:
        path.unlink()


def attach(root, directory):
    directory = env_path(root, directory)
    shared = root.resolve() / "release_pkg/tools"
    if not shared.is_dir() or linked(shared):
        raise ValueError("Missing real release toolchain directory: " + str(shared))
    directory.mkdir(exist_ok=True)
    tools = directory / "tools"
    if linked(tools):
        if tools.resolve() != shared.resolve():
            raise ValueError("Bootstrap tools link points at another directory: " + str(tools))
        return
    if tools.exists():
        if not tools.is_dir() or tools.resolve().parent != directory.resolve():
            raise ValueError("Refusing to replace an unexpected tools path: " + str(tools))
        shutil.rmtree(tools)
    if os.name == "nt":
        # A junction works on hosted Windows without enabling Developer Mode.
        # It is detached before deleting an old generation.
        subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(tools), str(shared)], check=True)
    else:
        tools.symlink_to(os.path.relpath(shared, directory), target_is_directory=True)
    if not linked(tools) or tools.resolve() != shared.resolve():
        raise ValueError("Failed to attach the shared toolchain: " + str(tools))
    print(f"Shared bootstrap tools: {tools} -> {shared}", flush=True)


def retire(root, old, active):
    old = env_path(root, old)
    active = env_path(root, active, active=True)
    if old == active:
        raise ValueError("Refusing to remove the active bootstrap generation")
    if not any((active / "bin" / name).is_file() for name in ("wlc", "wlc.exe")):
        raise ValueError("The replacement bootstrap environment has no compiler")
    if not old.exists():
        return
    tools = old / "tools"
    if linked(tools):
        expected = root.resolve() / "release_pkg/tools"
        if tools.resolve() != expected.resolve():
            raise ValueError("Refusing to retire an environment with an unexpected tools link")
        unlink_tools(tools)
    shutil.rmtree(old)
    print("Retired bootstrap environment: " + str(old), flush=True)


def report(root, minimum):
    usage = shutil.disk_usage(root)
    unit = 1024 ** 3
    snapshots = root / "Output_Snapshots"
    snapshot_bytes = sum(path.stat().st_size for path in snapshots.glob("*") if path.is_file())
    generations = sum(1 for path in root.glob("build_*") if path.is_dir())
    print(f"Bootstrap disk: {usage.free / unit:.2f} GiB free of {usage.total / unit:.2f} GiB, "
          f"{generations} generation directories, {snapshot_bytes / unit:.2f} GiB snapshots", flush=True)
    if usage.free < minimum * 1024 ** 2:
        raise ValueError(f"Less than {minimum} MiB free before bootstrap, stopping before the runner fills its disk")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("attach", "retire", "report"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        if name in ("attach", "retire"):
            command.add_argument("--env", type=Path, required=True)
        if name == "retire":
            command.add_argument("--active", type=Path, required=True)
        if name == "report":
            command.add_argument("--minimum-free-mib", type=int, default=0)
    args = parser.parse_args()
    try:
        if args.command == "attach":
            attach(args.root, args.env)
        elif args.command == "retire":
            retire(args.root, args.env, args.active)
        else:
            report(args.root, args.minimum_free_mib)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, "::error::" + str(error) + "\n")


if __name__ == "__main__":
    main()
