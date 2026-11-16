"""DLSS provisioning policy and shared component transactions without Wine or network."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from upscalers import dlss, storage


class DlssComponents(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {"XDG_DATA_HOME": str(self.root), "PROTON_UPSCALER_UPGRADE": "1"}
        self.data = deepcopy(dlss.catalog())
        self.payloads = {name: name.encode() for name in self.data["files"]}
        for name, item in self.data["files"].items():
            item.update(size=len(self.payloads[name]), sha256=hashlib.sha256(self.payloads[name]).hexdigest())
        catalog = mock.patch.object(dlss, "catalog", return_value=self.data)
        catalog.start()
        self.addCleanup(catalog.stop)
        download = mock.patch.object(storage, "download_file", side_effect=self.download)
        self.downloads = download.start()
        self.addCleanup(download.stop)

    def download(self, item, target, progress=None, **options):
        target = Path(target)
        target.write_bytes(self.payloads[target.name])
        if progress:
            progress("download", item["size"], item["size"])

    def test_automatic_shared_directory_is_reused_without_prefix_files(self):
        self.assertTrue(dlss.setup(self.env, gpu_eligible=True, log=lambda _: None))
        path = Path(self.env.pop("WINE_DLSS_PROVIDER"))
        self.assertEqual(path, self.root / "proton/components/dlss" / self.data["version"])
        self.assertEqual({p.name for p in path.iterdir()}, set(self.payloads) | {"metadata.json"})
        self.assertFalse(any(p.is_symlink() for p in path.iterdir()))
        self.downloads.reset_mock()
        with mock.patch.object(storage, "checksum", wraps=storage.checksum) as checksum:
            self.assertEqual(dlss.prepare(self.env), path)
        self.assertCountEqual(checksum.call_args_list, [mock.call(path / name) for name in self.payloads])
        self.downloads.assert_not_called()
        self.assertFalse((self.root / "proton/components/ffx").exists())

    def test_ineligible_gpu_never_downloads_or_enables(self):
        self.assertFalse(dlss.setup(self.env, gpu_eligible=False))
        self.downloads.assert_not_called()
        self.assertNotIn("WINE_DLSS_PROVIDER", self.env)
        self.assertFalse((self.root / "proton").exists())

    def test_explicit_path_bypasses_hardware_and_manifest_selection(self):
        path = self.root / "manual DLSS"
        path.mkdir()
        self.env["WINE_DLSS_PROVIDER"] = str(path)
        with mock.patch.object(dlss, "catalog", side_effect=AssertionError("manifest read")):
            self.assertTrue(dlss.setup(self.env, gpu_eligible=lambda: self.fail("GPU queried"), log=lambda _: None))
        self.assertEqual(self.env["WINE_DLSS_PROVIDER"], str(path))
        self.downloads.assert_not_called()

    def test_invalid_explicit_path_is_nonfatal(self):
        for value in ("relative/dlss", str(self.root / "missing")):
            with self.subTest(value=value):
                self.env["WINE_DLSS_PROVIDER"] = value
                self.assertFalse(dlss.setup(self.env, gpu_eligible=True, log=lambda _: None))
        self.downloads.assert_not_called()

    def test_optiscaler_owns_feature_selection(self):
        self.env["WINE_OPTISCALER_NAME"] = "dxgi.dll"
        before = dict(self.env)
        self.assertFalse(dlss.setup(self.env, gpu_eligible=lambda: self.fail("GPU queried")))
        self.assertEqual(self.env, before)
        self.downloads.assert_not_called()

    def test_failed_download_leaves_no_published_release_or_provider(self):
        self.downloads.side_effect = [None, OSError("offline")]
        self.assertFalse(dlss.setup(self.env, gpu_eligible=True, log=lambda _: None))
        self.assertNotIn("WINE_DLSS_PROVIDER", self.env)
        self.assertFalse((self.root / "proton/components/dlss" / self.data["version"]).exists())
        self.assertFalse(list((self.root / "proton/components/dlss").glob(".prepare-*")))

    def test_progress_covers_all_three_libraries(self):
        events = []
        dlss.prepare(self.env, lambda *args: events.append(args))
        self.assertEqual(events[-1][1:], (sum(len(p) for p in self.payloads.values()),) * 2)
        self.assertEqual([event[1] for event in events], sorted(event[1] for event in events))


class DlssManifest(unittest.TestCase):
    def test_pins_all_three_production_files_to_one_official_commit(self):
        data = dlss.catalog()
        self.assertEqual(set(data["files"]), {"nvngx_dlss.dll", "nvngx_dlssd.dll", "nvngx_dlssg.dll"})
        for name, item in data["files"].items():
            self.assertRegex(item["url"], r"^https://raw\.githubusercontent\.com/NVIDIA/DLSS/[0-9a-f]{40}/lib/Windows_x86_64/rel/" + name + "$" )
            self.assertEqual(item["file_version"], data["version"] + ".0")

    def test_rejects_incomplete_development_unpinned_or_unverified_manifests(self):
        data = dlss.catalog()
        cases = [[], {**data, "version": "../other"}, {**data, "files": {}},
                 {**data, "files": {**data["files"], "unexpected.dll": {}}}]
        first = next(iter(data["files"]))
        for key, value in (("url", data["files"][first]["url"].replace('/rel/', '/dev/')),
                           ("url", data["files"][first]["url"].replace('9981c6350dc58c0ce21f024fd65e31e851c31689', 'main')),
                           ("size", 0), ("size", True), ("sha256", "no checksum")):
            item = deepcopy(data)
            item["files"][first][key] = value
            cases.append(item)
        for case in cases:
            with self.subTest(manifest=case), mock.patch.object(Path, "read_text", return_value=json.dumps(case)):
                with self.assertRaises(storage.ComponentError):
                    dlss.catalog()


if __name__ == "__main__":
    unittest.main()
