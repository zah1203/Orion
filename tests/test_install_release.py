from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock

from scripts.install_release import prepare_release


class StagedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root/'source'
        self.source.mkdir()
        (self.source/'app.py').write_text('safe source')
        (self.source/'requirements.txt').write_text('dependencies')
        self.archive = self.root/'release.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as tar:
            for path in self.source.iterdir():
                tar.add(path, arcname=path.name)
        self.release = self.root/'release'
        self.current = self.root/'current'
        self.override = self.root/'override'
        self.run = Mock()

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        prepare_release(self.release, self.archive, run=self.run,
                        current=self.current, override=self.override)

    def staged(self):
        self.prepare()
        (self.release/'.venv/bin').mkdir(parents=True)
        (self.release/'.venv/bin/python').write_text('test fixture')
        (self.release/'.venv/pyvenv.cfg').write_text('test fixture')
        self.run.reset_mock()

    def test_new_release_installs_then_tests(self):
        self.prepare()
        self.assertEqual(self.run.call_count, 4)
        self.assertEqual(self.run.call_args.args[0][1:], ['-m','unittest','discover','-s','tests'])

    def test_matching_stage_retests_without_reinstallation_or_overwrite(self):
        self.staged()
        before = (self.release/'app.py').stat().st_mtime_ns
        self.prepare()
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual((self.release/'app.py').stat().st_mtime_ns, before)
        self.assertNotIn('pip', str(self.run.call_args))

    def test_changed_missing_extra_and_linked_content_refused(self):
        self.staged()
        target = self.release/'app.py'
        target.write_text('changed')
        with self.assertRaises(SystemExit): self.prepare()
        target.unlink()
        with self.assertRaises(SystemExit): self.prepare()
        target.symlink_to(self.source/'app.py')
        with self.assertRaises(SystemExit): self.prepare()
        target.unlink(); target.write_text('safe source')
        extra = self.release/'unexpected.py'; extra.write_text('unexpected')
        with self.assertRaises(SystemExit): self.prepare()
        self.run.assert_not_called()

    def test_incomplete_environment_is_not_repaired_silently(self):
        self.staged()
        (self.release/'.venv/pyvenv.cfg').unlink()
        with self.assertRaises(SystemExit): self.prepare()
        self.run.assert_not_called()

    def test_active_worker_or_portal_release_refused(self):
        self.staged()
        self.current.symlink_to(self.release)
        with self.assertRaises(SystemExit): self.prepare()
        self.current.unlink()
        self.override.write_text('WorkingDirectory='+str(self.release))
        with self.assertRaises(SystemExit): self.prepare()
        self.run.assert_not_called()

    def test_failed_acceptance_propagates_before_activation(self):
        self.staged()
        self.run.side_effect = subprocess.CalledProcessError(1, 'synthetic tests')
        with self.assertRaises(subprocess.CalledProcessError): self.prepare()

    def test_linked_release_and_unsafe_archive_refused(self):
        self.release.symlink_to(self.source)
        with self.assertRaises(SystemExit): self.prepare()
        self.release.unlink()
        with tarfile.open(self.archive, 'w:gz') as tar:
            tar.add(self.source/'app.py', arcname='../escape.py')
        with self.assertRaises(SystemExit): self.prepare()
        self.assertFalse(self.release.exists())
        self.run.assert_not_called()
