"""Exercise automatic pinned FSR provider setup without loading a GPU DLL."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from upscalers import fsr, storage


PAYLOAD = b"a small test provider"
ARCHIVE = b"a small test archive"


def release():
    return {
        "version": "26.10.2", "architecture": "x86_64", "release_notes": "https://www.amd.com/",
        "archive": {"url": "https://drivers.amd.com/test.exe", "size": len(ARCHIVE),
                    "sha256": hashlib.sha256(ARCHIVE).hexdigest()},
        "provider": {"member": "driver/amdxcffx64.dll", "size": len(PAYLOAD),
                     "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
                     "file_version": "2.4.0.4541"},
    }


class FsrComponents(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefix = self.root / "compat/pfx"
        self.env = {"XDG_DATA_HOME": str(self.root / "data"), "PROTON_UPSCALER_UPGRADE": "1"}
        self.entry = release()
        self.data = {"schema": 1, "default": self.entry["version"],
                     "releases": {self.entry["version"]: self.entry}}
        self.logs = []
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(fsr, "catalog", return_value=self.data).start()
        self.extractor = mock.patch.object(storage.sevenzip, "extractor", return_value=Path("/test/7zzs")).start()
        self.download = mock.patch.object(storage, "download", side_effect=self.fake_download).start()
        self.extract = mock.patch.object(storage, "extract", side_effect=self.fake_extract).start()

    @staticmethod
    def fake_download(entry, destination, progress=None):
        destination.write_bytes(ARCHIVE)

    @staticmethod
    def fake_extract(archive, entry, destination, progress=None, *, program):
        destination.write_bytes(PAYLOAD)

    def setup(self, **options):
        return fsr.setup(self.env, log=self.logs.append, **{"gpu_eligible": True, **options})

    def seed_store(self):
        store = storage.Store(self.env)
        with store.lock():
            return store.prepare(self.entry)

    def test_non_amd_does_not_open_the_store_or_download(self):
        with mock.patch.object(fsr, "Store") as store:
            self.assertFalse(self.setup(gpu_eligible=lambda: False))
        store.assert_not_called()
        self.download.assert_not_called()
        self.extractor.assert_not_called()
        self.assertNotIn("WINE_FSR4_PROVIDER", self.env)

    def test_first_launch_automatically_downloads_the_manifest_default(self):
        self.assertTrue(self.setup())
        self.download.assert_called_once()
        self.assertEqual(self.download.call_args.args[0], self.entry)
        self.assertEqual(Path(self.env["WINE_FSR4_PROVIDER"]).parent.name, self.data["default"])
        self.assertEqual(Path(self.env["WINE_FSR4_PROVIDER"]).read_bytes(), PAYLOAD)

    def test_repeated_launch_reuses_the_verified_cache(self):
        self.assertTrue(self.setup())
        selected = self.env.pop("WINE_FSR4_PROVIDER")
        provider = Path(selected)
        metadata = provider.parent / "metadata.json"
        before = provider.stat().st_ino, metadata.stat().st_ino
        progress = mock.Mock()
        with mock.patch.object(storage, "checksum", wraps=storage.checksum) as checksum:
            self.assertTrue(self.setup(progress=progress))
        checksum.assert_called_once_with(provider)
        self.assertEqual(self.env["WINE_FSR4_PROVIDER"], selected)
        self.assertEqual(before, (provider.stat().st_ino, metadata.stat().st_ino))
        progress.assert_not_called()
        self.download.assert_called_once()
        self.extractor.assert_called_once()
        self.extract.assert_called_once()

    def test_new_manifest_default_selects_a_new_release_and_preserves_the_old_cache(self):
        old_provider = self.seed_store()
        inode = old_provider.stat().st_ino
        newer = deepcopy(self.entry)
        newer["version"] = "26.11.1"
        newer["provider"]["file_version"] = "2.4.1.1234"
        self.data["default"] = newer["version"]
        self.data["releases"][newer["version"]] = newer
        self.download.reset_mock()
        self.assertTrue(self.setup())
        selected = Path(self.env["WINE_FSR4_PROVIDER"])
        self.assertEqual(selected.parent.name, newer["version"])
        self.assertEqual(selected.read_bytes(), PAYLOAD)
        self.assertEqual(self.download.call_args.args[0], newer)
        self.assertEqual(old_provider.stat().st_ino, inode)
        self.assertEqual(old_provider.read_bytes(), PAYLOAD)

    def test_new_games_reuse_the_manifest_provider_without_creating_configuration(self):
        self.env.update(HOME=str(self.root / "home"), XDG_CONFIG_HOME=str(self.root / "config"), SteamAppId="1")
        self.assertTrue(self.setup())
        selected = self.env.pop("WINE_FSR4_PROVIDER")
        self.env["SteamAppId"] = "2"
        self.assertTrue(self.setup())
        self.assertEqual(self.env["WINE_FSR4_PROVIDER"], selected)
        self.assertFalse((self.root / "config").exists())
        self.assertFalse((self.root / "home/.config").exists())
        self.download.assert_called_once()

    def test_manual_lite_value_is_never_modified(self):
        for value in (None, "", "0", "1", "false", "true", "invalid"):
            with self.subTest(value=value):
                self.env.pop("WINE_FSR4_PROVIDER", None)
                self.env.pop("WINE_FSR4_LITE", None)
                if value is not None:
                    self.env["WINE_FSR4_LITE"] = value
                self.assertTrue(self.setup())
                self.assertEqual(self.env.get("WINE_FSR4_LITE"), value)
                self.assertEqual("WINE_FSR4_LITE" in self.env, value is not None)

    def test_explicit_provider_bypasses_hardware_and_download_and_preserves_manual_lite(self):
        for value in (None, "", "0", "1", "false", "invalid"):
            with self.subTest(value=value):
                self.env["WINE_FSR4_PROVIDER"] = "/custom/provider.dll"
                self.env.pop("WINE_FSR4_LITE", None)
                if value is not None:
                    self.env["WINE_FSR4_LITE"] = value
                with mock.patch.object(fsr, "Store") as store:
                    self.assertTrue(self.setup(gpu_eligible=mock.Mock(side_effect=AssertionError("GPU queried"))))
                store.assert_not_called()
                self.assertEqual(self.env["WINE_FSR4_PROVIDER"], "/custom/provider.dll")
                self.assertEqual(self.env.get("WINE_FSR4_LITE"), value)
        self.download.assert_not_called()
        self.extractor.assert_not_called()

    def test_manual_lite_is_preserved_when_setup_is_ineligible_or_fails(self):
        self.env["WINE_FSR4_LITE"] = "1"
        self.assertFalse(self.setup(gpu_eligible=False))
        self.assertEqual(self.env["WINE_FSR4_LITE"], "1")
        self.download.side_effect = OSError("offline")
        self.assertFalse(self.setup())
        self.assertEqual(self.env["WINE_FSR4_LITE"], "1")
        self.assertNotIn("WINE_FSR4_PROVIDER", self.env)

    def test_deprecated_flags_do_not_disable_or_select_a_different_manifest_release(self):
        for value in ("0", "1", "4.1.2", "default"):
            with self.subTest(value=value):
                self.env.update(PROTON_FSR4_UPGRADE=value, FSR4_UPGRADE=value)
                self.env.pop("WINE_FSR4_PROVIDER", None)
                self.assertTrue(self.setup())
                self.assertEqual(Path(self.env["WINE_FSR4_PROVIDER"]).parent.name, self.data["default"])

    def test_old_config_choices_are_not_read_or_rewritten(self):
        config = self.root / "config/proton/config.ini"
        config.parent.mkdir(parents=True)
        original = "# old choices\n[global]\nfsr_upgrade=off\nfsr_lite=true\n[steam:2]\nfsr_upgrade=invalid\n"
        config.write_text(original)
        self.env.update(XDG_CONFIG_HOME=str(config.parent.parent), SteamAppId="2")
        self.assertTrue(self.setup())
        self.assertEqual(config.read_text(), original)
        self.assertNotIn("WINE_FSR4_LITE", self.env)

    def test_extractor_failure_does_not_download_driver(self):
        self.extractor.side_effect = ValueError("cannot prepare 7-Zip")
        self.assertFalse(self.setup())
        self.download.assert_not_called()
        self.assertNotIn("WINE_FSR4_PROVIDER", self.env)

    def test_empty_provider_path_still_uses_automatic_setup(self):
        self.env["WINE_FSR4_PROVIDER"] = ""
        self.assertTrue(self.setup())
        self.assertTrue(Path(self.env["WINE_FSR4_PROVIDER"]).is_absolute())

    def test_provider_is_the_only_enablement_signal_and_never_copied(self):
        self.assertTrue(self.setup())
        self.assertEqual(set(self.env), {"XDG_DATA_HOME", "PROTON_UPSCALER_UPGRADE", "WINE_FSR4_PROVIDER"})
        provider = Path(self.env["WINE_FSR4_PROVIDER"])
        self.assertTrue(provider.is_absolute())
        self.assertFalse(provider.is_symlink())
        self.assertEqual(provider.read_bytes(), PAYLOAD)
        self.assertFalse(self.prefix.exists())

    def test_extractor_is_prepared_before_the_driver_download(self):
        order = []
        def prepare_tool(**options):
            order.append("tool")
            self.assertIs(options["env"], self.env)
            return Path("/test/7zzs")
        def download(*args):
            order.append("driver")
            self.fake_download(*args)
        self.extractor.side_effect = prepare_tool
        self.download.side_effect = download
        self.assertTrue(self.setup())
        self.assertEqual(order, ["tool", "driver"])
        self.assertEqual(self.extract.call_args.kwargs["program"], Path("/test/7zzs"))

    def test_cached_provider_does_not_need_extractor_or_progress(self):
        provider = self.seed_store()
        metadata = provider.parent / "metadata.json"
        before = provider.stat().st_ino, metadata.stat().st_ino
        progress = mock.Mock()
        with mock.patch.object(storage.sevenzip, "extractor", side_effect=AssertionError("extractor checked")):
            self.assertTrue(self.setup(progress=progress))
        progress.assert_not_called()
        self.assertEqual(before, (provider.stat().st_ino, metadata.stat().st_ino))

    def test_mlfg_override_remains_independent(self):
        self.env["PROTON_MLFG_UPGRADE"] = "1"
        self.assertTrue(self.setup())
        self.assertEqual(self.env["MLFG_UPGRADE"], "1")
        self.env["MLFG_UPGRADE"] = "0"
        self.assertTrue(self.setup())
        self.assertEqual(self.env["MLFG_UPGRADE"], "0")

    def test_existing_prefix_files_are_untouched_and_do_not_block_selection(self):
        target = self.prefix / "drive_c/windows/system32/amdxcffx64.dll"
        target.parent.mkdir(parents=True)
        custom = self.root / "custom.dll"
        custom.write_bytes(b"user provider")
        target.symlink_to(custom)
        tracking = self.prefix.parent / "upscaler_files"
        tracking.write_text('{"fsr4_files": {"custom": "legacy"}}')
        before = custom.read_bytes(), tracking.read_bytes(), target.lstat().st_ino
        self.assertTrue(self.setup())
        self.assertNotEqual(Path(self.env["WINE_FSR4_PROVIDER"]), target)
        self.assertEqual(before, (custom.read_bytes(), tracking.read_bytes(), target.lstat().st_ino))

    def test_failed_download_never_activates_old_prefix_provider(self):
        target = self.prefix / "drive_c/windows/system32/amdxcffx64.dll"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old provider")
        self.download.side_effect = OSError("offline")
        self.assertFalse(self.setup())
        self.assertEqual(target.read_bytes(), b"old provider")
        self.assertNotIn("WINE_FSR4_PROVIDER", self.env)
        self.assertFalse(list(storage.Store(self.env).root.glob(".prepare-*")))

    def test_extraction_or_metadata_failure_never_publishes_component(self):
        for function in ("extract", "atomic_json"):
            with self.subTest(function=function), mock.patch.object(storage, function, side_effect=OSError("failure")):
                self.assertFalse(self.setup())
                self.assertNotIn("WINE_FSR4_PROVIDER", self.env)
                self.assertFalse((storage.Store(self.env).root / self.entry["version"]).exists())
                self.assertFalse(list(storage.Store(self.env).root.glob(".prepare-*")))

    def test_cancelled_preparation_never_selects_provider(self):
        self.download.side_effect = lambda entry, dest, progress: storage.report(progress, "download", 0, 1)
        self.assertFalse(self.setup(progress=lambda *args: False))
        self.assertNotIn("WINE_FSR4_PROVIDER", self.env)

    def test_busy_installer_skips_upgrade_without_delaying_launch(self):
        with storage.Store(self.env).lock(), ThreadPoolExecutor(1) as pool:
            future = pool.submit(fsr.setup, dict(self.env), gpu_eligible=True, log=self.logs.append)
            self.assertFalse(future.result(timeout=2))
        self.download.assert_not_called()
        self.assertTrue(self.setup())
        self.download.assert_called_once()

    def test_archive_and_extraction_use_cache_staging_and_are_removed_after_publish(self):
        self.assertTrue(self.setup())
        archive = Path(self.download.call_args.args[1])
        extracted = Path(self.extract.call_args.args[2])
        self.assertEqual(archive.parent.parent, storage.Store(self.env).root)
        self.assertEqual(extracted.parent.parent, archive.parent)
        self.assertFalse(archive.parent.exists())
        self.assertTrue(Path(self.env["WINE_FSR4_PROVIDER"]).is_file())

    def test_damaged_managed_cache_is_repaired_without_touching_the_prefix(self):
        provider = self.seed_store()
        provider.write_bytes(b"damaged")
        self.download.reset_mock()
        self.assertTrue(self.setup())
        self.assertEqual(provider.read_bytes(), PAYLOAD)
        self.assertEqual(Path(self.env["WINE_FSR4_PROVIDER"]), provider)
        self.assertFalse(self.prefix.exists())
        self.download.assert_called_once()

    def test_failed_repair_preserves_the_previous_cache(self):
        provider = self.seed_store()
        provider.write_bytes(b"damaged")
        metadata = (provider.parent / "metadata.json").read_bytes()
        self.download.side_effect = OSError("offline")
        self.assertFalse(self.setup())
        self.assertEqual(provider.read_bytes(), b"damaged")
        self.assertEqual((provider.parent / "metadata.json").read_bytes(), metadata)
        self.assertNotIn("WINE_FSR4_PROVIDER", self.env)
        self.assertFalse(list(storage.Store(self.env).root.glob(".prepare-*")))

    def test_changed_manifest_hash_for_the_same_version_refreshes_the_component(self):
        provider = self.seed_store()
        updated = b"an updated test provider"
        self.entry["provider"].update(size=len(updated), sha256=hashlib.sha256(updated).hexdigest(),
                                      file_version="2.4.0.4542")
        self.extract.side_effect = lambda archive, entry, path, progress=None, **kwargs: path.write_bytes(updated)
        self.download.reset_mock()
        self.assertTrue(self.setup())
        self.assertEqual(Path(self.env["WINE_FSR4_PROVIDER"]), provider)
        self.assertEqual(provider.read_bytes(), updated)
        metadata = json.loads((provider.parent / "metadata.json").read_text())
        self.assertEqual(metadata["provider"]["sha256"], self.entry["provider"]["sha256"])
        self.assertEqual(metadata["provider"]["file_version"], "2.4.0.4542")
        self.download.assert_called_once()

    def test_managed_cache_symlink_is_not_repaired_or_replaced(self):
        store = storage.Store(self.env)
        outside = self.root / "external"
        outside.mkdir()
        marker = outside / "user-file"
        marker.write_bytes(b"preserve me")
        store.root.mkdir(parents=True)
        target = store.root / self.entry["version"]
        target.symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.setup())
        self.assertTrue(target.is_symlink())
        self.assertEqual(marker.read_bytes(), b"preserve me")
        self.download.assert_not_called()
        self.extract.assert_not_called()

    def test_valid_symlinked_cache_is_not_used_or_rewritten(self):
        provider = self.seed_store()
        target = provider.parent
        outside = self.root / "external-valid-component"
        target.rename(outside)
        metadata = outside / "metadata.json"
        data = json.loads(metadata.read_text())
        data["provider"]["model_selection"] = "legacy"
        metadata.write_text(json.dumps(data))
        original = metadata.read_bytes()
        target.symlink_to(outside, target_is_directory=True)
        self.download.reset_mock()
        self.extract.reset_mock()
        self.assertFalse(self.setup())
        self.assertTrue(target.is_symlink())
        self.assertEqual(metadata.read_bytes(), original)
        self.assertEqual((outside / "amdxcffx64.dll").read_bytes(), PAYLOAD)
        self.assertNotIn("WINE_FSR4_PROVIDER", self.env)
        self.download.assert_not_called()
        self.extract.assert_not_called()

    def test_publish_failure_restores_the_previous_cache(self):
        provider = self.seed_store()
        provider.write_bytes(b"damaged")
        metadata = (provider.parent / "metadata.json").read_bytes()
        replace = storage.os.replace
        def fail_new_component(source, destination):
            if Path(source).name == "component" and Path(destination) == provider.parent:
                raise OSError("cannot publish")
            return replace(source, destination)
        with mock.patch.object(storage.os, "replace", side_effect=fail_new_component):
            self.assertFalse(self.setup())
        self.assertEqual(provider.read_bytes(), b"damaged")
        self.assertEqual((provider.parent / "metadata.json").read_bytes(), metadata)
        self.assertNotIn("WINE_FSR4_PROVIDER", self.env)

    def test_metadata_contains_installation_details_without_model_fields(self):
        self.entry["provider"].update(effect_version=None, model_selection="automatic",
                                      model_availability="unused", model_variants=[{"id": "int8-lite"}])
        provider = self.seed_store()
        metadata = json.loads((provider.parent / "metadata.json").read_text())
        self.assertEqual(set(metadata["provider"]), {"member", "size", "sha256", "file_version"})
        self.assertEqual(metadata["provider"]["sha256"], self.entry["provider"]["sha256"])

    def test_legacy_model_metadata_is_removed_without_redownloading_or_replacing_provider(self):
        provider = self.seed_store()
        path = provider.parent / "metadata.json"
        metadata = json.loads(path.read_text())
        metadata["provider"].update(effect_version="4.1.2", model_selection="automatic",
                                    model_variants=[{"id": "int8-lite"}], model_availability="unused")
        path.write_text(json.dumps(metadata))
        inode = provider.stat().st_ino
        self.download.reset_mock()
        self.extract.reset_mock()
        self.assertTrue(self.setup())
        self.assertEqual(provider.stat().st_ino, inode)
        self.assertEqual(set(json.loads(path.read_text())["provider"]),
                         {"member", "size", "sha256", "file_version"})
        self.download.assert_not_called()
        self.extract.assert_not_called()

    def test_read_only_legacy_metadata_does_not_disable_the_cached_provider(self):
        provider = self.seed_store()
        path = provider.parent / "metadata.json"
        metadata = json.loads(path.read_text())
        metadata["provider"]["model_selection"] = "automatic"
        path.write_text(json.dumps(metadata))
        original = path.read_bytes()
        self.download.reset_mock()
        with mock.patch.object(storage, "atomic_json", side_effect=PermissionError("read only")):
            self.assertTrue(self.setup())
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(Path(self.env["WINE_FSR4_PROVIDER"]), provider)
        self.download.assert_not_called()

    def test_malformed_cached_metadata_is_ignored(self):
        provider = self.seed_store()
        for value in ([], {"provider": []}, {"provider": None}, {"provider": "bad"}):
            with self.subTest(value=value):
                (provider.parent / "metadata.json").write_text(json.dumps(value))
                self.assertIsNone(storage.Store(self.env).installed(self.entry))


class FsrFallback(unittest.TestCase):
    setUp = FsrComponents.setUp
    fake_download = staticmethod(FsrComponents.fake_download)
    fake_extract = staticmethod(FsrComponents.fake_extract)
    setup = FsrComponents.setup
    seed_store = FsrComponents.seed_store
    def request_new(self):
        old = self.seed_store()
        requested = deepcopy(self.entry)
        requested['version'] = '26.11.1'
        self.data['default'] = requested['version']
        self.data['releases'][requested['version']] = requested
        self.env.pop('WINE_FSR4_PROVIDER', None)
        return old, requested

    def test_offline_upgrade_reuses_previous_provider(self):
        previous, requested = self.request_new()
        self.download.side_effect = OSError('offline')
        self.assertTrue(self.setup())
        self.assertEqual(Path(self.env['WINE_FSR4_PROVIDER']), previous)
        self.assertFalse((previous.parent.parent / requested['version']).exists())
        self.assertTrue(any('using verified cached release' in message for message in self.logs))
        self.assertEqual(self.logs[-1], 'AMD FSR provider ready: ' + str(previous))

    def test_bad_extracted_update_uses_previous_provider(self):
        previous, requested = self.request_new()
        self.extract.side_effect = storage.ComponentError('checksum mismatch')
        self.assertTrue(self.setup())
        self.assertEqual(Path(self.env['WINE_FSR4_PROVIDER']), previous)

    def test_legacy_cache_recovery_without_marker(self):
        previous, requested = self.request_new()
        (previous.parent.parent / '.last-good.json').unlink()
        self.download.side_effect = OSError('offline')
        self.assertTrue(self.setup())
        self.assertEqual(Path(self.env['WINE_FSR4_PROVIDER']), previous)

    def test_held_lock_uses_previous_without_network(self):
        previous, requested = self.request_new()
        self.download.reset_mock()
        with storage.Store(self.env).lock():
            self.assertTrue(self.setup())
        self.assertEqual(Path(self.env['WINE_FSR4_PROVIDER']), previous)
        self.download.assert_not_called()

    def test_missing_extractor_still_uses_previous_provider(self):
        previous, requested = self.request_new()
        self.extractor.side_effect = OSError('extractor unavailable')
        self.download.reset_mock()
        self.assertTrue(self.setup())
        self.assertEqual(Path(self.env['WINE_FSR4_PROVIDER']), previous)
        self.download.assert_not_called()

    def test_corrupt_previous_provider_is_not_used(self):
        previous, requested = self.request_new()
        previous.write_bytes(b'bad provider')
        self.download.side_effect = OSError('offline')
        self.assertFalse(self.setup())
        self.assertNotIn('WINE_FSR4_PROVIDER', self.env)

    def test_invalid_cached_metadata_cannot_escape_root(self):
        previous, requested = self.request_new()
        for version in ('../outside', '/tmp/outside', '..'):
            with self.subTest(version=version):
                record = deepcopy(self.entry)
                record['version'] = version
                storage.atomic_json(previous.parent.parent / '.last-good.json', record)
                storage.atomic_json(previous.parent / 'metadata.json', record)
                self.download.side_effect = OSError('offline')
                self.assertFalse(self.setup())
                self.assertNotIn('WINE_FSR4_PROVIDER', self.env)

    def test_disable_does_not_reactivate_previous_provider(self):
        previous, requested = self.request_new()
        self.env['PROTON_UPSCALER_UPGRADE'] = '0'
        self.download.reset_mock()
        self.assertFalse(self.setup())
        self.download.assert_not_called()
        self.assertNotIn('WINE_FSR4_PROVIDER', self.env)


class ArchiveStorage(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.entry = release()

    @staticmethod
    def response(data, url="https://drivers.amd.com/test.exe"):
        response = io.BytesIO(data)
        response.geturl = lambda: url
        return response

    def test_download_checks_exact_size_and_hash(self):
        destination = self.root / "download"
        for data in (ARCHIVE, b"wrong same-size data", ARCHIVE[:-1], ARCHIVE + b"x"):
            with self.subTest(data=data), mock.patch.object(storage.urllib.request, "urlopen",
                                                           return_value=self.response(data)):
                if data == ARCHIVE:
                    storage.download(self.entry, destination)
                    self.assertEqual(destination.read_bytes(), ARCHIVE)
                else:
                    with self.assertRaises(storage.ComponentError):
                        storage.download(self.entry, destination)

    def test_download_rejects_error_page_redirects(self):
        with mock.patch.object(storage.urllib.request, "urlopen",
                               return_value=self.response(ARCHIVE, "https://www.amd.com/error")):
            with self.assertRaises(storage.ComponentError):
                storage.download(self.entry, self.root / "download")

    def test_cancellation_aborts_preparation(self):
        with mock.patch.object(storage.urllib.request, "urlopen", return_value=self.response(ARCHIVE)):
            with self.assertRaises(storage.ComponentError):
                storage.download(self.entry, self.root / "download", lambda *args: False)

    @unittest.skipUnless(shutil.which("7z") or shutil.which("7zz"), "7z is unavailable")
    def test_real_archive_extracts_only_the_provider(self):
        program = shutil.which("7zz") or shutil.which("7z")
        member = self.root / self.entry["provider"]["member"]
        member.parent.mkdir()
        member.write_bytes(PAYLOAD)
        (self.root / "unrelated.dll").write_bytes(b"must not be installed")
        archive = self.root / "package.7z"
        subprocess.run([program, "a", str(archive), "driver", "unrelated.dll"], cwd=self.root,
                       check=True, stdout=subprocess.DEVNULL)
        self.entry["archive"].update({"size": archive.stat().st_size,
                                      "sha256": storage.checksum(archive)})
        store = storage.Store({"XDG_DATA_HOME": str(self.root / "data")})
        with store.lock():
            provider = store.prepare(self.entry, archive=archive)
        self.assertEqual(provider.read_bytes(), PAYLOAD)
        self.assertEqual(sorted(path.name for path in provider.parent.iterdir()),
                         ["amdxcffx64.dll", "metadata.json"])
        with store.lock():
            self.assertEqual(store.prepare(self.entry, archive=archive), provider)

    def test_local_import_rejects_wrong_archive(self):
        archive = self.root / "package.exe"
        archive.write_bytes(ARCHIVE + b"x")
        store = storage.Store({"XDG_DATA_HOME": str(self.root / "data")})
        with store.lock(), self.assertRaises(storage.ComponentError):
            store.prepare(self.entry, archive=archive)
        self.assertFalse((store.root / self.entry["version"]).exists())

    def test_atomic_metadata_failure_keeps_existing_contents(self):
        target = self.root / "metadata.json"
        target.write_text("original data")
        with mock.patch.object(storage.os, "replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                storage.atomic_json(target, {"new": "data"})
        self.assertEqual(target.read_text(), "original data")
        self.assertEqual(list(self.root.iterdir()), [target])



if __name__ == "__main__":
    unittest.main()
