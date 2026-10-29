#!/usr/bin/python3
"""Opt-out, once-a-day check for a newer release of this extension.

The other scripts call ``maybe_check_updates()`` after they have finished their
own job. It only reads a small state file and, when a check is due, starts a
detached ``update_check.py --check`` process, so it never slows an action down.

The checker asks GitHub's public releases API for the latest release. If it is
newer it asks the user (update / remind me later / skip this version). Only
after a click on "update" does it download the release package, verify it and
hand it to PopClip, which shows its own install confirmation. Nothing is
installed silently and nothing about the user's domains or Bettbox
configuration is ever sent.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feedback  # noqa: E402


APP_ID = "com.appshub.bettbox"
REPO = "Kameiro/bettbox-popclip-tools"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_BASE = f"https://github.com/{REPO}/releases/download/"
ASSET_NAME = "BettboxDomain.popclipextz"
EXTENSION_DIR = "BettboxDomain.popclipext"
EXTENSION_ID = "local.luna.popclip.bettbox-domain"
POPCLIP_BUNDLE_ID = "com.pilotmoon.popclip"
OPTION_ENV = "POPCLIP_OPTION_AUTOUPDATECHECK"

CURL = "/usr/bin/curl"
OPEN = "/usr/bin/open"
CHECK_INTERVAL = 24 * 3600
RETRY_INTERVAL = 3600
KEEP_DOWNLOADS = 3 * 24 * 3600
API_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 120
MAX_API_BYTES = 1_000_000
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
MAX_UNPACKED_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 64
MAX_PLIST_BYTES = 256 * 1024

BUTTON_IGNORE = "忽略此版本"
BUTTON_LATER = "稍后提醒"
BUTTON_UPDATE = "更新"

STATE_FILE = "popclip-bettbox-domain.update.json"
LOCK_FILE = "popclip-bettbox-domain.update.lock"
DISABLE_FILE = "popclip-bettbox-domain.no-update-check"
LOG_FILE = "popclip-bettbox-domain.log"
DOWNLOAD_DIR = "popclip-bettbox-domain-updates"

TAG_RE = re.compile(r"v(\d{1,6}(?:\.\d{1,6}){0,3})")
VERSION_RE = re.compile(r"\d{1,6}(?:\.\d{1,6}){0,3}")
DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})")

Version = tuple


class FetchError(Exception):
    """The network request failed; try again later."""


class UpdateError(Exception):
    """The release cannot be installed safely; the message is shown to the user."""


@dataclasses.dataclass(frozen=True)
class Release:
    tag: str
    text: str
    version: Version
    url: str
    sha256: str
    size: int


def test_mode() -> bool:
    return os.environ.get("BETTBOX_TEST_MODE") == "1"


def api_url() -> str:
    return (os.environ.get("BETTBOX_TEST_UPDATE_API_URL") if test_mode() else None) or API_URL


def download_base() -> str:
    return (
        os.environ.get("BETTBOX_TEST_UPDATE_DOWNLOAD_BASE") if test_mode() else None
    ) or DOWNLOAD_BASE


def support_dir() -> Path:
    if test_mode():
        return Path(os.environ["BETTBOX_SUPPORT_PATH"])
    return Path.home() / "Library" / "Application Support" / APP_ID


def log(stage: str, detail: str = "") -> None:
    try:
        support = support_dir()
        support.mkdir(parents=True, exist_ok=True)
        path = support / LOG_FILE
        if path.exists() and path.stat().st_size > 200_000:
            path.replace(path.with_suffix(".log.previous"))
        safe = detail.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\tupdate-{stage}\t{safe[:240]}\n")
    except (OSError, KeyError):
        pass


# --- versions -------------------------------------------------------------


def parse_tag(tag: object) -> Version | None:
    """``v1.2.3`` -> (1, 2, 3). Anything else (pre-release suffixes included) is rejected."""
    if not isinstance(tag, str):
        return None
    match = TAG_RE.fullmatch(tag)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def parse_version(text: object) -> Version | None:
    if not isinstance(text, str):
        return None
    return tuple(int(part) for part in text.split(".")) if VERSION_RE.fullmatch(text) else None


def is_newer(candidate: Version, current: Version) -> bool:
    width = max(len(candidate), len(current))
    pad = lambda value: value + (0,) * (width - len(value))  # noqa: E731
    return pad(candidate) > pad(current)


def current_version() -> tuple[str, Version] | None:
    text = os.environ.get("BETTBOX_TEST_CURRENT_VERSION") if test_mode() else None
    if not text:
        try:
            with (Path(__file__).resolve().parent / "Config.plist").open("rb") as handle:
                text = plistlib.load(handle).get("Extension Version")
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
    version = parse_version(text)
    return (text, version) if version is not None and isinstance(text, str) else None


# --- option and state -----------------------------------------------------


def check_enabled() -> bool:
    """The PopClip option "自动检查插件更新". On when PopClip does not provide it (default
    on); when it does, only an explicit "on" value counts, so a privacy switch never
    fails open on a value we do not recognise."""
    try:
        if (support_dir() / DISABLE_FILE).exists():  # works even without PopClip's option UI
            return False
    except (OSError, KeyError):
        pass
    value = os.environ.get(OPTION_ENV)
    return value is None or value.strip().lower() in {"1", "true", "yes", "on"}


def load_state(support: Path) -> dict[str, Any]:
    try:
        data = json.loads((support / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    state: dict[str, Any] = {}
    for key in ("last_check", "last_attempt"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            state[key] = float(value)
    for key in ("latest_version", "ignored_version"):
        if parse_version(data.get(key)) is not None:
            state[key] = data[key]
    return state


def save_state(support: Path, state: dict[str, Any]) -> None:
    support.mkdir(parents=True, exist_ok=True)
    path = support / STATE_FILE
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=support)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def should_check(state: dict[str, Any], now: float) -> bool:
    """A check is due after CHECK_INTERVAL, or RETRY_INTERVAL after a failed attempt.

    Timestamps in the future (clock change, damaged file) count as due, so a
    bad value can never switch the check off for good.
    """
    last_check = state.get("last_check")
    if isinstance(last_check, float) and 0 <= now - last_check < CHECK_INTERVAL:
        return False
    last_attempt = state.get("last_attempt")
    if isinstance(last_attempt, float) and 0 <= now - last_attempt < RETRY_INTERVAL:
        return False
    return True


# --- scheduling (called from the PopClip actions) --------------------------


def maybe_check_updates() -> None:
    """Start a background check if one is due. Cheap, never raises, never waits."""
    try:
        if not check_enabled() or current_version() is None:
            return
        if not should_check(load_state(support_dir()), time.time()):
            return
        if test_mode():
            events = os.environ.get("BETTBOX_TEST_UPDATE_EVENTS_PATH")
            if events:
                with Path(events).open("a", encoding="utf-8") as handle:
                    handle.write("check-scheduled\n")
            return
        subprocess.Popen(
            ["/usr/bin/python3", str(Path(__file__).resolve()), "--check"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:  # an update check must never disturb an action
        pass


# --- network --------------------------------------------------------------


def curl_command(url: str, dest: Path, *, timeout: int, max_bytes: int, accept: str) -> list[str]:
    """System curl, https only (also for redirects); ``-q`` ignores any ~/.curlrc.
    Tests may talk plain http to a local server."""
    version = (current_version() or ("0", ()))[0]
    protocols = "=http,https" if test_mode() else "=https"
    return [
        CURL, "-q", "--silent", "--show-error", "--fail", "--location",
        "--proto", protocols, "--proto-redir", protocols, "--max-redirs", "5",
        "--connect-timeout", "5", "--max-time", str(timeout),
        "--max-filesize", str(max_bytes),
        "--user-agent", f"bettbox-popclip-tools/{version}",
        "--header", f"Accept: {accept}",
        "--output", str(dest), "--url", url,
    ]  # fmt: skip


def curl_fetch(url: str, dest: Path, *, timeout: int, max_bytes: int, accept: str) -> None:
    """Download ``url`` to ``dest``."""
    command = curl_command(url, dest, timeout=timeout, max_bytes=max_bytes, accept=accept)
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout + 15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FetchError(type(exc).__name__) from exc
    if completed.returncode != 0:
        raise FetchError(f"curl {completed.returncode}: {completed.stderr.strip()[:120]}")
    try:
        received = dest.stat().st_size  # curl writes no file for an empty body
    except OSError as exc:
        raise FetchError("empty response") from exc
    if received > max_bytes:
        raise FetchError("response too large")


def parse_release(data: object, base: str) -> Release | None:
    """The latest release if it carries a package we are willing to install, else None."""
    if not isinstance(data, dict) or data.get("draft") is True or data.get("prerelease") is True:
        return None
    tag = data.get("tag_name")
    version = parse_tag(tag)
    assets = data.get("assets")
    if version is None or not isinstance(tag, str) or not isinstance(assets, list):
        return None
    asset = next(
        (item for item in assets if isinstance(item, dict) and item.get("name") == ASSET_NAME),
        None,
    )
    if asset is None:
        return None
    url = asset.get("browser_download_url")
    digest = asset.get("digest")
    size = asset.get("size")
    if url != f"{base}{tag}/{ASSET_NAME}":
        return None
    matched = DIGEST_RE.fullmatch(digest.lower()) if isinstance(digest, str) else None
    if matched is None:
        return None
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_PACKAGE_BYTES:
        return None
    return Release(tag, tag[1:], version, url, matched.group(1), size)


def fetch_latest_release(workdir: Path) -> Release | None:
    body = workdir / "latest.json"
    curl_fetch(
        api_url(), body, timeout=API_TIMEOUT, max_bytes=MAX_API_BYTES,
        accept="application/vnd.github+json",
    )  # fmt: skip
    try:
        data = json.loads(body.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError("unreadable release data") from exc
    return parse_release(data, download_base())


# --- package verification ---------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_package(path: Path, release: Release, current: Version) -> None:
    """Refuse anything but the exact package we announced, without extracting it."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError("更新包不是有效的压缩文件") from exc
    with archive:
        members = archive.infolist()
        if not members or len(members) > MAX_MEMBERS:
            raise UpdateError("更新包内容数量异常")
        total = 0
        names = set()
        for info in members:
            name = info.filename
            parts = name.rstrip("/").split("/")
            if (
                not name
                or name.startswith("/")
                or "\\" in name
                or "\x00" in name
                or any(part in ("", ".", "..") for part in parts)
                or parts[0] != EXTENSION_DIR
            ):
                raise UpdateError("更新包含有不允许的路径")
            # Some tools store permissions without the file-type bits; those are plain files.
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.flag_bits & 0x1 or kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise UpdateError("更新包含有不允许的文件类型")
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise UpdateError("更新包解压后过大")
            names.add(name)
        config_name = f"{EXTENSION_DIR}/Config.plist"
        if config_name not in names:
            raise UpdateError("更新包缺少 Config.plist")
        try:
            with archive.open(config_name) as handle:
                raw = handle.read(MAX_PLIST_BYTES + 1)
            if len(raw) > MAX_PLIST_BYTES:
                raise UpdateError("更新包的 Config.plist 过大")
            config = plistlib.loads(raw)
        except (OSError, zipfile.BadZipFile, plistlib.InvalidFileException, ValueError) as exc:
            raise UpdateError("更新包的 Config.plist 无法读取") from exc
    if not isinstance(config, dict) or config.get("Extension Identifier") != EXTENSION_ID:
        raise UpdateError("更新包不是本扩展")
    packaged = config.get("Extension Version")
    if packaged != release.text or parse_version(packaged) != release.version:
        raise UpdateError("更新包的版本与公告不一致")
    if not is_newer(release.version, current):
        raise UpdateError("更新包不比当前版本新")


