# поток: mkt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops import ozon_autoadd_flag_manifest as manifest


class ManifestTests(unittest.TestCase):
    def test_aborts_on_unknown_flag_without_writing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(manifest, 'scan_account', return_value={
                'account': 'oz_acc1', 'total': 1, 'enabled': 0,
                'disabled': 0, 'unknown': 1, 'pages': 1,
            }):
                with self.assertRaisesRegex(RuntimeError, 'unknown flags'):
                    manifest.build_manifest(('oz_acc1',))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_saved_file_is_private_and_hash_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = manifest.save_manifest({'at': '2026-10-29T12:00:00+00:00',
                                                   'rows': [], 'targets': []}, directory)
            import hashlib
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(path.read_text())['targets'], [])
