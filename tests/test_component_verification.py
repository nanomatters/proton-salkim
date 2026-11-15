"""Verify cached components on every enabled launch."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from upscalers import storage


class ComponentVerification(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.library = self.directory / "library.dll"
        self.payload = b"verified component"
        self.library.write_bytes(self.payload)
        self.files = {self.library.name: {
            "size": len(self.payload), "sha256": hashlib.sha256(self.payload).hexdigest()}}

    def verify(self):
        return storage.verified_files(self.directory, self.files)

    def test_every_launch_reads_unchanged_library_contents(self):
        with mock.patch.object(storage, "checksum", wraps=storage.checksum) as checksum:
            self.assertTrue(self.verify())
            self.assertTrue(self.verify())
        self.assertEqual(checksum.call_args_list, [mock.call(self.library)] * 2)
        result = subprocess.run([sys.executable, "-c", """
import json, sys
from pathlib import Path
from unittest import mock
from upscalers import storage
with mock.patch.object(storage, 'checksum', wraps=storage.checksum) as checksum:
    assert storage.verified_files(sys.argv[1], json.loads(sys.argv[2]))
    checksum.assert_called_once_with(Path(sys.argv[1]) / 'library.dll')
""", str(self.directory), json.dumps(self.files)],
            cwd=Path(storage.__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_size_corruption_with_restored_mtime_is_rejected(self):
        self.assertTrue(self.verify())
        before = self.library.stat()
        self.library.write_bytes(b"x" * len(self.payload))
        os.utime(self.library, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.library.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(self.verify())

    def test_obsolete_verification_record_cannot_bypass_checksums(self):
        self.library.write_bytes(b"x" * len(self.payload))
        info = self.library.stat()
        record = {"schema": 1, "files": {self.library.name: {
            "sha256": self.files[self.library.name]["sha256"],
            "stat": [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]}}}
        (self.directory / ".verified.json").write_text(json.dumps(record))
        with mock.patch.object(storage, "checksum", wraps=storage.checksum) as checksum:
            self.assertFalse(self.verify())
        checksum.assert_called_once_with(self.library)

    def test_changed_manifest_rejects_unchanged_file(self):
        self.assertTrue(self.verify())
        self.files[self.library.name]["sha256"] = hashlib.sha256(b"x" * len(self.payload)).hexdigest()
        self.assertFalse(self.verify())

    def test_missing_file_and_symlink_are_rejected(self):
        self.library.unlink()
        self.assertFalse(self.verify())
        target = self.directory / "external.dll"
        target.write_bytes(self.payload)
        self.library.symlink_to(target)
        self.assertFalse(self.verify())
        linked = self.directory / "linked"
        linked.symlink_to(self.directory, target_is_directory=True)
        self.assertFalse(storage.verified_files(linked, self.files))

    def test_read_errors_are_rejected(self):
        with mock.patch.object(storage, "checksum", side_effect=PermissionError("unreadable")):
            self.assertFalse(self.verify())

    def test_verification_needs_no_cache_write(self):
        with mock.patch.object(storage, "atomic_json", side_effect=AssertionError("verification wrote cache")):
            self.assertTrue(self.verify())
        self.assertEqual(list(self.directory.iterdir()), [self.library])

    def test_fallback_verifies_contents_on_each_selection(self):
        root = self.directory / "cache"
        directory = root / "1"
        directory.mkdir(parents=True)
        library = directory / self.library.name
        library.write_bytes(self.payload)
        manifest = {"schema": 1, "version": "1", "files": self.files}
        storage.atomic_json(directory / "metadata.json", manifest)
        with mock.patch.object(storage, "checksum", wraps=storage.checksum) as checksum:
            for _ in range(2):
                self.assertEqual(storage.cached_fallback(root, manifest, "offline", log=lambda _: None), directory)
        self.assertEqual(checksum.call_args_list, [mock.call(library)] * 2)
        library.write_bytes(b"x" * len(self.payload))
        self.assertIsNone(storage.cached_fallback(root, manifest, "offline", log=lambda _: None))


if __name__ == "__main__":
    unittest.main()
