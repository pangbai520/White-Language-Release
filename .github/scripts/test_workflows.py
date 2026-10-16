"""Local regressions for release planning, deadlines and diagnostic handling."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = load("bootstrap-plan")
machine = load("validate-machine")
macos_lld = load("macos-lld")
workspace = load("bootstrap-workspace")
checkpoint = load("compiler-checkpoint")


def bash_program():
    bash = shutil.which("bash")
    if sys.platform == "win32":
        for parent in Path(shutil.which("git")).parents:
            candidate = parent / "bin/bash.exe"
            if candidate.is_file():
                return str(candidate)
    return bash


class History(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="white-ci-history-")
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "CI test")
        self.git("config", "user.email", "ci@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.base = self.change("src/wlc.wl", "seed")
        self.git("tag", "v0.3.6")

    def git(self, *args):
        return plan.git(self.repo, *args).stdout.strip()

    def change(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content + "\n", encoding="utf-8")
        self.git("add", path)
        self.git("commit", "-m", content)
        return self.git("rev-parse", "HEAD")

    def release(self, tag, **kwargs):
        return dict(tag_name=tag, draft=False, prerelease=False,
                    assets=[dict(name=f"whitelang-linux-x64-{tag[1:]}.tar.gz")], **kwargs)

    def test_skip_docs_but_keep_final_release_commit(self):
        code = self.change("src/wlc.wl", "new compiler")
        self.change("README.md", "docs")
        final = self.change("README.md", "release docs")
        self.assertEqual(plan.ordered_commits(self.repo, self.base, final), [code, final])

    def test_readme_side_branch_does_not_roll_back_the_compiler(self):
        self.git("checkout", "-b", "compiler")
        code = self.change("src/wlc.wl", "new compiler")
        self.git("checkout", "main")
        self.change("README.md", "readme update")
        self.git("merge", "--no-ff", "compiler", "-m", "release merge")
        final = self.git("rev-parse", "HEAD")
        self.assertEqual(plan.ordered_commits(self.repo, self.base, final), [code, final])

    def test_source_side_branches_need_an_explicit_bridge(self):
        self.git("checkout", "-b", "compiler")
        self.change("src/branch.wl", "branch change")
        self.git("checkout", "main")
        self.change("src/main.wl", "main change")
        self.git("merge", "--no-ff", "compiler", "-m", "source merge")
        with self.assertRaisesRegex(ValueError, "diverge"):
            plan.ordered_commits(self.repo, self.base, "HEAD")

    def test_reject_non_ancestor_seed(self):
        self.git("checkout", "-b", "side")
        side = self.change("src/side.wl", "side")
        self.git("checkout", "main")
        final = self.change("src/wlc.wl", "main")
        with self.assertRaisesRegex(ValueError, "ancestor"):
            plan.ordered_commits(self.repo, side, final)

    def test_closest_stable_ancestor_with_matching_assets(self):
        self.change("src/wlc.wl", "stable")
        self.git("tag", "v0.3.7")
        self.change("src/wlc.wl", "alpha")
        self.git("tag", "v0.3.8-alpha1")
        self.change("src/wlc.wl", "draft")
        self.git("tag", "v0.3.8")
        final = self.change("src/wlc.wl", "target")
        releases = [self.release("v0.3.6"), self.release("v0.3.7"),
                    self.release("v0.3.8-alpha1"), self.release("v0.3.8")]
        releases[-1]["draft"] = True
        tag, base, target = plan.choose_seed(self.repo, final, releases, ["linux-x64"])
        self.assertEqual(tag, "v0.3.7")
        self.assertEqual(base, plan.commit(self.repo, tag))
        self.assertEqual(target, final)

    def test_missing_platform_assets_are_not_a_seed(self):
        final = self.change("src/wlc.wl", "target")
        with self.assertRaisesRegex(ValueError, "all requested platform assets"):
            plan.choose_seed(self.repo, final, [self.release("v0.3.6")], ["windows-x64"])

    def test_paginated_json_arrays_and_objects(self):
        values = [dict(tag_name="v0.3.6"), dict(tag_name="v0.3.7")]
        for data in (json.dumps(values), "\n".join(map(json.dumps, values)),
                     "\n".join(json.dumps([value]) for value in values)):
            self.assertEqual(list(plan.json_stream(data)), values)

    def test_order_cli_emits_lf_bytes(self):
        final = self.change("src/wlc.wl", "target")
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "bootstrap-plan.py"), "order",
                                 "--source", str(self.repo), "--base", self.base, "--target", final],
                                capture_output=True, check=True, timeout=15)
        self.assertEqual(result.stdout, (final + "\n").encode("ascii"))

    def test_bootstrap_loops_accept_crlf_and_reject_bad_revisions(self):
        bash = bash_program()
        if not bash:
            self.skipTest("Bash is unavailable")
        workflow = (SCRIPTS.parent / "workflows/release.yml").read_text(encoding="utf-8")
        prefixes = re.findall(r'for COMMIT in \$COMMITS; do\n(.*?)echo "-+"', workflow, re.DOTALL)
        self.assertEqual(len(prefixes), 2, "Expected Windows and Unix bootstrap loops")
        final = self.change("src/wlc.wl", "target")
        expected = [self.git("rev-parse", "--short", oid) for oid in (self.base, final)]
        for prefix in prefixes:
            script = 'for COMMIT in $TEST_COMMITS; do\n' + prefix + 'printf "%s\\n" "$SHORT_SHA"\ndone\n'
            for separator in ("\n", "\r\n"):
                payload = separator.join((self.base, final)) + separator
                result = subprocess.run([bash, "-e", "-s"], input=script.encode(), capture_output=True,
                                        cwd=self.repo, env={**os.environ, "TEST_COMMITS": payload}, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(result.stdout.decode().splitlines(), expected)
            result = subprocess.run([bash, "-e", "-s"], input=script.encode(), capture_output=True,
                                    cwd=self.repo, env={**os.environ, "TEST_COMMITS": "not-a-commit\r\n"}, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"Invalid bootstrap commit", result.stdout)

    def select_targets(self, event="workflow_dispatch", tag="v0.3.8-alpha1"):
        bash = bash_program()
        if not bash:
            self.skipTest("Bash is unavailable")
        workflow = (SCRIPTS.parent / "workflows/release.yml").read_text(encoding="utf-8")
        match = re.search(r'      - name: Select build targets\n.*?        run: \|\n(.*?)(?=\n      - name:)',
                          workflow, re.DOTALL)
        self.assertIsNotNone(match)
        output = self.repo / ("outputs-" + event)
        env = dict(os.environ, GITHUB_EVENT_NAME=event, GITHUB_REF="refs/tags/" + tag,
                   GITHUB_REF_NAME=tag, REQUESTED_OPERATION="repair", REQUESTED_TAG=tag,
                   REQUESTED_PLATFORM="all", GITHUB_OUTPUT=output.as_posix())
        result = subprocess.run([bash, "-e", "-s"], input=textwrap.dedent(match.group(1)).encode(),
                                env=env, cwd=self.repo, capture_output=True, timeout=15)
        values = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
        return result, values

    def test_all_repair_matches_a_full_tag_build(self):
        repair, repaired = self.select_targets()
        push, tagged = self.select_targets(event="push")
        self.assertEqual(repair.returncode, 0, repair.stderr.decode(errors="replace"))
        self.assertEqual(push.returncode, 0, push.stderr.decode(errors="replace"))
        self.assertEqual(repaired, tagged)
        platforms = {target["os_name"] for target in json.loads(repaired["matrix"])}
        self.assertEqual(platforms, {"windows-x64", "windows-x32", "macos-x64", "macos-arm64",
                                    "linux-x64", "linux-x86", "linux-arm64"})
        self.assertEqual(repaired["build_armv7"], "true")
        self.assertEqual(repaired["full_release"], "true")
        self.assertEqual(repaired["publish"], "true")
        self.assertEqual(repaired["prerelease"], "true")

    def test_all_repair_rejects_an_invalid_release_tag(self):
        result, outputs = self.select_targets(tag="invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outputs, {})
        self.assertIn(b"valid release_tag is required", result.stdout)


class Deadlines(unittest.TestCase):
    def run_command(self, seconds, code):
        return subprocess.run([sys.executable, str(SCRIPTS / "run-with-timeout.py"), str(seconds),
                               sys.executable, "-c", code], capture_output=True, timeout=25)

    def test_success_and_exit_status(self):
        self.assertEqual(self.run_command(5, "print('PASS:')").returncode, 0)
        self.assertEqual(self.run_command(5, "raise SystemExit(7)").returncode, 7)

    def test_timeout_terminates_descendants_holding_stdout(self):
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"
        result = self.run_command(0.4, code)
        self.assertEqual(result.returncode, 124)
        self.assertIn(b"Timed out", result.stderr)


class MacToolchain(unittest.TestCase):
    def setUp(self):
        self.bash = bash_program()
        if not self.bash:
            self.skipTest("Bash is unavailable")
        self.directory = tempfile.TemporaryDirectory(prefix="white-ci-mac-tools-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.llvm = self.root / "LLVM package"
        (self.llvm / "bin").mkdir(parents=True)
        self.executable("clang")

    def executable(self, name):
        path = self.llvm / "bin" / name
        path.write_text("#!/bin/sh\nprintf 'test tool version\\n'\n", encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def setup_linker(self):
        return subprocess.run([self.bash, str(SCRIPTS / "setup-macos-linker.sh"), str(self.llvm)],
                              cwd=self.root, capture_output=True, timeout=15)

    def test_config_is_relocatable_and_repeatable(self):
        self.executable("ld64.lld")
        config = self.llvm / "bin/clang.cfg"
        config.write_text("-Wno-override-module\n", encoding="utf-8")
        target = self.llvm / "bin/arm64-apple-darwin.cfg"
        target.write_text("-mmacosx-version-min=11.0\n", encoding="utf-8")
        for repeat in range(2):
            result = self.setup_linker()
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        for file in (config, target):
            self.assertEqual(file.read_text().count("-fuse-ld=lld"), 1)
            self.assertNotIn(str(self.root), file.read_text())
        self.assertIn("-Wno-override-module", config.read_text())

    def test_generic_lld_supplies_the_macho_alias(self):
        self.executable("lld")
        result = self.setup_linker()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertTrue((self.llvm / "bin/ld64.lld").is_file())

    def test_missing_lld_fails_instead_of_using_the_system_linker(self):
        result = self.setup_linker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"complete macOS LLVM toolchain", result.stderr)

    def test_conflicting_linker_config_is_rejected(self):
        self.executable("ld64.lld")
        (self.llvm / "bin/clang.cfg").write_text("-fuse-ld=/some/other/ld\n", encoding="utf-8")
        result = self.setup_linker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Conflicting linker selection", result.stderr)

    def test_matching_flag_does_not_hide_a_conflicting_path(self):
        self.executable("ld64.lld")
        (self.llvm / "bin/clang.cfg").write_text(
            "-fuse-ld=lld\n--ld-path=/some/other/ld\n", encoding="utf-8")
        result = self.setup_linker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Conflicting linker selection", result.stderr)

    def test_dylib_directories_are_preserved(self):
        library = self.root / "llvm_full/lib"
        (library / "c++").mkdir(parents=True)
        (library / "libc++.1.dylib").write_text("root runtime", encoding="utf-8")
        (library / "c++/libc++.1.dylib").write_text("private runtime", encoding="utf-8")
        workflow = (SCRIPTS.parent / "workflows/release.yml").read_text(encoding="utf-8")
        match = re.search(r'(find llvm_full -type f -o -type l \| while IFS= read -r library; do\n.*?\n          done)',
                          workflow, re.DOTALL)
        self.assertIsNotNone(match)
        script = textwrap.dedent(match.group(1)).replace('${{ matrix.os_name }}', "macos-arm64")
        result = subprocess.run([self.bash, "-e", "-s"], input=script.encode(), cwd=self.root,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        copied = self.root / "release_pkg/tools/llvm/lib"
        self.assertEqual((copied / "libc++.1.dylib").read_text(), "root runtime")
        self.assertEqual((copied / "c++/libc++.1.dylib").read_text(), "private runtime")


class MacLldBuild(unittest.TestCase):
    def test_full_upstream_versions(self):
        for line in ("clang version 18.1.8", "Homebrew clang version 20.1.7 (build 123)"):
            self.assertRegex(macos_lld.clang_version(line), r"^\d+\.\d+\.\d+$")

    def test_apple_and_development_versions_need_an_explicit_source(self):
        for line in ("Apple clang version 16.0.0", "clang version 23.0.0git", "clang version 18", ""):
            with self.subTest(line=line), self.assertRaises(ValueError):
                macos_lld.clang_version(line)

    def test_release_version_cannot_be_a_path_or_ref(self):
        for version in ("../18.1.8", "18.1.8;echo bad", "main", "18.1"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                macos_lld.release_version(version)

    def test_inspect_records_missing_linker_with_lf(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            output = directory / "output"
            with patch.object(macos_lld, "run", return_value="clang version 18.1.8\n"):
                macos_lld.inspect(directory, output)
            self.assertEqual(output.read_bytes(), b"version=18.1.8\nneeded=true\n")

    def test_existing_linker_does_not_need_a_build(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "bin").mkdir()
            binary = directory / "bin/lld"
            binary.touch()
            binary.chmod(0o755)
            with patch.object(macos_lld, "run", return_value="clang version 18.1.8\n"):
                macos_lld.inspect(directory, directory / "output")
            self.assertIn(b"needed=false\n", (directory / "output").read_bytes())

    def test_system_libraries_are_relocatable(self):
        dependencies = "ld64.lld:\n\t/usr/lib/libc++.1.dylib (compatibility version 1.0.0)\n"
        with patch.object(macos_lld, "run", side_effect=["LLD 18.1.8", dependencies]):
            macos_lld.check_binary(Path("ld64.lld"), "18.1.8")

    def test_build_machine_dependencies_are_rejected(self):
        for dependency in ("@rpath/libLLVM.dylib", "/opt/homebrew/lib/libzstd.dylib", "/usr/local/lib/libz.dylib"):
            with self.subTest(dependency=dependency):
                with patch.object(macos_lld, "run", side_effect=["LLD 18.1.8", "ld64.lld:\n\t" + dependency]):
                    with self.assertRaisesRegex(ValueError, "non-system dependency"):
                        macos_lld.check_binary(Path("ld64.lld"), "18.1.8")

    def test_mismatched_binary_version_is_rejected(self):
        with patch.object(macos_lld, "run", return_value="LLD 18.1.7"):
            with self.assertRaisesRegex(ValueError, "Expected LLD 18.1.8"):
                macos_lld.check_binary(Path("ld64.lld"), "18.1.8")

    def test_build_uses_system_compiler_and_static_llvm(self):
        with patch.object(macos_lld, "run", side_effect=["/Xcode/clang\n", "/Xcode/clang++\n", "/Xcode/SDK\n"]):
            with patch.object(macos_lld.shutil, "which", return_value="/usr/bin/ninja"):
                args = macos_lld.configure(Path("source"), Path("build"))
        self.assertIn("-DCMAKE_CXX_COMPILER=/Xcode/clang++", args)
        self.assertIn("-DLLVM_LINK_LLVM_DYLIB=OFF", args)
        self.assertIn("-DBUILD_SHARED_LIBS=OFF", args)
        self.assertIn("-DLLVM_ENABLE_PROJECTS=lld", args)
        self.assertIn("-DLLVM_ENABLE_ZSTD=OFF", args)

    def test_checked_cache_is_copied_and_wrong_host_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "cache"
            cache.mkdir()
            (root / "llvm/bin").mkdir(parents=True)
            binary = cache / "ld64.lld"
            binary.write_bytes(b"test linker")
            metadata = dict(version="18.1.8", arch="x86_64", deployment="11.0",
                            binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
            (cache / "build.json").write_text(json.dumps(metadata))
            with patch.object(macos_lld.platform, "machine", return_value="arm64"):
                with self.assertRaisesRegex(ValueError, "host architecture"):
                    macos_lld.install(root / "llvm", cache, "18.1.8")
            with patch.object(macos_lld.platform, "machine", return_value="x86_64"):
                with patch.object(macos_lld, "check_binary") as check:
                    macos_lld.install(root / "llvm", cache, "18.1.8")
                    check.assert_called_once()
            self.assertEqual((root / "llvm/bin/ld64.lld").read_bytes(), b"test linker")

    def test_modified_cached_binary_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            (cache / "ld64.lld").write_bytes(b"modified")
            (cache / "build.json").write_text(json.dumps(dict(version="18.1.8", arch="x86_64",
                deployment="11.0", binary_sha256="0" * 64)))
            with patch.object(macos_lld.platform, "machine", return_value="x86_64"):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    macos_lld.install(cache, cache, "18.1.8")

    def test_cache_save_follows_actual_link_probe(self):
        workflow = (SCRIPTS.parent / "workflows/release.yml").read_text(encoding="utf-8")
        build = workflow.index('[macOS] Build the missing LLD')
        install = workflow.index('[macOS] Add matching LLD')
        probe = workflow.index('[macOS] Configure and check')
        save = workflow.index('[macOS] Cache the checked LLD')
        self.assertLess(build, install)
        self.assertLess(install, probe)
        self.assertLess(probe, save)
        self.assertIn("hashFiles('.github/scripts/macos-lld.py')", workflow)


class BootstrapWorkspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="white-bootstrap-space-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.shared = self.root / "release_pkg/tools"
        self.shared.mkdir(parents=True)
        (self.shared / "sentinel").write_text("shared toolchain")

    def generation(self, name):
        directory = self.root / name
        (directory / "bin").mkdir(parents=True)
        (directory / "bin/wlc").write_text("test compiler")
        return directory

    def test_generations_share_tools_but_keep_their_own_std(self):
        first = self.generation("build_1234567")
        second = self.generation("build_2345678")
        for directory in (first, second):
            workspace.attach(self.root, directory)
            (directory / "std").mkdir()
            (directory / "std/type.wl").write_text(directory.name)
        workspace.attach(self.root, second)
        self.assertEqual((first / "tools").resolve(), self.shared.resolve())
        self.assertEqual((second / "tools").resolve(), self.shared.resolve())
        self.assertNotEqual((first / "std/type.wl").read_text(), (second / "std/type.wl").read_text())

    def test_retiring_a_generation_keeps_active_tools_and_snapshot(self):
        first = self.generation("build_1234567")
        second = self.generation("build_2345678")
        workspace.attach(self.root, first)
        workspace.attach(self.root, second)
        snapshots = self.root / "Output_Snapshots"
        snapshots.mkdir()
        (snapshots / "first.tar.gz").write_bytes(b"completed snapshot")
        workspace.retire(self.root, first, second)
        self.assertFalse(first.exists())
        self.assertEqual((second / "tools/sentinel").read_text(), "shared toolchain")
        self.assertTrue((snapshots / "first.tar.gz").exists())

    def test_seed_tools_are_replaced_without_changing_shared_files(self):
        seed = self.generation("boot_env")
        (seed / "tools").mkdir()
        (seed / "tools/old-tool").write_text("old")
        workspace.attach(self.root, seed)
        self.assertFalse((seed / "tools/old-tool").exists())
        self.assertEqual((seed / "tools/sentinel").read_text(), "shared toolchain")

    def test_retirement_requires_a_replacement_compiler(self):
        first = self.generation("build_1234567")
        empty = self.root / "build_2345678"
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, "no compiler"):
            workspace.retire(self.root, first, empty)
        self.assertTrue(first.exists())

    def test_refuses_active_and_protected_directories(self):
        active = self.generation("build_1234567")
        for old in (active, self.root, self.root / "release_pkg", self.root / "source_repo", self.root.parent / "build_1234567"):
            with self.subTest(old=old), self.assertRaises(ValueError):
                workspace.retire(self.root, old, active)
        self.assertTrue((self.shared / "sentinel").exists())

    def test_refuses_an_unrelated_tools_link(self):
        first = self.generation("build_1234567")
        second = self.generation("build_2345678")
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        (unrelated / "sentinel").write_text("keep")
        if os.name == "nt":
            subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(first / "tools"), str(unrelated)], check=True)
        else:
            (first / "tools").symlink_to(unrelated, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "another directory"):
            workspace.attach(self.root, first)
        with self.assertRaisesRegex(ValueError, "unexpected tools link"):
            workspace.retire(self.root, first, second)
        self.assertEqual((unrelated / "sentinel").read_text(), "keep")

    def test_many_hops_keep_only_the_active_generation(self):
        old = self.generation("boot_env")
        for number in range(8):
            active = self.generation(f"build_{number:07x}")
            workspace.attach(self.root, active)
            workspace.retire(self.root, old, active)
            self.assertEqual(len(list(self.root.glob("build_*"))), 1)
            self.assertEqual((active / "tools/sentinel").read_text(), "shared toolchain")
            old = active

    def test_low_space_stops_before_the_runner_loses_its_logs(self):
        with patch.object(workspace.shutil, "disk_usage", return_value=SimpleNamespace(free=1024, total=2**30)):
            with self.assertRaisesRegex(ValueError, "Less than 2048 MiB"):
                workspace.report(self.root, 2048)

    def test_both_bootstrap_loops_share_and_retire(self):
        workflow = (SCRIPTS.parent / "workflows/release.yml").read_text(encoding="utf-8")
        self.assertNotIn("cp -r ../release_pkg/tools ../build_", workflow)
        self.assertEqual(workflow.count('bootstrap-workspace.py" retire'), 2)
        self.assertEqual(workflow.count('bootstrap-workspace.py" attach'), 2)
        self.assertEqual(workflow.count('bootstrap-workspace.py" report --root "$GITHUB_WORKSPACE" --minimum-free-mib 2048'), 2)


class Diagnostics(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="white-ci-diagnostic-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.validation = machine.Validation(SimpleNamespace(source=root, wlc=root / "wlc",
            artifacts=root / "logs", std_root=root, clang=root / "clang", levels=["-O0"]))
        self.source = root / "test.wl"
        self.source.write_text('// Expected Error: "RuntimeError: bounds"\n', encoding="utf-8")
        (self.validation.out / ("machine-diagnostic" + self.validation.suffix)).touch()

    def diagnostic(self, outputs):
        def command(args, *unused):
            status, text = outputs.pop(0)
            if status == 0:
                Path(args[-1]).touch()
            return status, text
        with patch.object(self.validation, "command", side_effect=command):
            self.validation.diagnostic(self.source, "-O0")

    def test_runtime_error_after_successful_compile_is_expected(self):
        self.diagnostic([(0, "Build success"), (1, "RuntimeError: bounds")])

    def test_compile_error_can_match_the_expectation(self):
        self.diagnostic([(1, "RuntimeError: bounds")])

    def test_metadata_accepts_trailing_whitespace(self):
        self.source.write_text('// Expected Error: "RuntimeError: bounds" \t\n', encoding="utf-8")
        self.diagnostic([(1, "RuntimeError: bounds")])

    def test_timeout_with_expected_text_still_fails(self):
        with self.assertRaises(AssertionError):
            self.diagnostic([(124, "RuntimeError: bounds")])

    def test_unexpected_success_fails(self):
        with self.assertRaises(AssertionError):
            self.diagnostic([(0, "Build success"), (0, "RuntimeError: bounds")])

    def test_missing_executable_is_not_a_successful_compile(self):
        with patch.object(self.validation, "command", return_value=(0, "Build success")):
            with self.assertRaisesRegex(AssertionError, "without an executable"):
                self.validation.diagnostic(self.source, "-O0")


class CompilerCheckpoint(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="white-checkpoint-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.package = self.root / "package"
        self.cache = self.root / "cache"
        (self.package / "bin").mkdir(parents=True)
        (self.package / "std/builtin").mkdir(parents=True)
        (self.package / "tools").mkdir()
        (self.package / "bin/wlc").write_bytes(b"compiler")
        (self.package / "std/builtin/_pkg.wl").write_bytes(b"library")
        (self.package / "tools/keep").write_bytes(b"checked toolchain")
        self.identity = checkpoint.identity("1" * 40, "linux-x64", "v0.3.8-alpha1")

    def save(self):
        checkpoint.save(self.package, self.cache, self.identity)

    def test_round_trip_excludes_the_toolchain(self):
        self.save()
        restored = self.root / "restored"
        checkpoint.restore(restored, self.cache, self.identity)
        self.assertEqual((restored / "bin/wlc").read_bytes(), b"compiler")
        self.assertFalse((restored / "tools").exists())
        self.assertFalse((self.cache / "tools").exists())

    def test_different_source_platform_or_version_cannot_be_reused(self):
        self.save()
        for key in ("commit", "platform", "version"):
            with self.assertRaisesRegex(ValueError, "does not match"):
                checkpoint.restore(self.package, self.cache, dict(self.identity, **{key: "different"}))

    def test_changed_binary_is_rejected_before_copying(self):
        self.save()
        (self.cache / "bin/wlc").write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            checkpoint.restore(self.root / "restored", self.cache, self.identity)
        self.assertFalse((self.root / "restored").exists())

    def test_missing_or_extra_library_is_rejected(self):
        self.save()
        (self.cache / "std/extra.wl").touch()
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            checkpoint.restore(self.package, self.cache, self.identity)

    def test_path_traversal_is_rejected(self):
        self.save()
        manifest = self.cache / "build.json"
        metadata = json.loads(manifest.read_text())
        metadata["files"]["bin/../../outside"] = "0" * 64
        manifest.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "Invalid checkpoint path"):
            checkpoint.restore(self.package, self.cache, self.identity)

    def test_existing_checkpoint_is_not_overwritten(self):
        self.save()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.save()

    def test_incomplete_package_is_not_saved(self):
        (self.package / "bin/wlc").unlink()
        with self.assertRaisesRegex(ValueError, "no compiler"):
            self.save()
        self.assertFalse(self.cache.exists())

    def test_checkpoint_is_saved_before_ffi_and_positive_tests(self):
        workflow = (SCRIPTS.parent / "workflows/release.yml").read_text(encoding="utf-8")
        saved = workflow.index('      - name: Cache the completed compiler')
        self.assertLess(saved, workflow.index('      - name: "[Windows] Prepare C FFI host"'))
        self.assertLess(saved, workflow.index('      - name: Run language, integration'))
        self.assertEqual(workflow.count("&& steps.restore-compiler.outputs.cache-hit != 'true'"), 2)
        self.assertIn("      - name: Store bootstrap snapshots\n        if: always()", workflow)
        self.assertLess(workflow.index('      - name: "[Windows x32] Configure the target linker"'),
                        workflow.index('      - name: Restore completed compiler bootstrap'))


if __name__ == "__main__":
    unittest.main()
