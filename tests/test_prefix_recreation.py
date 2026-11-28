import os
import shutil
import sys
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class PrefixRecreationTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(root), "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(root)},
                                    clear=True))
        modules = {"protonfixes": ModuleType("protonfixes"), "utilities": SimpleNamespace(proton_add_config=dict)}
        with patch.object(sys, "path", [str(ROOT), *sys.path]), patch.dict(sys.modules, modules):
            loader = SourceFileLoader("proton_launcher", str(ROOT / "proton"))
            launcher = module_from_spec(spec_from_loader(loader.name, loader))
            loader.exec_module(launcher)

        tool = launcher.g_proton = launcher.Proton(str(root / "tool"))
        self.compat = launcher.g_compatdata = launcher.CompatData(str(root))
        launcher.g_session = SimpleNamespace(env={}, compat_config=set(), dlloverrides={}, nvidia_wine_dll_dir=None)
        self.enterContext(patch.object(os, "sync"))
        self.enterContext(patch.object(launcher, "try_copy"))  # Steam and runtime files.
        for directory in (tool.fonts_dir, tool.wine_fonts_dir):
            Path(directory).mkdir(parents=True)
        Path(tool.wine_inf).touch()

        self.dlls = []
        for folder, arch in (("system32", "x86_64-windows"), ("syswow64", "i386-windows")):
            relative = Path("drive_c/windows", folder, "d3dx9_24.dll")
            source = Path(tool.lib_dir, "wine", arch, relative.name)
            source.parent.mkdir(parents=True)
            source.write_bytes(bytes(0x40) + b"Wine builtin DLL" + arch.encode())
            template = Path(tool.default_pfx_dir) / relative
            template.parent.mkdir(parents=True)
            template.symlink_to(source)
            self.dlls.append((Path(self.compat.prefix_dir) / relative, source))

        self.compat.setup_prefix()
        shutil.rmtree(self.compat.prefix_dir)

    def assert_dlls_copied(self):
        for dll, source in self.dlls:
            self.assertFalse(dll.is_symlink(), dll)
            self.assertEqual(dll.read_bytes(), source.read_bytes())

    def test_recreate_with_matching_config(self):
        self.compat.setup_prefix()
        self.assert_dlls_copied()
        with patch.object(self.compat, "update_builtin_libs") as update:
            self.compat.setup_prefix()
            update.assert_not_called()

    def test_retry_after_interrupted_dll_update(self):
        with patch.object(self.compat, "update_builtin_libs", side_effect=InterruptedError), \
             self.assertRaises(InterruptedError):
            self.compat.setup_prefix()
        self.assertTrue(Path(self.compat.creation_sync_guard).is_file())
        self.compat.setup_prefix()
        self.assert_dlls_copied()


if __name__ == "__main__":
    unittest.main()
