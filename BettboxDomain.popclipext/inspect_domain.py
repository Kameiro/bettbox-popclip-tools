#!/usr/bin/python3
"""Run Bettbox's read-only live diagnosis for the selected domain or URL."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


TESTER = Path(__file__).with_name("bettbox_rule_test.py")
SUPPORT = Path.home() / "Library" / "Application Support" / "com.appshub.bettbox"
LIVE_LABELS = {
    "accessible": "可访问",
    "reachable_but_rejected": "收到 HTTP 拒绝响应",
    "transport_failed": "网络连接失败",
    "no_http_response": "没有 HTTP 响应",
    "http_response": "收到 HTTP 响应",
    "invalid_target": "目标无效",
    "tool_unavailable": "测试工具不可用",
}
MATCH_LABELS = {"yes": "命中", "no": "未命中", "unknown": "无法静态判断"}


class UserError(Exception):
    pass


def copy_text(value: str) -> None:
    if os.environ.get("POPCLIP_TEST_MODE") == "1":
        return
    completed = subprocess.run(
        ["/usr/bin/pbcopy"],
        input=value,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        raise UserError("无法复制诊断结果")


def yes_no(value: Any) -> str:
    return "是" if value is True else "否" if value is False else "未知"


def diagnosis_hint(file_match: str, live_state: str, http_status: Any) -> str | None:
    if http_status == 403:
        if file_match == "no":
            return "可能被墙、地区限制或站点风控；当前未命中自定义覆写，可加入代理后复测"
        if file_match == "yes":
            return "已命中自定义覆写仍返回 403；请确认核心已重载，或更换代理节点复测"
        return "可能被墙、地区限制或站点风控；HTTP 403 本身无法单独区分"
    if http_status == 451:
        return "站点因地区或法律限制拒绝访问，可更换代理地区复测"
    if http_status == 429:
        return "请求过于频繁，稍后再试或更换代理节点"
    if isinstance(http_status, int) and 500 <= http_status <= 599:
        return "网站或上游服务异常，不一定是代理问题"
    if live_state == "transport_failed":
        return "DNS、连接、TLS 或网络路径失败，可能与被墙、代理节点或本地缓存有关"
    return None


def format_report(report: dict[str, Any]) -> tuple[str, str]:
    results = report.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise UserError("诊断结果结构无效")
    result = results[0]
    dns = result.get("dns") if isinstance(result.get("dns"), dict) else {}
    live = result.get("live") if isinstance(result.get("live"), dict) else {}
    runtime = report.get("runtime") if isinstance(report.get("runtime"), dict) else {}

    addresses = dns.get("addresses") if isinstance(dns.get("addresses"), list) else []
    lines = [
        "Bettbox 域名诊断",
        f"目标: {result.get('target') or '<unknown>'}",
        f"覆写规则: {MATCH_LABELS.get(str(result.get('file_match')), result.get('file_match'))}",
    ]
    if result.get("matched_rule"):
        lines.append(f"命中规则: {result['matched_rule']}")
    if result.get("proxy_group"):
        lines.append(f"代理组: {result['proxy_group']}")
    lines.extend(
        [
            f"TUN 配置: {yes_no(report.get('tun_enabled_in_plist'))}",
            f"Bettbox 运行: {yes_no(runtime.get('bettbox_running'))}",
            f"BettboxCore 运行: {yes_no(runtime.get('bettbox_core_running'))}",
        ]
    )
    if runtime.get("reload_warning"):
        lines.append(f"重载提示: {runtime['reload_warning']}")
    lines.extend(
        [
            f"DNS: {dns.get('dns_status') or 'unknown'}",
            f"解析地址: {', '.join(str(item) for item in addresses) if addresses else '-'}",
            f"实际结果: {LIVE_LABELS.get(str(live.get('live_status')), live.get('live_status') or 'unknown')}",
            f"HTTP: {live.get('http_status') if live.get('http_status') is not None else '-'}",
            f"实际连接 IP: {live.get('remote_ip') or '-'}",
            f"耗时: {live.get('time_total_ms') if live.get('time_total_ms') is not None else '-'} ms",
        ]
    )
    if live.get("error"):
        lines.append(f"错误: {live['error']}")
    if live.get("remote_ip_note"):
        lines.append(f"路径提示: {live['remote_ip_note']}")

    live_state = str(live.get("live_status") or "unknown")
    http_status = live.get("http_status")
    file_match = str(result.get("file_match") or "unknown")
    hint = diagnosis_hint(file_match, live_state, http_status)
    if hint:
        lines.append(f"判断: {hint}")
    if live_state == "accessible":
        summary = f"✓ HTTP {http_status}，诊断已复制"
    elif http_status == 403 and file_match == "no":
        summary = "⚠ HTTP 403，可能被墙或站点限制；未命中覆写，诊断已复制"
    elif http_status == 403 and file_match == "yes":
        summary = "⚠ HTTP 403，已命中覆写；检查重载或更换节点，诊断已复制"
    elif http_status == 403:
        summary = "⚠ HTTP 403，可能被墙、地区限制或风控，诊断已复制"
    elif live_state == "reachable_but_rejected":
        summary = f"⚠ HTTP {http_status}，收到拒绝响应，诊断已复制"
    elif live.get("error"):
        summary = f"✗ {live['error']}，诊断已复制"
    else:
        summary = "诊断完成，结果已复制"
    return "\n".join(lines) + "\n", summary


def notify_user(message: str) -> None:
    subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            "on run argv",
            "-e",
            'display notification (item 1 of argv) with title "Bettbox 域名诊断"',
            "-e",
            "end run",
            message,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def run(selected: str) -> tuple[str, str]:
    if not selected.strip():
        raise UserError("请选择一个域名或 URL")
    if not TESTER.is_file():
        raise UserError("找不到 Bettbox 域名测试工具")
    try:
        completed = subprocess.run(
            [
                "/usr/bin/python3",
                str(TESTER),
                "--live",
                "--json",
                "--timeout",
                "12",
                selected.strip(),
            ],
            text=True,
            capture_output=True,
            timeout=32,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UserError("域名诊断超时") from exc
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UserError("无法读取域名诊断结果") from exc
    return format_report(payload)


def main() -> int:
    try:
        report, summary = run(os.environ.get("POPCLIP_TEXT", ""))
        SUPPORT.mkdir(parents=True, exist_ok=True)
        (SUPPORT / "popclip-domain-check-latest.txt").write_text(report, encoding="utf-8")
        copy_text(report)
        if os.environ.get("POPCLIP_TEST_MODE") == "1":
            print(report, end="")
        else:
            notify_user(summary)
            print(summary)
        return 0
    except UserError as exc:
        print(f"检查失败：{exc}")
        return 0
    except Exception:
        print("检查失败：未预期的错误")
        return 0


if __name__ == "__main__":
    sys.exit(main())
