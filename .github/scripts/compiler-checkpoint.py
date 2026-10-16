"""Keep a completed bootstrap when validation needs another run."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil


PARTS = ('bin', 'std', 'runtime')


def linked(path):
    return path.is_symlink() or (os.name == 'nt' and path.is_junction())


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def files(root):
    result = {}
    for part in PARTS:
        directory = root / part
        if linked(directory):
            raise ValueError('Checkpoint directory must not be a symbolic link: ' + str(directory))
        if directory.exists():
            for path in sorted(directory.rglob('*')):
                if linked(path):
                    raise ValueError('Checkpoint contains a symbolic link: ' + str(path))
                if path.is_file():
                    result[path.relative_to(root).as_posix()] = digest(path)
    if not any(name in result for name in ('bin/wlc', 'bin/wlc.exe')):
        raise ValueError('Checkpoint has no compiler')
    if not any(name.startswith('std/') for name in result):
        raise ValueError('Checkpoint has no standard library')
    return result


def identity(commit, platform, version):
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Checkpoint requires a full source commit')
    return dict(schema=1, commit=commit, platform=platform, version=version)


def save(package, checkpoint, expected):
    if checkpoint.exists():
        raise ValueError('Refusing to overwrite a checkpoint: ' + str(checkpoint))
    manifest = dict(expected, files=files(package))
    checkpoint.mkdir(parents=True)
    for part in PARTS:
        if (package / part).exists():
            shutil.copytree(package / part, checkpoint / part)
    (checkpoint / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


def restore(package, checkpoint, expected):
    manifest = json.loads((checkpoint / 'build.json').read_text(encoding='utf-8'))
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError('Checkpoint source, platform or release version does not match this build')
    for name in manifest.get('files', {}):
        path = PurePosixPath(name)
        if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in name or path.parts[0] not in PARTS:
            raise ValueError('Invalid checkpoint path: ' + name)
    if files(checkpoint) != manifest.get('files'):
        raise ValueError('Checkpoint files do not match their recorded SHA-256 values')
    # Tools belong to this runner's checked toolchain, they never enter the
    # compiler cache. Only reuse the binary and library that passed bootstrap.
    for part in PARTS:
        destination = package / part
        if linked(destination):
            raise ValueError('Package directory must not be a symbolic link: ' + str(destination))
        if (checkpoint / part).exists():
            shutil.copytree(checkpoint / part, destination, dirs_exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['save', 'restore'])
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    try:
        expected = identity(args.commit, args.platform, args.version)
        action = save if args.command == 'save' else restore
        action(args.package, args.checkpoint, expected)
    except (OSError, ValueError) as error:
        parser.exit(1, '::error::' + str(error) + '\n')
    print('Compiler checkpoint ' + args.command + ' complete, source ' + args.commit)


if __name__ == '__main__':
    main()
