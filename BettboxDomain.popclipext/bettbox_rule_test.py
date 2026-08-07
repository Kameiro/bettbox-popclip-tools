#!/usr/bin/env python3
"""Read-only static and live tester for Bettbox profile override rules."""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import plistlib
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from typing import Any, Iterable


APP_ID = "com.appshub.bettbox"
PREFS_PATH = Path.home() / "Library" / "Preferences" / f"{APP_ID}.plist"
SUPPORTED_DOMAIN_RULES = {"DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD"}
SUPPORTED_IP_RULES = {"IP-CIDR", "IP-CIDR6"}
UNIVERSAL_RULES = {"MATCH", "FINAL"}
PROXY_ENV_VARS = {
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}
CURL_ERRORS = {
    6: "DNS 解析失败",
    7: "无法建立连接",
    28: "连接或请求超时",
    35: "TLS 握手失败",
    47: "重定向次数过多",
    52: "服务器没有返回内容",
    56: "接收响应失败",
    60: "TLS 证书验证失败",
}
SENSITIVE_GROUP_RE = re.compile(
    r"(?:https?://|(?:ss|ssr|socks(?:4|5)?|http|https|vmess|trojan|vless)://)"
    r"|(?:password|passwd|token|secret|uuid|private.?key)\s*=|"
    r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Target:
    display: str
    host: str
    port: int | None
    kind: str


@dataclass(frozen=True)
class ParsedRule:
    rule_type: str
    pattern: str | None
    group: str
    display: str | None
    known: bool
    reason: str | None = None


@dataclass(frozen=True)
class RuleMatch:
    state: str
    matched_rule: str | None
    proxy_group: str | None
    unknown_types: tuple[str, ...]
    reason: str | None = None


def fail(message: str) -> None:
    raise SystemExit(f"error: {message}")


def safe_group(value: str | None) -> str:
    """Keep group output useful while refusing obvious credential-like values."""
    group = " ".join(str(value or "").split())
    if not group:
        return "(未指定)"
    if len(group) > 80 or SENSITIVE_GROUP_RE.search(group):
        return "<redacted>"
    return group


def normalize_hostname(value: str) -> str:
    host = value.strip().rstrip(".").lower()
    if not host or any(char in host for char in "/?#@:") or any(char.isspace() for char in host):
        raise ValueError("invalid hostname")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid hostname") from exc
    if len(host) > 253 or any(not label for label in host.split(".")):
        raise ValueError("invalid hostname")
    return host


def parse_port(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 1 or value > 65535:
        raise ValueError("port must be between 1 and 65535")
    return value


def host_display(host: str, port: int | None) -> str:
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        rendered = host
    else:
        rendered = f"[{host}]" if parsed.version == 6 else host
    return f"{rendered}:{port}" if port is not None else rendered


def parse_target(raw: str) -> Target:
    """Parse a host, URL, or IP while discarding paths, queries, and fragments."""
    value = raw.strip()
    if not value:
        raise ValueError("empty target")

    scheme: str | None = None
    port: int | None = None
    host_value: str

    if "://" in value:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("URL must contain a host and no userinfo")
        host_value = parsed.hostname
        try:
            port = parse_port(parsed.port)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    elif value.startswith("[") or "/" in value:
        parsed = urlsplit(f"//{value}")
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("target must contain a host")
        host_value = parsed.hostname
        try:
            port = parse_port(parsed.port)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    elif value.count(":") == 1:
        host_value, raw_port = value.rsplit(":", 1)
        if not raw_port.isdigit():
            raise ValueError("hostname ports must be numeric")
        port = parse_port(int(raw_port))
    else:
        host_value = value

    try:
        host = normalize_hostname(host_value)
    except ValueError:
        try:
            host = str(ipaddress.ip_address(host_value.strip("[]")))
        except ValueError as exc:
            raise ValueError("invalid host or IP address") from exc

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        kind = "domain"
    else:
        kind = "ipv4" if address.version == 4 else "ipv6"

    prefix = f"{scheme}://" if scheme else ""
    return Target(
        display=f"{prefix}{host_display(host, port)}",
        host=host,
        port=port,
        kind=kind,
    )


def safe_target_label(raw: str) -> str:
    try:
        return parse_target(raw).display
    except ValueError:
        return "<invalid target>"


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than 0")
    return seconds


def live_request_url(raw_target: str, target: Target, default_scheme: str) -> str:
    """Build a safe HTTP(S) URL while preserving an optional path/query for the request."""
    value = raw_target.strip()
    path = "/"
    query = ""
    if "://" in value:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError("live test only supports http and https")
        path = parsed.path or "/"
        query = parsed.query
    elif value.startswith("[") or "/" in value:
        parsed = urlsplit(f"//{value}")
        scheme = default_scheme
        path = parsed.path or "/"
        query = parsed.query
    else:
        scheme = default_scheme

    rendered_host = f"[{target.host}]" if target.kind == "ipv6" else target.host
    netloc = rendered_host
    if target.port is not None:
        netloc = f"{netloc}:{target.port}"
    return urlunsplit((scheme, netloc, path, query, ""))


def safe_origin(raw_url: str) -> str:
    """Report only the origin so query strings and paths are never printed."""
    try:
        parsed = urlsplit(raw_url)
        host = parsed.hostname
        if not host:
            return "<unknown>"
        port = parsed.port
        rendered_host = host_display(host, port)
        return f"{parsed.scheme.lower()}://{rendered_host}"
    except ValueError:
        return "<unknown>"


def metric_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dns_probe(raw_target: str, timeout: float) -> dict[str, Any]:
    """Resolve a domain with the system resolver so stale DNS is visible in the report."""
    try:
        target = parse_target(raw_target)
    except ValueError as exc:
        return {"dns_status": "invalid_target", "error": str(exc)}
    if target.kind != "domain":
        return {"dns_status": "not_applicable", "addresses": []}

    previous_timeout = socket.getdefaulttimeout()
    started = time.monotonic()
    try:
        socket.setdefaulttimeout(min(timeout, 5.0))
        records = socket.getaddrinfo(
            target.host,
            target.port or 443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        return {
            "dns_status": "failed",
            "addresses": [],
            "error": f"系统解析失败: {exc}",
            "time_total_ms": round((time.monotonic() - started) * 1000, 1),
            "resolver_note": "使用 macOS 系统解析器，结果可能受本地 DNS 缓存影响",
        }
    finally:
        socket.setdefaulttimeout(previous_timeout)

    addresses = sorted({record[4][0] for record in records if record[4] and record[4][0]})
    return {
        "dns_status": "resolved" if addresses else "empty",
        "addresses": addresses,
        "time_total_ms": round((time.monotonic() - started) * 1000, 1),
        "resolver_note": "使用 macOS 系统解析器，结果可能受本地 DNS 缓存影响",
    }


def live_probe(raw_target: str, default_scheme: str, timeout: float) -> dict[str, Any]:
    """Perform a real request through the default system/TUN path, without a CLI proxy."""
    try:
        target = parse_target(raw_target)
        request_url = live_request_url(raw_target, target, default_scheme)
    except ValueError as exc:
        return {
            "live_status": "invalid_target",
            "request_origin": "<invalid>",
            "error": str(exc),
        }

    write_format = "\t".join(
        [
            "%{http_code}",
            "%{url_effective}",
            "%{remote_ip}",
            "%{http_version}",
            "%{content_type}",
            "%{size_download}",
            "%{time_total}",
        ]
    )
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--location",
        "--max-time",
        f"{timeout:.3f}",
        "--connect-timeout",
        f"{min(timeout, 10.0):.3f}",
        "--output",
        os.devnull,
        "--write-out",
        write_format,
        "--user-agent",
        "BettboxRuleTest/1.0",
        request_url,
    ]
    environment = {key: value for key, value in os.environ.items() if key not in PROXY_ENV_VARS}
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=environment,
            timeout=timeout + 3.0,
            check=False,
        )
    except FileNotFoundError:
        return {
            "live_status": "tool_unavailable",
            "request_origin": safe_origin(request_url),
            "error": "curl 不可用",
        }
    except subprocess.TimeoutExpired:
        return {
            "live_status": "transport_failed",
            "request_origin": safe_origin(request_url),
            "error": "测试进程超时",
        }

    fields = completed.stdout.rstrip("\n").split("\t")
    if len(fields) < 7:
        fields = ["000", "", "", "", "", "", ""]
    try:
        status_code = int(fields[0])
    except ValueError:
        status_code = 0
    if status_code == 0:
        status_code_value: int | None = None
    else:
        status_code_value = status_code

    if completed.returncode != 0:
        live_status = "transport_failed"
    elif status_code_value is None:
        live_status = "no_http_response"
    elif 200 <= status_code_value < 400:
        live_status = "accessible"
    elif 400 <= status_code_value < 600:
        live_status = "reachable_but_rejected"
    else:
        live_status = "http_response"

    total_seconds = metric_float(fields[6])
    bytes_value = metric_float(fields[5])
    result: dict[str, Any] = {
        "live_status": live_status,
        "request_origin": safe_origin(request_url),
        "final_origin": safe_origin(fields[1]) if fields[1] else None,
        "http_status": status_code_value,
        "remote_ip": fields[2] or None,
        "http_version": fields[3] or None,
        "content_type": fields[4] or None,
        "bytes_received": int(bytes_value) if bytes_value is not None and bytes_value.is_integer() else bytes_value,
        "time_total_ms": round((total_seconds if total_seconds is not None else time.monotonic() - started) * 1000, 1),
        "curl_exit_code": completed.returncode,
        "transport_note": "未指定命令行代理；使用当前系统网络路径，若 Bettbox TUN/Core 正常运行则由它接管",
    }
    if fields[2]:
        try:
            if ipaddress.ip_address(fields[2]) in ipaddress.ip_network("198.18.0.0/15"):
                result["remote_ip_note"] = "198.18.0.0/15 是常见的 fake-IP/TUN 地址；说明请求经过本机 TUN 的 DNS/连接路径"
        except ValueError:
            pass
    if completed.returncode != 0:
        result["error"] = CURL_ERRORS.get(completed.returncode, f"curl 错误 {completed.returncode}")
    return result


def load_config(plist_path: Path) -> dict[str, Any]:
    if not plist_path.exists():
        fail(f"preferences file not found: {plist_path}")
    try:
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        fail(f"could not read preferences plist: {exc}")
    raw = plist.get("flutter.config")
    if not isinstance(raw, str) or not raw:
        fail("flutter.config is missing or is not a JSON string")
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"flutter.config is invalid JSON: {exc}")
    if not isinstance(config, dict):
        fail("flutter.config root is not an object")
    return config