def prune_downloads(folder: Path, now: float) -> None:
    if not folder.is_dir():
        return
    for child in folder.iterdir():
        try:
            if now - child.lstat().st_mtime < KEEP_DOWNLOADS:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            pass


def open_in_popclip(package: Path) -> None:
    """Hand the package to PopClip, which asks the user to confirm the install."""
    if test_mode():
        log_path = os.environ.get("BETTBOX_TEST_OPEN_PATH")
        if log_path:
            with Path(log_path).open("a", encoding="utf-8") as handle:
                handle.write(f"{package}\n")
        return
    try:
        completed = subprocess.run(
            [OPEN, "-b", POPCLIP_BUNDLE_ID, str(package)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("无法把更新包交给 PopClip") from exc
    if completed.returncode != 0:
        raise UpdateError("无法把更新包交给 PopClip")


def download_and_open(release: Release, current: Version, support: Path) -> Path:
    folder = support / DOWNLOAD_DIR
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    workdir = Path(tempfile.mkdtemp(prefix="dl-", dir=folder))  # mode 0700
    try:
        package = workdir / ASSET_NAME
        try:
            curl_fetch(
                release.url, package, timeout=DOWNLOAD_TIMEOUT,
                max_bytes=min(release.size, MAX_PACKAGE_BYTES), accept="application/octet-stream",
            )  # fmt: skip
        except FetchError as exc:
            raise UpdateError("下载更新失败，请稍后再试") from exc
        if package.stat().st_size != release.size or sha256_of(package) != release.sha256:
            raise UpdateError("更新包校验失败，已放弃安装")
        inspect_package(package, release, current)
        final = folder / f"BettboxDomain-{release.text}.popclipextz"
        os.chmod(package, 0o600)
        os.replace(package, final)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    open_in_popclip(final)
    return final


# --- the check itself (runs in the detached process) -----------------------


def say(message: str) -> None:
    print(message, flush=True)


def run_check(force: bool = False) -> int:
    support = support_dir()
    support.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with (support / LOCK_FILE).open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("已有一个更新检查正在进行")
            return 0
        current = current_version()
        state = load_state(support)
        if current is None or (not force and not should_check(state, now)):
            return 0
        current_text, current_tuple = current
        downloads = support / DOWNLOAD_DIR
        downloads.mkdir(parents=True, exist_ok=True)
        os.chmod(downloads, 0o700)
        prune_downloads(downloads, now)
        state["last_attempt"] = now
        save_state(support, state)
        with tempfile.TemporaryDirectory(prefix="check-", dir=downloads) as scratch:
            try:
                release = fetch_latest_release(Path(scratch))
            except FetchError as exc:
                log("fetch-failed", str(exc))
                say(f"无法获取最新版本信息：{exc}")
                return 0
        state["last_check"] = now
        if release is None:
            save_state(support, state)
            log("no-offer")
            say("最新发布没有可安装的更新包")
            return 0
        state["latest_version"] = release.text
        save_state(support, state)
        if not is_newer(release.version, current_tuple):
            say(f"当前版本 {current_text}，最新版本 {release.text}，无需更新")
            return 0
        ignored = parse_version(state.get("ignored_version"))
        if ignored is not None and not is_newer(release.version, ignored) and not force:
            log("ignored", release.text)
            return 0
        choice = feedback.ask(
            f"Bettbox 域名覆写有新版本 {release.text}（当前 {current_text}）。\n\n"
            "要现在下载并安装吗？安装前 PopClip 还会再让你确认一次。",
            (BUTTON_IGNORE, BUTTON_LATER, BUTTON_UPDATE),
        )
        log("choice", choice or "none")
        if choice == BUTTON_IGNORE:
            state["ignored_version"] = release.text
            save_state(support, state)
            return 0
        if choice != BUTTON_UPDATE:
            return 0
        try:
            download_and_open(release, current_tuple, support)
        except UpdateError as exc:
            log("install-failed", str(exc))
            feedback.notify(f"更新到 {release.text} 失败：{exc}。当前版本不受影响。")
            say(f"更新失败：{exc}")
            return 0
        feedback.notify(f"{release.text} 已下载，请在 PopClip 弹出的提示中确认安装。")
        log("handed-to-popclip", release.text)
        return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in (["--check"], ["--check", "--force"]):
        try:
            return run_check(force="--force" in args)
        except Exception:
            log("error", "unexpected")
            return 1
    print("用法：update_check.py --check [--force]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
