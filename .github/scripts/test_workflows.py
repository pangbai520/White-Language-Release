"""Local regressions for release planning, deadlines and diagnostic handling."""
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
