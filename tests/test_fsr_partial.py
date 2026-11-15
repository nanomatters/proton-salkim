"""Pinned driver ranges, HTTP fallbacks and real 7-Zip extraction."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

from upscalers import fsr, storage


def part(data, offset, size):
    return {"offset": offset, "size": size,
            "sha256": hashlib.sha256(data[offset:offset + size]).hexdigest()}


class PartialDriverDownload(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data = bytes(range(256)) * 8
        self.entry = {
            "version": "26.10.2", "architecture": "x86_64", "release_notes": "https://www.amd.com/",
            "archive": {"url": "https://drivers.amd.com/driver.exe", "size": len(self.data),
                        "sha256": hashlib.sha256(self.data).hexdigest(),
                        "ranges": [part(self.data, 0, 64), part(self.data, 512, 256),
                                   part(self.data, len(self.data) - 32, 32)]},
            "provider": {"member": "driver/amdxcffx64.dll", "size": 1, "sha256": "0" * 64},
        }
        self.requests = []
        self.responses = []

    def respond(self, request, **_options):
        self.requests.append(request)
        value = request.get_header("Range")
        if value:
            start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", value).groups())
            response = io.BytesIO(self.data[start:end + 1])
            response.status = 206
            response.headers = {"Content-Range": f"bytes {start}-{end}/{len(self.data)}",
                                "Content-Length": str(end - start + 1)}
        else:
            response = io.BytesIO(self.data)
            response.status = 200
            response.headers = {"Content-Length": str(len(self.data))}
        response.geturl = lambda: request.full_url
        self.responses.append(response)
        return response

    def test_only_requested_ranges_are_written_and_progress_counts_downloaded_bytes(self):
        updates = []
        with mock.patch.object(storage.urllib.request, "urlopen", side_effect=self.respond):
            storage.download(self.entry, self.root / "driver", lambda *args: updates.append(args))
        ranges = self.entry["archive"]["ranges"]
        expected = bytearray(len(self.data))
        for item in ranges:
            start, end = item["offset"], item["offset"] + item["size"]
            expected[start:end] = self.data[start:end]
        self.assertEqual((self.root / "driver").read_bytes(), expected)
        self.assertEqual([request.get_header("Range") for request in self.requests],
                         [f"bytes={item['offset']}-{item['offset'] + item['size'] - 1}" for item in ranges])
        total = sum(item["size"] for item in ranges)
        self.assertEqual(updates[-1], ("download", total, total))
        self.assertTrue(all(done <= size for _, done, size in updates))
        self.assertTrue(all(response.closed for response in self.responses))

    def test_ignored_range_is_closed_without_reading_and_retried_as_a_full_download(self):
        def respond(request, **options):
            response = self.respond(request, **options)
            if request.get_header("Range"):
                response.status = 200
                response.read1 = mock.Mock(side_effect=AssertionError("read an ignored range"))
            return response
        with mock.patch.object(storage.urllib.request, "urlopen", side_effect=respond):
            storage.download(self.entry, self.root / "driver")
        self.assertEqual([request.get_header("Range") for request in self.requests], ["bytes=0-63", None])
        self.assertEqual((self.root / "driver").read_bytes(), self.data)
        self.assertTrue(all(response.closed for response in self.responses))

    def test_range_http_rejections_fall_back_to_a_verified_complete_archive(self):
        for status in (400, 405, 416, 501):
            with self.subTest(status=status):
                self.requests.clear()
                body = io.BytesIO(b"error")
                def respond(request, **options):
                    if request.get_header("Range"):
                        self.requests.append(request)
                        raise urllib.error.HTTPError(request.full_url, status, "no ranges", {}, body)
                    return self.respond(request, **options)
                with mock.patch.object(storage.urllib.request, "urlopen", side_effect=respond):
                    storage.download(self.entry, self.root / "driver")
                self.assertTrue(body.closed)
                self.assertEqual(len(self.requests), 2)
                self.assertEqual((self.root / "driver").read_bytes(), self.data)

    def test_later_range_rejection_restarts_the_complete_download(self):
        def respond(request, **options):
            response = self.respond(request, **options)
            if request.get_header("Range") and len(self.requests) == 2:
                response.status = 200
            return response
        with mock.patch.object(storage.urllib.request, "urlopen", side_effect=respond):
            storage.download(self.entry, self.root / "driver")
        self.assertEqual([request.get_header("Range") for request in self.requests],
                         ["bytes=0-63", "bytes=512-767", None])
        self.assertEqual((self.root / "driver").read_bytes(), self.data)

    def test_full_fallback_still_rejects_an_incorrect_archive_checksum(self):
        self.entry["archive"]["sha256"] = "0" * 64
        def respond(request, **options):
            response = self.respond(request, **options)
            if request.get_header("Range"):
                response.status = 200
            return response
        with mock.patch.object(storage.urllib.request, "urlopen", side_effect=respond), \
                self.assertRaisesRegex(storage.ComponentError, "does not match"):
            storage.download(self.entry, self.root / "driver")

    def test_invalid_partial_headers_are_rejected_without_reading_the_body(self):
        for header, value in (("Content-Range", None), ("Content-Range", "bytes 1-64/2048"),
                              ("Content-Range", "bytes 0-63/2049"), ("Content-Length", "65"),
                              ("Content-Encoding", "gzip")):
            with self.subTest(header=header, value=value):
                self.requests.clear()
                def respond(request, **options):
                    response = self.respond(request, **options)
                    if value is None:
                        response.headers.pop(header, None)
                    else:
                        response.headers[header] = value
                    response.read1 = mock.Mock(side_effect=AssertionError("read invalid partial response"))
                    return response
                with mock.patch.object(storage.urllib.request, "urlopen", side_effect=respond), \
                        self.assertRaisesRegex(storage.ComponentError, "Invalid partial"):
                    storage.download(self.entry, self.root / "driver")
                self.assertEqual(len(self.requests), 1)
                self.assertTrue(self.responses[-1].closed)

    def test_partial_response_without_content_length_is_allowed(self):
        def respond(request, **options):
            response = self.respond(request, **options)
            response.headers.pop("Content-Length", None)
            return response
        with mock.patch.object(storage.urllib.request, "urlopen", side_effect=respond):
            storage.download(self.entry, self.root / "driver")
        self.assertEqual(len(self.requests), 3)

    def test_short_oversized_and_corrupt_ranges_do_not_trigger_full_download(self):
        for replacement in (self.data[:63], self.data[:65], b"x" * 64):
            with self.subTest(size=len(replacement)):
                self.requests.clear()
                def respond(request, **options):
                    response = self.respond(request, **options)
                    response.seek(0)
                    response.truncate()
                    response.write(replacement)
                    response.seek(0)
                    return response
                with mock.patch.object(storage.urllib.request, "urlopen", side_effect=respond), \
                        self.assertRaises(storage.ComponentError):
                    storage.download(self.entry, self.root / "driver")
                self.assertEqual(len(self.requests), 1)

    def test_unrecognised_redirect_is_rejected_before_a_full_retry(self):
        def respond(request, **options):
            response = self.respond(request, **options)
            response.status = 200
            response.geturl = lambda: "https://example.com/driver.exe"
            return response
        with mock.patch.object(storage.urllib.request, "urlopen", side_effect=respond), \
                self.assertRaisesRegex(storage.ComponentError, "Unrecognised"):
            storage.download(self.entry, self.root / "driver")
        self.assertEqual(len(self.requests), 1)

    def test_cancellation_never_falls_back_or_requests_another_range(self):
        for threshold in (0, 64):
            with self.subTest(threshold=threshold):
                self.requests.clear()
                with mock.patch.object(storage.urllib.request, "urlopen", side_effect=self.respond), \
                        self.assertRaisesRegex(storage.ComponentError, "cancelled"):
                    storage.download(self.entry, self.root / "driver", lambda _, done, total: done < threshold)
                self.assertEqual(len(self.requests), int(threshold != 0))

    def test_invalid_range_manifests_fail_before_network_access(self):
        valid = self.entry["archive"]["ranges"]
        invalid = [[], {}, [None], [{**valid[0], "offset": -1}], [{**valid[0], "offset": True}],
                   [{**valid[0], "size": 0}], [{**valid[0], "size": True}],
                   [{**valid[0], "offset": len(self.data)}], [{**valid[0], "sha256": "invalid"}],
                   [{**valid[0], "sha256": None}], [valid[0], valid[0]], list(reversed(valid)),
                   [part(self.data, 0, len(self.data))]]
        for ranges in invalid:
            with self.subTest(ranges=ranges):
                self.entry["archive"]["ranges"] = ranges
                with mock.patch.object(storage.urllib.request, "urlopen") as request, \
                        self.assertRaisesRegex(storage.ComponentError, "ranges"):
                    storage.download(self.entry, self.root / "driver")
                request.assert_not_called()

    def test_catalog_validates_ranges_before_selecting_the_release(self):
        self.entry["archive"]["ranges"][0]["offset"] = -1
        catalog = {"schema": 1, "default": self.entry["version"],
                   "releases": {self.entry["version"]: self.entry}}
        with mock.patch.object(fsr.Path, "read_text", return_value=json.dumps(catalog)), \
                self.assertRaisesRegex(storage.ComponentError, "ranges"):
            fsr.catalog()

    def make_archive(self):
        program = shutil.which("7zz") or shutil.which("7z")
        if not program:
            self.skipTest("7-Zip is unavailable")
        member = self.root / self.entry["provider"]["member"]
        member.parent.mkdir()
        payload = b"a provider fixture\0" + bytes(range(256)) * 8
        member.write_bytes(payload)
        (self.root / "unrelated.bin").write_bytes(b"unrelated archive content" * 4096)
        archive = self.root / "fixture.7z"
        subprocess.run([program, "a", "-t7z", "-mx=0", "-ms=off", "-mhc=off", str(archive),
                        "driver", "unrelated.bin"], cwd=self.root, check=True, stdout=subprocess.DEVNULL)
        self.data = archive.read_bytes()
        offset = self.data.index(payload, 32)
        header = 32 + struct.unpack_from("<Q", self.data, 12)[0]
        self.entry["provider"].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        self.entry["archive"].update(size=len(self.data), sha256=hashlib.sha256(self.data).hexdigest(),
                                     ranges=[part(self.data, 0, 32), part(self.data, offset, len(payload)),
                                             part(self.data, header, len(self.data) - header)])
        return payload

    @contextmanager
    def local_server(self, ignore_ranges=False):
        data, requests = self.data, self.requests
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                value = self.headers.get("Range")
                requests.append(value)
                if value and not ignore_ranges:
                    start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", value).groups())
                    body = data[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                else:
                    body = data
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        urlopen = urllib.request.urlopen

        def connect(request, **options):
            local = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/driver.exe",
                                           headers=dict(request.header_items()))
            response = urlopen(local, **options)
            response.geturl = lambda: request.full_url
            return response

        try:
            with mock.patch.object(storage.urllib.request, "urlopen", side_effect=connect):
                yield
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_real_http_ranges_and_archive_publish_only_the_verified_provider(self):
        payload = self.make_archive()
        store = storage.Store({"XDG_DATA_HOME": str(self.root / "cache")})
        with self.local_server(), store.lock():
            provider = store.prepare(self.entry)
        self.assertEqual(provider.read_bytes(), payload)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual({p.name for p in provider.parent.iterdir()},
                         {"amdxcffx64.dll", "metadata.json"})
        self.assertFalse(list(store.root.glob(".prepare-*")))
        with store.lock(), mock.patch.object(storage, "download", side_effect=AssertionError("downloaded cache")):
            self.assertEqual(store.prepare(self.entry), provider)

    def test_real_http_full_fallback_extracts_and_removes_the_archive(self):
        payload = self.make_archive()
        store = storage.Store({"XDG_DATA_HOME": str(self.root / "cache")})
        with self.local_server(ignore_ranges=True), store.lock():
            provider = store.prepare(self.entry)
        self.assertEqual(provider.read_bytes(), payload)
        self.assertEqual(self.requests, ["bytes=0-31", None])
        self.assertFalse(list(store.root.glob(".prepare-*")))

    def test_failed_partial_update_preserves_and_selects_the_previous_provider(self):
        payload = self.make_archive()
        env = {"XDG_DATA_HOME": str(self.root / "cache"), "PROTON_UPSCALER_UPGRADE": "1"}
        store = storage.Store(env)
        with store.lock():
            previous = store.prepare(self.entry, archive=self.root / "fixture.7z")
        newer = deepcopy(self.entry)
        newer["version"] = "26.11.1"
        newer["archive"]["ranges"][0]["sha256"] = "0" * 64
        catalog = {"schema": 1, "default": newer["version"], "releases": {newer["version"]: newer}}
        with mock.patch.object(fsr, "catalog", return_value=catalog), \
                mock.patch.object(storage.urllib.request, "urlopen", side_effect=self.respond):
            self.assertTrue(fsr.setup(env, gpu_eligible=True, log=lambda message: None))
        self.assertEqual(Path(env["WINE_FSR4_PROVIDER"]), previous)
        self.assertEqual(previous.read_bytes(), payload)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((store.root / newer["version"]).exists())
        self.assertFalse(list(store.root.glob(".prepare-*")))


if __name__ == "__main__":
    unittest.main()
