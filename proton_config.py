"""Persistent Proton settings shared by launcher installations."""

import configparser
import os
from pathlib import Path
import re


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_STEAM_ID = re.compile(r"[0-9]+\Z")


def _parser(text):
    if "\0" in text:
        raise ValueError("configuration contains a NUL character")
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",),
                                       empty_lines_in_values=False)
    parser.optionxform = str
    parser.read_string(text)
    if parser.defaults():
        raise ValueError("use [global] instead of [DEFAULT]")
    for section in parser.sections():
        if section == "global" or section.startswith(("steam:", "umu:")):
            for key in parser[section]:
                if not _ENV_NAME.fullmatch(key):
                    raise ValueError("invalid environment variable: " + key)
    return parser


def _game_section(env):
    umu_id = env.get("UMU_ID", "")
    if umu_id and umu_id not in ("0", "umu-default"):
        if not any(character in umu_id for character in "\r\n\0[]"):
            return "umu:" + umu_id
        return None
    for key in ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID"):
        appid = env.get(key, "")
        if _STEAM_ID.fullmatch(appid) and appid.strip("0"):
            return "steam:" + appid
    return None


def _read(path):
    try:
        with path.open(encoding="utf-8", newline="") as source:
            return source.read()
    except FileNotFoundError:
        return ""


class Settings:
    """Read global and per-game environment defaults."""

    def __init__(self, env, log=lambda message: None):
        config_home = env.get("XDG_CONFIG_HOME", "")
        if not config_home or not Path(config_home).is_absolute():
            config_home = str(Path(env.get("HOME") or Path.home()) / ".config")
        self.path = Path(config_home) / "proton" / "config.ini"
        self.game_section = _game_section(env)
        self._log = log
        self._parser = _parser("")
        self.refresh()

    def refresh(self):
        """Reload saved preferences, retaining the last valid configuration on error."""
        try:
            parsed = _parser(_read(self.path))
        except (OSError, UnicodeError, ValueError, configparser.Error) as error:
            self._log("Ignoring Proton configuration %s: %s" % (self.path, error))
            return False
        self._parser = parsed
        return True

    def apply(self, env):
        """Fill absent environment variables; explicit launch values win."""
        values = {}
        for section in ("global", self.game_section):
            if section is not None and self._parser.has_section(section):
                values.update(self._parser[section])
        for key, value in values.items():
            env.setdefault(key, value)
