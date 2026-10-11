import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from scripts.release_archive import build_archive, FILES


class ReleaseArchiveTests(unittest.TestCase):
    def test_extracted_release_can_run_permissions_test_without_checkout(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            temporary=Path(temporary); archive=temporary/'release.tar.gz'
            build_archive(archive,root=root)
            target=temporary/'extracted'; target.mkdir()
            with tarfile.open(archive) as tar:
                self.assertTrue(set(FILES).issubset(tar.getnames()))
                tar.extractall(target,filter='data')
            # This is the exact test that failed on EC2 when bootstrap/ was
            # omitted. Run from the extracted artifact, not the repository.
            result=subprocess.run([sys.executable,'-m','unittest',
                'test_backup_operations.BackupOperationsTests.test_bootstrap_allows_secret_kms_validation_with_scoped_data_key'],
                cwd=target/'tests',env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(target)},
                capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_missing_required_fixture_fails_before_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name in FILES[:-1]: (root/name).write_text('fixture')
            with self.assertRaisesRegex(ValueError,'Required release file'):
                build_archive(root/'archive.tar.gz',root=root)

    def test_symlink_source_is_not_packaged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); (root/'orion').mkdir()
            (root/'outside').write_text('private')
            (root/'orion/linked').symlink_to(root/'outside')
            with self.assertRaisesRegex(ValueError,'symlinks'):
                build_archive(root/'archive.tar.gz',root=root)
