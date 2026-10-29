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

scan_spec = importlib.util.spec_from_file_location(
    "scan", EXTENSION / "scan_web_domains.py"
)
scan = importlib.util.module_from_spec(scan_spec)
assert scan_spec.loader
scan_spec.loader.exec_module(scan)


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
assert '"/usr/bin/open", "-g", str(APP_PATH)' in source
assert "Shell Script File" in config_source
assert "show-status" in config_source
assert not (EXTENSION / "bettbox_ui").exists()
assert not (EXTENSION / "bettbox_ui.swift").exists()
assert scan.playwright_setup_error("browserType.launch: Executable doesn't exist") == (
    scan.CHROMIUM_INSTALL,
    "首次使用需安装 Chromium；安装命令已复制，请粘贴到终端运行",
)
assert scan.playwright_setup_error("Error: 找不到 Playwright") == (
    scan.PLAYWRIGHT_INSTALL,
    "首次使用需安装 Playwright；安装命令已复制，请粘贴到终端运行",
)

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


def write_support_config(root: Path, config: dict) -> None:
    support = root / "support"
    support.mkdir(parents=True, exist_ok=True)
    (support / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


# Bettbox >= 1.19 reads support/config.json first and mirrors it to the plist.
# The rule must survive a launch that behaves like that (regression test for
# "规则已写入但 Bettbox 启动后未保留该规则").
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    prefs = root / "prefs.plist"
    write_fixture(prefs, base_config())
    write_support_config(root, base_config())
    env = fixture_env(root)
    env["BETTBOX_TEST_STARTUP_MODEL"] = "file-first"
    env["POPCLIP_TEXT"] = "file-first.example"
    result = subprocess.run(
        ["/usr/bin/python3", str(HELPER)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "已加入 Bettbox 并恢复 Mihomo" in result.stdout, result.stdout
    support = root / "support"
    from_file = json.loads((support / "config.json").read_text(encoding="utf-8"))
    from_plist = json.loads(plistlib.loads((prefs).read_bytes())["flutter.config"])
    for source in (from_file, from_plist):
        value = source["profiles"][0]["overrideData"]["rule"]["addedRules"][0]["value"]
        assert value == "DOMAIN-SUFFIX,file-first.example," + helper.TARGET, value


def write_profile_yaml(root: Path, profile_id: str, text: str) -> None:
    folder = root / "support" / "profiles"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{profile_id}.yaml").write_text(text, encoding="utf-8")


def run_helper(root: Path, domain: str, extra_env: dict | None = None):
    env = fixture_env(root)
    env["POPCLIP_TEXT"] = domain
    env.update(extra_env or {})
    return subprocess.run(
        ["/usr/bin/python3", str(HELPER)], env=env, text=True, capture_output=True, check=True
    )


# The manual-select group may be renamed (for example "☝️ 手动选择"): the rule must
# target whatever the profile really contains, and stale rules pointing at the old
# name must be repointed (Mihomo rejects a rule whose target group does not exist).
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    prefs = root / "prefs.plist"
    config = base_config()
    # Bettbox keeps stale keys in selectedMap after a group is renamed.
    config["profiles"][0]["selectedMap"] = {"🚀 手动选择": "old", "☝️ 手动选择": "node"}
    config["profiles"][0]["overrideData"]["rule"]["addedRules"] = [
        {"id": "stale", "value": "DOMAIN-SUFFIX,stale.example,🚀 手动选择"}
    ]
    write_fixture(prefs, config)
    write_support_config(root, config)
    write_profile_yaml(
        root,
        "p1",
        'proxy-groups:\n  - {"name":"GLOBAL","type":"select","proxies":["DIRECT"]}\n'
        '  - {"name":"☝️ 手动选择","type":"select","proxies":["DIRECT"]}\n'
        '  - {"name":"♻️ 自动选择(测速)","type":"url-test","proxies":["a"]}\n',
    )
    result = run_helper(root, "renamed.example", {"BETTBOX_TEST_STARTUP_MODEL": "file-first"})
    assert "已加入 Bettbox 并恢复 Mihomo" in result.stdout, result.stdout
    rules = json.loads((root / "support" / "config.json").read_text(encoding="utf-8"))[
        "profiles"
    ][0]["overrideData"]["rule"]["addedRules"]
    values = [item["value"] for item in rules]
    assert values[0] == "DOMAIN-SUFFIX,renamed.example,☝️ 手动选择", values
    assert "DOMAIN-SUFFIX,stale.example,☝️ 手动选择" in values, values
    assert not any("🚀" in value for value in values), values

# Block-style YAML is understood as well as SubMerger's flow style.
with tempfile.TemporaryDirectory() as td:
    folder = Path(td)
    (folder / "a.yaml").write_text(
        "proxy-groups:\n- name: GLOBAL\n  type: select\n- name: 手动选择\n  type: select\n- name: 自动手动选择测试\n  type: url-test\n",
        encoding="utf-8",
    )
    assert helper.profile_group_names(folder / "a.yaml") == [
        ("GLOBAL", "select"),
        ("手动选择", "select"),
        ("自动手动选择测试", "url-test"),
    ]
    assert helper.profile_group_names(folder / "missing.yaml") is None

# No manual-select group in the profile: refuse before Bettbox is asked to quit.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    prefs = root / "prefs.plist"
    write_fixture(prefs, base_config())
    write_support_config(root, base_config())
    write_profile_yaml(root, "p1", 'proxy-groups:\n  - {"name":"GLOBAL","type":"select","proxies":["DIRECT"]}\n')
    result = run_helper(root, "nogroup.example")
    assert "未添加：当前 profile 中没有名称含“手动选择”的策略组" in result.stdout, result.stdout
    events = root / "events.log"
    assert not events.exists() or events.read_text(encoding="utf-8").strip() == "", "Bettbox must not be stopped"
    on_disk = json.loads((root / "support" / "config.json").read_text(encoding="utf-8"))
    assert not on_disk["profiles"][0]["overrideData"]["rule"]["addedRules"]

# Store selection mirrors Bettbox: config.json first, plist only as a fallback.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    prefs = root / "prefs.plist"
    plist_config = base_config()
    plist_config["currentProfileId"] = "from-plist"
    plist_config["profiles"][0]["id"] = "from-plist"
    file_config = base_config()
    write_fixture(prefs, plist_config)
    assert helper.load_config(prefs)["currentProfileId"] == "from-plist"  # no config.json yet
    write_support_config(root, file_config)
    assert helper.load_config(prefs)["currentProfileId"] == "p1"  # file wins
    write_support_config(root, {"profiles": []})
    assert helper.load_config(prefs)["currentProfileId"] == "from-plist"  # empty file -> fallback
    (root / "support" / "config.json").write_text("{not json", encoding="utf-8")
    assert helper.load_config(prefs)["currentProfileId"] == "from-plist"  # corrupt file -> fallback


print("all command-only helper tests passed")
