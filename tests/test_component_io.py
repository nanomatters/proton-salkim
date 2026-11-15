"""Bounded optional component I/O and publication across filesystems."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request

from upscalers import storage, sevenzip


class ComponentIO(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_minimum_progress_stops_a_live_trickle_without_waiting_for_a_full_chunk(self):
        class Trickle(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                        time.sleep(.01)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Trickle)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        worker.start()
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/' % server.server_port, timeout=1) as response:
                start = time.monotonic()
                with mock.patch.object(storage, 'DOWNLOAD_PROGRESS_WINDOW', .08), \
                        self.assertRaisesRegex(storage.ComponentError, 'stalled'):
                    list(storage.response_chunks(response))
                self.assertLess(time.monotonic() - start, .8)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_slow_healthy_transfer_has_no_total_duration_limit(self):
        response = io.BytesIO(b'payload')
        response.read1 = mock.Mock(side_effect=[b'x' * 1024] * 50 + [b''])
        # Forty-nine minutes of progress exceeds the old fifteen-minute deadline.
        with mock.patch.object(storage.time, 'monotonic', side_effect=range(0, 60 * 100, 60)):
            self.assertEqual(sum(map(len, storage.response_chunks(response))), 50 * 1024)

    def test_connection_without_progress_still_propagates_socket_timeout(self):
        response = io.BytesIO(b'payload')
        response.geturl = lambda: 'https://drivers.amd.com/test.exe'
        response.read1 = mock.Mock(side_effect=TimeoutError('socket timed out'))
        with mock.patch.object(storage.urllib.request, 'urlopen', return_value=response) as connect:
            with self.assertRaises(TimeoutError):
                storage.download_file({'url': response.geturl(), 'size': 7, 'sha256': 'unused'},
                                      self.root / 'download', hosts=('drivers.amd.com',))
        self.assertEqual(connect.call_args.kwargs['timeout'], 30)
        self.assertTrue(response.closed)

    def test_sevenzip_download_uses_the_same_progress_check_and_keeps_old_archive(self):
        archive = self.root / '7zip.tar.xz'
        archive.write_bytes(b'old archive')
        response = io.BytesIO(b'new')
        with mock.patch.object(sevenzip.urllib.request, 'urlopen', return_value=response), \
                mock.patch.object(storage, 'response_chunks', side_effect=storage.ComponentError('stalled')):
            with self.assertRaisesRegex(storage.ComponentError, 'stalled'):
                sevenzip._download('https://example.com/7zip', archive, 'unused')
        self.assertEqual(archive.read_bytes(), b'old archive')
        self.assertEqual(list(self.root.iterdir()), [archive])
        self.assertTrue(response.closed)

    def test_stalled_extractor_is_killed_before_eof(self):
        program = self.root / 'extractor'
        program.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(30)\n')
        program.chmod(0o700)
        started = []
        popen = subprocess.Popen

        def spawn(*args, **kwargs):
            process = popen(*args, **kwargs)
            started.append(process)
            return process

        release = {'provider': {'member': 'provider.dll', 'size': 7,
                                'sha256': hashlib.sha256(b'payload').hexdigest()}}
        start = time.monotonic()
        with mock.patch.object(storage, 'EXTRACT_TIMEOUT', .1), \
                mock.patch.object(storage.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaisesRegex(storage.ComponentError, 'extraction timed out'):
                storage.extract(self.root / 'archive', release, self.root / 'provider', program=program)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].returncode)
        self.assertTrue(started[0].stdout.closed)

    def test_same_filesystem_publication_renames_without_copying(self):
        target = self.root / 'install'
        source = self.root / 'sdk'
        source.mkdir()
        payload = source / 'library'
        payload.write_bytes(b'new')
        inode = payload.stat().st_ino
        with mock.patch.object(storage.shutil, 'copytree', side_effect=AssertionError('copied local data')):
            storage.publish(source, target)
        self.assertEqual((target / 'library').stat().st_ino, inode)
        self.assertEqual(list(self.root.iterdir()), [target])

    @unittest.skipUnless(os.path.isdir('/dev/shm') and os.access('/dev/shm', os.W_OK),
                         'A second writable filesystem is required')
    def test_cross_filesystem_copy_failure_keeps_previous_install(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm') as directory:
            source = Path(directory) / 'sdk'
            source.mkdir()
            (source / 'library').write_bytes(b'new')
            if source.stat().st_dev == self.root.stat().st_dev:
                self.skipTest('No separate filesystem available')
            target = self.root / 'install'
            target.mkdir()
            (target / 'library').write_bytes(b'old')
            with mock.patch.object(storage.shutil, 'copytree', side_effect=OSError('no space')):
                with self.assertRaises(OSError):
                    storage.publish(source, target)
            self.assertEqual((target / 'library').read_bytes(), b'old')
            self.assertEqual(list(self.root.iterdir()), [target])
            storage.publish(source, target)
            self.assertEqual((target / 'library').read_bytes(), b'new')

    def test_locked_cleanup_removes_abandoned_downloads_without_following_links(self):
        stage = self.root / '.prepare-abandoned'
        stage.mkdir()
        (stage / 'large-partial-download').write_bytes(b'partial')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep').write_bytes(b'keep')
        (stage / 'link').symlink_to(outside, target_is_directory=True)
        linked_stage = self.root / '.prepare-link'
        linked_stage.symlink_to(outside, target_is_directory=True)
        with storage.locked(self.root / '.lock'):
            self.assertFalse(stage.exists())
            self.assertFalse(linked_stage.is_symlink())
        self.assertEqual((outside / 'keep').read_bytes(), b'keep')

    def test_cleanup_recovers_interrupted_publication_before_deleting_stage(self):
        for journal in (True, False):
            with self.subTest(journal=journal):
                stage = self.root / '.prepare-publish'
                previous = stage / 'previous'
                previous.mkdir(parents=True)
                (previous / 'library').write_bytes(b'old')
                storage.atomic_json(previous / 'metadata.json', {'version': '1.0'})
                if journal:
                    storage.atomic_json(stage / 'restore.json', {'target': '1.0'})
                with storage.locked(self.root / '.lock'):
                    self.assertEqual((self.root / '1.0/library').read_bytes(), b'old')
                    self.assertFalse(stage.exists())
                storage.shutil.rmtree(self.root / '1.0')

    def test_cleanup_preserves_unrecoverable_backups_and_unsafe_journals(self):
        stage = self.root / '.prepare-publish'
        previous = stage / 'previous'
        previous.mkdir(parents=True)
        (previous / 'library').write_bytes(b'old')
        for journal in ({'target': '../outside'}, {'target': '/tmp/outside'}, {}):
            storage.atomic_json(stage / 'restore.json', journal)
            with storage.locked(self.root / '.lock'):
                self.assertEqual((previous / 'library').read_bytes(), b'old')
        storage.atomic_json(stage / 'restore.json', {'target': '1.0'})
        with mock.patch.object(storage.os, 'replace', side_effect=PermissionError('read only')):
            with storage.locked(self.root / '.lock'):
                self.assertEqual((previous / 'library').read_bytes(), b'old')

    def test_failed_rollback_preserves_backup_for_next_launch(self):
        target = self.root / '1.0'
        target.mkdir()
        (target / 'library').write_bytes(b'old')
        source = self.root / 'sdk'
        source.mkdir()
        (source / 'library').write_bytes(b'new')
        replace = storage.os.replace

        def fail_after_backup(old, new):
            if Path(old) in (source,) or Path(old).name == 'previous':
                raise PermissionError('cannot publish or roll back')
            return replace(old, new)

        with mock.patch.object(storage.os, 'replace', side_effect=fail_after_backup):
            with self.assertRaises(PermissionError):
                storage.publish(source, target)
        self.assertFalse(target.exists())
        backups = list(self.root.glob('.prepare-*/previous/library'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'old')
        with storage.locked(self.root / '.lock'):
            self.assertEqual((target / 'library').read_bytes(), b'old')
            self.assertFalse(list(self.root.glob('.prepare-*')))

    def test_cleanup_runs_before_reusing_a_valid_component(self):
        root = self.root / 'proton/components/dlss'
        stage = root / '.prepare-abandoned'
        stage.mkdir(parents=True)
        (stage / 'archive').write_bytes(b'partial')
        target = root / '1.0'
        target.mkdir()
        payload = b'valid'
        (target / 'library.dll').write_bytes(payload)
        manifest = {'version': '1.0', 'files': {'library.dll': {
            'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}}}
        with mock.patch.object(storage, 'download_file', side_effect=AssertionError('downloaded valid cache')):
            self.assertEqual(storage.prepare_libraries({'XDG_DATA_HOME': str(self.root)}, 'dlss', manifest), target)
        self.assertFalse(stage.exists())



if __name__ == '__main__':
    unittest.main()
