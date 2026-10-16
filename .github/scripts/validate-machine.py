"""Exercise the released native backend without changing the default compiler build."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys


class Validation:
    def __init__(self, args):
        self.source = args.source.resolve()
        self.compiler = args.wlc.resolve()
        self.out = args.artifacts.resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.env = os.environ.copy()
        self.env["WL_PATH"] = str(args.std_root.resolve() if args.std_root else self.compiler.parent.parent)
        self.suffix = ".exe" if os.name == "nt" else ""
        self.clang = (args.clang or self.compiler.parent.parent / "tools/llvm/bin" / ("clang" + self.suffix)).resolve()
        self.deadline = Path(__file__).with_name("run-with-timeout.py")
        self.passed = 0
        self.failed = 0
        self.failures = []
        self.levels = args.levels
        self.boot_status = "not run"

    def command(self, args, timeout=180, cwd=None, env=None):
        command = [str(arg) for arg in args]
        process = subprocess.run([sys.executable, str(self.deadline), str(timeout), *command],
                                 cwd=cwd or self.source, env=env or self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        output = process.stdout.decode("utf-8", errors="replace")
        with (self.out / "machine.log").open("a", encoding="utf-8") as stream:
            stream.write("\nCOMMAND " + repr(command) + "\n" + output)
        return process.returncode, output

    def required(self, args, timeout=180, cwd=None, env=None):
        status, output = self.command(args, timeout, cwd, env)
        if status:
            raise RuntimeError(f"Command failed ({status}): {args}\n{output[-12000:]}")
        return output

    def check(self, name, callback):
        print("[RUNNING] " + name, flush=True)
        try:
            callback()
        except (AssertionError, RuntimeError, OSError) as error:
            self.failed += 1
            self.failures.append(name)
            print(f"[FAIL] {name}\n{error}", flush=True)
        else:
            self.passed += 1
            print("[PASS] " + name, flush=True)
        finally:
            self.summary()

    def summary(self):
        (self.out / "tests-passed").write_text(str(self.passed) + "\n", encoding="utf-8")
        (self.out / "tests-failed").write_text(str(self.failed) + "\n", encoding="utf-8")
        (self.out / "bootstrap-build-status").write_text(self.boot_status + "\n", encoding="utf-8")
        (self.out / "machine-result.json").write_text(json.dumps(dict(
            passed=self.passed, failed=self.failed, failures=self.failures, levels=self.levels,
            machine_self_build=self.boot_status), indent=2), encoding="utf-8")

    def smoke(self, level):
        source = self.out / "machine-probe.wl"
        source.write_text('func main() -> Int { let value = 40 + 2; print("PASS: native", value); return 0; }\n', encoding="utf-8")
        program = self.out / ("machine-probe" + self.suffix)
        obj = self.out / ("machine-probe.obj" if os.name == "nt" else "machine-probe.o")
        ir = self.out / "machine-probe.wir"
        for output in (program, obj, ir):
            output.unlink(missing_ok=True)
        output = self.required([self.compiler, source, "--backend=machine", level, "-c", "--verbose", "-o", obj])
        assert "Lowered source through the machine backend" in output, output
        data = obj.read_bytes()
        if os.name == "nt":
            assert len(data) >= 20 and struct.unpack_from("<H", data)[0] == 0x8664
        else:
            assert len(data) >= 64 and data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", data, 18)[0] == 62
        self.required([self.compiler, source, "--backend=machine", level, "-o", program])
        assert "PASS:" in self.required([program], 30)
        self.required([self.compiler, source, "--backend=machine", level, "--emit-wir", "-o", ir])
        assert "func @" in ir.read_text(encoding="utf-8")
        rejected = self.out / "unsupported.s"
        rejected.unlink(missing_ok=True)
        status, output = self.command([self.compiler, source, "--backend=machine", "-S", "-o", rejected])
        assert status not in (0, 124, 137) and "does not support" in output and not rejected.exists(), output

    def self_build(self):
        self.boot_status = "failed"
        generations = [self.out / ("machine-gen1" + self.suffix), self.out / ("machine-gen2" + self.suffix)]
        previous = self.compiler
        for compiler in generations:
            self.required([previous, "src/wlc.wl", "--backend=machine", "-O2", "-o", compiler], 300)
            self.required([compiler, "--version"], 30)
            previous = compiler
        hashes = []
        for index, compiler in enumerate(generations, 1):
            obj = self.out / (f"self-gen{index}.obj" if os.name == "nt" else f"self-gen{index}.o")
            self.required([compiler, "src/wlc.wl", "--backend=machine", "-O2", "-c", "-o", obj], 300)
            hashes.append(hashlib.sha256(obj.read_bytes()).hexdigest())
        assert hashes[0] == hashes[1], f"Machine self-build objects differ: {hashes}"
        self.boot_status = "success (two machine-built compilers emit identical self-build objects)"
        (self.out / "machine-self-sha256").write_text(hashes[0] + "\n", encoding="utf-8")

    def positive(self, source, level):
        program = self.out / ("machine-test" + self.suffix)
        program.unlink(missing_ok=True)
        self.required([self.compiler, source, "--backend=machine", level, "-o", program])
        assert "PASS:" in self.required([program], 60), "Program returned zero without PASS marker."

    def diagnostic(self, source, level):
        declaration = re.search(r'^// Expected Error: "(.*)"[ \t]*$',
                                source.read_text(encoding="utf-8-sig"), re.MULTILINE)
        expected = declaration.group(1) if declaration else None
        assert expected, f"Missing Expected Error: {source}"
        program = self.out / ("machine-diagnostic" + self.suffix)
        program.unlink(missing_ok=True)
        status, output = self.command([self.compiler, source, "--backend=machine", level, "-o", program])
        assert status not in (124, 137), output
        if status == 0:
            assert program.is_file(), "Compiler returned success without an executable."
            status, run_output = self.command([program], 60)
            output += run_output
        assert status not in (0, 124, 137) and expected in output, f"Expected {expected!r}, status={status}\n{output}"

    def ffi(self, level):
        directory = self.out / ("ffi-" + level[1:])
        directory.mkdir(exist_ok=True)
        ffi_env = self.env.copy()
        if sys.platform == "linux":
            # The White host has no rpath, keep the test library ahead of LLVM's
            # shared libraries without leaking this path into later tests.
            ffi_env["LD_LIBRARY_PATH"] = str(directory) + os.pathsep + ffi_env.get("LD_LIBRARY_PATH", "")
        for name in ("test_lib_export.wl", "test_dll_host.c", "test_dll_host.wl", "test_dll_host.py"):
            shutil.copyfile(self.source / "tests/integration/ffi" / name, directory / name)
        self.required([self.compiler, "test_lib_export.wl", "--backend=machine", level, "--shared"], cwd=directory)
        c_host = directory / ("c-host" + self.suffix)
        wl_host = directory / ("wl-host" + self.suffix)
        if os.name == "nt":
            library = directory / "test_lib_export.lib"
            assert library.is_file(), "Shared machine build did not produce its import library."
            c_args = [self.clang, "--target=x86_64-w64-windows-gnu",
                      Path(__file__).with_name("windows-ffi-host.c").resolve(), library,
                      "-nostdlib", "-lkernel32", "-Wl,--entry,mainCRTStartup", "-Wl,--subsystem,console", "-o", c_host]
        else:
            library = directory / "libtest_lib_export.so"
            assert library.is_file()
            shutil.copyfile(library, directory / "test_lib_export.so")
            c_args = [self.clang, "test_dll_host.c", "-L.", "-ltest_lib_export", "-Wl,-rpath,$ORIGIN", "-o", c_host]
        self.required(c_args, cwd=directory)
        assert "PASS:" in self.required([c_host], 30, directory, env=ffi_env)
        assert "PASS:" in self.required([sys.executable, "test_dll_host.py"], 30, directory, env=ffi_env)
        self.required([self.compiler, "test_dll_host.wl", library, "--backend=machine", level, "-o", wl_host], cwd=directory)
        assert "PASS:" in self.required([wl_host], 30, directory, env=ffi_env)

    def run(self):
        self.summary()
        assert sys.platform in ("win32", "linux") and platform.machine().lower() in ("amd64", "x86_64"), "Only native x64 Windows/Linux is supported."
        assert self.compiler.is_file() and self.clang.is_file()
        version = self.required([self.compiler, "--version", "--verbose"], 30)
        print(version, flush=True)
        (self.out / "compiler-version.txt").write_text(version, encoding="utf-8")
        for level in self.levels:
            self.check("native output modes " + level, lambda: self.smoke(level))
        self.check("machine self-bootstrap fixed point", self.self_build)
        positive = [path for path in sorted((self.source / "tests").rglob("*.wl"))
                    if not any(part in ("fixtures", "ffi", "failures") for part in path.relative_to(self.source / "tests").parts)]
        diagnostics = sorted((self.source / "tests/diagnostics/failures").rglob("*.wl"))
        for level in self.levels:
            for path in positive:
                self.check(f"{level} {path.relative_to(self.source)}", lambda: self.positive(path, level))
            for path in diagnostics:
                self.check(f"{level} diagnostic {path.name}", lambda: self.diagnostic(path, level))
            self.check("C/Python/White machine FFI " + level, lambda: self.ffi(level))
        self.summary()
        return 1 if self.failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wlc", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--std-root", type=Path)
    parser.add_argument("--clang", type=Path)
    parser.add_argument("--levels", nargs="+", choices=["O0", "O1", "O2", "O3", "Os", "Oz"], default=["O0", "O2", "Oz"])
    args = parser.parse_args()
    args.levels = ["-" + level for level in args.levels]
    validation = Validation(args)
    try:
        return validation.run()
    except (AssertionError, RuntimeError, OSError) as error:
        validation.failed += 1
        validation.failures.append("validation setup")
        validation.summary()
        print(f"[FAIL] validation setup\n{error}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
