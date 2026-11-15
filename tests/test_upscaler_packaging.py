"""Test upscaler package synchronization without building Proton."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UpscalerPackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "source"
        self.package = self.source / "upscalers"
        (self.package / "releases").mkdir(parents=True)
        for name in ("__init__.py", "progress.py", "fsr.py", "sevenzip.py", "storage.py"):
            (self.package / name).write_text("# current " + name + "\n")
        (self.package / "releases/fsr.json").write_text('{"version": "current"}\n')
        (self.source / "Makefile.in").write_text("# build rules\n")
        self.destination = self.directory / "dist/upscalers"
        self.target = self.destination / ".stamp"
        makefile = (ROOT / "Makefile.in").read_text()
        rules = makefile[makefile.index("UPSCALERS_TARGET :="):makefile.index("all-dist: $(UPSCALERS_TARGET)")]
        self.makefile = self.directory / "Makefile"
        self.makefile.write_text(
            f"SRCDIR := {self.source}\nDST_BASE := {self.directory / 'dist'}\n"
            "DST_DIR := $(DST_BASE)/files\n" + rules + "\n$(DST_DIR):\n\tmkdir -p $@\n")

    def build(self):
        result = subprocess.run(
            ["make", "--no-print-directory", "-f", str(self.makefile), str(self.target)],
            cwd=self.directory, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_sync_removes_obsolete_files_and_excludes_bytecode(self):
        (self.destination / "releases").mkdir(parents=True)
        (self.destination / "hardware.py").write_text("# removed hardware detection\n")
        (self.destination / "dialog.py").write_text("# removed GTK dialog\n")
        (self.destination / "releases/obsolete.json").write_text("{}\n")
        for location in (self.package, self.destination):
            (location / "__pycache__").mkdir()
            (location / "__pycache__/progress.cpython-314.pyc").write_bytes(b"bytecode")
            (location / "progress.pyc").write_bytes(b"bytecode")
        (self.package / "developer-notes.md").write_text("not a runtime file\n")
        self.build()
        expected = {".stamp", "__init__.py", "progress.py", "fsr.py", "sevenzip.py", "storage.py",
                    "releases/fsr.json"}
        self.assertEqual({str(path.relative_to(self.destination))
                          for path in self.destination.rglob("*") if path.is_file()}, expected)
        self.assertEqual((self.destination / "progress.py").read_bytes(), (self.package / "progress.py").read_bytes())
        self.assertEqual((self.destination / "releases/fsr.json").read_bytes(),
                         (self.package / "releases/fsr.json").read_bytes())
        self.assertFalse((self.destination / "__pycache__").exists())
        self.assertFalse((self.destination / "dialog.py").exists())
        self.assertFalse((self.destination / "hardware.py").exists())

    def test_unchanged_package_keeps_its_stamp(self):
        self.build()
        before = self.target.stat().st_mtime_ns
        result = self.build()
        self.assertEqual(self.target.stat().st_mtime_ns, before)
        self.assertNotIn("rsync", result.stdout)

    def test_removed_source_file_refreshes_the_package(self):
        self.build()
        (self.package / "progress.py").unlink()
        updated = self.target.stat().st_mtime_ns + 1_000_000_000
        os.utime(self.package, ns=(updated, updated))
        result = self.build()
        self.assertIn("rsync", result.stdout)
        self.assertFalse((self.destination / "progress.py").exists())
        self.assertTrue(self.target.is_file())


if __name__ == "__main__":
    unittest.main()