def select_profile(config: dict[str, Any], profile_id: str | None) -> dict[str, Any]:
    profiles = config.get("profiles")
    if not isinstance(profiles, list):
        fail("config has no profiles list")
    current_id = config.get("currentProfileId")
    wanted = profile_id or (str(current_id) if current_id is not None else None)
    if wanted is None and len(profiles) == 1 and isinstance(profiles[0], dict):
        wanted = str(profiles[0].get("id"))
    for profile in profiles:
        if isinstance(profile, dict) and str(profile.get("id")) == wanted:
            return profile
    fail(f"profile not found: {wanted or '<current>'}")


def active_override(profile: dict[str, Any]) -> tuple[bool, str, str, list[Any]]:
    data = profile.get("overrideData")
    if not isinstance(data, dict):
        return False, "override", "overrideRules", []
    rule_data = data.get("rule")
    if not isinstance(rule_data, dict):
        return bool(data.get("enable")), "override", "overrideRules", []
    mode = str(rule_data.get("type") or "override").lower()
    if mode == "added":
        key = "addedRules"
    elif mode == "override":
        key = "overrideRules"
    else:
        return bool(data.get("enable")), mode, "<unsupported>", []
    rules = rule_data.get(key) or []
    if not isinstance(rules, list):
        rules = []
    return bool(data.get("enable")), mode, key, rules


