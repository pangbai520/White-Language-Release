"""Repair seeds must stay on the source ancestry and survive archive validation."""
import io
import hashlib
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from test_workflows import load


resume = load('bootstrap-resume')
plan = resume.plan


class RepairSeeds(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='white-repair-seed-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'CI test')
        self.git('config', 'user.email', 'ci@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'core.autocrlf', 'false')
        (self.source / 'std').mkdir()
        (self.source / 'std/prelude.wl').write_text('const value: Int = 1;\n')
        self.base = self.change('first')
        self.previous = self.change('second')
        self.target = self.change('final')
        self.platform = 'windows-x64'
        self.tag = 'v0.3.8-alpha1'

    def git(self, *args):
        return plan.git(self.source, *args).stdout.strip()

    def change(self, text):
        (self.source / 'wlc.wl').write_text(text)
        self.git('add', '.')
        self.git('commit', '-m', text)
        return self.git('rev-parse', 'HEAD')

    def asset(self, commit, platform=None):
        return dict(name=(platform or self.platform) + '-0.3.8-alpha1-snapshot-' + commit[:7] + '.zip', id=1)

    def releases(self, *assets):
        return [dict(tag_name=self.tag + '-snapshots', draft=False, assets=list(assets))]

    def choose(self, releases):
        return resume.select_snapshot(self.source, self.target, self.tag, self.platform, releases)

    def package(self):
        package = self.root / 'package'
        (package / 'bin').mkdir(parents=True)
        (package / 'std').mkdir()
        binary = bytearray(64)
        binary[:2] = b'MZ'
        struct.pack_into('<I', binary, 60, 64)
        binary += b'PE\0\0' + struct.pack('<H', 0x8664)
        (package / 'bin/wlc.exe').write_bytes(binary)
        (package / 'std/prelude.wl').write_text('const value: Int = 1;\n')
        return package

    def identity(self):
        return resume.checkpoint.identity(self.previous, self.platform, self.tag)

    def test_chooses_nearest_source_ancestor_for_each_platform(self):
        selected = self.choose(self.releases(self.asset(self.base), self.asset(self.previous), self.asset(self.target, 'windows-x32')))
        self.assertEqual(selected['commit'], self.previous)
        self.assertEqual(selected['commits'], [self.target])

    def test_rebuilds_final_source_instead_of_treating_a_snapshot_as_a_release(self):
        selected = self.choose(self.releases(self.asset(self.target), self.asset(self.previous)))
        self.assertEqual(selected['commit'], self.previous)

    def test_no_usable_snapshot_keeps_the_published_plan(self):
        self.assertIsNone(self.choose(self.releases(self.asset(self.previous, 'linux-x64'))))

    def test_rejects_snapshots_from_a_rewritten_side_branch(self):
        self.git('checkout', '-b', 'old-history', self.base)
        unrelated = self.change('rewritten branch')
        self.git('checkout', 'main')
        selected = self.choose(self.releases(self.asset(unrelated), self.asset(self.base)))
        self.assertEqual(selected['commit'], self.base)

    def test_release_dates_do_not_choose_the_seed(self):
        releases = self.releases(self.asset(self.previous)) + self.releases(self.asset(self.base))
        releases[-1]['published_at'] = '2099-01-01T00:00:00Z'
        self.assertEqual(self.choose(releases)['commit'], self.previous)

    def test_other_tags_and_draft_snapshot_releases_are_ignored(self):
        releases = self.releases(self.asset(self.previous))
        releases[0]['draft'] = True
        self.assertIsNone(self.choose(releases))
        releases[0]['draft'] = False
        releases[0]['tag_name'] = 'v0.3.8-alpha2-snapshots'
        self.assertIsNone(self.choose(releases))

    def test_draft_manifest_records_the_source_commit(self):
        metadata = self.identity()
        data = json.dumps(metadata).encode()
        asset = dict(name='build-windows-x64-0.3.8-alpha1.json', id=123,
                     digest='sha256:' + hashlib.sha256(data).hexdigest(), size=len(data))
        releases = [dict(draft=True, tag_name=self.tag, assets=[asset])]
        with patch.object(resume.subprocess, 'run', return_value=SimpleNamespace(stdout=data)):
            # Git commands also use subprocess.run, leave ancestry to Git's
            # separate regression tests while checking the downloaded manifest.
            with patch.object(plan, 'ancestor', return_value=True):
                self.assertEqual(resume.draft_source('owner/release', releases, self.tag,
                                                    self.platform, self.source, self.target), self.previous)
        asset['digest'] = 'sha256:' + '0' * 64
        with patch.object(resume.subprocess, 'run', return_value=SimpleNamespace(stdout=data)):
            with self.assertRaisesRegex(ValueError, 'digest does not match'):
                resume.draft_source('owner/release', releases, self.tag,
                                    self.platform, self.source, self.target)

    def test_old_draft_does_not_guess_source_from_release_repository_main(self):
        releases = [dict(draft=True, tag_name=self.tag, target_commitish='main', assets=[])]
        self.assertIsNone(resume.draft_source('owner/release', releases, self.tag,
                                            self.platform, self.source, self.target))

    def test_workflow_keeps_seeds_before_tests_and_defaults_to_all(self):
        workflow = (Path(__file__).resolve().parents[1] / 'workflows/release.yml').read_text(encoding='utf-8')
        self.assertIn('        default: all', workflow)
        self.assertIn('inputs.operation == \'repair\' && steps.restore-compiler.outputs.cache-hit != \'true\'', workflow)
        self.assertLess(workflow.index('Resume repair from an ancestor snapshot'),
                        workflow.index('[Windows] Bootstrap Compiler'))
        self.assertLess(workflow.index('Keep the final compiler as a repair seed'),
                        workflow.index('Run language, integration'))
        self.assertLess(workflow.index('snapshot --package "$ARM_ENV"'),
                        workflow.index('          ERRORS=0', workflow.index('snapshot --package "$ARM_ENV"')))
        self.assertIn('build-${{ matrix.os_name }}-${VERSION_NO_V}.json', workflow)

    def test_snapshot_round_trip_verifies_metadata_and_binary(self):
        package = self.package()
        for suffix in ('.zip', '.tar.gz'):
            archive = self.root / ('seed' + suffix)
            resume.snapshot(package, archive, self.identity())
            extracted = self.root / ('extracted' + suffix)
            extracted.mkdir()
            resume.extract(archive, extracted)
            resume.check_snapshot(extracted, self.source, self.identity())
            self.assertEqual((extracted / 'bin/wlc.exe').read_bytes(), (package / 'bin/wlc.exe').read_bytes())
            self.assertFalse(archive.with_name(archive.name + '.part').exists())

    def test_legacy_snapshot_library_is_checked_against_git(self):
        package = self.package()
        resume.check_snapshot(package, self.source, self.identity())
        (package / 'std/prelude.wl').write_text('const value: Int = 2;\n')
        with self.assertRaisesRegex(ValueError, 'differs from its source'):
            resume.check_snapshot(package, self.source, self.identity())

    def test_legacy_snapshot_missing_library_is_rejected(self):
        package = self.package()
        (package / 'std/prelude.wl').unlink()
        with self.assertRaisesRegex(ValueError, 'does not match the source tree'):
            resume.check_snapshot(package, self.source, self.identity())

    def test_a_modified_manifest_binary_cannot_be_restored(self):
        package = self.package()
        resume.write_manifest(package, self.identity())
        (package / 'bin/wlc.exe').write_bytes(b'modified')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            resume.check_snapshot(package, self.source, self.identity())

    def test_platform_and_source_in_manifest_must_match(self):
        package = self.package()
        resume.write_manifest(package, self.identity())
        for key, value in [('commit', self.base), ('platform', 'windows-x32'), ('version', 'v0.3.8-alpha2')]:
            with self.assertRaisesRegex(ValueError, 'manifest does not match'):
                resume.check_snapshot(package, self.source, dict(self.identity(), **{key: value}))

    def test_machine_type_is_checked_even_for_a_legacy_snapshot(self):
        package = self.package()
        with self.assertRaisesRegex(ValueError, 'architecture'):
            resume.check_snapshot(package, self.source, dict(self.identity(), platform='windows-x32'))

    def test_zip_cannot_escape_or_hide_a_symlink(self):
        for name in ('../escape', '/absolute', 'std/../../escape', 'std/file:stream', 'std\\..\\escape'):
            archive = self.root / 'unsafe.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr(name, b'data')
            with self.assertRaises(ValueError):
                resume.extract(archive, self.root / 'extracted')
        with zipfile.ZipFile(self.root / 'link.zip', 'w') as output:
            item = zipfile.ZipInfo('std/link')
            item.external_attr = 0o120777 << 16
            output.writestr(item, b'/outside')
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            resume.extract(self.root / 'link.zip', self.root / 'extracted')

    def test_tar_links_and_case_collisions_are_rejected(self):
        archive = self.root / 'link.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            item = tarfile.TarInfo('std/link')
            item.type = tarfile.SYMTYPE
            item.linkname = '/outside'
            output.addfile(item)
        with self.assertRaisesRegex(ValueError, 'link or special'):
            resume.extract(archive, self.root / 'extracted')
        archive = self.root / 'duplicate.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('std/a.wl', b'a')
            output.writestr('std/A.wl', b'b')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            resume.extract(archive, self.root / 'extracted')


if __name__ == '__main__':
    unittest.main()
