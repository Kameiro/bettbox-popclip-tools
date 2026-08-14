#!/usr/bin/python3
"""Add a PopClip-selected domain to Bettbox without UI automation."""

from __future__ import annotations

import datetime as dt
import fcntl
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
TARGET = "🚀 手动选择"
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


def load_preferences(prefs: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not prefs.is_file():
        raise UserError("找不到 Bettbox 偏好设置")
    try:
        with prefs.open("rb") as handle:
            plist = plistlib.load(handle)
        raw = plist.get("flutter.config")
        config = json.loads(raw) if isinstance(raw, str) else None
    except (OSError, plistlib.InvalidFileException, json.JSONDecodeError) as exc:
        raise UserError("Bettbox 配置无法读取") from exc
    if not isinstance(config, dict) or not isinstance(config.get("profiles"), list):
        raise UserError("Bettbox 配置结构无效")
    return plist, config


def load_config(prefs: Path) -> dict[str, Any]:
    return load_preferences(prefs)[1]


def current_profile(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    wanted = str(config.get("currentProfileId", ""))
    for profile in config["profiles"]:
        if isinstance(profile, dict) and str(profile.get("id", "")) == wanted:
            selected = profile.get("selectedMap")
            if not isinstance(selected, dict) or TARGET not in selected:
                raise UserError(f"当前 profile 中不存在策略组 {TARGET}")
            return wanted, profile
    raise UserError("找不到 Bettbox 当前 profile")


def same_rule(value: object, domain: str) -> bool:
    if not isinstance(value, str):
        return False
    parts = [part.strip() for part in value.split(",", 2)]
    if len(parts) != 3 or parts[0].upper() != RULE_TYPE or parts[2] != TARGET:
        return False
    try:
        return normalize_domain(parts[1]) == domain
    except UserError:
        return False


def has_active_rule(profile: dict[str, Any], domain: str) -> bool:
    data = profile.get("overrideData") or {}
    rule_data = data.get("rule") or {}
    rules = rule_data.get("addedRules") or []
    return (
        data.get("enable") is True
        and rule_data.get("type") == "added"
        and any(
            isinstance(item, dict) and same_rule(item.get("value"), domain)
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


def launch_bettbox_once(prefs: Path) -> int:
    if test_mode():
        record_test_event("launch")
        return int(os.environ.get("BETTBOX_TEST_LAUNCHED_PID", "901"))
    if not APP_PATH.is_dir():
        raise UserError("找不到 Bettbox 应用")
    try:
        result = subprocess.run(
            ["/usr/bin/open", "-gj", "-a", "Bettbox"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=6,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserError("无法启动 Bettbox") from exc
    if result.returncode != 0:
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
    backup_dir = support / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_dir / f"{APP_ID}.plist.{stamp}.bak"
    shutil.copy2(prefs, backup)
    return backup


def write_atomic(prefs: Path, plist: dict[str, Any], config: dict[str, Any]) -> None:
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


def persist_rule(domain: str, prefs: Path, support: Path) -> str:
    plist, config = load_preferences(prefs)
    profile_id, profile = current_profile(config)
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
    duplicate = any(
        isinstance(item, dict) and same_rule(item.get("value"), domain)
        for item in rules
    )
    if not duplicate:
        rules.insert(
            0,
            {
                "id": str(uuid.uuid4()),
                "value": f"{RULE_TYPE},{domain},{TARGET}",
            },
        )
    data["enable"] = True
    rule_data["type"] = "added"
    rule_data["addedRules"] = rules
    backup = backup_preferences(prefs, support)
    write_diagnostic("backup", backup.name)
    try:
        record_test_event("write")
        write_atomic(prefs, plist, config)
        written = load_config(prefs)
        written_id, written_profile = current_profile(written)
        if written_id != profile_id or not has_active_rule(written_profile, domain):
            raise RuntimeError("written rule validation failed")
    except Exception:
        shutil.copy2(backup, prefs)
        raise
    write_diagnostic("saved", domain)
    return "exists" if duplicate else "added"


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
        if len(parts) != 3 or parts[0].upper() != RULE_TYPE or parts[2] != TARGET:
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
    config = load_config(prefs)
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
        _, initial_config = load_preferences(prefs)
        _, initial_profile = current_profile(initial_config)
        already_in_file = has_active_rule(initial_profile, domain)
        if already_in_file and confirmed_applied(domain, prefs, support):
            write_diagnostic("exists-applied", domain)
            return "exists"
        if bettbox_pid() is not None or core_pid() is not None:
            write_diagnostic("stop-request")
            stop_bettbox_safely()
        status = persist_rule(domain, prefs, support)
        launch_bettbox_once(prefs)
        final_config = load_config(prefs)
        _, final_profile = current_profile(final_config)
        if not has_active_rule(final_profile, domain):
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


def main() -> int:
    popclip_call = len(sys.argv) == 1
    try:
        if popclip_call:
            domain = normalize_domain(os.environ.get("POPCLIP_TEXT", ""))
            write_diagnostic("start", domain)
            if test_mode():
                status = perform_worker(domain)
                print(
                    f"已存在，无需修改：{domain}"
                    if status == "exists"
                    else f"已加入 Bettbox 并恢复 Mihomo：{domain}"
                )
            else:
                prefs, support = paths()
                config = load_config(prefs)
                _, profile = current_profile(config)
                if has_active_rule(profile, domain) and confirmed_applied(
                    domain, prefs, support
                ):
                    print(f"已存在，无需修改：{domain}")
                    return 0
                spawn_worker(domain)
                print(f"正在通过命令加入 Bettbox：{domain}")
            return 0
        if len(sys.argv) == 3 and sys.argv[1] in {"--worker", "--run-now"}:
            domain = normalize_domain(sys.argv[2])
            status = perform_worker(domain)
            if sys.argv[1] == "--run-now":
                print(
                    f"已存在且已生效：{domain}"
                    if status == "exists"
                    else f"已加入 Bettbox 并恢复 Mihomo：{domain}"
                )
            return 0
        raise UserError("调用参数无效")
    except UserError as exc:
        write_diagnostic("error", str(exc))
        if popclip_call or (len(sys.argv) > 1 and sys.argv[1] == "--run-now"):
            print(f"未添加：{exc}")
            return 0
        return 1
    except Exception:
        write_diagnostic("error", "unexpected")
        if popclip_call or (len(sys.argv) > 1 and sys.argv[1] == "--run-now"):
            print("未添加：写入或验证失败，原配置已保留/恢复")
            return 0
        return 1


if __name__ == "__main__":
    sys.exit(main())
