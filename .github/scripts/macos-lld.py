"""Supply a matching Mach-O linker for LLVM packages which omit LLD."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile


DEPLOYMENT = "11.0"


def run(args):
    print("+ " + shlex.join(map(str, args)), flush=True)
    return subprocess.check_output(list(map(str, args)), text=True)


def clang_version(output):
    first = output.splitlines()[0] if output else ""
    version = re.search(r"\bclang version (\d+\.\d+\.\d+)(?=\s|$)", first)
    if "Apple clang" in first or not version:
        raise ValueError("Expected an upstream release Clang with a full X.Y.Z version: " + first)
    return version.group(1)


def release_version(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid LLVM release version: " + version)
    return version


def inspect(llvm, output):
    version = clang_version(run([llvm / "bin/clang", "--version"]))
    needed = not any(os.access(llvm / "bin" / name, os.X_OK) for name in ("ld64.lld", "lld"))
    print(f"Packaged Clang: {version}, build matching LLD: {needed}", flush=True)
    with output.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"version={version}\nneeded={str(needed).lower()}\n")


def check_binary(binary, version):
    actual = run([binary, "--version"])
    if not re.search(r"\bLLD " + re.escape(version) + r"(?=\s|$)", actual):
        raise ValueError(f"Expected LLD {version}, got {actual.strip()}")
    # The cache and release package move independently of the build runner.
    # A static LLVM build leaves only Apple's system libraries here.
    dependencies = run(["otool", "-L", binary])
    for line in dependencies.splitlines()[1:]:
        name = line.strip().split(" (", 1)[0]
        if name and not name.startswith(("/usr/lib/", "/System/Library/")):
            raise ValueError("LLD has a non-system dependency: " + name)


def configure(source, build):
    args = ["cmake", "-S", source / "llvm", "-B", build]
    if shutil.which("ninja"):
        args += ["-G", "Ninja"]
    args += [
        "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_C_FLAGS_RELEASE=-O1 -DNDEBUG",
        "-DCMAKE_CXX_FLAGS_RELEASE=-O1 -DNDEBUG",
        "-DCMAKE_C_COMPILER=" + run(["xcrun", "--find", "clang"]).strip(),
        "-DCMAKE_CXX_COMPILER=" + run(["xcrun", "--find", "clang++"]).strip(),
        "-DCMAKE_OSX_SYSROOT=" + run(["xcrun", "--show-sdk-path"]).strip(),
        "-DCMAKE_OSX_DEPLOYMENT_TARGET=" + DEPLOYMENT,
        "-DLLVM_ENABLE_PROJECTS=lld", "-DLLVM_TARGETS_TO_BUILD=X86;AArch64",
        "-DBUILD_SHARED_LIBS=OFF", "-DLLVM_BUILD_LLVM_DYLIB=OFF", "-DLLVM_LINK_LLVM_DYLIB=OFF",
        "-DLLVM_INCLUDE_TESTS=OFF", "-DLLVM_INCLUDE_EXAMPLES=OFF", "-DLLVM_INCLUDE_BENCHMARKS=OFF",
        "-DLLVM_BUILD_TOOLS=OFF", "-DLLD_BUILD_TOOLS=ON", "-DLLVM_ENABLE_ASSERTIONS=OFF",
        "-DLLVM_TOOL_LLVM_DRIVER_BUILD=OFF",
        "-DLLVM_ENABLE_ZLIB=OFF", "-DLLVM_ENABLE_ZSTD=OFF", "-DLLVM_ENABLE_LIBXML2=OFF",
        "-DLLVM_ENABLE_LIBEDIT=OFF", "-DLLVM_ENABLE_TERMINFO=OFF", "-DLLVM_ENABLE_CURL=OFF",
    ]
    return args


def build_lld(version, cache):
    version = release_version(version)
    if platform.system() != "Darwin":
        raise ValueError("The packaged Mach-O linker must be built on macOS")
    with tempfile.TemporaryDirectory(prefix="white-lld-", dir=os.environ.get("RUNNER_TEMP")) as temp:
        work = Path(temp)
        asset = f"llvm-project-{version}.src.tar.xz"
        release = json.loads(run(["gh", "api", f"repos/llvm/llvm-project/releases/tags/llvmorg-{version}"]))
        matches = [item for item in release["assets"] if item["name"] == asset]
        if len(matches) != 1:
            raise ValueError(f"Official LLVM {version} release has no unique {asset}")
        subprocess.run(["gh", "release", "download", "llvmorg-" + version, "--repo", "llvm/llvm-project",
                        "--pattern", asset, "--dir", str(work)], check=True)
        archive = work / asset
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        published_digest = matches[0].get("digest")
        if published_digest and published_digest != "sha256:" + digest:
            raise ValueError("LLVM source archive does not match the published SHA-256")
        print(f"LLVM source SHA-256: {digest}", flush=True)
        with tarfile.open(archive) as tar:
            tar.extractall(work, filter="data")
        source = work / ("llvm-project-" + version + ".src")
        if not (source / "llvm/CMakeLists.txt").is_file():
            raise ValueError("LLVM source archive has an unexpected layout")
        build = work / "build"
        subprocess.run(list(map(str, configure(source, build))), check=True)
        subprocess.run(["cmake", "--build", str(build), "--target", "lld", "--parallel",
                        str(min(os.cpu_count() or 2, 4))], check=True)
        cache.mkdir(parents=True, exist_ok=True)
        binary = cache / "ld64.lld"
        shutil.copy2(build / "bin/lld", binary)
        binary.chmod(0o755)
        check_binary(binary, version)
        (cache / "build.json").write_text(json.dumps(dict(version=version, arch=platform.machine(),
            deployment=DEPLOYMENT, source_sha256=digest,
            binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest()), indent=2) + "\n", encoding="utf-8")


def install(llvm, cache, version):
    version = release_version(version)
    metadata = json.loads((cache / "build.json").read_text(encoding="utf-8"))
    if (metadata["version"], metadata["arch"], metadata["deployment"]) != (version, platform.machine(), DEPLOYMENT):
        raise ValueError("Cached LLD does not match the requested Clang, host architecture or deployment target")
    binary = cache / "ld64.lld"
    if hashlib.sha256(binary.read_bytes()).hexdigest() != metadata["binary_sha256"]:
        raise ValueError("Cached LLD checksum mismatch")
    binary.chmod(0o755)
    check_binary(binary, version)
    destination = llvm / "bin/ld64.lld"
    if destination.exists() or destination.is_symlink():
        raise ValueError("Refusing to replace an existing linker: " + str(destination))
    shutil.copy2(binary, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("inspect")
    probe.add_argument("--llvm", required=True, type=Path)
    probe.add_argument("--github-output", required=True, type=Path)
    build = sub.add_parser("build")
    build.add_argument("--version", required=True)
    build.add_argument("--cache", required=True, type=Path)
    copy = sub.add_parser("install")
    copy.add_argument("--llvm", required=True, type=Path)
    copy.add_argument("--cache", required=True, type=Path)
    copy.add_argument("--version", required=True)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            inspect(args.llvm.resolve(), args.github_output)
        elif args.command == "build":
            build_lld(args.version, args.cache.resolve())
        else:
            install(args.llvm.resolve(), args.cache.resolve(), args.version)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, "::error::" + str(error) + "\n")


if __name__ == "__main__":
    main()