def rule_value(item: Any) -> str | None:
    if isinstance(item, dict):
        value = item.get("value")
        return value if isinstance(value, str) else None
    if isinstance(item, str):
        return item
    return None


def domain_pattern(value: str) -> str:
    pattern = value.strip()
    if not pattern or pattern.startswith(".") or any(char in pattern for char in "/,*"):
        raise ValueError("invalid domain pattern")
    return normalize_hostname(pattern)


def parse_rule(value: str) -> ParsedRule:
    parts = [part.strip() for part in value.split(",")]
    rule_type = parts[0].upper() if parts and parts[0] else "<invalid>"
    if rule_type in SUPPORTED_DOMAIN_RULES:
        if len(parts) < 2:
            return ParsedRule(rule_type, None, "", None, False, "missing domain pattern")
        try:
            pattern = domain_pattern(parts[1])
        except ValueError:
            return ParsedRule(rule_type, None, "", None, False, "invalid domain pattern")
        group = parts[2] if len(parts) > 2 else ""
        return ParsedRule(
            rule_type,
            pattern,
            group,
            f"{rule_type},{pattern},{safe_group(group)}",
            True,
        )

    if rule_type in SUPPORTED_IP_RULES:
        if len(parts) < 2:
            return ParsedRule(rule_type, None, "", None, False, "missing network")
        try:
            network = ipaddress.ip_network(parts[1], strict=False)
        except ValueError:
            return ParsedRule(rule_type, None, "", None, False, "invalid network")
        expected_version = 4 if rule_type == "IP-CIDR" else 6
        if network.version != expected_version:
            return ParsedRule(rule_type, None, "", None, False, "address family does not match rule type")
        pattern = str(network)
        group = parts[2] if len(parts) > 2 else ""
        return ParsedRule(
            rule_type,
            pattern,
            group,
            f"{rule_type},{pattern},{safe_group(group)}",
            True,
        )

    if rule_type in UNIVERSAL_RULES:
        group = parts[1] if len(parts) > 1 else ""
        return ParsedRule(
            rule_type,
            None,
            group,
            f"{rule_type},{safe_group(group)}",
            True,
        )

    return ParsedRule(
        rule_type,
        None,
        "",
        None,
        False,
        "rule type is not statically supported",
    )


