"""Select a published ancestor seed and a forward-only source bootstrap path."""
import argparse
import fnmatch
import json
from pathlib import Path
import re
import subprocess
import sys

VERSION = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
ASSETS = {
    "windows-x64": "WhiteLanguage-Windows-x64-Portable-*.zip",
    "windows-x32": "WhiteLanguage-Windows-x32-Portable-*.zip",
    **{name: f"whitelang-{name}-*.tar.gz" for name in (
        "linux-x64", "linux-x86", "linux-arm64", "linux-armv7", "macos-x64", "macos-arm64")},
}


def git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", check=check)


def commit(repo, ref):
    return git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").stdout.strip()


def ancestor(repo, older, newer):
    result = git(repo, "merge-base", "--is-ancestor", older, newer, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip())
    return result.returncode == 0


def ordered_commits(repo, base, target):
    base, target = commit(repo, base), commit(repo, target)
    if not ancestor(repo, base, target):
        raise ValueError("The bootstrap seed is not an ancestor of the requested source.")
    result = []
    previous = base
    for oid in git(repo, "rev-list", "--topo-order", "--reverse", f"{base}..{target}").stdout.split():
        parents = git(repo, "show", "-s", "--format=%P", oid).stdout.split()
        changed = git(repo, "diff", "--name-only", parents[0], oid, "--",
                      "src", "std", "runtime", ".bootstrap-stage1-only").stdout.strip()
        # A README-only side branch is not a compiler generation. The final
        # commit is still built so version stamping binds to the release SHA.
        if not changed and oid != target:
            continue
        if not ancestor(repo, previous, oid):
            raise ValueError(f"Source-changing branches diverge at {oid[:12]}. "
                             "Provide a linear bootstrap bridge instead of checking out older source mid-build.")
        result.append(oid)
        previous = oid
    return result


def json_stream(text):
    decoder = json.JSONDecoder()
    while text.strip():
        value, offset = decoder.raw_decode(text.lstrip())
        text = text.lstrip()[offset:]
        yield from value if isinstance(value, list) else [value]


def choose_seed(repo, target, releases, platforms):
    target = commit(repo, target)
    candidates = []
    for release in releases:
        tag = release.get("tag_name", "")
        if not VERSION.fullmatch(tag) or release.get("draft") or release.get("prerelease"):
            continue
        # Do not trust the prerelease bit alone, old releases may have been
        # published with the wrong flag by an earlier workflow.
        if "-" in tag.split("+", 1)[0]:
            continue
        names = [asset["name"] if isinstance(asset, dict) else asset for asset in release.get("assets", [])]
        if not all(any(fnmatch.fnmatchcase(name, ASSETS[platform].replace("*", tag[1:])) for name in names)
                   for platform in platforms):
            continue
        try:
            source = commit(repo, tag)
        except subprocess.CalledProcessError:
            continue
        if source == target or not ancestor(repo, source, target):
            continue
        distance = int(git(repo, "rev-list", "--count", f"{source}..{target}").stdout)
        candidates.append((distance, tag, source))
    if not candidates:
        raise ValueError("No published stable ancestor has all requested platform assets. "
                         "Check the source tags and release assets, no unrelated seed will be used.")
    _, tag, source = min(candidates)
    return tag, source, target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    select = sub.add_parser("select")
    select.add_argument("--source", type=Path, required=True)
    select.add_argument("--target", required=True)
    select.add_argument("--releases", type=Path, required=True)
    select.add_argument("--platforms", required=True)
    select.add_argument("--github-output", type=Path, required=True)
    select.add_argument("--summary", type=Path)
    order = sub.add_parser("order")
    order.add_argument("--source", type=Path, required=True)
    order.add_argument("--base", required=True)
    order.add_argument("--target", required=True)
    args = parser.parse_args()
    if args.operation == "order":
        # Git Bash retains CR in word splitting, emit the revision protocol as
        # bytes so Windows text-mode stdout cannot turn LF into CRLF.
        commits = ordered_commits(args.source, args.base, args.target)
        sys.stdout.buffer.write("".join(oid + "\n" for oid in commits).encode("ascii"))
        return
    platforms = args.platforms.split(",")
    if not platforms or any(platform not in ASSETS for platform in platforms):
        raise ValueError("Unknown bootstrap platform list.")
    tag, source, target = choose_seed(args.source, args.target,
                                      json_stream(args.releases.read_text(encoding="utf-8")), platforms)
    commits = ordered_commits(args.source, source, target)
    with args.github_output.open("a", encoding="utf-8") as stream:
        stream.write(f"bootstrap_tag={tag}\nsource_base_commit={source}\ntarget_commit={target}\n")
    summary = ["### Bootstrap plan", "", f"- Seed release: `{tag}`", f"- Seed source: `{source}`",
               f"- Target source: `{target}`", f"- Compiler generations to visit: {len(commits)}", "",
               "```text", *commits, "```", ""]
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(summary))
    print(f"Selected {tag}, {len(commits)} forward source steps.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
