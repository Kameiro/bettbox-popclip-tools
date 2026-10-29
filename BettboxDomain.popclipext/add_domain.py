#!/usr/bin/python3
"""Add a PopClip-selected domain to Bettbox without UI automation."""

from __future__ import annotations

import datetime as dt
import fcntl
import importlib
import ipaddress
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


APP_ID = "com.appshub.bettbox"
APP_PATH = Path("/Applications/Bettbox.app")
APP_BINARY = APP_PATH / "Contents" / "MacOS" / "Bettbox"
CONFIG_FILE = "config.json"
# Default (legacy) name of the manual-select group. The real target is resolved
# per profile by resolve_target(), so a renamed group such as "☝️ 手动选择" works too.
TARGET = "🚀 手动选择"
MANUAL_MARK = "手动选择"
RULE_TYPE = "DOMAIN-SUFFIX"
STOP_TIMEOUT = 60.0
START_TIMEOUT = 60.0


class UserError(Exception):
    pass


def paths() -> tuple[Path, Path]:
    if os.environ.get("BETTBOX_TEST_MODE") == "1":
        return (
            Path(os.environ["BETTBOX_PREFS_PATH"]),
            Path(os.environ["BETTBOX_SUPPORT_PATH"]),
        )
    home = Path.home()
    return (
        home / "Library" / "Preferences" / f"{APP_ID}.plist",
        home / "Library" / "Application Support" / APP_ID,
    )


def test_mode() -> bool:
    return os.environ.get("BETTBOX_TEST_MODE") == "1"


def record_test_event(event: str) -> None:
    if not test_mode():
        return
    event_path = os.environ.get("BETTBOX_TEST_EVENTS_PATH")
    if event_path:
        with Path(event_path).open("a", encoding="utf-8") as handle:
            handle.write(event + "\n")


def write_diagnostic(stage: str, detail: str = "") -> None:
    try:
        _, support = paths()
        support.mkdir(parents=True, exist_ok=True)
        log_path = support / "popclip-bettbox-domain.log"
        if log_path.exists() and log_path.stat().st_size > 200_000:
            log_path.replace(log_path.with_suffix(".log.previous"))
        safe = detail.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{stage}\t{safe[:240]}\n")
    except OSError:
        pass


def load_sibling(name: str) -> Any:
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    return importlib.import_module(name)


def notify(message: str) -> None:
    """Say something PopClip's tick/cross cannot. Never raises."""
    try:
        load_sibling("feedback").notify(message)
    except Exception:
        write_diagnostic("notify-failed")


def check_for_updates() -> None:
    """Start the once-a-day update check if one is due. Never raises, never waits."""
    try:
        load_sibling("update_check").maybe_check_updates()
    except Exception:
        write_diagnostic("update-hook-failed")


