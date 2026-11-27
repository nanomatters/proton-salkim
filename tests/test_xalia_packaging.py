"""Test Xalia packaging in temporary directories without building Proton."""

from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


class XaliaPackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "source"
        self.object = self.directory / "object"
        (self.source / "contrib").mkdir(parents=True)
        self.object.mkdir()
        self.fixups = (ROOT / "xalia-fixups.gudl").read_bytes()
        (self.source / "xalia-fixups.gudl").write_bytes(self.fixups)
        makefile = (ROOT / "Makefile.in").read_text()
        # Use the distribution rules without building unrelated components.
        rules = makefile[makefile.index("XALIA_VER :="):makefile.index("xalia-distclean::")]
        self.makefile = self.directory / "Makefile"
        self.makefile.write_text(
            f"SRC := {self.source}\nOBJ := {self.object}\n"
            "DST_DIR := $(OBJ)/dist/files\nSHELL := /bin/bash\n" + rules
        )
        self.xalia_version = next(line.split(":=", 1)[1].strip() for line in rules.splitlines()
                                  if line.startswith("XALIA_VER :="))
        self.target = self.object / ".xalia-dist"
        self.installed = self.object / "dist/files/share/xalia"

    def fixtures(self, architecture, include_sdl=True):
        suffix = "arm64-mono" if architecture == "arm64" else "mono"
        archive = self.source / "contrib" / f"xalia-{self.xalia_version}-net48-{suffix}.zip"
        with zipfile.ZipFile(archive, "w") as package:
            if include_sdl:
                package.writestr("SDL3.dll", architecture.encode())
            package.writestr("main.gudl", b"\xef\xbb\xbf// original rules\n")

    def build(self, architecture):
        return subprocess.run(
            ["make", "--no-print-directory", "-j2", "-f", str(self.makefile),
             f"TARGET_ARCH={architecture}", str(self.target)],
            cwd=self.object, capture_output=True, text=True, timeout=30,
        )

    def check_success(self, architecture):
        self.fixtures(architecture)
        result = self.build(architecture)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.target.is_file())
        library = self.installed / "SDL3.dll"
        self.assertFalse(library.is_symlink())
        self.assertEqual(library.read_bytes(), architecture.encode())
        self.assertEqual((self.installed / "main.gudl").read_bytes(),
                         self.fixups + b"// original rules\n")

    def test_x86_64_package(self):
        self.check_success("x86_64")

    def test_arm64_package(self):
        self.check_success("arm64")

    def check_missing_library(self, architecture):
        self.fixtures(architecture, include_sdl=False)
        result = self.build(architecture)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.installed / "SDL3.dll").exists())

    def test_missing_x86_64_library_fails(self):
        self.check_missing_library("x86_64")

    def test_missing_arm64_library_fails(self):
        self.check_missing_library("arm64")


if __name__ == "__main__":
    unittest.main()
