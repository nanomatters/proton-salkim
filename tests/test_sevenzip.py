"""Exercise system selection and the shared fallback without real downloads."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest import mock

from upscalers import sevenzip, storage


FILES = {"7zzs": b"static executable", "License.txt": b"license", "readme.txt": b"readme",
         "MANUAL/general/license.htm": b"license details"}


def archive(*, linked=False):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:xz") as output:
        for name, data in {**FILES, "7zz": b"unused executable"}.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            if linked and name == "7zzs":
                member.type, member.linkname = tarfile.SYMTYPE, "/not-a-tool"
            output.addfile(member, io.BytesIO(data))
    return stream.getvalue()


class SevenZipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.env = {"HOME": str(self.root), "PATH": "", "XDG_CACHE_HOME": str(self.root / "xdg")}
        self.data = archive()
        digest = hashlib.sha256(self.data).hexdigest()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(sevenzip.BUILDS, {"x86_64": ("x64", digest),
                                         "aarch64": ("arm64", digest)}, clear=True).start()
        self.machine = mock.patch.object(sevenzip.platform, "machine", return_value="x86_64").start()
        self.which = mock.patch.object(sevenzip.shutil, "which", return_value=None).start()
        self.download = mock.patch.object(sevenzip.urllib.request, "urlopen",
                                          side_effect=lambda *a, **k: io.BytesIO(self.data)).start()

    def extract(self, **options):
        return sevenzip.extractor(self.cache, env=self.env, **options)

    def check_payload(self, tool):
        self.assertEqual(tool.name, "7zzs")
        for name, data in FILES.items():
            path = tool.parent / name
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if name == "7zzs" else 0o600)
        self.assertFalse((tool.parent / "7zz").exists())

    def test_system_tool_wins_without_cache_or_download(self):
        for available in ({"7zz", "7z", "7zzs"}, {"7z", "7zzs"}, {"7zzs"}):
            with self.subTest(available=available):
                self.which.side_effect = lambda name, **kw: "/system/" + name if name in available else None
                expected = next(name for name in ("7zz", "7z", "7zzs") if name in available)
                self.assertEqual(self.extract(), Path("/system") / expected)
        self.download.assert_not_called()
        self.assertFalse(self.cache.exists())

    def test_lookup_uses_the_launchers_path(self):
        self.which.return_value = "/custom/bin/7zz"
        self.env["PATH"] = "/custom/bin"
        self.assertEqual(self.extract(), Path("/custom/bin/7zz"))
        self.which.assert_called_once_with("7zz", path="/custom/bin")
        self.download.assert_not_called()

    def test_missing_system_tool_downloads_and_reuses_verified_cache(self):
        tool = self.extract()
        self.check_payload(tool)
        self.assertEqual(self.extract(), tool)
        self.download.assert_called_once()
        request = self.download.call_args.args[0]
        self.assertEqual(request.full_url,
            "https://github.com/ip7z/7zip/releases/download/26.03/7z2603-linux-x64.tar.xz")

    def test_system_tool_installed_later_takes_precedence(self):
        self.extract()
        self.download.reset_mock()
        self.which.return_value = "/system/7zz"
        self.assertEqual(self.extract(), Path("/system/7zz"))
        self.download.assert_not_called()

    def test_cached_binary_and_permissions_are_repaired_without_download(self):
        tool = self.extract()
        tool.write_bytes(b"corrupted")
        tool.chmod(0o600)
        self.assertEqual(self.extract(), tool)
        self.check_payload(tool)
        tool.chmod(0o600)
        self.extract()
        self.check_payload(tool)
        self.download.assert_called_once()

    def test_cached_symlink_is_replaced_without_touching_its_target(self):
        tool = self.extract()
        other = self.root / "other"
        other.write_bytes(FILES["7zzs"])
        tool.unlink()
        tool.symlink_to(other)
        self.extract()
        self.assertFalse(tool.is_symlink())
        self.assertEqual(other.read_bytes(), FILES["7zzs"])

    def test_bad_download_never_publishes_a_tool(self):
        self.download.side_effect = lambda *a, **k: io.BytesIO(b"wrong archive")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.extract()
        self.assertEqual([path.name for path in self.cache.rglob("*") if path.is_file()], [".lock"])

    def test_failed_download_keeps_cached_files_and_removes_temporary_file(self):
        tool = self.extract()
        archive_path = next(tool.parent.glob("*.tar.xz"))
        archive_path.write_bytes(b"damaged archive")
        self.download.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.extract()
        self.assertEqual(archive_path.read_bytes(), b"damaged archive")
        self.assertEqual(tool.read_bytes(), FILES["7zzs"])
        self.assertEqual({p.name for p in tool.parent.iterdir()},
                         {".lock", "7zzs", "License.txt", "readme.txt", "MANUAL", archive_path.name})

    def test_archive_links_are_rejected(self):
        self.data = archive(linked=True)
        sevenzip.BUILDS["x86_64"] = ("x64", hashlib.sha256(self.data).hexdigest())
        with self.assertRaisesRegex(ValueError, "Unexpected 7-Zip member"):
            self.extract()
        self.assertFalse(any(path.name == "7zzs" for path in self.cache.rglob("*")))

    def test_invalid_archive_is_rejected(self):
        self.data = b"not even an archive"
        sevenzip.BUILDS["x86_64"] = ("x64", hashlib.sha256(self.data).hexdigest())
        with self.assertRaisesRegex(ValueError, "Invalid 7-Zip archive"):
            self.extract()

    def test_fallback_matches_host_architecture(self):
        self.machine.return_value = "aarch64"
        tool = self.extract()
        self.assertEqual(tool.parent.name, "arm64")
        self.check_payload(tool)
        self.assertTrue(self.download.call_args.args[0].full_url.endswith("-linux-arm64.tar.xz"))

    def test_unsupported_host_can_still_use_a_system_tool(self):
        self.machine.return_value = "unsupported"
        self.which.return_value = "/system/7z"
        self.assertEqual(self.extract(), Path("/system/7z"))
        self.which.return_value = None
        with self.assertRaisesRegex(ValueError, "No bundled 7-Zip build"):
            self.extract()
        self.download.assert_not_called()
        self.assertFalse(self.cache.exists())

    def test_default_cache_reuses_optiscalers_existing_location(self):
        tool = sevenzip.extractor(env=self.env)
        self.assertEqual(tool, self.root / "xdg/protonfixes/upscalers/7zip/26.03/x64/7zzs")
        self.assertEqual(sevenzip.extractor(self.root / "xdg/protonfixes/upscalers", env=self.env), tool)
        self.download.assert_called_once()

    def test_relative_xdg_path_falls_back_to_home(self):
        self.env["XDG_CACHE_HOME"] = "relative"
        tool = sevenzip.extractor(env=self.env)
        self.assertEqual(tool, self.root / ".cache/protonfixes/upscalers/7zip/26.03/x64/7zzs")

    def test_busy_installer_returns_without_waiting(self):
        lock = self.cache / "7zip/26.03/x64/.lock"
        with storage.locked(lock), ThreadPoolExecutor(1) as workers:
            future = workers.submit(self.extract)
            with self.assertRaisesRegex(storage.ComponentError, "in progress"):
                future.result(timeout=2)
        self.download.assert_not_called()
        self.check_payload(self.extract())
        self.download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
