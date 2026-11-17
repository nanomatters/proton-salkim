"""Exercise launcher defaults from its actual environment-selection statements."""

import ast
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "proton").read_text())
SESSION = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "Session")
INIT_SESSION = next(node for node in SESSION.body if isinstance(node, ast.FunctionDef)
                    and node.name == "init_session")


def has_string(node, text):
    return any(isinstance(value, ast.Constant) and value.value == text for value in ast.walk(node))


def compile_statements(statements):
    return compile(ast.Module(body=statements, type_ignores=[]), str(ROOT / "proton"), "exec")


class LauncherDefaults(unittest.TestCase):
    def wow64(self, appid, override=None, settings=None, architecture="x86_64"):
        environment = {"SteamGameId": appid}
        if override is not None:
            environment["PROTON_USE_WOW64"] = override
        session = SimpleNamespace(env=environment, compat_config=set())
        method = next(node for node in SESSION.body if isinstance(node, ast.FunctionDef)
                      and node.name == "check_environment")
        namespace = {"nonzero": lambda value: value not in ("0", "", "false")}
        exec(compile_statements([method]), namespace)
        session.check_environment = lambda *args: namespace["check_environment"](session, *args)
        statements = INIT_SESSION.body[:3] + [node for node in INIT_SESSION.body
                                             if has_string(node, "PROTON_USE_WOW64")]
        namespace.update({
            "self": session,
            "g_compatdata": SimpleNamespace(prefix_dir="/fake-prefix"),
            "g_proton": SimpleNamespace(user_settings_file="/fake-settings"),
            "file_exists": lambda *args, **kwargs: settings is not None,
            "platform": SimpleNamespace(machine=lambda: architecture),
        })
        with patch.dict("sys.modules", {"user_settings": SimpleNamespace(user_settings=settings or {})}):
            exec(compile_statements(statements), namespace)
        return session

    def test_dating_defaults(self):
        session = self.wow64("4005300")
        self.assertEqual(session.env["PROTON_USE_WOW64"], "1")
        self.assertEqual(session.env["WINEARCH"], "wow64")
        self.assertIn("wow64", session.compat_config)

    def test_explicit_wow64_choices(self):
        for value in ("0", "", "1"):
            with self.subTest(value=value):
                session = self.wow64("4005300", override=value)
                self.assertEqual(session.env["PROTON_USE_WOW64"], value)
                self.assertEqual("WINEARCH" in session.env, value == "1")
                self.assertEqual("wow64" in session.compat_config, value == "1")

    def test_user_settings_wow64_choices(self):
        for value in ("0", "", "1"):
            with self.subTest(value=value):
                session = self.wow64("4005300", settings={"PROTON_USE_WOW64": value})
                self.assertEqual(session.env["PROTON_USE_WOW64"], value)
                self.assertEqual("WINEARCH" in session.env, value == "1")

    def test_launch_environment_precedes_settings(self):
        session = self.wow64("4005300", override="0", settings={"PROTON_USE_WOW64": "1"})
        self.assertEqual(session.env["PROTON_USE_WOW64"], "0")
        self.assertNotIn("WINEARCH", session.env)

    def test_user_settings_architecture_is_preserved(self):
        session = self.wow64("4005300", settings={"WINEARCH": "win64"})
        self.assertEqual(session.env["WINEARCH"], "win64")

    def test_other_games_and_arm(self):
        self.assertNotIn("PROTON_USE_WOW64", self.wow64("0").env)
        self.assertNotIn("WINEARCH", self.wow64("4005300", architecture="aarch64").env)
        self.assertEqual(self.wow64("0", override="1").env["WINEARCH"], "wow64")

    def test_nvapi_architecture_and_overrides(self):
        assignment = next(node for node in ast.walk(TREE) if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "use_nvapi"
                                  for target in node.targets))
        for architecture in ("x86_64-windows", "aarch64-windows"):
            for configuration, expected in ((set(), True), ({"disablenvapi"}, False),
                                            ({"forcenvapi"}, True),
                                            ({"disablenvapi", "forcenvapi"}, True)):
                with self.subTest(architecture=architecture, configuration=configuration):
                    namespace = {
                        "g_session": SimpleNamespace(compat_config=configuration),
                        "g_proton": SimpleNamespace(host_pe_arch=architecture),
                    }
                    exec(compile_statements([assignment]), namespace)
                    self.assertEqual(namespace["use_nvapi"], expected)

    def test_upscaler_info_controls_both_vendor_indicators(self):
        method = next(node for node in SESSION.body if isinstance(node, ast.FunctionDef)
                      and node.name == "check_environment")
        statements = [node for node in INIT_SESSION.body if
                      has_string(node, "PROTON_UPSCALER_INFO") or
                      isinstance(node, ast.If) and has_string(node.test, "upscalerinfo")]
        for value in (None, "", "0", "1"):
            with self.subTest(value=value):
                environment = {} if value is None else {"PROTON_UPSCALER_INFO": value}
                session = SimpleNamespace(env=environment, compat_config=set())
                namespace = {"self": session, "nonzero": lambda value: bool(value) and value != "0"}
                exec(compile_statements([method]), namespace)
                session.check_environment = lambda *args: namespace["check_environment"](session, *args)
                exec(compile_statements(statements), namespace)
                enabled = value == "1"
                self.assertEqual(environment.get("FSR_WATERMARK"), "1" if enabled else None)
                self.assertEqual(environment.get("FSR_FG_WATERMARK"), "1" if enabled else None)
                self.assertEqual(environment["DXVK_NVAPI_SET_NGX_DEBUG_OPTIONS"],
                                 "DLSSIndicator=1024,DLSSGIndicator=2," if enabled else
                                 "DLSSIndicator=0,DLSSGIndicator=0,")
                self.assertNotIn("PROTON_UPSCALER_UPGRADE", environment)

    def test_fex_cache_default_and_overrides(self):
        block = next(node for node in INIT_SESSION.body if isinstance(node, ast.If)
                     and has_string(node.test, "aarch64") and has_string(node, "FEX_DISKCACHE"))
        default = next(node for node in block.body if has_string(node, "FEX_DISKCACHE"))
        statement = ast.fix_missing_locations(ast.If(test=block.test, body=[default], orelse=[]))
        for architecture in ("x86_64", "aarch64"):
            for override in (None, "0", "", "1"):
                with self.subTest(architecture=architecture, override=override):
                    environment = {} if override is None else {"FEX_DISKCACHE": override}
                    namespace = {
                        "self": SimpleNamespace(env=environment),
                        "platform": SimpleNamespace(machine=lambda: architecture),
                    }
                    exec(compile_statements([statement]), namespace)
                    expected = "1" if architecture == "aarch64" and override is None else override
                    self.assertEqual(environment.get("FEX_DISKCACHE"), expected)

    def test_openssl_game_default_and_overrides(self):
        block = next(node for node in INIT_SESSION.body if isinstance(node, ast.If)
                     and has_string(node, "OPENSSL_ia32cap"))
        for appid in ("522020", "661920", "0"):
            for override in (None, "", "0", "~0x10000000"):
                with self.subTest(appid=appid, override=override):
                    environment = {} if override is None else {"OPENSSL_ia32cap": override}
                    namespace = {"self": SimpleNamespace(env=environment), "os": os}
                    with patch.dict(os.environ, {"SteamGameId": appid}, clear=True):
                        exec(compile_statements([block]), namespace)
                    expected = "~0x20000000" if appid != "0" and override is None else override
                    self.assertEqual(environment.get("OPENSSL_ia32cap"), expected)


if __name__ == "__main__":
    unittest.main()
