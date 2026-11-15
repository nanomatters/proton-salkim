"""Verify global and per-game environment defaults."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from proton_config import Settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.environment = {"HOME": str(self.home)}
        self.path = self.home / ".config/proton/config.ini"
        self.logs = []

    def write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data.encode())

    def load(self, **env):
        return Settings({**self.environment, **env}, self.logs.append)

    def test_missing_file_is_silent_and_does_not_create_directory(self):
        env = {"PROTON_HUD": "0"}
        self.load().apply(env)
        self.assertEqual(env, {"PROTON_HUD": "0"})
        self.assertEqual(self.logs, [])
        self.assertFalse(self.path.parent.exists())

    def test_game_overrides_global_and_launch_values_win(self):
        self.write("[global]\nPROTON_HUD=3\nPROTON_RADV=1\nVKD3D_CONFIG=global\n"
                   "[steam:292030]\nPROTON_HUD=4\nVKD3D_CONFIG=game\n"
                   "[steam:1]\nPROTON_RADV=0\n"
                   "[example.metadata]\ndownload=allow\n")
        env = {"VKD3D_CONFIG": "launch", "PROTON_RADV": ""}
        self.load(SteamAppId="292030").apply(env)
        self.assertEqual(env, {"PROTON_HUD": "4", "VKD3D_CONFIG": "launch", "PROTON_RADV": ""})

    def test_literal_values_case_empty_and_zero(self):
        self.write("[global]\nPROTON_HUD=0\nWINEDEBUG=\nMixedCase=%path%;x=y\n")
        env = {}
        self.load().apply(env)
        self.assertEqual(env, {"PROTON_HUD": "0", "WINEDEBUG": "", "MixedCase": "%path%;x=y"})
        override = {"PROTON_HUD": "", "WINEDEBUG": "0"}
        self.load().apply(override)
        self.assertEqual(override["PROTON_HUD"], "")
        self.assertEqual(override["WINEDEBUG"], "0")

    def test_steam_id_resolution(self):
        cases = [
            ({"SteamAppId": "2", "SteamGameId": "3", "STEAM_COMPAT_APP_ID": "4"}, "steam:2"),
            ({"SteamAppId": "0", "SteamGameId": "3"}, "steam:3"),
            ({"SteamAppId": "", "SteamGameId": "000", "STEAM_COMPAT_APP_ID": "4"}, "steam:4"),
            ({"SteamAppId": "invalid", "SteamGameId": "3"}, "steam:3"),
            ({"SteamAppId": "00", "SteamGameId": "0"}, None),
            ({"SteamAppId": "1\n"}, None),
        ]
        for env, expected in cases:
            with self.subTest(env=env):
                self.assertEqual(self.load(**env).game_section, expected)

    def test_umu_id_does_not_inherit_synthetic_steam_game(self):
        self.write("[global]\nPROTON_HUD=1\n[umu:umu-123]\nPROTON_HUD=2\n"
                   "[steam:292030]\nPROTON_HUD=3\n")
        settings = self.load(UMU_ID="umu-123", SteamAppId="292030")
        env = {}
        settings.apply(env)
        self.assertEqual(settings.game_section, "umu:umu-123")
        self.assertEqual(env["PROTON_HUD"], "2")
        for sentinel in ("", "0", "umu-default"):
            with self.subTest(sentinel=sentinel):
                self.assertEqual(self.load(UMU_ID=sentinel, SteamAppId="292030").game_section, "steam:292030")
        self.assertIsNone(self.load(UMU_ID="bad]\n", SteamAppId="292030").game_section)

    def test_xdg_directory_must_be_absolute(self):
        other = self.home / "other-config"
        self.assertEqual(self.load(XDG_CONFIG_HOME=str(other)).path, other / "proton/config.ini")
        for value in ("", "relative", "~/config"):
            with self.subTest(value=value):
                self.assertEqual(self.load(XDG_CONFIG_HOME=value).path, self.path)

    def test_invalid_file_is_never_partially_applied(self):
        cases = [
            "[global]\nPROTON_HUD=3\nbroken\n",
            "[global]\nPROTON_HUD=3\nPROTON_HUD=4\n",
            "[global]\nPROTON_HUD=3\n[global]\nPROTON_RADV=1\n",
            "[global]\nPROTON_HUD=3\nBAD-NAME=1\n",
            "[global]\nPROTON_HUD=3\n[steam:7]\nBAD NAME=1\n",
            "[global]\nPROTON_HUD=3\n[other]\nkey=bad\0value\n",
            "[DEFAULT]\nPROTON_HUD=3\n[global]\nPROTON_RADV=1\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.logs.clear()
                self.write(text)
                env = {}
                self.load().apply(env)
                self.assertEqual(env, {})
                self.assertEqual(len(self.logs), 1)
                self.assertEqual(self.path.read_bytes(), text.encode())

    def test_invalid_encoding_and_unreadable_file(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"[global]\nKEY=\xff\n")
        self.load().apply({})
        self.assertEqual(len(self.logs), 1)
        self.logs.clear()
        with mock.patch("proton_config._read", side_effect=PermissionError("permission denied")):
            self.load().apply({})
        self.assertEqual(len(self.logs), 1)

    def test_read_preserves_comments_symlink_crlf_and_permissions(self):
        target = self.home / "dotfiles/proton.ini"
        target.parent.mkdir()
        original = b"# user notes\r\n[global]\r\nPROTON_HUD=3\r\n"
        target.write_bytes(original)
        target.chmod(0o640)
        self.path.parent.mkdir(parents=True)
        self.path.symlink_to(target)
        env = {}
        self.load().apply(env)
        self.assertEqual(env, {"PROTON_HUD": "3"})
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_refresh_observes_external_edits(self):
        self.write("[global]\nPROTON_HUD=3\n")
        settings = self.load()
        self.write("[global]\nPROTON_HUD=4\n")
        self.assertTrue(settings.refresh())
        env = {}
        settings.apply(env)
        self.assertEqual(env, {"PROTON_HUD": "4"})

    def test_refresh_retains_valid_preferences_if_an_edit_is_malformed(self):
        self.write("[global]\nPROTON_HUD=3\n")
        settings = self.load()
        self.write("[global]\nPROTON_HUD=4\nbroken\n")
        self.assertFalse(settings.refresh())
        env = {}
        settings.apply(env)
        self.assertEqual(env, {"PROTON_HUD": "3"})
        self.assertEqual(len(self.logs), 1)

    def test_refresh_does_not_replace_already_applied_environment(self):
        self.write("[global]\nPROTON_HUD=3\n")
        settings = self.load()
        env = {}
        settings.apply(env)
        self.write("[global]\nPROTON_HUD=4\n")
        self.assertTrue(settings.refresh())
        settings.apply(env)
        self.assertEqual(env["PROTON_HUD"], "3")


if __name__ == "__main__":
    unittest.main()
