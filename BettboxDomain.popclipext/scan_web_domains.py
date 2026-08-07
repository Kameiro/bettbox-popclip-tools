#!/usr/bin/python3
"""Open a page in headless Chromium and list every observed request domain."""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


SUPPORT = Path.home() / "Library" / "Application Support" / "com.appshub.bettbox"
CAPTURER = Path(__file__).with_name("capture_web_domains_playwright.js")
NODE_CANDIDATES = (
    Path("/usr/local/bin/node"),
    Path("/opt/homebrew/bin/node"),
    Path.home()
    / ".cache"
    / "codex-runtimes"
    / "codex-primary-runtime"
    / "dependencies"
    / "node"
    / "bin"
    / "node",
)
TYPE_LABELS = {
    "document": "页面",
    "stylesheet": "样式",
    "image": "图片",
    "media": "媒体",
    "font": "字体",
    "script": "脚本",
    "texttrack": "字幕",
    "xhr": "XHR",
    "fetch": "Fetch",
    "eventsource": "事件流",
    "websocket": "WebSocket",
    "manifest": "清单",
    "other": "其他",
}


class UserError(Exception):
    pass


def normalize_url(value: str) -> str:
    text = value.strip()
    if not text or any(char.isspace() for char in text):
        raise UserError("请选择一个不含空白的域名或 URL")
    if "://" not in text:
        if any(mark in text for mark in "/:@?#*"):
            raise UserError("域名格式无效")
        text = f"https://{text}"
    try:
        parsed = urlsplit(text)
        _ = parsed.port
    except ValueError as exc:
        raise UserError("URL 格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UserError("只支持 HTTP 或 HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise UserError("不接受含凭据的 URL")
    if not parsed.hostname:
        raise UserError("URL 中没有域名")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise UserError("网页捕获不接受 IP 地址")
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UserError("域名无法规范化") from exc
    if host == "localhost" or "." not in host:
        raise UserError("域名无效")
    rendered = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), rendered, parsed.path or "/", parsed.query, ""))


def safe_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if not host:
            return "<unknown>"
        rendered = host if parsed.port is None else f"{host}:{parsed.port}"
        return f"{parsed.scheme.lower()}://{rendered}"
    except ValueError:
        return "<unknown>"


def node_path() -> Path:
    for candidate in NODE_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise UserError("找不到 Node.js 运行时")


def capture(url: str) -> dict[str, Any]:
    if not CAPTURER.is_file():
        raise UserError("找不到无头浏览器捕获器")
    try:
        completed = subprocess.run(
            [str(node_path()), str(CAPTURER), url],
            text=True,
            capture_output=True,
            timeout=42,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UserError("网页域名捕获超时") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[0] if completed.stderr.strip() else "未知错误"
        raise UserError(detail[:160])
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UserError("无法读取无头浏览器结果") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("domains"), list):
        raise UserError("无头浏览器结果结构无效")
    return payload


def format_report(payload: dict[str, Any]) -> tuple[str, str]:
    domains = [item for item in payload["domains"] if isinstance(item, dict) and item.get("host")]
    if not domains:
        raise UserError("页面加载期间没有捕获到域名")
    status = payload.get("main_status")
    total_requests = int(payload.get("total_requests") or 0)
    total_failed = int(payload.get("total_failed") or 0)
    capture_ms = int(payload.get("capture_ms") or 0)
    lines = [
        "网页动态域名捕获",
        f"请求站点: {safe_origin(str(payload.get('target_url') or ''))}",
        f"最终站点: {safe_origin(str(payload.get('final_url') or ''))}",
        f"主页面 HTTP: {status if status is not None else '-'}",
        f"浏览器: {payload.get('browser') or 'Chromium 无头模式'}",
        f"捕获时长: {capture_ms / 1000:.1f} 秒",
        f"发现域名: {len(domains)}",
        f"网络请求: {total_requests}",
        f"失败请求: {total_failed}",
        "说明: 已执行页面 JavaScript；结果来自无头浏览器加载期间实际发生的请求。",
        "",
    ]
    for item in domains:
        request_count = int(item.get("requests") or 0)
        failed_count = int(item.get("failed") or 0)
        statuses = item.get("statuses") if isinstance(item.get("statuses"), list) else []
        types = item.get("types") if isinstance(item.get("types"), list) else []
        status_text = ",".join(str(value) for value in statuses) if statuses else "-"
        type_text = ",".join(TYPE_LABELS.get(str(value), str(value)) for value in types) or "-"
        has_error_status = any(isinstance(value, int) and value >= 400 for value in statuses)
        marker = "FAIL" if failed_count >= request_count and request_count else "WARN" if failed_count or has_error_status else "OK"
        lines.append(
            f"[{marker}] {item['host']}\t请求 {request_count}\t失败 {failed_count}\t"
            f"HTTP {status_text}\t类型 {type_text}"
        )

    prefix = "✓" if isinstance(status, int) and status < 400 and total_failed == 0 else "⚠"
    status_part = f"HTTP {status}，" if status is not None else ""
    summary = (
        f"{prefix} {status_part}捕获 {len(domains)} 个域名、{total_requests} 个请求，"
        f"失败 {total_failed}，已复制"
    )
    return "\n".join(lines) + "\n", summary


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
        raise UserError("无法复制捕获结果")


def notify_user(message: str) -> None:
    subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            "on run argv",
            "-e",
            'display notification (item 1 of argv) with title "Bettbox 网页域名捕获"',
            "-e",
            "end run",
            message,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def main() -> int:
    try:
        url = normalize_url(os.environ.get("POPCLIP_TEXT", ""))
        report, summary = format_report(capture(url))
        SUPPORT.mkdir(parents=True, exist_ok=True)
        report_path = SUPPORT / "popclip-domain-scan-latest.txt"
        report_path.write_text(report, encoding="utf-8")
        copy_text(report)
        if os.environ.get("POPCLIP_TEST_MODE") == "1":
            print(report, end="")
        else:
            notify_user(summary)
            print(summary)
        return 0
    except UserError as exc:
        message = f"捕获失败：{exc}"
        if os.environ.get("POPCLIP_TEST_MODE") != "1":
            notify_user(message)
        print(message)
        return 0
    except Exception:
        message = "捕获失败：未预期的错误"
        if os.environ.get("POPCLIP_TEST_MODE") != "1":
            notify_user(message)
        print(message)
        return 0


if __name__ == "__main__":
    sys.exit(main())
