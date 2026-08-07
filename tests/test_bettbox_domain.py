#!/usr/bin/python3
from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import subprocess
import tempfile
from pathlib import Path


EXTENSION = Path(__file__).parent.parent / "BettboxDomain.popclipext"
HELPER = EXTENSION / "add_domain.py"
spec = importlib.util.spec_from_file_location("helper", HELPER)
helper = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(helper)


def write_fixture(path: Path, config: dict) -> bytes:
    with path.open("wb") as handle:
        plistlib.dump(
            {"flutter.config": json.dumps(config)},
            handle,
            fmt=plistlib.FMT_BINARY,
        )
    return path.read_bytes()


def fixture_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "BETTBOX_TEST_MODE": "1",
            "BETTBOX_PREFS_PATH": str(root / "prefs.plist"),
            "BETTBOX_SUPPORT_PATH": str(root / "support"),
            "BETTBOX_TEST_EVENTS_PATH": str(root / "events.log"),
            "BETTBOX_TEST_PID": "899",
            "BETTBOX_TEST_CORE_PID": "94472",
            "BETTBOX_TEST_LAUNCHED_PID": "901",
            "BETTBOX_TEST_TUN_READY": "1",
            "BETTBOX_TEST_PROXY_READY": "1",
        }
    )
    return env


def base_config() -> dict:
    return {
        "currentProfileId": "p1",
        "appSetting": {"autoRun": False},
        "patchClashConfig": {
            "mixed-port": 7890,
            "tun": {"enable": True},
        },
        "networkProps": {"systemProxy": True},
        "profiles": [
            {
                "id": "p1",
                "label": "test-profile",
                "selectedMap": {helper.TARGET: "node"},
                "overrideData": {
                    "enable": True,
                    "rule": {"type": "added", "addedRules": []},
                },
            }
        ],
    }


source = HELPER.read_text(encoding="utf-8")
config_source = (EXTENSION / "Config.plist").read_text(encoding="utf-8")
for forbidden in [
    "bettbox_ui",
    "AppleScript",
    "osascript",
    "System Events",
    "click at",
    "pkill",
    "kill(",
]:
    assert forbidden not in source
assert 'APP_BINARY), "--exit"' in source
assert '"/usr/bin/open", "-b", APP_ID' in source
assert "Shell Script File" in config_source
assert "show-status" in config_source
assert not (EXTENSION / "bettbox_ui").exists()
assert not (EXTENSION / "bettbox_ui.swift").exists()

assert helper.normalize_domain("HTTPS://WWW.Example.COM.:443/a?q=1") == "www.example.com"
assert helper.normalize_domain("例子.中国") == "xn--fsqu00a.xn--fiqs8s"
for bad in [
    "localhost",
    "127.0.0.1",
    "[::1]",
    "*.example.com",
    "example.com;touch /tmp/x",
    "https://u:p@example.com",
    "https://example.com:99999",
    "https://example.com bad",
    "ftp://example.com",
    "example",
]:
    try:
        helper.normalize_domain(bad)
    except helper.UserError:
        pass
    else:
        raise AssertionError(f"accepted invalid input: {bad!r}")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    prefs = root / "prefs.plist"
    write_fixture(prefs, base_config())
    env = fixture_env(root)
    env["POPCLIP_TEXT"] = "command-only.example"
    result = subprocess.run(
        ["/usr/bin/python3", str(HELPER)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "已加入 Bettbox 并恢复 Mihomo" in result.stdout
    events = (root / "events.log").read_text(encoding="utf-8").splitlines()
    assert events == ["stop", "write", "launch"]
    _, profile = helper.current_profile(helper.load_config(prefs))
    assert helper.load_config(prefs)["appSetting"]["autoRun"] is True
    assert helper.has_active_rule(profile, "command-only.example")
    rules = profile["overrideData"]["rule"]["addedRules"]
    assert rules[0]["value"] == "DOMAIN-SUFFIX,command-only.example,🚀 手动选择"
    assert len(list((root / "support" / "backups").glob("*.bak"))) == 1


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    prefs = root / "prefs.plist"
    config = base_config()
    config["profiles"][0]["overrideData"]["rule"]["addedRules"] = [
        {
            "id": "existing",
            "value": "DOMAIN-SUFFIX,EXAMPLE.COM.,🚀 手动选择",
        }
    ]
    write_fixture(prefs, config)
    env = fixture_env(root)
    env["POPCLIP_TEXT"] = "example.com"
    result = subprocess.run(
        ["/usr/bin/python3", str(HELPER)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "已存在，无需修改" in result.stdout
    _, profile = helper.current_profile(helper.load_config(prefs))
    assert len(profile["overrideData"]["rule"]["addedRules"]) == 1


print("all command-only helper tests passed")
