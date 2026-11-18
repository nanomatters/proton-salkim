"""Independent recovery cases for shared component provisioning."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from upscalers import storage, ffx, dlss, xess, progress


class SdkFallback(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {'XDG_DATA_HOME': str(self.root), "PROTON_UPSCALER_UPGRADE": "1"}

    def fixture(self, component, version):
        data = deepcopy(component.catalog())
        data['version'] = version
        payloads = {}
        for name, item in data['files'].items():
            content = (version + ':' + name).encode()
            item.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
            payloads[item['sha256']] = content
        return data, payloads

    def install(self, component, data, payloads):
        def download(item, path, *args, **kwargs):
            Path(path).write_bytes(payloads[item['sha256']])
        with mock.patch.object(component, 'catalog', return_value=data), \
                mock.patch.object(storage, 'download_file', side_effect=download):
            return component.prepare(self.env)

    def offline(self, component, requested):
        with mock.patch.object(component, 'catalog', return_value=requested), \
                mock.patch.object(storage, 'download_file', side_effect=OSError('offline')):
            return component.prepare(self.env)

    def test_all_sdks_reuse_previous_complete_release_after_new_download_fails(self):
        for component in (ffx, dlss, xess):
            with self.subTest(component=component.__name__):
                old, payloads = self.fixture(component, '1.0')
                previous = self.install(component, old, payloads)
                requested, _ = self.fixture(component, '2.0')
                self.assertEqual(self.offline(component, requested), previous)
                self.assertFalse((previous.parent / '2.0').exists())
                for name, item in old['files'].items():
                    self.assertEqual((previous / name).read_bytes(), payloads[item['sha256']])

    def test_partial_new_sdk_never_mixes_with_fallback(self):
        old, payloads = self.fixture(ffx, '1.0')
        previous = self.install(ffx, old, payloads)
        requested, new_payloads = self.fixture(ffx, '2.0')
        calls = []
        def partial(item, path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('connection dropped')
            Path(path).write_bytes(new_payloads[item['sha256']])
        with mock.patch.object(ffx, 'catalog', return_value=requested), \
                mock.patch.object(storage, 'download_file', side_effect=partial):
            self.assertEqual(ffx.prepare(self.env), previous)
        self.assertEqual(len(calls), 2)
        self.assertFalse(Path(calls[0]).exists())
        for name, item in old['files'].items():
            self.assertEqual((previous / name).read_bytes(), payloads[item['sha256']])

    def test_corrupt_last_selected_release_uses_earlier_verified_release(self):
        old, payloads = self.fixture(dlss, '1.0')
        previous = self.install(dlss, old, payloads)
        newer, payloads = self.fixture(dlss, '2.0')
        recent = self.install(dlss, newer, payloads)
        (recent / next(iter(newer['files']))).write_bytes(b'corrupt')
        requested, _ = self.fixture(dlss, '3.0')
        self.assertEqual(self.offline(dlss, requested), previous)

    def test_legacy_metadata_cache_works_without_last_good_record(self):
        old, payloads = self.fixture(xess, '1.0')
        previous = self.install(xess, old, payloads)
        (previous.parent / '.last-good.json').unlink(missing_ok=True)
        requested, _ = self.fixture(xess, '2.0')
        self.assertEqual(self.offline(xess, requested), previous)

    def test_read_only_last_good_record_does_not_disable_valid_cache(self):
        data, payloads = self.fixture(dlss, '1.0')
        previous = self.install(dlss, data, payloads)
        (previous.parent / '.last-good.json').unlink(missing_ok=True)
        original = storage.atomic_json
        def no_marker(path, value):
            if Path(path).name == '.last-good.json':
                raise OSError('read-only state')
            original(path, value)
        with mock.patch.object(storage, 'atomic_json', side_effect=no_marker), \
                mock.patch.object(dlss, 'catalog', return_value=data), \
                mock.patch.object(storage, 'download_file', side_effect=AssertionError('downloaded valid cache')):
            self.assertEqual(dlss.prepare(self.env), previous)

    def test_lock_contention_uses_old_cache_without_network_or_wait(self):
        old, payloads = self.fixture(ffx, '1.0')
        previous = self.install(ffx, old, payloads)
        requested, _ = self.fixture(ffx, '2.0')
        with storage.locked(previous.parent / '.lock'), \
                mock.patch.object(ffx, 'catalog', return_value=requested), \
                mock.patch.object(storage, 'download_file', side_effect=AssertionError('network under contention')):
            self.assertEqual(ffx.prepare(self.env), previous)

    def test_corrupt_or_missing_caches_do_not_become_fallbacks(self):
        old, payloads = self.fixture(xess, '1.0')
        previous = self.install(xess, old, payloads)
        (previous / next(iter(old['files']))).unlink()
        requested, _ = self.fixture(xess, '2.0')
        with self.assertRaises(OSError):
            self.offline(xess, requested)

    def test_disable_and_explicit_directory_do_not_use_cached_fallback(self):
        data, payloads = self.fixture(xess, '1.0')
        previous = self.install(xess, data, payloads)
        self.env['PROTON_UPSCALER_UPGRADE'] = '0'
        with mock.patch.object(xess, 'catalog', side_effect=AssertionError('manifest queried')):
            self.assertFalse(xess.setup(self.env, gpu_eligible=True))
            custom = self.root / 'custom'
            custom.mkdir()
            self.env['WINE_XESS_PROVIDER'] = str(custom)
            self.assertEqual(xess.prepare(self.env), custom)
        self.assertTrue(previous.exists())

    def test_last_selected_release_takes_precedence_over_newer_installation(self):
        old, payloads = self.fixture(ffx, '1.0')
        previous = self.install(ffx, old, payloads)
        newer, payloads = self.fixture(ffx, '2.0')
        self.install(ffx, newer, payloads)
        self.install(ffx, old, {})  # A later launch selected the older Proton build.
        requested, _ = self.fixture(ffx, '3.0')
        self.assertEqual(self.offline(ffx, requested), previous)

    def test_download_checksum_failure_uses_previous_complete_release(self):
        old, payloads = self.fixture(dlss, '1.0')
        previous = self.install(dlss, old, payloads)
        requested, payloads = self.fixture(dlss, '2.0')
        item = next(iter(requested['files'].values()))
        response = io.BytesIO(b'x' * item['size'])
        response.geturl = lambda: item['url']
        with mock.patch.object(dlss, 'catalog', return_value=requested), \
                mock.patch.object(storage.urllib.request, 'urlopen', return_value=response):
            self.assertEqual(dlss.prepare(self.env), previous)
        self.assertFalse((previous.parent / '2.0').exists())

    def test_invalid_saved_manifests_never_supply_a_fallback(self):
        old, payloads = self.fixture(xess, '1.0')
        previous = self.install(xess, old, payloads)
        metadata = previous / 'metadata.json'
        marker = previous.parent / '.last-good.json'
        requested, _ = self.fixture(xess, '2.0')
        first = next(iter(old['files']))
        cases = [[], {**old, 'version': '../outside'}, {**old, 'version': '/tmp/outside'},
                 {**old, 'files': {**old['files'], '../escape.dll': old['files'][first]}},
                 {**old, 'files': {first: {**old['files'][first], 'size': True}}}]
        for case in cases:
            with self.subTest(manifest=case):
                metadata.write_text(json.dumps(case))
                marker.write_text(json.dumps(case))
                with self.assertRaises(OSError):
                    self.offline(xess, requested)

    def test_cancelled_update_keeps_verified_release_and_skips_other_downloads(self):
        old, payloads = self.fixture(ffx, '1.0')
        previous = self.install(ffx, old, payloads)
        requested, _ = self.fixture(ffx, '2.0')
        with mock.patch.object(progress.Progress, '_cancelled', True), \
                mock.patch.object(storage.urllib.request, 'urlopen') as network, \
                mock.patch.object(ffx, 'catalog', return_value=requested):
            with progress.Progress() as window:
                self.assertEqual(ffx.prepare(self.env, window.update), previous)
            with progress.Progress(component='Intel XeSS') as window:
                with self.assertRaisesRegex(storage.ComponentError, 'cancelled'):
                    xess.prepare({'XDG_DATA_HOME': str(self.root / 'other'), 'PROTON_UPSCALER_UPGRADE': '1'}, window.update)
            network.assert_not_called()

    def test_symlinked_old_release_is_not_used(self):
        data, payloads = self.fixture(ffx, '1.0')
        previous = self.install(ffx, data, payloads)
        outside = self.root / 'outside'
        previous.rename(outside)
        previous.symlink_to(outside, target_is_directory=True)
        requested, _ = self.fixture(ffx, '2.0')
        with self.assertRaises(OSError):
            self.offline(ffx, requested)


if __name__ == '__main__':
    unittest.main()
