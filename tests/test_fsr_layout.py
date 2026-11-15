"""Flat FSR caches reuse and migrate legacy installs without breaking live paths."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from upscalers import storage, fsr


class FsrLayout(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.env = {'XDG_DATA_HOME': str(self.data), "PROTON_UPSCALER_UPGRADE": "1"}
        self.store = storage.Store(self.env)
        self.legacy = storage.Store(self.env, root=self.store.root / 'driver')
        self.payload = b'a verified provider'
        self.release = {
            'version': '26.10.2', 'architecture': 'x86_64', 'release_notes': 'https://www.amd.com/',
            'archive': {'url': 'https://drivers.amd.com/test.exe', 'size': 7,
                        'sha256': hashlib.sha256(b'archive').hexdigest()},
            'provider': {'member': 'driver/amdxcffx64.dll', 'size': len(self.payload),
                         'sha256': hashlib.sha256(self.payload).hexdigest(), 'file_version': '2.4.0.4541'},
        }
        boot = mock.patch.object(storage, '_boot_id', return_value='11111111-1111-1111-1111-111111111111')
        self.boot = boot.start()
        self.addCleanup(boot.stop)
        downloader = mock.patch.object(storage, 'download', side_effect=lambda release, path, progress=None:
                                       Path(path).write_bytes(b'archive'))
        self.download = downloader.start()
        self.addCleanup(downloader.stop)
        extractor = mock.patch.object(storage, 'extract', side_effect=lambda archive, release, path, progress=None, **kwargs:
                                      Path(path).write_bytes(self.payload))
        self.extract = extractor.start()
        self.addCleanup(extractor.stop)
        tool = mock.patch.object(storage.sevenzip, 'extractor', return_value=Path('/test/7zz'))
        tool.start()
        self.addCleanup(tool.stop)

    def seed(self, store, release=None, payload=None, *, record=True):
        release = self.release if release is None else release
        payload = self.payload if payload is None else payload
        directory = store.root / release['version']
        directory.mkdir(parents=True)
        provider = directory / 'amdxcffx64.dll'
        provider.write_bytes(payload)
        storage.atomic_json(directory / 'metadata.json', release)
        if record:
            with store.lock():
                storage._remember(store.root, release)
        return provider

    def prepare(self, release=None):
        with self.store.lock():
            return self.store.prepare(self.release if release is None else release)

    def next_boot(self):
        self.boot.return_value = '22222222-2222-2222-2222-222222222222'

    def test_new_install_uses_flat_root(self):
        provider = self.prepare()
        self.assertEqual(provider, self.data / 'proton/components/fsr/26.10.2/amdxcffx64.dll')
        self.assertFalse(self.legacy.root.exists())
        self.assertFalse(provider.is_symlink())
        self.download.assert_called_once()

    def test_current_boot_legacy_cache_is_reused_without_copy_or_download(self):
        provider = self.seed(self.legacy)
        inode = provider.stat().st_ino
        self.assertEqual(self.prepare(), provider)
        self.assertEqual(provider.stat().st_ino, inode)
        self.assertFalse((self.store.root / self.release['version']).exists())
        self.download.assert_not_called()
        self.extract.assert_not_called()

    def test_next_boot_moves_versions_and_state_without_copying(self):
        provider = self.seed(self.legacy)
        inode = provider.stat().st_ino
        metadata = (provider.parent / 'metadata.json').read_bytes()
        self.next_boot()
        selected = self.prepare()
        self.assertEqual(selected, self.store.root / '26.10.2/amdxcffx64.dll')
        self.assertEqual(selected.stat().st_ino, inode)
        self.assertEqual((selected.parent / 'metadata.json').read_bytes(), metadata)
        self.assertFalse(provider.exists())
        self.assertTrue((self.legacy.root / '.lock').is_file())
        self.assertTrue((self.legacy.root / '.retention.lock').is_file())
        last_good = json.loads((self.store.root / '.last-good.json').read_text())
        self.assertEqual(last_good, self.release)
        self.assertEqual(json.loads((self.store.root / '.retention.json').read_text())['boot_id'], self.boot.return_value)
        self.download.assert_not_called()

    def test_legacy_cache_without_usage_record_defers_until_reboot(self):
        provider = self.seed(self.legacy, record=False)
        self.assertEqual(self.prepare(), provider)
        state = json.loads((self.legacy.root / '.retention.json').read_text())
        self.assertIn(self.release['version'], state['used'])
        self.next_boot()
        self.assertEqual(self.prepare(), self.store.root / '26.10.2/amdxcffx64.dll')
        self.download.assert_not_called()

    def test_unknown_or_corrupt_boot_record_never_moves_live_paths(self):
        provider = self.seed(self.legacy)
        storage.atomic_json(self.legacy.root / '.retention.json', {'boot_id': 'corrupt'})
        self.assertEqual(self.prepare(), provider)
        self.assertTrue(provider.is_file())
        self.download.assert_not_called()

    def test_unavailable_update_can_fall_back_to_legacy_release(self):
        provider = self.seed(self.legacy)
        requested = {**self.release, 'version': '26.11.1'}
        self.download.side_effect = OSError('offline')
        with self.assertRaises(OSError) as failure:
            self.prepare(requested)
        selected = self.store.fallback(requested, failure.exception, log=lambda _: None)
        self.assertEqual(selected, provider.parent)
        self.assertTrue(provider.is_file())

    def test_offline_update_after_migration_uses_flat_fallback(self):
        self.seed(self.legacy)
        requested = {**self.release, 'version': '26.11.1'}
        self.next_boot()
        self.download.side_effect = OSError('offline')
        with self.assertRaises(OSError) as failure:
            self.prepare(requested)
        self.assertEqual(self.store.fallback(requested, failure.exception, log=lambda _: None),
                         self.store.root / self.release['version'])

    def test_existing_flat_release_wins_without_overwriting_legacy_collision(self):
        old = self.seed(self.legacy)
        old_bytes, old_inode = old.read_bytes(), old.stat().st_ino
        flat_release = deepcopy(self.release)
        flat_payload = b'a different verified flat provider'
        flat_release['provider'].update(size=len(flat_payload), sha256=hashlib.sha256(flat_payload).hexdigest())
        flat = self.seed(self.store, flat_release, flat_payload)
        self.next_boot()
        self.assertEqual(self.prepare(flat_release), flat)
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(old.stat().st_ino, old_inode)
        self.assertEqual(flat.read_bytes(), flat_payload)
        self.download.assert_not_called()

    def test_failed_migration_still_reuses_verified_legacy_cache(self):
        provider = self.seed(self.legacy)
        self.next_boot()
        with mock.patch.object(storage.os, 'replace', side_effect=PermissionError('read only')):
            self.assertEqual(self.prepare(), provider)
        self.download.assert_not_called()
        self.assertTrue(provider.is_file())

    def test_missing_boot_id_defers_migration_without_redownload(self):
        provider = self.seed(self.legacy)
        self.boot.return_value = None
        self.assertEqual(self.prepare(), provider)
        self.assertTrue(provider.is_file())
        self.download.assert_not_called()

    def test_saved_explicit_legacy_path_follows_a_migrated_verified_release(self):
        original = self.seed(self.legacy)
        self.next_boot()
        selected = self.prepare()
        env = {**self.env, 'WINE_FSR4_PROVIDER': str(original),
               'PROTON_UPSCALER_UPGRADE': '0'}
        self.assertTrue(fsr.setup(env, gpu_eligible=lambda: self.fail('GPU queried'), log=lambda _: None))
        self.assertEqual(env['WINE_FSR4_PROVIDER'], str(selected))
        self.download.assert_not_called()

    def test_existing_explicit_legacy_path_is_preserved(self):
        original = self.seed(self.legacy)
        env = {**self.env, 'WINE_FSR4_PROVIDER': str(original)}
        self.assertTrue(fsr.setup(env, gpu_eligible=lambda: self.fail('GPU queried'), log=lambda _: None))
        self.assertEqual(env['WINE_FSR4_PROVIDER'], str(original))
        self.assertTrue(original.is_file())
        self.download.assert_not_called()

    def test_corrupted_flat_release_is_not_used_to_rewrite_an_explicit_path(self):
        original = self.seed(self.legacy)
        self.next_boot()
        selected = self.prepare()
        selected.write_bytes(b'corrupted')
        self.assertEqual(fsr._provider_override(self.env, str(original)), str(original))

    def test_migration_does_not_move_unrelated_sdk_metadata(self):
        self.seed(self.legacy)
        directory = self.legacy.root / 'custom-sdk'
        directory.mkdir()
        (directory / 'custom.dll').write_bytes(b'x')
        storage.atomic_json(directory / 'metadata.json', {'schema': 1, 'version': 'custom-sdk',
                            'files': {'custom.dll': {'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()}}})
        self.next_boot()
        self.assertEqual(self.prepare().parent.parent, self.store.root)
        self.assertTrue((directory / 'custom.dll').is_file())
        self.assertFalse((self.store.root / 'custom-sdk').exists())

    def test_legacy_symlink_is_not_modified_or_reused(self):
        outside = self.data / 'external'
        outside.mkdir()
        (outside / 'keep').write_text('preserve')
        self.store.root.mkdir(parents=True)
        self.legacy.root.symlink_to(outside, target_is_directory=True)
        provider = self.prepare()
        self.assertEqual(provider.parent.parent, self.store.root)
        self.assertEqual((outside / 'keep').read_text(), 'preserve')
        self.assertTrue(self.legacy.root.is_symlink())


if __name__ == '__main__':
    unittest.main()