def rule_matches(rule: ParsedRule, target: Target) -> bool | None:
    if not rule.known:
        return None
    if rule.rule_type in UNIVERSAL_RULES:
        return True
    if rule.rule_type in SUPPORTED_DOMAIN_RULES:
        if target.kind != "domain":
            return False
        if rule.rule_type == "DOMAIN":
            return target.host == rule.pattern
        if rule.rule_type == "DOMAIN-SUFFIX":
            return target.host == rule.pattern or target.host.endswith(f".{rule.pattern}")
        return bool(rule.pattern and rule.pattern in target.host)
    if target.kind == "domain":
        return None
    address = ipaddress.ip_address(target.host)
    network = ipaddress.ip_network(rule.pattern or "0.0.0.0/32")
    return address.version == network.version and address in network


def evaluate_rules(rule_values: Iterable[Any], target: Target) -> RuleMatch:
    unknown_types: list[str] = []
    for item in rule_values:
        value = rule_value(item)
        if value is None:
            unknown_types.append("<invalid>")
            continue
        rule = parse_rule(value)
        if not rule.known:
            unknown_types.append(rule.rule_type)
            continue
        result = rule_matches(rule, target)
        if result is None:
            unknown_types.append(f"{rule.rule_type}(需要DNS或外部数据)")
            continue
        if result:
            # Only unknown rules seen before this candidate can affect ordering.
            if not unknown_types:
                return RuleMatch("yes", rule.display, safe_group(rule.group), ())
            return RuleMatch(
                "unknown",
                None,
                None,
                tuple(dict.fromkeys(unknown_types)),
                "前置规则无法仅靠 overrideData 判断，不能确认该候选规则是否首先命中",
            )
    if unknown_types:
        return RuleMatch(
            "unknown",
            None,
            None,
            tuple(dict.fromkeys(unknown_types)),
            "规则列表中存在无法仅靠 overrideData 判断的前置规则",
        )
    return RuleMatch("no", None, None, ())


def result_for_target(
    raw_target: str,
    enabled: bool,
    mode: str,
    source_key: str,
    rule_values: Iterable[Any],
) -> dict[str, Any]:
    try:
        target = parse_target(raw_target)
    except ValueError as exc:
        return {
            "target": safe_target_label(raw_target),
            "parsed_type": "invalid",
            "file_match": "unknown",
            "matched_rule": None,
            "proxy_group": None,
            "conclusion": "无法解析目标",
            "reason": str(exc),
        }

    match = evaluate_rules(rule_values, target)
    if not enabled:
        conclusion = "覆写已关闭；文件中的静态结果不会生效"
    elif match.state == "yes":
        conclusion = "文件中命中；不代表运行中的 BettboxCore 已重载"
    elif match.state == "unknown":
        conclusion = "unknown：存在无法仅靠覆写文件判断的规则"
    else:
        conclusion = "当前 active override 列表未命中；不代表原始订阅规则未命中"

    result: dict[str, Any] = {
        "target": target.display,
        "parsed_type": target.kind,
        "port": target.port,
        "file_match": match.state,
        "matched_rule": match.matched_rule,
        "proxy_group": match.proxy_group,
        "conclusion": conclusion,
    }
    if match.unknown_types:
        result["unknown_rule_types"] = list(match.unknown_types)
    if match.reason:
        result["reason"] = match.reason
    return result


