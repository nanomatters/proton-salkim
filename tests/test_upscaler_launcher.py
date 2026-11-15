"""Exercise the launcher boundary without loading Vulkan or starting a game."""

import ast
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from proton_config import Settings
import upscalers
from upscalers import fsr, storage


ROOT = Path(__file__).resolve().parents[1]


def gpu(vendor, *extensions, kind="DISCRETE_GPU"):
    return SimpleNamespace(vendorID=vendor, deviceType=SimpleNamespace(name=kind), extensions=[
        SimpleNamespace(extensionName=name) for name in extensions])


class LauncherIntegration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / 'config'),
                    "XDG_DATA_HOME": str(self.root / 'data'), "SteamAppId": '292030', "PROTON_UPSCALER_UPGRADE": "1"}
        self.path = self.root / 'config/proton/config.ini'
        self.path.parent.mkdir(parents=True)
        self.prefix = self.root / 'compat/pfx'
        self.query = mock.Mock(return_value=[gpu(0x1002, 'VK_KHR_fragment_shading_rate')])
        self.logs = []
        patcher = mock.patch.object(upscalers.platform, 'machine', return_value='x86_64')
        patcher.start()
        self.addCleanup(patcher.stop)
        progress = mock.patch.object(upscalers.progress, "Progress")
        progress.start()
        self.addCleanup(progress.stop)

    def run_setup(self, mode='waitforexitandrun', command=None):
        return upscalers.setup(self.env,
                               mode=mode, command=['C:\\game\\game.exe'] if command is None else command,
                               gpu_query=self.query, log=self.logs.append)

    def test_config_is_applied_before_private_driver_and_protonfixes_import(self):
        tree = ast.parse((ROOT / 'proton').read_text())
        settings = next(i for i, node in enumerate(tree.body)
                        if isinstance(node, ast.Assign) and any(
                            isinstance(target, ast.Name) and target.id == 'g_user_config'
                            for target in node.targets))
        private = next(i for i, node in enumerate(tree.body)
                       if isinstance(node, ast.If) and any(
                           isinstance(child, ast.Constant) and child.value == 'PROTON_RADV'
                           for child in ast.walk(node)))
        fixes = next(i for i, node in enumerate(tree.body)
                     if isinstance(node, ast.Import) and any(name.name == 'protonfixes' for name in node.names))
        self.assertLess(settings, private)
        self.assertLess(settings + 1, fixes)
        self.path.write_text('[global]\nPROTON_RADV = 1\nPROTON_HUD = 3\n')
        namespace = {'Settings': Settings, 'os': SimpleNamespace(environ=self.env)}
        # Use the actual launcher construction and application statements.
        namespace['sys'] = SimpleNamespace(stderr=None)
        exec(compile(ast.Module(body=tree.body[settings:settings + 2], type_ignores=[]),
                     'proton', 'exec'), namespace)
        self.assertEqual(self.env['PROTON_RADV'], '1')
        self.assertEqual(self.env['PROTON_HUD'], '3')

    def test_provider_setup_runs_after_legacy_install_and_before_game_fixes(self):
        text = (ROOT / 'proton').read_text()
        legacy = text.index('    protonfixes.setup_upscalers(g_session.compat_config')
        new = text.index('    upscalers.setup(g_session.env')
        fixes = text.index('    protonfixes.execute()', legacy)
        self.assertLess(legacy, new)
        self.assertLess(new, fixes)

    def test_gpu_eligibility_uses_vendor_and_capability(self):
        self.assertFalse(upscalers.eligible_amd([]))
        self.assertFalse(upscalers.eligible_amd([gpu(0x10de, 'VK_KHR_fragment_shading_rate')]))
        self.assertFalse(upscalers.eligible_amd([gpu(0x1002)]))
        self.assertTrue(upscalers.eligible_amd([gpu(0x1002, 'VK_EXT_shader_float8')]))
        self.assertTrue(upscalers.eligible_amd([
            gpu(0x10de), gpu(0x1002, 'VK_KHR_fragment_shading_rate')]))

    def test_integrated_only_uses_the_same_vendor_and_capability_checks(self):
        self.assertFalse(upscalers.eligible_amd([gpu(0x1002, kind='INTEGRATED_GPU')]))
        self.assertFalse(upscalers.eligible_amd([
            gpu(0x8086, 'VK_KHR_fragment_shading_rate', kind='INTEGRATED_GPU')]))
        for extension in ('VK_KHR_fragment_shading_rate', 'VK_EXT_shader_float8'):
            with self.subTest(extension=extension):
                integrated = gpu(0x1002, extension, kind='INTEGRATED_GPU')
                self.assertTrue(upscalers.eligible_amd([integrated]))

    def test_software_renderers_do_not_disqualify_integrated_only_system(self):
        integrated = gpu(0x1002, 'VK_KHR_fragment_shading_rate', kind='INTEGRATED_GPU')
        software = gpu(0x10005, kind='CPU')
        self.assertTrue(upscalers.eligible_amd([software, integrated]))
        self.assertTrue(upscalers.eligible_amd(iter([integrated, software])))
        self.assertFalse(upscalers.eligible_amd([software]))

    def test_any_discrete_gpu_blocks_automatic_integrated_selection(self):
        integrated = gpu(0x1002, 'VK_KHR_fragment_shading_rate', kind='INTEGRATED_GPU')
        for vendor in (0x10de, 0x8086, 0x1002):
            for devices in ([integrated, gpu(vendor)], [gpu(vendor), integrated]):
                with self.subTest(vendor=vendor, devices=devices):
                    self.assertFalse(upscalers.eligible_amd(devices))

    def test_unknown_and_virtual_devices_do_not_count_as_integrated_only(self):
        integrated = gpu(0x1002, 'VK_EXT_shader_float8', kind='INTEGRATED_GPU')
        for kind in ('OTHER', 'VIRTUAL_GPU', ''):
            with self.subTest(kind=kind):
                self.assertFalse(upscalers.eligible_amd([integrated, gpu(0x1234, kind=kind)]))

    def cached_provider(self):
        provider = self.root / "shared/amdxcffx64.dll"
        provider.parent.mkdir(exist_ok=True)
        provider.write_bytes(b"verified provider fixture")
        return provider

    def test_compatible_amd_is_automatic_without_creating_config(self):
        provider = self.cached_provider()
        self.env["WINE_FSR4_LITE"] = "1"
        with mock.patch.object(fsr, "Store") as store:
            store.return_value.prepare.return_value = provider
            self.assertTrue(self.run_setup())
        self.assertEqual(self.env["WINE_FSR4_PROVIDER"], str(provider))
        self.assertEqual(self.env["WINE_FSR4_LITE"], "1")
        self.assertFalse(self.path.exists())
        self.assertFalse(self.prefix.exists())
        self.assertNotIn("FSR4_UPGRADE", self.env)
        self.assertNotIn("PROTON_FSR4_UPGRADE", self.env)
        store.return_value.prepare.assert_called_once()
        self.query.assert_called_once()

    def test_integrated_only_system_also_installs_automatically(self):
        self.query.return_value = [gpu(0x1002, "VK_KHR_fragment_shading_rate", kind="INTEGRATED_GPU")]
        provider = self.cached_provider()
        with mock.patch.object(fsr, "Store") as store:
            store.return_value.prepare.return_value = provider
            self.assertTrue(self.run_setup())
        self.assertEqual(self.env["WINE_FSR4_PROVIDER"], str(provider))

    def test_hybrid_with_only_amd_integrated_does_not_download_for_other_discrete_gpu(self):
        for vendor in (0x10de, 0x8086):
            self.query.return_value = [gpu(0x1002, "VK_KHR_fragment_shading_rate", kind="INTEGRATED_GPU"),
                                       gpu(vendor)]
            with self.subTest(vendor=vendor), mock.patch.object(fsr, "Store") as store:
                self.assertFalse(self.run_setup())
            store.assert_not_called()
            self.assertNotIn("WINE_FSR4_PROVIDER", self.env)

    def test_nvidia_only_does_not_download(self):
        self.query.return_value = [gpu(0x10de, "VK_KHR_fragment_shading_rate")]
        with mock.patch.object(fsr, "Store") as store:
            self.assertFalse(self.run_setup())
        store.assert_not_called()

    def test_custom_provider_and_lite_bypass_hardware_detection(self):
        self.env.update(WINE_FSR4_PROVIDER="/custom/amdxcffx64.dll", WINE_FSR4_LITE="1")
        self.query.side_effect = AssertionError("GPU queried")
        with mock.patch.object(fsr, "Store") as store:
            self.assertTrue(self.run_setup())
        store.assert_not_called()
        self.query.assert_not_called()
        self.assertEqual(self.env["WINE_FSR4_PROVIDER"], "/custom/amdxcffx64.dll")
        self.assertEqual(self.env["WINE_FSR4_LITE"], "1")

    def test_old_fsr_config_does_not_affect_automatic_setup(self):
        original = "[global]\nfsr_upgrade=off\nfsr_lite=false\n[steam:292030]\nfsr_upgrade=off\n"
        self.path.write_text(original)
        self.env["WINE_FSR4_LITE"] = "1"
        Settings(self.env).apply(self.env)
        provider = self.cached_provider()
        with mock.patch.object(fsr, "Store") as store:
            store.return_value.prepare.return_value = provider
            self.assertTrue(self.run_setup())
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(self.env["WINE_FSR4_LITE"], "1")
        self.assertEqual(self.env["WINE_FSR4_PROVIDER"], str(provider))

    def test_non_game_modes_and_helpers_never_probe_or_install(self):
        with mock.patch.object(fsr, "setup") as setup:
            for mode, command in (("getcompatpath", ["game.exe"]), ("runinprefix", ["game.exe"]),
                                  ("run", []), ("run", ["C:\\windows\\system32\\winecfg.exe"]),
                                  ("waitforexitandrun", ["/path/IScriptEvaluator.exe"])):
                with self.subTest(mode=mode, command=command):
                    self.assertFalse(self.run_setup(mode=mode, command=command))
            setup.assert_not_called()
        self.query.assert_not_called()

    def test_unsupported_process_architecture_never_runs_setup(self):
        with mock.patch.object(fsr, "setup") as setup:
            self.env["WINEARCH"] = "win32"
            self.assertFalse(self.run_setup())
            del self.env["WINEARCH"]
            with mock.patch.object(upscalers.platform, "machine", return_value="aarch64"):
                self.assertFalse(self.run_setup())
            setup.assert_not_called()

    def test_cached_launch_never_starts_a_progress_process(self):
        provider = self.cached_provider()
        with mock.patch.object(fsr, "Store") as store, \
                mock.patch.object(upscalers.progress.subprocess, "Popen") as popen:
            store.return_value.prepare.return_value = provider
            self.assertTrue(self.run_setup())
        popen.assert_not_called()

    def test_only_explicit_opt_in_runs_automatic_setup(self):
        with mock.patch.object(fsr, "setup") as setup:
            for value in (None, "", "0", "true", "yes"):
                with self.subTest(value=value):
                    self.env.pop("PROTON_UPSCALER_UPGRADE", None)
                    if value is not None:
                        self.env["PROTON_UPSCALER_UPGRADE"] = value
                    self.assertFalse(self.run_setup())
        setup.assert_not_called()
        self.query.assert_not_called()

    def test_explicit_provider_works_without_automatic_upgrades(self):
        self.env.pop("PROTON_UPSCALER_UPGRADE")
        self.env["WINE_FSR4_PROVIDER"] = "/manual/amd.dll"
        with mock.patch.object(fsr, "Store") as store:
            self.assertTrue(self.run_setup())
        self.assertEqual(self.env["WINE_FSR4_PROVIDER"], "/manual/amd.dll")
        store.assert_not_called()
        self.query.assert_not_called()

    def test_upgrade_can_be_enabled_per_game(self):
        self.env.pop("PROTON_UPSCALER_UPGRADE")
        self.path.write_text('[global]\nPROTON_UPSCALER_UPGRADE=0\n'
                             '[steam:292030]\nPROTON_UPSCALER_UPGRADE=1\n')
        Settings(self.env).apply(self.env)
        self.assertEqual(self.env["PROTON_UPSCALER_UPGRADE"], "1")
        with mock.patch.object(fsr, "setup", return_value=True) as setup:
            self.assertTrue(self.run_setup())
        setup.assert_called_once()

    def test_upgrade_can_be_disabled_per_game(self):
        self.env.pop("PROTON_UPSCALER_UPGRADE")
        self.path.write_text('[global]\nPROTON_UPSCALER_UPGRADE=1\n'
                             '[steam:292030]\nPROTON_UPSCALER_UPGRADE=0\n')
        Settings(self.env).apply(self.env)
        with mock.patch.object(fsr, "setup") as setup:
            self.assertFalse(self.run_setup())
        setup.assert_not_called()
        self.query.assert_not_called()

    def test_integrated_lite_default_and_explicit_override(self):
        provider = self.cached_provider()
        for requested in (None, "0", "1", ""):
            for fp8 in (False, True):
                with self.subTest(requested=requested, fp8=fp8):
                    self.env.pop("WINE_FSR4_PROVIDER", None)
                    self.env.pop("WINE_FSR4_LITE", None)
                    if requested is not None:
                        self.env["WINE_FSR4_LITE"] = requested
                    extensions = ["VK_KHR_fragment_shading_rate"]
                    if fp8:
                        extensions.append("VK_EXT_shader_float8")
                    self.query.return_value = [gpu(0x1002, *extensions, kind="INTEGRATED_GPU")]
                    with mock.patch.object(fsr, "Store") as store:
                        store.return_value.prepare.return_value = provider
                        self.assertTrue(self.run_setup())
                    expected = requested if requested is not None else None if fp8 else "1"
                    self.assertEqual(self.env.get("WINE_FSR4_LITE"), expected)

    def test_lite_does_not_affect_discrete_or_unknown_gpu_systems(self):
        integrated = gpu(0x1002, "VK_KHR_fragment_shading_rate", kind="INTEGRATED_GPU")
        self.assertTrue(upscalers.default_amd_lite([integrated, gpu(0x10005, kind="CPU")]))
        for kind in ("DISCRETE_GPU", "VIRTUAL_GPU", "OTHER", ""):
            for vendor in (0x1002, 0x10de, 0x8086):
                self.assertFalse(upscalers.default_amd_lite([integrated, gpu(vendor, kind=kind)]))
        self.assertFalse(upscalers.default_amd_lite([]))
        self.assertFalse(upscalers.default_amd_lite([gpu(0x1002, kind="INTEGRATED_GPU")]))
        self.assertFalse(upscalers.default_amd_lite([
            integrated, gpu(0x1002, "VK_EXT_shader_float8", kind="INTEGRATED_GPU")]))

    def test_component_entry_points_require_opt_in_but_accept_explicit_overrides(self):
        disabled = {}
        self.assertFalse(fsr.setup(disabled, gpu_eligible=lambda: self.fail("GPU queried")))

    def test_old_add_config_aliases_do_not_set_upgrade_flags(self):
        tree = ast.parse((ROOT / "utilities.py").read_text())
        nodes = [node for node in tree.body if
                 isinstance(node, ast.FunctionDef) and node.name == "proton_add_config" or
                 isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
                 target.id == "_config_envvars" for target in node.targets)]
        env = {"PROTON_ADD_CONFIG": "fsr3,fsr4=4.1.2,dlss"}
        namespace = {"os": SimpleNamespace(environ=env)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "utilities", "exec"), namespace)
        namespace["proton_add_config"]()
        self.assertNotIn("PROTON_FSR4_UPGRADE", env)
        self.assertNotIn("FSR4_UPGRADE", env)
        self.assertEqual(env["PROTON_DLSS_UPGRADE"], "1")


if __name__ == "__main__":
    unittest.main()
