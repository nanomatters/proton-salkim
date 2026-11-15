"""Retain three recent releases without removing current-boot users."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from upscalers import storage, fsr


class ReleaseRetention(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.env = {'XDG_DATA_HOME': str(self.data)}
        self.root = self.data / 'proton/components/dlss'
        self.root.mkdir(parents=True)
        boot = mock.patch.object(storage, '_boot_id', return_value='11111111-1111-1111-1111-111111111111')
        self.boot = boot.start()
        self.addCleanup(boot.stop)

    def install(self, version):
        directory = self.root / version
        directory.mkdir()
        payload = version.encode()
        (directory / 'library.dll').write_bytes(payload)
        manifest = {'schema': 1, 'version': version, 'files': {'library.dll': {
            'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}}}
        storage.atomic_json(directory / 'metadata.json', manifest)
        os.utime(directory / 'metadata.json', ns=(int(version) * 1000000000,) * 2)
        return manifest

    def select(self, manifest):
        with storage.locked(self.root / '.lock'):
            storage._remember(self.root, manifest)

    def versions(self):
        return {path.name for path in self.root.iterdir() if path.name.isdigit() and path.is_dir()}

    def seed(self):
        manifests = {str(i): self.install(str(i)) for i in range(1, 6)}
        self.select(manifests['5'])
        return manifests

    def test_initial_migration_preserves_existing_releases_until_next_boot(self):
        manifests = self.seed()
        self.assertEqual(self.versions(), set(manifests))
        self.boot.return_value = '22222222-2222-2222-2222-222222222222'
        self.select(manifests['5'])
        self.assertEqual(self.versions(), {'3', '4', '5'})

    def test_old_file_lists_and_schemas_are_pruned_together(self):
        old = {str(i): self.install(str(i)) for i in range(1, 5)}
        self.select(old['4'])
        requested = {'schema': 2, 'version': '5', 'files': {
            **old['4']['files'], 'new-effect.dll': old['4']['files']['library.dll']}}
        # Old layouts remain ineligible for fallback, despite broader pruning.
        self.assertIsNone(storage.cached_fallback(self.root, requested, 'offline', lambda _: None))
        for i in range(5, 9):
            self.boot.return_value = f'2222222{i}-2222-2222-2222-222222222222'
            manifest = self.install(str(i))
            manifest['schema'] = 2
            manifest['files']['new-effect.dll'] = manifest['files']['library.dll']
            (self.root / str(i) / 'new-effect.dll').write_bytes(str(i).encode())
            storage.atomic_json(self.root / str(i) / 'metadata.json', manifest)
            self.select(manifest)
        self.assertEqual(self.versions(), {'6', '7', '8'})

    def test_old_provider_architectures_are_counted_for_retention(self):
        manifests = self.seed()
        for version, manifest in manifests.items():
            manifest['provider'] = manifest.pop('files')['library.dll']
            manifest.pop('schema')
            manifest['architecture'] = 'x86' if version in ('1', '2') else 'x86_64'
            storage.atomic_json(self.root / version / 'metadata.json', manifest)
        self.boot.return_value = '22222222-2222-2222-2222-222222222222'
        self.select(manifests['5'])
        self.assertEqual(self.versions(), {'3', '4', '5'})

    def test_invalid_file_names_do_not_make_a_custom_folder_prunable(self):
        manifests = self.seed()
        directory = self.root / 'custom'
        directory.mkdir()
        storage.atomic_json(directory / 'metadata.json', {
            'version': 'custom', 'files': {'../outside': manifests['5']['files']['library.dll']}})
        self.boot.return_value = '22222222-2222-2222-2222-222222222222'
        self.select(manifests['5'])
        self.assertTrue(directory.is_dir())

    def test_retention_uses_selection_order_instead_of_version_number(self):
        manifests = self.seed()
        self.boot.return_value = '22222222-2222-2222-2222-222222222222'
        self.select(manifests['1'])
        self.assertEqual(self.versions(), {'1', '4', '5'})

    def test_current_boot_fallback_can_temporarily_exceed_three(self):
        manifests = self.seed()
        self.boot.return_value = '22222222-2222-2222-2222-222222222222'
        fallback = storage.cached_fallback(self.root, manifests['1'], OSError('offline'), log=lambda _: None)
        self.assertEqual(fallback, self.root / '5')
        # Pin an explicit old cached release before a new launch selects current.
        storage.protect_managed(self.env, 'dlss', self.root / '1')
        self.select(manifests['5'])
        self.assertEqual(self.versions(), {'1', '4', '5'})
        self.select(manifests['4'])
        sixth = self.install('6')
        self.select(sixth)
        self.assertEqual(self.versions(), {'1', '4', '5', '6'})
        self.boot.return_value = '33333333-3333-3333-3333-333333333333'
        self.select(sixth)
        self.assertEqual(self.versions(), {'4', '5', '6'})

    def test_fallback_records_usage_without_pruning(self):
        manifests = self.seed()
        self.boot.return_value = '22222222-2222-2222-2222-222222222222'
        marker = self.root / '.last-good.json'
        storage.atomic_json(marker, manifests['1'])
        self.assertEqual(storage.cached_fallback(self.root, manifests['5'], 'offline', lambda _: None),
                         self.root / '1')
        self.assertEqual(self.versions(), set(manifests))
        state = json.loads((self.root / '.retention.json').read_text())
        self.assertIn('1', state['used'])

    def test_retention_guard_times_out_without_selecting_unprotected_release(self):
        manifests = self.seed()
        start = time.monotonic()
        with (self.root / '.retention.lock').open('a+b') as lock, \
                mock.patch.object(storage, 'RETENTION_LOCK_TIMEOUT', .02):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(storage.ComponentError):
                self.select(manifests['5'])
            self.assertIsNone(storage.cached_fallback(self.root, manifests['1'], 'offline', lambda _: None))
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(self.versions(), set(manifests))

    def test_brief_contention_allows_fallback_selection(self):
        manifests = self.seed()
        with (self.root / '.retention.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            timer = threading.Timer(.05, lambda: fcntl.flock(lock, fcntl.LOCK_UN))
            timer.start()
            try:
                self.assertEqual(storage.cached_fallback(self.root, manifests['1'], 'offline', lambda _: None),
                                 self.root / '5')
            finally:
                timer.join()

    def test_fallback_holds_guard_through_verification_and_usage_record(self):
        manifests = self.seed()
        original = storage.verified_files
        calls = []

        def check(directory, files):
            with (self.root / '.retention.lock').open('a+b') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            calls.append(directory)
            return original(directory, files)

        with mock.patch.object(storage, 'verified_files', side_effect=check):
            self.assertEqual(storage.cached_fallback(self.root, manifests['1'], 'offline', lambda _: None),
                             self.root / '5')
        self.assertTrue(calls)

    def test_unknown_custom_and_symlinked_directories_are_preserved(self):
        manifests = self.seed()
        outside = self.data / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('keep')
        (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        custom = self.root / 'custom'
        custom.mkdir()
        (custom / 'keep').write_text('keep')
        malformed = self.root / 'bad'
        malformed.mkdir()
        storage.atomic_json(malformed / 'metadata.json', {'version': '../outside', 'files': {}})
        self.boot.return_value = '22222222-2222-2222-2222-222222222222'
        self.select(manifests['5'])
        self.assertEqual((outside / 'keep').read_text(), 'keep')
        self.assertTrue((self.root / 'linked').is_symlink())
        self.assertEqual((custom / 'keep').read_text(), 'keep')
        self.assertTrue(malformed.is_dir())

    def test_failed_state_write_keeps_cache_usable_and_disables_pruning(self):
        manifests = self.seed()
        self.boot.return_value = '22222222-2222-2222-2222-222222222222'
        original = storage.atomic_json

        def readonly(path, value):
            if Path(path).name == '.retention.json':
                raise PermissionError('read only')
            return original(path, value)

        with mock.patch.object(storage, 'atomic_json', side_effect=readonly):
            self.select(manifests['5'])
            self.assertEqual(storage.cached_fallback(self.root, manifests['1'], 'offline', lambda _: None),
                             self.root / '5')
        self.assertEqual(self.versions(), set(manifests))

    def test_unavailable_retention_guard_cannot_race_cleanup(self):
        manifests = self.seed()
        original = Path.open

        def no_descriptors(path, *args, **kwargs):
            if path.name == '.retention.lock':
                raise OSError(errno.EMFILE, 'too many open files')
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'open', no_descriptors):
            self.assertIsNone(storage.cached_fallback(self.root, manifests['1'], 'offline', lambda _: None))
            with self.assertRaises(storage.ComponentError):
                self.select(manifests['5'])

    def test_invalid_boot_record_preserves_potential_current_users(self):
        manifests = self.seed()
        path = self.root / '.retention.json'
        state = json.loads(path.read_text())
        state['boot_id'] = 'invalid'
        storage.atomic_json(path, state)
        self.select(manifests['5'])
        self.assertEqual(self.versions(), set(manifests))

    def test_missing_boot_id_never_prunes(self):
        manifests = self.seed()
        self.boot.return_value = None
        self.select(manifests['5'])
        self.assertEqual(self.versions(), set(manifests))


    def test_explicit_fsr_provider_pins_cached_release_without_pruning(self):
        root = self.data / 'proton/components/fsr'
        manifests = {}
        for i in range(1, 6):
            version = str(i)
            directory = root / version
            directory.mkdir(parents=True)
            payload = version.encode()
            (directory / 'amdxcffx64.dll').write_bytes(payload)
            manifests[version] = {'version': version, 'architecture': 'x86_64', 'provider': {
                'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}}
            storage.atomic_json(directory / 'metadata.json', manifests[version])
            os.utime(directory / 'metadata.json', ns=(i * 1000000000,) * 2)
        with storage.locked(root / '.lock'):
            storage._remember(root, manifests['5'])
        self.boot.return_value = '22222222-2222-2222-2222-222222222222'
        provider = root / '1/amdxcffx64.dll'
        env = {**self.env, 'WINE_FSR4_PROVIDER': str(provider)}
        self.assertTrue(fsr.setup(env, gpu_eligible=lambda: self.fail('GPU queried'), log=lambda _: None))
        self.assertEqual(len(list(root.glob('*/metadata.json'))), 5)
        with storage.locked(root / '.lock'):
            storage._remember(root, manifests['5'])
        self.assertTrue(provider.is_file())
        self.assertEqual({path.parent.name for path in root.glob('*/metadata.json')}, {'1', '4', '5'})

    def test_last_good_release_is_preserved_even_outside_recent_three(self):
        manifests = self.seed()
        self.boot.return_value = '22222222-2222-2222-2222-222222222222'
        storage.atomic_json(self.root / '.last-good.json', manifests['1'])
        with storage._retention_guard(self.root):
            storage._retain_selected(self.root, manifests['5'])
        self.assertEqual(self.versions(), {'1', '3', '4', '5'})

    def test_explicit_provider_setup_survives_retention_lock_timeout(self):
        root = self.data / 'proton/components/fsr'
        directory = root / '26.10.2'
        directory.mkdir(parents=True)
        provider = directory / 'amdxcffx64.dll'
        provider.write_bytes(b'x')
        storage.atomic_json(directory / 'metadata.json', {'version': '26.10.2', 'architecture': 'x86_64',
                            'provider': {'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()}})
        env = {**self.env, 'WINE_FSR4_PROVIDER': str(provider), 'PROTON_MLFG_UPGRADE': '1'}
        with (root / '.retention.lock').open('a+b') as lock, \
                mock.patch.object(storage, 'RETENTION_LOCK_TIMEOUT', .02):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(fsr.setup(env, gpu_eligible=lambda: self.fail('GPU queried'), log=lambda _: None))
        self.assertEqual(env['WINE_FSR4_PROVIDER'], str(provider))
        self.assertEqual(env['MLFG_UPGRADE'], '1')

    def test_explicit_external_provider_is_not_modified(self):
        external = self.data / 'external'
        external.mkdir()
        (external / 'keep').write_text('keep')
        storage.protect_managed(self.env, 'dlss', external)
        self.assertEqual(list(external.iterdir()), [external / 'keep'])
        self.assertFalse((self.root / '.retention.json').exists())


if __name__ == '__main__':
    unittest.main()