def process_exists(name: str) -> bool:
    completed = subprocess.run(
        ["pgrep", "-x", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def process_start_time(name: str) -> dt.datetime | None:
    completed = subprocess.run(
        ["pgrep", "-x", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    pid = next((line.strip() for line in completed.stdout.splitlines() if line.strip().isdigit()), None)
    if pid is None:
        return None
    started = subprocess.run(
        ["ps", "-p", pid, "-o", "lstart="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    ).stdout.strip()
    try:
        return dt.datetime.strptime(started, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None


def runtime_snapshot(plist_path: Path) -> dict[str, Any]:
    """Read process/file timestamps without stopping or reloading any Bettbox process."""
    result: dict[str, Any] = {
        "bettbox_running": process_exists("Bettbox"),
        "bettbox_core_running": process_exists("BettboxCore"),
    }
    try:
        preferences_mtime = dt.datetime.fromtimestamp(plist_path.stat().st_mtime)
    except OSError:
        preferences_mtime = None
    if preferences_mtime is not None:
        result["preferences_mtime"] = preferences_mtime.isoformat(timespec="seconds")
    core_started = process_start_time("BettboxCore")
    if core_started is not None:
        result["core_start_time"] = core_started.isoformat(timespec="seconds")
        if preferences_mtime is not None:
            newer = preferences_mtime > core_started
            result["preferences_newer_than_core_start"] = newer
            if newer:
                result["reload_warning"] = "覆写文件晚于 BettboxCore 启动时间；核心可能尚未重载最新文件"
    return result


def runtime_note(live: bool = False) -> str:
    if live:
        return "已发起真实 HTTP(S) 请求；未强制重载 BettboxCore，流量使用当前系统网络路径。"
    return "只检查 plist 文件；未验证运行中的 BettboxCore 是否已重载覆写规则。"


def normal_report(
    enabled: bool,
    mode: str,
    source_key: str,
    results: list[dict[str, Any]],
    live: bool,
    tun_enabled: bool,
    runtime: dict[str, Any] | None,
) -> None:
    print("Bettbox 覆写规则测试（只读）")
    print(f"override_enabled: {'yes' if enabled else 'no'}")
    print(f"override_mode: {mode}")
    print(f"rule_source: {source_key}")
    print(f"runtime_note: {runtime_note(live)}")
    if live:
        print(f"tun_enabled_in_plist: {'yes' if tun_enabled else 'no'}")
        if runtime:
            print("runtime:")
            for key, value in runtime.items():
                print(f"  {key}: {value}")
    for index, result in enumerate(results, start=1):
        print(f"\n[{index}]")
        for key in (
            "target",
            "parsed_type",
            "port",
            "file_match",
            "matched_rule",
            "proxy_group",
            "conclusion",
            "unknown_rule_types",
            "reason",
        ):
            if key in result:
                print(f"{key}: {result[key]}")
        for section in ("dns", "live"):
            if section not in result:
                continue
            print(f"{section}:")
            for key, value in result[section].items():
                print(f"  {key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "targets",
        nargs="+",
        help="domain, URL, IPv4, IPv6, or host/IP with an optional port",
    )
    parser.add_argument(
        "--plist",
        type=Path,
        default=PREFS_PATH,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--profile-id", help="select a profile instead of currentProfileId")
    parser.add_argument("--json", action="store_true", help="emit a redacted JSON report")
    parser.add_argument(
        "--live",
        action="store_true",
        help="also make a real HTTP(S) request through the current system/TUN path",
    )
    parser.add_argument(
        "--scheme",
        choices=["https", "http"],
        default="https",
        help="scheme for bare domains/IPs in --live mode (default: https)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=15.0,
        help="live request and DNS timeout in seconds (default: 15)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.plist)
    profile = select_profile(config, args.profile_id)
    enabled, mode, source_key, rules = active_override(profile)
    if source_key == "<unsupported>":
        fail(f"unsupported override mode: {mode}")

    results = [result_for_target(target, enabled, mode, source_key, rules) for target in args.targets]
    tun_enabled = bool((config.get("vpnProps") or {}).get("enable"))
    if args.live:
        for raw_target, result in zip(args.targets, results):
            if result["parsed_type"] == "invalid":
                continue
            dns_result = dns_probe(raw_target, args.timeout)
            live_result = live_probe(raw_target, args.scheme, args.timeout)
            remote_ip = live_result.get("remote_ip")
            addresses = dns_result.get("addresses") or []
            if remote_ip and addresses:
                live_result["dns_connection_match"] = remote_ip in addresses
            result["dns"] = dns_result
            result["live"] = live_result
    runtime = runtime_snapshot(args.plist) if args.live else None
    report = {
        "override_enabled": enabled,
        "override_mode": mode,
        "rule_source": source_key,
        "runtime_note": runtime_note(args.live),
        "results": results,
    }
    if args.live:
        report["tun_enabled_in_plist"] = tun_enabled
        report["runtime"] = runtime
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        normal_report(enabled, mode, source_key, results, args.live, tun_enabled, runtime)
    if any(result["parsed_type"] == "invalid" for result in results):
        return 2
    if args.live and any(
        result.get("live", {}).get("live_status")
        in {"transport_failed", "no_http_response", "reachable_but_rejected", "invalid_target", "tool_unavailable"}
        for result in results
    ):
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
