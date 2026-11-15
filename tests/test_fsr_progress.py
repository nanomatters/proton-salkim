"""Exercise the existing Zenity progress executable without starting a game."""

import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from upscalers import progress


ROOT = Path(__file__).resolve().parents[1]
SMOKE_ZENITY = next((path for path in (
    ROOT / "protonfixes/files/bin/zenity",
    ROOT.parent / "build/dist/protonfixes/files/bin/zenity",
) if path.is_file() and os.access(path, os.X_OK)), None)


class BinarySelectionTests(unittest.TestCase):
    def setUp(self):
        cancelled = mock.patch.object(progress.Progress, "_cancelled", False)
        cancelled.start()
        self.addCleanup(cancelled.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = mock.patch.object(progress, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.program = self.root / "protonfixes/files/bin/zenity"
        self.program.parent.mkdir(parents=True)

    def test_executable_bundled_binary_is_preferred(self):
        self.program.write_text("#!/bin/sh\nexit 0\n")
        self.program.chmod(0o755)
        self.assertEqual(progress._zenity_binary(), str(self.program))

    def test_missing_or_nonexecutable_bundle_does_not_use_a_different_gui(self):
        for exists in (False, True):
            with self.subTest(exists=exists):
                if exists:
                    self.program.write_text("not executable")
                    self.program.chmod(0o644)
                self.assertIsNone(progress._zenity_binary())

    def test_executable_receives_zenity_options_and_progress_protocol(self):
        output = self.root / "received.json"
        self.program.write_text("#!" + sys.executable + "\nimport json, pathlib, sys\n"
                                "pathlib.Path(" + repr(str(output)) + ").write_text(json.dumps("
                                "{'args': sys.argv[1:], 'input': sys.stdin.read()}))\n")
        self.program.chmod(0o755)
        with mock.patch.dict(os.environ, {"DISPLAY": ":test"}), progress.Progress() as window:
            window.update("download", 10, 10)
            window.update("extract", 10, 10)
        received = json.loads(output.read_text())
        self.assertIn("--progress", received["args"])
        self.assertNotIn("--no-cancel", received["args"])
        self.assertIn("--cancel-label=Skip updates", received["args"])
        self.assertIn("--auto-close", received["args"])
        self.assertNotIn("--auto-kill", received["args"])
        self.assertEqual([int(line) for line in received["input"].splitlines() if line.isdigit()],
                         [90, 99, 100])
        self.assertEqual(window.process.returncode, 0)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        cancelled = mock.patch.object(progress.Progress, "_cancelled", False)
        cancelled.start()
        self.addCleanup(cancelled.stop)
        self.readers = []
        self.addCleanup(lambda: [os.close(reader) for reader in self.readers])
        patcher = mock.patch.dict(os.environ, {"DISPLAY": ":test"})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(progress, "_zenity_binary", return_value="/bundled/zenity")
        self.resolver = patcher.start()
        self.addCleanup(patcher.stop)

    def process(self, **kwargs):
        reader, writer = os.pipe()
        self.readers.append(reader)
        stdin = os.fdopen(writer, "wb")
        self.addCleanup(stdin.close)
        return SimpleNamespace(stdin=stdin, poll=mock.Mock(return_value=None),
                               wait=mock.Mock(return_value=0), terminate=mock.Mock(), kill=mock.Mock(), **kwargs)

    def test_progress_is_lazy_and_uses_the_existing_binary_directly(self):
        process = self.process()
        with mock.patch.object(subprocess, "Popen", return_value=process) as popen:
            with progress.Progress() as window:
                popen.assert_not_called()
                window.update("download", 1, 100)
                self.assertFalse(os.get_blocking(process.stdin.fileno()))
        args = popen.call_args.args[0]
        self.assertEqual(args[0], "/bundled/zenity")
        self.assertIn("--percentage=0", args)
        self.assertEqual(popen.call_args.kwargs, {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL,
                                                "stderr": subprocess.DEVNULL})
        self.resolver.assert_called_once()

    def test_dlss_uses_its_own_window_label(self):
        process = self.process()
        with mock.patch.object(subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(progress.os, "write") as write:
            with progress.Progress(component="NVIDIA DLSS") as window:
                window.update("download", 1, 100)
        self.assertIn("--title=Proton - NVIDIA DLSS", popen.call_args.args[0])
        self.assertIn(b"# Updating NVIDIA DLSS components...\n", write.call_args_list[0].args[1])

    def test_no_display_does_not_resolve_or_spawn_a_window(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(subprocess, "Popen") as popen:
            with progress.Progress() as window:
                window.update("download", 1, 100)
        self.resolver.assert_not_called()
        popen.assert_not_called()

    def test_missing_binary_is_logged_once_and_never_retried(self):
        self.resolver.return_value = None
        messages = []
        with mock.patch.object(subprocess, "Popen") as popen, progress.Progress(log=messages.append) as window:
            self.assertIsNone(window.update("download", 0, 100))
            self.assertIsNone(window.update("extract", 0, 100))
        self.assertEqual(len(messages), 1)
        self.resolver.assert_called_once()
        popen.assert_not_called()

    def test_spawn_failure_does_not_cancel_or_retry_the_download(self):
        messages = []
        with mock.patch.object(subprocess, "Popen", side_effect=FileNotFoundError("missing")) as popen:
            with progress.Progress(log=messages.append) as window:
                self.assertIsNone(window.update("download", 0, 100))
                self.assertIsNone(window.update("extract", 0, 100))
        self.assertEqual(len(messages), 1)
        popen.assert_called_once()

    def test_completed_download_and_extraction_do_not_close_early(self):
        process = self.process()
        with mock.patch.object(subprocess, "Popen", return_value=process), \
                mock.patch.object(progress.time, "monotonic", side_effect=range(1, 20)), \
                mock.patch.object(progress.os, "write") as write:
            with progress.Progress() as window:
                for stage, done in (("download", 0), ("download", 50), ("download", 100),
                                    ("extract", 0), ("extract", 50), ("extract", 100),
                                    ("download", 0)):
                    window.update(stage, done, 100)
                self.assertTrue(all(b"100\n" != call.args[1] for call in write.call_args_list))
            payloads = [call.args[1].decode() for call in write.call_args_list]
        percentages = [int(line) for payload in payloads for line in payload.splitlines() if line.isdigit()]
        self.assertEqual(percentages, [0, 45, 90, 90, 94, 99, 99, 100])
        self.assertIn("# Updating AMD FSR components...\n", payloads[0])
        self.assertIn("# Extracting AMD FSR components...\n", payloads[3])

    def test_frequent_updates_are_throttled_except_stage_changes_and_completion(self):
        process = self.process()
        with mock.patch.object(subprocess, "Popen", return_value=process), \
                mock.patch.object(progress.time, "monotonic", side_effect=[1, 1.01, 1.02, 1.03]), \
                mock.patch.object(progress.os, "write") as write:
            with progress.Progress() as window:
                window.update("download", 1, 100)
                window.update("download", 2, 100)
                window.update("extract", 0, 100)
                window.update("extract", 100, 100)
        self.assertEqual(write.call_count, 4)

    def test_closed_window_is_reaped_and_never_respawned(self):
        process = self.process()
        process.poll.return_value = 1
        with mock.patch.object(subprocess, "Popen", return_value=process) as popen:
            with progress.Progress() as window:
                self.assertFalse(window.update("download", 0, 100))
                self.assertFalse(window.update("extract", 0, 100))
        self.assertTrue(process.stdin.closed)
        process.wait.assert_called_once_with(timeout=1)
        popen.assert_called_once()

    def test_cancelling_one_component_also_skips_subsequent_downloads(self):
        from upscalers import storage
        process = self.process()
        with mock.patch.object(subprocess, "Popen", return_value=process) as popen:
            with progress.Progress() as window:
                window.update("download", 1, 100)
                process.poll.return_value = 1
                # Cancellation must be observed even if the next UI update is throttled.
                self.assertFalse(window.update("download", 2, 100))
            with progress.Progress(component="Intel XeSS") as next_window:
                with mock.patch.object(storage.urllib.request, "urlopen") as network:
                    with self.assertRaisesRegex(storage.ComponentError, "cancelled"):
                        storage.download_file({'url': 'https://example.com/a', 'size': 1},
                                              Path('/unused'), next_window.update, hosts=('example.com',))
                    network.assert_not_called()
        popen.assert_called_once()

    def test_cancel_during_final_cleanup_skips_later_components(self):
        process = self.process()
        process.wait.return_value = 1
        with mock.patch.object(subprocess, "Popen", return_value=process) as popen:
            with progress.Progress() as window:
                window.update("extract", 100, 100)
            with progress.Progress(component="Intel XeSS") as next_window:
                self.assertFalse(next_window.update("download", 0, 100))
        popen.assert_called_once()
        self.assertTrue(progress.Progress._cancelled)

    def test_gui_error_does_not_cancel_component_preparation(self):
        process = self.process()
        process.poll.return_value = 100
        with mock.patch.object(subprocess, "Popen", return_value=process):
            with progress.Progress() as window:
                self.assertIsNone(window.update("download", 1, 100))
        self.assertFalse(progress.Progress._cancelled)

    def test_broken_pipe_never_cancels_the_download_or_respawns(self):
        process = self.process()
        os.close(self.readers.pop())
        with mock.patch.object(subprocess, "Popen", return_value=process) as popen:
            with progress.Progress() as window:
                self.assertIsNone(window.update("download", 1, 100))
                self.assertIsNone(window.update("extract", 0, 100))
        self.assertTrue(process.stdin.closed)
        popen.assert_called_once()

    def test_full_pipe_drops_updates_without_blocking(self):
        process = self.process()
        with mock.patch.object(subprocess, "Popen", return_value=process), \
                mock.patch.object(progress.os, "write", side_effect=BlockingIOError):
            with progress.Progress() as window:
                self.assertIsNone(window.update("download", 1, 100))
                self.assertFalse(window._closed)
        process.wait.assert_called_once_with(timeout=1)

    def test_close_without_updates_does_not_spawn_a_window(self):
        with mock.patch.object(subprocess, "Popen") as popen, progress.Progress():
            pass
        popen.assert_not_called()

    def test_unresponsive_window_is_reaped_with_bounded_waits(self):
        process = self.process()
        process.wait.side_effect = [subprocess.TimeoutExpired("zenity", 1),
                                    subprocess.TimeoutExpired("zenity", 1), 0]
        window = progress.Progress()
        window.process = process
        window.close()
        window.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=1)] * 3)


@unittest.skipUnless(shutil.which("Xvfb") and SMOKE_ZENITY,
                     "Xvfb and Proton's bundled Zenity are required")
class BundledZenitySmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.xvfb = subprocess.Popen(
            ["Xvfb", "-displayfd", "1", "-screen", "0", "800x600x24", "-nolisten", "tcp"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if not select.select([cls.xvfb.stdout], [], [], 5)[0]:
            cls.xvfb.terminate()
            cls.xvfb.wait(timeout=5)
            cls.xvfb.stdout.close()
            raise unittest.SkipTest("Xvfb did not start")
        display = cls.xvfb.stdout.readline().decode().strip()
        if not display.isdigit():
            cls.xvfb.wait(timeout=5)
            cls.xvfb.stdout.close()
            raise unittest.SkipTest("Xvfb could not allocate a display")
        cls.runtime = tempfile.TemporaryDirectory()
        cls.environment = os.environ.copy()
        cls.environment.pop("WAYLAND_DISPLAY", None)
        cls.environment.update(DISPLAY=":" + display, XDG_RUNTIME_DIR=cls.runtime.name,
                               XDG_SESSION_TYPE="x11", DBUS_SESSION_BUS_ADDRESS="",
                               LIBGL_ALWAYS_SOFTWARE="1", GALLIUM_DRIVER="llvmpipe")

    @classmethod
    def tearDownClass(cls):
        cls.xvfb.terminate()
        cls.xvfb.wait(timeout=5)
        cls.xvfb.stdout.close()
        cls.runtime.cleanup()

    def setUp(self):
        cancelled = mock.patch.object(progress.Progress, "_cancelled", False)
        cancelled.start()
        self.addCleanup(cancelled.stop)

    def test_bundled_progress_stays_open_until_preparation_finishes(self):
        with mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(progress, "_zenity_binary", return_value=str(SMOKE_ZENITY)):
            with progress.Progress() as window:
                window.update("download", 100, 100)
                time.sleep(0.3)
                self.assertIsNone(window.process.poll())
                window.update("extract", 100, 100)
                time.sleep(0.1)
                self.assertIsNone(window.process.poll())
            self.assertEqual(window.process.returncode, 0)

    @unittest.skipUnless(shutil.which("xdotool"), "xdotool is required")
    def test_bundled_cancel_button_returns_cancelled_status(self):
        with mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(progress, "_zenity_binary", return_value=str(SMOKE_ZENITY)):
            with progress.Progress(component="Cancellation test") as window:
                window.update("download", 0, 100)
                result = subprocess.run(['xdotool', 'search', '--sync', '--name',
                                         '^Proton - Cancellation test$'], env=self.environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                handle = result.stdout.splitlines()[0]
                geometry = subprocess.check_output(['xdotool', 'getwindowgeometry', '--shell', handle],
                                                   env=self.environment, text=True,
                                                   stderr=subprocess.PIPE, timeout=5)
                dimensions = dict(line.split('=', 1) for line in geometry.splitlines())
                subprocess.run(['xdotool', 'mousemove', '--sync', '--window', handle,
                                str(int(dimensions['WIDTH']) * 4 // 5),
                                str(int(dimensions['HEIGHT']) * 2 // 3), 'click', '1'],
                               env=self.environment, check=True, capture_output=True, timeout=5)
                self.assertEqual(window.process.wait(timeout=5), 1)
                self.assertFalse(window.update("download", 1, 100))
            self.assertTrue(progress.Progress._cancelled)


if __name__ == "__main__":
    unittest.main()