def normalize_domain(value: str) -> str:
    text = value.strip()
    if not text or any(ch.isspace() for ch in text):
        raise UserError("请选择一个不含空白的域名或 URL")
    if "*" in text:
        raise UserError("不接受通配符域名")
    if "://" in text:
        try:
            parsed = urlsplit(text)
        except ValueError as exc:
            raise UserError("URL 格式无效") from exc
        if parsed.scheme.lower() not in {"http", "https"}:
            raise UserError("只接受 HTTP 或 HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise UserError("不接受含凭据的 URL")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise UserError("URL 端口无效") from exc
        host = parsed.hostname
    else:
        if any(mark in text for mark in "/:@?#"):
            raise UserError("域名格式无效；若包含路径，请选择完整 HTTP(S) URL")
        host = text
    if not host:
        raise UserError("未找到域名")
    host = host.rstrip(".").lower()
    if not host or host == "localhost":
        raise UserError("不接受 localhost")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UserError("不接受 IP 地址")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UserError("国际化域名无法规范化") from exc
    if len(ascii_host) > 253 or "." not in ascii_host:
        raise UserError("域名必须包含有效的后缀")
    label_re = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    labels = ascii_host.split(".")
    if any(not label_re.fullmatch(label) for label in labels):
        raise UserError("域名标签格式无效")
    if all(label.isdigit() for label in labels):
        raise UserError("不接受数字形式的主机")
    return ascii_host


def support_for(prefs: Path) -> Path:
    """Application Support directory that belongs to this preferences file."""
    known_prefs, known_support = paths()
    return known_support if prefs == known_prefs else prefs.parent / "support"


def read_json_config(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def has_profiles(config: object) -> bool:
    return (
        isinstance(config, dict)
        and isinstance(config.get("profiles"), list)
        and len(config["profiles"]) > 0
    )


def load_preferences(
    prefs: Path, support: Path | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load Bettbox's config the way Bettbox >= 1.19 does.

    ``support/config.json`` is the primary store; the plist ``flutter.config``
    value is a mirror that is only used when the file is missing or has no
    profiles. Returns the plist (needed to write the mirror) and the config.
    """
    support = support or support_for(prefs)
    file_config = read_json_config(support / CONFIG_FILE)
    plist: dict[str, Any] = {}
    plist_config = None
    if prefs.is_file():
        try:
            with prefs.open("rb") as handle:
                plist = plistlib.load(handle)
            raw = plist.get("flutter.config")
            plist_config = json.loads(raw) if isinstance(raw, str) else None
        except (OSError, plistlib.InvalidFileException, json.JSONDecodeError) as exc:
            if not has_profiles(file_config):
                raise UserError("Bettbox 配置无法读取") from exc
    elif not has_profiles(file_config):
        raise UserError("找不到 Bettbox 偏好设置")
    if has_profiles(file_config):
        return plist, file_config
    if not has_profiles(plist_config):
        raise UserError("Bettbox 配置结构无效")
    return plist, plist_config


def load_config(prefs: Path, support: Path | None = None) -> dict[str, Any]:
    return load_preferences(prefs, support)[1]


def current_profile(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    wanted = str(config.get("currentProfileId", ""))
    for profile in config["profiles"]:
        if isinstance(profile, dict) and str(profile.get("id", "")) == wanted:
            return wanted, profile
    raise UserError("找不到 Bettbox 当前 profile")


def profile_group_names(profile_file: Path) -> list[tuple[str, str]] | None:
    """(name, type) of every proxy group in a Bettbox profile file, or None if unreadable.

    Handles both the flow style written by SubMerger ({"name":"x","type":"select",...})
    and ordinary block-style YAML, without needing a YAML library.
    """
    try:
        text = profile_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    groups: list[tuple[str, str]] = []
    for match in re.finditer(
        r'\{\s*"name"\s*:\s*("(?:[^"\\]|\\.)*")\s*,\s*"type"\s*:\s*"?([A-Za-z-]+)"?', text
    ):
        try:
            groups.append((json.loads(match.group(1)), match.group(2).lower()))
        except json.JSONDecodeError:
            continue
    if groups:
        return groups
    lines = text.splitlines()
    for index, line in enumerate(lines):
        named = re.match(r"^\s*-\s+name:\s*(.+?)\s*$", line)
        if not named:
            continue
        name = named.group(1).strip("'\"")
        for follow in lines[index + 1 : index + 8]:
            if re.match(r"^\s*-\s+name:", follow):
                break
            kind = re.match(r"^\s+type:\s*['\"]?([A-Za-z-]+)", follow)
            if kind:
                groups.append((name, kind.group(1).lower()))
                break
    return groups


def is_manual_target(target: str) -> bool:
    return MANUAL_MARK in target


def resolve_target(profile_id: str, profile: dict[str, Any], support: Path) -> str:
    """Name of the profile's manual-select group (a ``select`` group whose name contains 手动选择)."""
    groups = profile_group_names(support / "profiles" / f"{profile_id}.yaml")
    if groups is not None and groups:
        candidates = [n for n, kind in groups if kind == "select" and is_manual_target(n)]
    else:
        selected = profile.get("selectedMap")
        candidates = (
            [k for k in selected if isinstance(k, str) and is_manual_target(k)]
            if isinstance(selected, dict)
            else []
        )
    if not candidates:
        raise UserError(f"当前 profile 中没有名称含“{MANUAL_MARK}”的策略组")
    return TARGET if TARGET in candidates else candidates[0]


def same_rule(value: object, domain: str, target: str | None = None) -> bool:
    if not isinstance(value, str):
        return False
    parts = [part.strip() for part in value.split(",", 2)]
    if len(parts) != 3 or parts[0].upper() != RULE_TYPE:
        return False
    if target is not None:
        if parts[2] != target:
            return False
    elif not is_manual_target(parts[2]):
        return False
    try:
        return normalize_domain(parts[1]) == domain
    except UserError:
        return False


def has_active_rule(
    profile: dict[str, Any], domain: str, target: str | None = None
) -> bool:
    data = profile.get("overrideData") or {}
    rule_data = data.get("rule") or {}
    rules = rule_data.get("addedRules") or []
    return (
        data.get("enable") is True
        and rule_data.get("type") == "added"
        and any(
            isinstance(item, dict) and same_rule(item.get("value"), domain, target)
            for item in rules
        )
    )


def process_pid(pattern: str, test_key: str) -> int | None:
    if test_mode():
        value = os.environ.get(test_key, "")
        return int(value) if value.isdigit() else None
    result = subprocess.run(
        ["/usr/bin/pgrep", "-f", pattern],
        text=True,
        capture_output=True,
        check=False,
    )
    values = [line for line in result.stdout.splitlines() if line.strip().isdigit()]
    return int(values[0]) if result.returncode == 0 and len(values) == 1 else None


def bettbox_pid() -> int | None:
    return process_pid(
        r"^/Applications/Bettbox\.app/Contents/MacOS/Bettbox$",
        "BETTBOX_TEST_PID",
    )


def core_pid() -> int | None:
    return process_pid(
        r"^/Applications/Bettbox\.app/Contents/MacOS/BettboxCore( |$)",
        "BETTBOX_TEST_CORE_PID",
    )


def configured_service_requirements(prefs: Path) -> tuple[bool, bool, int]:
    config = load_config(prefs)
    patch = config.get("patchClashConfig") or {}
    tun = patch.get("tun") or {}
    network = config.get("networkProps") or {}
    return (
        bool(tun.get("enable")),
        bool(network.get("systemProxy")),
        int(network.get("port") or patch.get("mixed-port") or 7890),
    )


def tun_ready() -> bool:
    if test_mode():
        return os.environ.get("BETTBOX_TEST_TUN_READY", "1") == "1"
    result = subprocess.run(
        ["/sbin/ifconfig"], text=True, capture_output=True, check=False
    )
    blocks = re.split(r"(?=^utun\d+:)", result.stdout, flags=re.MULTILINE)
    return any(
        "UP" in block.splitlines()[0] and "inet 198.18.0.1" in block
        for block in blocks
        if block.startswith("utun") and block.splitlines()
    )


def tcp_listening(port: int) -> bool:
    if test_mode():
        return os.environ.get("BETTBOX_TEST_PROXY_READY", "1") == "1"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def services_ready(prefs: Path) -> bool:
    if core_pid() is None:
        return False
    tun_required, proxy_required, port = configured_service_requirements(prefs)
    return (
        (not tun_required or tun_ready())
        and (not proxy_required or tcp_listening(port))
    )


def stop_bettbox_safely() -> None:
    if test_mode():
        record_test_event("stop")
        return
    if bettbox_pid() is None and core_pid() is None:
        return
    if not APP_BINARY.is_file():
        raise UserError("找不到 Bettbox 应用")
    try:
        result = subprocess.run(
            [str(APP_BINARY), "--exit"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=6,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserError("无法请求 Bettbox 正常退出") from exc
    if result.returncode != 0:
        raise UserError("Bettbox 拒绝了正常退出请求")
    official_deadline = time.monotonic() + 5.0
    while time.monotonic() < official_deadline:
        if bettbox_pid() is None:
            write_diagnostic("stopped", "official")
            return
        time.sleep(0.25)

    quit_helper = Path(__file__).with_name("bettbox_quit")
    if not quit_helper.is_file() or not os.access(quit_helper, os.X_OK):
        raise UserError("Bettbox 原生命令辅助程序缺失，未写入规则")
    try:
        result = subprocess.run(
            [str(quit_helper)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=6,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserError("无法请求 macOS 正常终止 Bettbox") from exc
    if result.returncode != 0:
        raise UserError("macOS 拒绝了 Bettbox 正常终止请求")
    write_diagnostic("stop-fallback", "native-terminate")

    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if bettbox_pid() is None:
            write_diagnostic("stopped", "native-terminate")
            return
        time.sleep(0.25)
    raise UserError("Bettbox 未能正常退出，未写入规则")


def simulate_bettbox_startup(prefs: Path, support: Path) -> None:
    """Test-only model of Bettbox >= 1.19 starting up.

    The app reads support/config.json first, falls back to the preferences
    plist only when that file is missing or has no profiles, and then writes
    what it loaded back to both stores. Edits made only to the plist are
    therefore lost on the next launch.
    """
    if os.environ.get("BETTBOX_TEST_STARTUP_MODEL") != "file-first":
        return
    try:
        loaded = json.loads((support / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        loaded = None
    if not (isinstance(loaded, dict) and loaded.get("profiles")):
        with prefs.open("rb") as handle:
            loaded = json.loads(plistlib.load(handle)["flutter.config"])
    text = json.dumps(loaded, ensure_ascii=False, separators=(",", ":"))
    (support / "config.json").write_text(text, encoding="utf-8")
    with prefs.open("rb") as handle:
        plist = plistlib.load(handle)
    plist["flutter.config"] = text
    with prefs.open("wb") as handle:
        plistlib.dump(plist, handle, fmt=plistlib.FMT_BINARY, sort_keys=False)


def launch_bettbox_once(prefs: Path) -> int:
    if test_mode():
        record_test_event("launch")
        simulate_bettbox_startup(prefs, paths()[1])
        return int(os.environ.get("BETTBOX_TEST_LAUNCHED_PID", "901"))
    if not APP_PATH.is_dir():
        raise UserError("找不到 Bettbox 应用")
    for attempt in range(3):
        time.sleep(1.0 if attempt == 0 else 2.0)
        try:
            result = subprocess.run(
                ["/usr/bin/open", "-g", str(APP_PATH)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=6,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if attempt == 2:
                raise UserError("无法启动 Bettbox") from exc
            continue
        if result.returncode == 0:
            break
        write_diagnostic("launch-retry", result.stderr.strip()[:160])
    else:
        raise UserError("无法启动 Bettbox")
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        main_pid = bettbox_pid()
        if main_pid is not None and services_ready(prefs):
            write_diagnostic("services-ready", str(main_pid))
            return main_pid
        time.sleep(0.25)
    raise UserError("规则已保存，但 Bettbox、Mihomo 或网络代理未恢复")


def backup_preferences(prefs: Path, support: Path) -> Path:
    """Back up both stores: the plist mirror and, if present, the primary config.json."""
    backup_dir = support / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_dir / f"{APP_ID}.plist.{stamp}.bak"
    shutil.copy2(prefs, backup)
    config_file = support / CONFIG_FILE
    if config_file.is_file():
        shutil.copy2(config_file, backup_dir / f"{APP_ID}.config.json.{stamp}.bak")
    return backup


def config_backup_for(backup: Path) -> Path:
    return backup.with_name(backup.name.replace(".plist.", ".config.json.", 1))


def write_text_atomic(path: Path, text: str, default_mode: int = 0o644) -> None:
    mode = (path.stat().st_mode & 0o777) if path.exists() else default_mode
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def write_atomic(prefs: Path, plist: dict[str, Any], config: dict[str, Any]) -> None:
    """Write the plist mirror (flutter.config) atomically."""
    plist["flutter.config"] = json.dumps(
        config, ensure_ascii=False, separators=(",", ":")
    )
    mode = prefs.stat().st_mode & 0o777
    fd, temp_name = tempfile.mkstemp(prefix=f".{prefs.name}.", dir=prefs.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(plist, handle, fmt=plistlib.FMT_BINARY, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, prefs)
    finally:
        if temp.exists():
            temp.unlink()


def write_stores(
    prefs: Path, support: Path, plist: dict[str, Any], config: dict[str, Any]
) -> None:
    """Write the config to both stores. config.json (primary) goes first so that
    an interrupted write can never leave Bettbox reading a stale primary."""
    text = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    support.mkdir(parents=True, exist_ok=True)
    write_text_atomic(support / CONFIG_FILE, text, default_mode=0o644)
    write_atomic(prefs, plist, config)


def restore_stores(prefs: Path, support: Path, backup: Path) -> None:
    shutil.copy2(backup, prefs)
    config_backup = config_backup_for(backup)
    if config_backup.is_file():
        shutil.copy2(config_backup, support / CONFIG_FILE)


def persist_rule(domain: str, prefs: Path, support: Path) -> str:
    plist, config = load_preferences(prefs, support)
    profile_id, profile = current_profile(config)
    target = resolve_target(profile_id, profile, support)
    app_setting = config.get("appSetting")
    if not isinstance(app_setting, dict):
        raise UserError("Bettbox 自动运行配置结构无效")
    app_setting["autoRun"] = True
    data = profile.setdefault("overrideData", {})
    if not isinstance(data, dict):
        data = {}
        profile["overrideData"] = data
    rule_data = data.setdefault("rule", {})
    if not isinstance(rule_data, dict):
        rule_data = {}
        data["rule"] = rule_data
    rules = rule_data.get("addedRules")
    if not isinstance(rules, list):
        rules = []
    # Rules written earlier may point at a manual-select group that was renamed
    # since; repoint them, otherwise Mihomo rejects the whole profile.
    migrated = 0
    for item in rules:
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            continue
        parts = [part.strip() for part in item["value"].split(",", 2)]
        if (
            len(parts) == 3
            and parts[0].upper() == RULE_TYPE
            and is_manual_target(parts[2])
            and parts[2] != target
        ):
            item["value"] = f"{parts[0]},{parts[1]},{target}"
            migrated += 1
    duplicate = any(
        isinstance(item, dict) and same_rule(item.get("value"), domain, target)
        for item in rules
    )
    if not duplicate:
        rules.insert(
            0,
            {
                "id": str(uuid.uuid4()),
                "value": f"{RULE_TYPE},{domain},{target}",
            },
        )
    data["enable"] = True
    rule_data["type"] = "added"
    rule_data["addedRules"] = rules
    backup = backup_preferences(prefs, support)
    write_diagnostic("backup", backup.name)
    if migrated:
        write_diagnostic("migrated-target", str(migrated))
    try:
        record_test_event("write")
        write_stores(prefs, support, plist, config)
        written = load_config(prefs, support)
        written_id, written_profile = current_profile(written)
        mirror = json.loads(plistlib.loads(prefs.read_bytes())["flutter.config"])
        mirror_id, mirror_profile = current_profile(mirror)
        if (
            written_id != profile_id
            or mirror_id != profile_id
            or not has_active_rule(written_profile, domain, target)
            or not has_active_rule(mirror_profile, domain, target)
        ):
            raise RuntimeError("written rule validation failed")
    except Exception:
        restore_stores(prefs, support, backup)
        raise
    write_diagnostic("saved", domain)
    return "exists" if duplicate and not migrated else "added"


def active_target_domains(profile: dict[str, Any]) -> list[str]:
    rule_data = (profile.get("overrideData") or {}).get("rule") or {}
    domains: set[str] = set()
    for item in rule_data.get("addedRules") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, str):
            continue
        parts = [part.strip() for part in value.split(",", 2)]
        if len(parts) != 3 or parts[0].upper() != RULE_TYPE or not is_manual_target(parts[2]):
            continue
        try:
            domains.add(normalize_domain(parts[1]))
        except UserError:
            continue
    return sorted(domains)


def mark_applied(prefs: Path, support: Path) -> None:
    running_core = core_pid()
    if running_core is None:
        return
    config = load_config(prefs, support)
    _, profile = current_profile(config)
    state = {
        "core_pid": running_core,
        "domains": active_target_domains(profile),
    }
    marker = support / "popclip-bettbox-domain.applied.json"
    temp = marker.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, marker)


def confirmed_applied(domain: str, prefs: Path, support: Path) -> bool:
    marker = support / "popclip-bettbox-domain.applied.json"
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        state.get("core_pid") == core_pid()
        and domain in state.get("domains", [])
        and services_ready(prefs)
    )


def perform_worker(domain: str) -> str:
    prefs, support = paths()
    support.mkdir(parents=True, exist_ok=True)
    lock_path = support / "popclip-bettbox-domain.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UserError("已有一条 Bettbox 规则正在处理") from exc
        write_diagnostic("worker-start", domain)
        _, initial_config = load_preferences(prefs, support)
        initial_id, initial_profile = current_profile(initial_config)
        # Fail early with a clear message, before Bettbox is asked to quit.
        initial_target = resolve_target(initial_id, initial_profile, support)
        already_in_file = has_active_rule(initial_profile, domain, initial_target)
        if already_in_file and confirmed_applied(domain, prefs, support):
            write_diagnostic("exists-applied", domain)
            return "exists"
        if bettbox_pid() is not None or core_pid() is not None:
            write_diagnostic("stop-request")
            stop_bettbox_safely()
        status = persist_rule(domain, prefs, support)
        launch_bettbox_once(prefs)
        # Read it back the way Bettbox itself does (config.json first).
        final_config = load_config(prefs, support)
        final_id, final_profile = current_profile(final_config)
        if not has_active_rule(
            final_profile, domain, resolve_target(final_id, final_profile, support)
        ):
            raise UserError("Bettbox 启动后未保留该规则")
        mark_applied(prefs, support)
        write_diagnostic("complete", domain)
        return "exists" if already_in_file and status == "exists" else "added"


def worker_running() -> bool:
    if test_mode():
        return False
    pattern = re.escape(str(Path(__file__).resolve())) + r" --worker "
    return subprocess.run(
        ["/usr/bin/pgrep", "-f", pattern],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def spawn_worker(domain: str) -> None:
    if worker_running():
        raise UserError("已有一条 Bettbox 规则正在处理")
    try:
        subprocess.Popen(
            ["/usr/bin/python3", str(Path(__file__).resolve()), "--worker", domain],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise UserError("无法启动 Bettbox 命令任务") from exc


def done_message(domain: str, status: str) -> str:
    return (
        f"已存在，无需修改：{domain}"
        if status == "exists"
        else f"已加入 Bettbox 并恢复 Mihomo：{domain}"
    )


def report_failure(
    reason: str, domain: str | None, popclip_call: bool, background: bool
) -> None:
    """Make a failure visible. The exit code (set by main) draws PopClip's cross;
    the reason goes to a notification, because the cross carries no text."""
    if popclip_call:
        print(f"未添加：{reason}")
        notify(f"未添加：{reason}")
        check_for_updates()
    elif background:
        notify(f"未能加入{' ' + domain if domain else ''}：{reason}")
        check_for_updates()
    else:
        print(f"未添加：{reason}")


def main() -> int:
    popclip_call = len(sys.argv) == 1
    background = len(sys.argv) == 3 and sys.argv[1] == "--worker"
    run_now = len(sys.argv) == 3 and sys.argv[1] == "--run-now"
    domain: str | None = None
    try:
        if popclip_call:
            domain = normalize_domain(os.environ.get("POPCLIP_TEXT", ""))
            write_diagnostic("start", domain)
            if test_mode():
                message = done_message(domain, perform_worker(domain))
                print(message)
                notify(message)
                check_for_updates()
                return 0
            prefs, support = paths()
            config = load_config(prefs, support)
            profile_id, profile = current_profile(config)
            target = resolve_target(profile_id, profile, support)
            if has_active_rule(profile, domain, target) and confirmed_applied(
                domain, prefs, support
            ):
                print(f"已存在，无需修改：{domain}")
                check_for_updates()
                return 0
            spawn_worker(domain)
            print(f"正在通过命令加入 Bettbox：{domain}")
            # The update check waits for the worker: Bettbox restarting cuts the network.
            return 0
        if background or run_now:
            domain = normalize_domain(sys.argv[2])
            status = perform_worker(domain)
            if run_now:
                print(
                    f"已存在且已生效：{domain}"
                    if status == "exists"
                    else f"已加入 Bettbox 并恢复 Mihomo：{domain}"
                )
            else:
                notify(done_message(domain, status))
                check_for_updates()
            return 0
        raise UserError("调用参数无效")
    except UserError as exc:
        write_diagnostic("error", str(exc))
        report_failure(str(exc), domain, popclip_call, background)
        return 1
    except Exception:
        write_diagnostic("error", "unexpected")
        report_failure(
            "写入或验证失败，原配置已保留/恢复", domain, popclip_call, background
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
