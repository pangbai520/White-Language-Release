"""Resume a repair from a checked source ancestor, without replaying the old release chain."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import zipfile


SCRIPTS = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = load('bootstrap-plan')
checkpoint = load('compiler-checkpoint')


def select_snapshot(source, target, tag, platform, releases):
    target = plan.commit(source, target)
    pattern = re.compile(re.escape(platform + '-' + tag[1:] + '-snapshot-') + r'([0-9a-f]{7,40})\.(zip|tar\.gz)$')
    candidates = []
    for release in releases:
        if release.get('tag_name') != tag + '-snapshots' or release.get('draft'):
            continue
        for asset in release.get('assets', []):
            match = pattern.fullmatch(asset.get('name', ''))
            if not match:
                continue
            try:
                base = plan.commit(source, match[1])
            except subprocess.CalledProcessError:
                continue
            # Rebuild the final commit even when a previous attempt archived it,
            # the normal exact-commit checkpoint handles that separate fast path.
            if base == target or not plan.ancestor(source, base, target):
                continue
            distance = int(plan.git(source, 'rev-list', '--count', base + '..' + target).stdout)
            candidates.append((distance, base, asset))
    if not candidates:
        return None
    _, base, asset = min(candidates, key=lambda item: (item[0], item[1]))
    commits = plan.ordered_commits(source, base, target)
    return dict(commit=base, target=target, asset=asset, commits=commits)


def member_path(name):
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        raise ValueError('Unsafe snapshot path: ' + name)
    if path.parts[0] not in checkpoint.PARTS + ('build.json',):
        raise ValueError('Unexpected snapshot member: ' + name)
    return path


def extract(archive, destination):
    total = 0
    seen = set()

    def copy(name, size, stream):
        nonlocal total
        path = member_path(name)
        key = path.as_posix().casefold()
        if key in seen:
            raise ValueError('Duplicate snapshot member: ' + name)
        seen.add(key)
        total += size
        if total > 256 * 1024 * 1024:
            raise ValueError('Snapshot exceeds the compiler-only size limit')
        output = destination.joinpath(*path.parts)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('wb') as writer:
            shutil.copyfileobj(stream, writer)

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            for item in package.infolist():
                member_path(item.filename)
                if item.external_attr >> 16 & 0o170000 == 0o120000:
                    raise ValueError('Snapshot contains a symbolic link')
                if not item.is_dir():
                    with package.open(item) as stream:
                        copy(item.filename, item.file_size, stream)
    else:
        with tarfile.open(archive, 'r:gz') as package:
            for item in package:
                member_path(item.name)
                if item.isdir():
                    continue
                if not item.isfile():
                    raise ValueError('Snapshot contains a link or special file')
                with package.extractfile(item) as stream:
                    copy(item.name, item.size, stream)


def check_binary(package, platform):
    binary = package / 'bin' / ('wlc.exe' if platform.startswith('windows-') else 'wlc')
    with binary.open('rb') as stream:
        header = stream.read(64)
        if platform.startswith('windows-'):
            if len(header) < 64 or header[:2] != b'MZ':
                raise ValueError('Snapshot compiler is not a PE image')
            stream.seek(struct.unpack_from('<I', header, 60)[0])
            signature = stream.read(6)
            expected = 0x14c if platform == 'windows-x32' else 0x8664
            valid = signature[:4] == b'PE\0\0' and len(signature) == 6 and struct.unpack_from('<H', signature, 4)[0] == expected
        elif platform.startswith('linux-'):
            machines = {'linux-x86': (1, 3), 'linux-x64': (2, 62), 'linux-armv7': (1, 40), 'linux-arm64': (2, 183)}
            bits, machine = machines[platform]
            valid = len(header) >= 20 and header[:4] == b'\x7fELF' and header[4:6] == bytes((bits, 1)) and struct.unpack_from('<H', header, 18)[0] == machine
        else:
            cpu = 0x1000007 if platform == 'macos-x64' else 0x100000c
            valid = len(header) >= 8 and header[:4] == b'\xcf\xfa\xed\xfe' and struct.unpack_from('<I', header, 4)[0] == cpu
        if not valid:
            raise ValueError('Snapshot compiler architecture does not match ' + platform)
    if not platform.startswith('windows-'):
        binary.chmod(0o755)


def check_snapshot(package, source, expected):
    manifest = package / 'build.json'
    if manifest.exists():
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise ValueError('Snapshot manifest does not match its source, platform or tag')
        if checkpoint.files(package) != metadata.get('files'):
            raise ValueError('Snapshot files do not match their recorded SHA-256 values')
    else:
        # Older snapshots have no manifest. Resolve their short SHA uniquely and
        # compare the bundled library with that Git tree before running anything.
        names = plan.git(source, 'ls-tree', '-r', '--name-only', expected['commit'], '--', 'std').stdout.splitlines()
        actual = {path.relative_to(package).as_posix() for path in (package / 'std').rglob('*') if path.is_file()}
        if set(names) != actual:
            raise ValueError('Legacy snapshot standard library does not match the source tree')
        for name in names:
            original = subprocess.run(['git', 'show', expected['commit'] + ':' + name], cwd=source, capture_output=True, check=True).stdout
            bundled = (package / name).read_bytes()
            if bundled.replace(b'\r\n', b'\n') != original.replace(b'\r\n', b'\n'):
                raise ValueError('Legacy snapshot library differs from its source: ' + name)
    check_binary(package, expected['platform'])


def write_manifest(package, expected):
    metadata = dict(expected, files=checkpoint.files(package))
    manifest = package / 'build.json'
    manifest.write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    return metadata


def draft_source(repository, releases, tag, platform, source, target):
    name = 'build-' + platform + '-' + tag[1:] + '.json'
    for release in releases:
        if not release.get('draft') or (release.get('tag_name') != tag and release.get('name') != 'White Language ' + tag[1:]):
            continue
        for asset in release.get('assets', []):
            if asset.get('name') != name:
                continue
            if not isinstance(asset.get('id'), int) or asset.get('size', 0) > 2 * 1024 * 1024:
                raise ValueError('Invalid draft source manifest asset')
            expected = asset.get('digest') or ''
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', expected):
                raise ValueError('Draft source manifest has no GitHub SHA-256 digest')
            response = subprocess.run(['gh', 'api', 'repos/' + repository + '/releases/assets/' + str(asset['id']), '-H', 'Accept: application/octet-stream'], capture_output=True, check=True)
            if hashlib.sha256(response.stdout).hexdigest() != expected.split(':', 1)[1]:
                raise ValueError('Draft source manifest digest does not match')
            metadata = json.loads(response.stdout)
            identity = checkpoint.identity(metadata.get('commit', ''), platform, tag)
            if any(metadata.get(key) != value for key, value in identity.items()):
                raise ValueError('Draft source manifest belongs to another platform or tag')
            if plan.ancestor(source, identity['commit'], target):
                return identity['commit']
            print('::notice::Draft source is outside the target ancestry, it will not be used as a seed.')
    return None


def snapshot(package, archive, expected):
    metadata = write_manifest(package, expected)
    manifest = package / 'build.json'
    archive.parent.mkdir(parents=True, exist_ok=True)
    partial = archive.with_name(archive.name + '.part')
    paths = [manifest] + [package / name for name in metadata['files']]
    if archive.suffix == '.zip':
        with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED) as output:
            for path in paths:
                output.write(path, path.relative_to(package).as_posix())
    else:
        with tarfile.open(partial, 'w:gz') as output:
            for path in paths:
                output.add(path, arcname=path.relative_to(package).as_posix(), recursive=False)
    partial.replace(archive)


def prepare(args):
    if args.releases:
        releases = list(plan.json_stream(args.releases.read_text(encoding='utf-8')))
    else:
        response = subprocess.run(['gh', 'api', '--paginate', 'repos/' + args.repository + '/releases?per_page=100'], capture_output=True, text=True, encoding='utf-8', check=True)
        releases = list(plan.json_stream(response.stdout))
    target = plan.commit(args.source, args.target)
    origin = draft_source(args.repository, releases, args.tag, args.platform, args.source, target)
    if origin:
        print('Draft asset source: ' + origin)
    elif any(release.get('draft') and (release.get('tag_name') == args.tag or release.get('name') == 'White Language ' + args.tag[1:]) for release in releases):
        print('Draft has no source manifest, resolving snapshot ancestry from Git instead.')
    selected = select_snapshot(args.source, target, args.tag, args.platform, releases)
    if selected is None:
        print('No matching ancestor snapshot, using the published bootstrap plan.')
        return
    asset = selected['asset']
    if not isinstance(asset.get('id'), int) or not re.fullmatch(r'sha256:[0-9a-f]{64}', asset.get('digest') or ''):
        raise ValueError('Snapshot asset has no GitHub SHA-256 digest or asset ID')
    package = args.package.resolve()
    if package.name != 'boot_env' or package.is_symlink() or (os.name == 'nt' and package.is_junction()):
        raise ValueError('Resume destination must be the isolated boot_env directory')
    package.parent.mkdir(parents=True, exist_ok=True)
    expected = checkpoint.identity(selected['commit'], args.platform, args.tag)
    with tempfile.TemporaryDirectory(prefix='.resume-', dir=package.parent) as temp:
        directory = Path(temp)
        archive = directory / 'snapshot'
        with archive.open('wb') as output:
            subprocess.run(['gh', 'api', 'repos/' + args.repository + '/releases/assets/' + str(asset['id']), '-H', 'Accept: application/octet-stream'], stdout=output, check=True)
        if checkpoint.digest(archive) != asset['digest'].split(':', 1)[1]:
            raise ValueError('Downloaded snapshot does not match the GitHub asset digest')
        extracted = directory / 'files'
        extracted.mkdir()
        extract(archive, extracted)
        check_snapshot(extracted, args.source, expected)
        package.mkdir(exist_ok=True)
        for part in checkpoint.PARTS:
            destination = package / part
            if checkpoint.linked(destination):
                raise ValueError('Resume directory must not be a link: ' + str(destination))
            if destination.exists():
                shutil.rmtree(destination)
            if (extracted / part).exists():
                shutil.move(str(extracted / part), destination)
    with args.github_env.open('a', encoding='utf-8') as output:
        output.write('SOURCE_BASE_COMMIT=' + selected['commit'] + '\nREPAIR_SNAPSHOT=true\n')
    message = ['### Repair bootstrap: ' + args.platform, '', '- Snapshot: `' + asset['name'] + '`',
               '- Seed source: `' + selected['commit'] + '`', '- Target source: `' + selected['target'] + '`',
               '- Remaining source steps: ' + str(len(selected['commits'])), '', '```text', *selected['commits'], '```', '']
    if origin:
        message.insert(3, '- Draft asset source: `' + origin + '`')
    if args.summary:
        with args.summary.open('a', encoding='utf-8') as output:
            output.write('\n'.join(message))
    print('Repair seed: ' + selected['commit'] + ', ' + str(len(selected['commits'])) + ' forward source step(s).')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    repair = commands.add_parser('prepare')
    repair.add_argument('--source', type=Path, required=True)
    repair.add_argument('--target', required=True)
    repair.add_argument('--tag', required=True)
    repair.add_argument('--platform', choices=plan.ASSETS, required=True)
    repair.add_argument('--package', type=Path, required=True)
    repair.add_argument('--repository', required=True)
    repair.add_argument('--github-env', type=Path, required=True)
    repair.add_argument('--summary', type=Path)
    repair.add_argument('--releases', type=Path)
    save = commands.add_parser('snapshot')
    save.add_argument('--package', type=Path, required=True)
    save.add_argument('--archive', type=Path, required=True)
    save.add_argument('--commit', required=True)
    save.add_argument('--platform', choices=plan.ASSETS, required=True)
    save.add_argument('--tag', required=True)
    manifest = commands.add_parser('manifest')
    manifest.add_argument('--package', type=Path, required=True)
    manifest.add_argument('--commit', required=True)
    manifest.add_argument('--platform', choices=plan.ASSETS, required=True)
    manifest.add_argument('--tag', required=True)
    args = parser.parse_args()
    if not plan.VERSION.fullmatch(args.tag):
        parser.error('Invalid release tag')
    if args.command == 'prepare':
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository):
            parser.error('Invalid release repository')
        prepare(args)
    elif args.command == 'snapshot':
        snapshot(args.package, args.archive, checkpoint.identity(args.commit, args.platform, args.tag))
    else:
        write_manifest(args.package, checkpoint.identity(args.commit, args.platform, args.tag))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise SystemExit('::error::' + str(error))
