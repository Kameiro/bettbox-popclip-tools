#!/usr/bin/python3
"""Tests for the update checker. No real network: a local fake GitHub serves the API and the package."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import plistlib
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path


EXTENSION = Path(__file__).parent.parent / "BettboxDomain.popclipext"
sys.path.insert(0, str(EXTENSION))
import feedback  # noqa: E402
import update_check as uc  # noqa: E402

IDENTIFIER = "local.luna.popclip.bettbox-domain"
NAME = uc.ASSET_NAME
FOLDER = uc.EXTENSION_DIR
CURRENT = "1.1.0"


# --- fixtures ---------------------------------------------------------------


def build_package(
    version: str,
    *,
    identifier: str = IDENTIFIER,
    extra: list[tuple[str, bytes, int]] | None = None,
    plist_version: str | None = None,
    plain_bytes: bytes | None = None,
) -> bytes:
    if plain_bytes is not None:
        return plain_bytes
    config = plistlib.dumps(
        {
            "Extension Identifier": identifier,
            "Extension Version": plist_version or version,
        }
    )
    path = Path(tempfile.mkdtemp()) / "p.zip"
    with zipfile.ZipFile(path, "w") as archive:
        directory = zipfile.ZipInfo(f"{FOLDER}/")
        directory.external_attr = (stat.S_IFDIR | 0o755) << 16
        archive.writestr(directory, b"")
        for name, data, mode in [
            (f"{FOLDER}/Config.plist", config, stat.S_IFREG | 0o644),
            (f"{FOLDER}/add_domain.py", b"#!/usr/bin/python3\n", stat.S_IFREG | 0o755),
        ] + (extra or []):
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            archive.writestr(info, data)
    data = path.read_bytes()
    shutil.rmtree(path.parent)
    return data


class FakeGitHub:
    def __init__(self) -> None:
        self.api_status = 200
        self.api_body: object = {}
        self.files: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                fake.requests.append((self.path, self.headers.get("User-Agent", "")))
                if self.path == "/api":
                    body = json.dumps(fake.api_body).encode()
                    status = fake.api_status
                elif self.path in fake.files:
                    body, status = fake.files[self.path], 200
                else:
                    body, status = b"not found", 404
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(
        self,
        version: str,
        package: bytes | None = None,
        *,
        digest: str | None | bool = True,
        url: str | None = None,
        size: int | None = None,
        asset_name: str = NAME,
        tag: str | None = None,
        **flags: object,
    ) -> bytes:
        package = build_package(version) if package is None else package
        tag = tag or f"v{version}"
        self.files[f"/dl/{tag}/{NAME}"] = package
        asset: dict[str, object] = {
            "name": asset_name,
            "browser_download_url": url or f"{self.base}/dl/{tag}/{NAME}",
            "size": len(package) if size is None else size,
        }
        if digest is True:
            asset["digest"] = "sha256:" + hashlib.sha256(package).hexdigest()
        elif isinstance(digest, str):
            asset["digest"] = digest
        self.api_body = {"tag_name": tag, "assets": [asset], **flags}
        return package

    def count(self, path: str) -> int:
        return sum(1 for item, _ in self.requests if item == path)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def env_for(root: Path, fake: FakeGitHub | None, **extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if "proxy" not in key.lower() and not key.startswith("POPCLIP_OPTION_")
    }
    env.update(
        {
            "BETTBOX_TEST_MODE": "1",
            "BETTBOX_SUPPORT_PATH": str(root / "support"),
            "BETTBOX_TEST_CURRENT_VERSION": CURRENT,
            "BETTBOX_TEST_OPEN_PATH": str(root / "open.log"),
            "BETTBOX_TEST_NOTIFY_PATH": str(root / "notify.log"),
            "BETTBOX_TEST_DIALOG_CHOICE": uc.BUTTON_UPDATE,
        }
    )
    if fake is not None:
        env["BETTBOX_TEST_UPDATE_API_URL"] = f"{fake.base}/api"
        env["BETTBOX_TEST_UPDATE_DOWNLOAD_BASE"] = f"{fake.base}/dl/"
    env.update(extra)
    return env


def run_check(root: Path, fake: FakeGitHub | None, *flags: str, **extra: str):
    return subprocess.run(
        ["/usr/bin/python3", str(EXTENSION / "update_check.py"), "--check", *flags],
        env=env_for(root, fake, **extra),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def lines(root: Path, name: str) -> list[str]:
    path = root / name
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def state_of(root: Path) -> dict:
    return json.loads((root / "support" / uc.STATE_FILE).read_text(encoding="utf-8"))


def age_state(root: Path, seconds: float) -> None:
    path = root / "support" / uc.STATE_FILE
    state = json.loads(path.read_text(encoding="utf-8"))
    for key in ("last_check", "last_attempt"):
        if key in state:
            state[key] -= seconds
    path.write_text(json.dumps(state), encoding="utf-8")


class Sandbox:
    def __enter__(self) -> tuple[Path, FakeGitHub]:
        self.dir = tempfile.TemporaryDirectory()
        self.fake = FakeGitHub()
        return Path(self.dir.name), self.fake

    def __exit__(self, *exc: object) -> None:
        self.fake.stop()
        self.dir.cleanup()


# --- versions ---------------------------------------------------------------

assert uc.parse_tag("v1.2.3") == (1, 2, 3)
assert uc.parse_tag("v10") == (10,)
for bad in ["1.2.3", "v1.2.3-beta", "v1.2.3.4.5", "v", "vx", "latest", "v1..2", "v1.2 ", None, 5]:
    assert uc.parse_tag(bad) is None, bad
assert uc.parse_version("1.1.0") == (1, 1, 0) and uc.parse_version("v1.1.0") is None
assert uc.is_newer((1, 1, 0), (1, 0, 2))
assert uc.is_newer((1, 10), (1, 9, 9))  # numeric, not textual
assert uc.is_newer((2,), (1, 9, 9))
assert not uc.is_newer((1, 1, 0), (1, 1))  # trailing zeros do not make a version newer
assert not uc.is_newer((1, 0, 2), (1, 1, 0))
assert uc.current_version() is not None  # reads Config.plist (no test override in this process)
assert uc.current_version()[1] == uc.parse_version(
    plistlib.loads((EXTENSION / "Config.plist").read_bytes())["Extension Version"]
)

# --- throttle and option -----------------------------------------------------

now = 1_000_000.0
assert uc.should_check({}, now)
assert not uc.should_check({"last_check": now - 3600}, now)
assert not uc.should_check({"last_check": now - uc.CHECK_INTERVAL + 5}, now)
assert uc.should_check({"last_check": now - uc.CHECK_INTERVAL}, now)
assert not uc.should_check({"last_attempt": now - 60}, now)  # failed a moment ago
assert uc.should_check({"last_attempt": now - uc.RETRY_INTERVAL}, now)
assert uc.should_check({"last_check": now + 10_000}, now)  # clock went backwards / bad file

saved_env = dict(os.environ)
try:
    os.environ.pop(uc.OPTION_ENV, None)
    assert uc.check_enabled()  # default on
    for off in ["0", "false", "No", "OFF", "", "2", "maybe"]:  # unknown values fail safe (off)
        os.environ[uc.OPTION_ENV] = off
        assert not uc.check_enabled(), off
    for on in ["1", "true", "YES", " on "]:
        os.environ[uc.OPTION_ENV] = on
        assert uc.check_enabled(), on
finally:
    os.environ.clear()
    os.environ.update(saved_env)

with tempfile.TemporaryDirectory() as td:
    support = Path(td)
    assert uc.load_state(support) == {}
    (support / uc.STATE_FILE).write_text("{broken", encoding="utf-8")
    assert uc.load_state(support) == {}
    (support / uc.STATE_FILE).write_text(
        json.dumps({"last_check": "yesterday", "latest_version": "v9; rm -rf", "ignored_version": "1.2.0",
                    "last_attempt": True, "other": 1}),
        encoding="utf-8",
    )
    assert uc.load_state(support) == {"ignored_version": "1.2.0"}
    uc.save_state(support, {"last_check": 5.0})
    assert uc.load_state(support) == {"last_check": 5.0}
    assert stat.S_IMODE((support / uc.STATE_FILE).stat().st_mode) == 0o600

# --- release parsing: only offer what we can verify ---------------------------

base = "https://github.com/Kameiro/bettbox-popclip-tools/releases/download/"
good_asset = {
    "name": NAME,
    "browser_download_url": f"{base}v1.2.0/{NAME}",
    "digest": "sha256:" + "ab" * 32,
    "size": 1234,
}


def release_with(**changes: object) -> dict:
    asset = {**good_asset, **{k: v for k, v in changes.items() if k in good_asset}}
    asset = {k: v for k, v in asset.items() if v is not None}
    top = {"tag_name": "v1.2.0", "assets": [asset]}
    top.update({k: v for k, v in changes.items() if k not in good_asset})
    return top


parsed = uc.parse_release(release_with(), base)
assert parsed and parsed.version == (1, 2, 0) and parsed.text == "1.2.0" and parsed.sha256 == "ab" * 32
for changes in [
    {"digest": None},  # no digest: fail closed
    {"digest": "sha256:" + "ab" * 31},
    {"digest": "md5:" + "ab" * 16},
    {"digest": "sha256:" + "zz" * 32},
    {"browser_download_url": "https://evil.example/BettboxDomain.popclipextz"},
    {"browser_download_url": f"{base}v1.9.9/{NAME}"},  # not the tagged path
    {"browser_download_url": f"http://github.com/Kameiro/bettbox-popclip-tools/releases/download/v1.2.0/{NAME}"},
    {"size": 0},
    {"size": uc.MAX_PACKAGE_BYTES + 1},
    {"size": True},
    {"name": "other.zip"},
    {"tag_name": "v1.2.0-rc1"},
    {"tag_name": "latest"},
    {"draft": True},
    {"prerelease": True},
]:
    assert uc.parse_release(release_with(**changes), base) is None, changes
assert uc.parse_release({"tag_name": "v1.2.0"}, base) is None
assert uc.parse_release([], base) is None
assert uc.parse_release(release_with(digest="SHA256:" + "AB" * 32), base).sha256 == "ab" * 32

# --- package inspection (never extracts) --------------------------------------


def inspect(package: bytes, tag_version: str = "1.2.0", current: tuple = (1, 1, 0)) -> str | None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / NAME
        path.write_bytes(package)
        release = uc.Release(f"v{tag_version}", tag_version, uc.parse_version(tag_version), "u", "0" * 64, 1)
        try:
            uc.inspect_package(path, release, current)
        except uc.UpdateError as exc:
            return str(exc)
        return None


assert inspect(build_package("1.2.0")) is None
assert inspect(b"not a zip at all") == "更新包不是有效的压缩文件"
assert inspect(build_package("1.2.0", extra=[(f"{FOLDER}/../evil", b"x", stat.S_IFREG | 0o644)])) == "更新包含有不允许的路径"
assert inspect(build_package("1.2.0", extra=[("/etc/evil", b"x", stat.S_IFREG | 0o644)])) == "更新包含有不允许的路径"
assert inspect(build_package("1.2.0", extra=[("Other.popclipext/x", b"x", stat.S_IFREG | 0o644)])) == "更新包含有不允许的路径"
assert inspect(build_package("1.2.0", extra=[(f"{FOLDER}\\evil", b"x", stat.S_IFREG | 0o644)])) == "更新包含有不允许的路径"
assert inspect(build_package("1.2.0", extra=[(f"{FOLDER}//x", b"x", stat.S_IFREG | 0o644)])) == "更新包含有不允许的路径"
assert inspect(build_package("1.2.0", extra=[(f"{FOLDER}/link", b"/etc/passwd", stat.S_IFLNK | 0o777)])) == "更新包含有不允许的文件类型"
assert inspect(build_package("1.2.0", extra=[(f"{FOLDER}/dev", b"", stat.S_IFCHR | 0o644)])) == "更新包含有不允许的文件类型"
assert inspect(build_package("1.2.0", extra=[(f"{FOLDER}/f{i}", b"x", stat.S_IFREG | 0o644) for i in range(uc.MAX_MEMBERS)])) == "更新包内容数量异常"
assert inspect(build_package("1.2.0", extra=[(f"{FOLDER}/big", b"\0" * (uc.MAX_UNPACKED_BYTES + 1), stat.S_IFREG | 0o644)])) == "更新包解压后过大"
assert inspect(build_package("1.2.0", identifier="com.example.other")) == "更新包不是本扩展"
assert inspect(build_package("1.2.0", plist_version="1.1.5")) == "更新包的版本与公告不一致"
assert inspect(build_package("1.2.0"), tag_version="1.2.1") == "更新包的版本与公告不一致"
assert inspect(build_package("1.1.0"), tag_version="1.1.0") == "更新包不比当前版本新"
assert inspect(build_package("1.0.9"), tag_version="1.0.9") == "更新包不比当前版本新"
with tempfile.TemporaryDirectory() as td:  # no Config.plist
    path = Path(td) / "p.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{FOLDER}/add_domain.py", "x")
    release = uc.Release("v1.2.0", "1.2.0", (1, 2, 0), "u", "0" * 64, 1)
    try:
        uc.inspect_package(path, release, (1, 1, 0))
    except uc.UpdateError as exc:
        assert str(exc) == "更新包缺少 Config.plist"
    else:
        raise AssertionError("accepted a package without Config.plist")

# --- scheduling hook (called from the PopClip actions) ------------------------

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    events = root / "events.log"
    saved_env = dict(os.environ)
    try:
        os.environ.update(env_for(root, None, BETTBOX_TEST_UPDATE_EVENTS_PATH=str(events)))
        uc.maybe_check_updates()
        assert events.read_text(encoding="utf-8") == "check-scheduled\n"  # due, enabled
        os.environ[uc.OPTION_ENV] = "0"
        uc.maybe_check_updates()
        assert events.read_text(encoding="utf-8") == "check-scheduled\n"  # switched off: nothing new
        os.environ[uc.OPTION_ENV] = "1"
        uc.save_state(root / "support", {"last_check": time.time() - 60})
        uc.maybe_check_updates()
        assert events.read_text(encoding="utf-8") == "check-scheduled\n"  # checked a minute ago
        uc.save_state(root / "support", {"last_check": time.time() - uc.CHECK_INTERVAL - 60})
        uc.maybe_check_updates()
        assert events.read_text(encoding="utf-8") == "check-scheduled\ncheck-scheduled\n"
        (root / "support" / uc.DISABLE_FILE).write_text("", encoding="utf-8")
        uc.maybe_check_updates()  # the marker file switches the check off, option or not
        assert events.read_text(encoding="utf-8") == "check-scheduled\ncheck-scheduled\n"
        (root / "support" / uc.DISABLE_FILE).unlink()
        os.environ["BETTBOX_TEST_CURRENT_VERSION"] = "not a version"
        os.environ["BETTBOX_SUPPORT_PATH"] = "/dev/null/not-a-directory"
        uc.maybe_check_updates()  # must swallow every error
    finally:
        os.environ.clear()
        os.environ.update(saved_env)

# --- the real curl command line (tests above talk plain http to localhost) -----

saved_env = dict(os.environ)
try:
    os.environ.pop("BETTBOX_TEST_MODE", None)
    command = uc.curl_command(uc.API_URL, Path("/tmp/x"), timeout=10, max_bytes=5, accept="a/b")
    assert command[:2] == ["/usr/bin/curl", "-q"]  # -q must be first: ignore ~/.curlrc
    assert command[command.index("--proto") + 1] == "=https"
    assert command[command.index("--proto-redir") + 1] == "=https"
    assert command[-2:] == ["--url", uc.API_URL]
    assert "--fail" in command and "--max-time" in command and "--max-filesize" in command
    assert uc.API_URL.startswith("https://api.github.com/repos/Kameiro/bettbox-popclip-tools/")
    assert uc.DOWNLOAD_BASE == "https://github.com/Kameiro/bettbox-popclip-tools/releases/download/"
    # Overrides are for tests only.
    os.environ["BETTBOX_TEST_UPDATE_API_URL"] = "http://127.0.0.1:1/api"
    assert uc.api_url() == uc.API_URL and uc.download_base() == uc.DOWNLOAD_BASE
finally:
    os.environ.clear()
    os.environ.update(saved_env)

# --- end to end against the fake GitHub ---------------------------------------

# A newer release, the user presses "更新": downloaded, verified, handed to PopClip.
with Sandbox() as (root, fake):
    package = fake.publish("1.2.0")
    result = run_check(root, fake)
    assert result.returncode == 0, result.stderr
    handed = lines(root, "open.log")
    assert len(handed) == 1 and handed[0].endswith("BettboxDomain-1.2.0.popclipextz"), handed
    saved = Path(handed[0])
    assert saved.read_bytes() == package and stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert stat.S_IMODE(saved.parent.stat().st_mode) == 0o700
    assert [p.name for p in saved.parent.iterdir()] == [saved.name]  # scratch dirs are gone
    assert "请在 PopClip 弹出的提示中确认安装" in lines(root, "notify.log")[0]
    state = state_of(root)
    assert state["latest_version"] == "1.2.0" and "last_check" in state and "ignored_version" not in state
    assert fake.count("/api") == 1 and fake.count(f"/dl/v1.2.0/{NAME}") == 1
    assert fake.requests[0][1] == f"bettbox-popclip-tools/{CURRENT}"
    # Checked a moment ago: a second run does not even contact GitHub.
    run_check(root, fake)
    assert fake.count("/api") == 1 and len(lines(root, "open.log")) == 1

# Same version / older version: nothing to offer, no dialog, nothing downloaded.
for published in ["1.1.0", "1.0.9", "1.1"]:
    with Sandbox() as (root, fake):
        fake.publish(published)
        result = run_check(root, fake)
        assert result.returncode == 0 and "无需更新" in result.stdout, (published, result.stdout)
        assert lines(root, "open.log") == [] and lines(root, "notify.log") == []
        assert fake.count(f"/dl/v{published}/{NAME}") == 0
        assert state_of(root)["latest_version"] == published

# "稍后提醒" and a dismissed/timed-out dialog install nothing and ignore nothing.
for choice in [uc.BUTTON_LATER, "", "something else"]:
    with Sandbox() as (root, fake):
        fake.publish("1.2.0")
        run_check(root, fake, BETTBOX_TEST_DIALOG_CHOICE=choice)
        assert lines(root, "open.log") == [] and fake.count(f"/dl/v1.2.0/{NAME}") == 0, choice
        assert "ignored_version" not in state_of(root)
        age_state(root, 2 * 86400)  # a day later it asks again
        run_check(root, fake)
        assert len(lines(root, "open.log")) == 1

# "忽略此版本": silent until a higher version exists.
with Sandbox() as (root, fake):
    fake.publish("1.2.0")
    run_check(root, fake, BETTBOX_TEST_DIALOG_CHOICE=uc.BUTTON_IGNORE)
    assert state_of(root)["ignored_version"] == "1.2.0" and lines(root, "open.log") == []
    age_state(root, 2 * 86400)
    run_check(root, fake)  # dialog choice would be "更新" - but there must be no dialog
    assert lines(root, "open.log") == [] and fake.count(f"/dl/v1.2.0/{NAME}") == 0
    fake.publish("1.2.1")
    age_state(root, 2 * 86400)
    run_check(root, fake)
    assert len(lines(root, "open.log")) == 1 and lines(root, "open.log")[0].endswith("1.2.1.popclipextz")
    # --force is for people debugging by hand and ignores both the throttle and the skip list.
    fake.publish("1.2.1")
    fake.requests.clear()
    run_check(root, fake, "--force", BETTBOX_TEST_DIALOG_CHOICE=uc.BUTTON_IGNORE)
    assert fake.count("/api") == 1

# GitHub unreachable, rate limited or answering rubbish: silent, remembered as an attempt,
# retried after an hour rather than after a day.
for failure in ["500", "403", "garbage"]:
    with Sandbox() as (root, fake):
        fake.publish("1.2.0")
        if failure == "garbage":
            fake.files["/api"] = b"x"
            fake.api_body = "not an object"
        else:
            fake.api_status = int(failure)
        result = run_check(root, fake)
        assert result.returncode == 0 and lines(root, "open.log") == [], failure
        state = state_of(root)
        assert "last_attempt" in state and ("last_check" in state) == (failure == "garbage"), (failure, state)
        before = fake.count("/api")
        run_check(root, fake)
        if failure != "garbage":
            assert fake.count("/api") == before, "retry must wait"  # throttled, not hammered
with Sandbox() as (root, fake):
    fake.publish("1.2.0")
    fake.api_status = 500
    run_check(root, fake)
    fake.api_status = 200
    age_state(root, uc.RETRY_INTERVAL + 5)
    run_check(root, fake)
    assert len(lines(root, "open.log")) == 1  # recovered after the retry interval
fake = FakeGitHub()
dead_base = fake.base
fake.stop()
with tempfile.TemporaryDirectory() as td:  # connection refused
    root = Path(td)
    unreachable = FakeGitHub()
    unreachable.base = dead_base
    started = time.monotonic()
    result = run_check(root, unreachable)
    assert result.returncode == 0 and time.monotonic() - started < 30
    assert lines(root, "open.log") == [] and "last_attempt" in state_of(root)

# Releases we must not install even though the user would press "更新".
with Sandbox() as (root, fake):  # digest missing
    fake.publish("1.2.0", digest=None)
    run_check(root, fake)
    assert lines(root, "open.log") == [] and fake.count(f"/dl/v1.2.0/{NAME}") == 0
with Sandbox() as (root, fake):  # points somewhere else
    fake.publish("1.2.0", url="https://evil.example/x/BettboxDomain.popclipextz")
    run_check(root, fake)
    assert lines(root, "open.log") == [] and [p for p, _ in fake.requests] == ["/api"]
with Sandbox() as (root, fake):  # checksum does not match the bytes
    fake.publish("1.2.0", digest="sha256:" + "0" * 64)
    run_check(root, fake)
    assert lines(root, "open.log") == []
    assert "更新包校验失败" in lines(root, "notify.log")[0] and "当前版本不受影响" in lines(root, "notify.log")[0]
    downloads = root / "support" / uc.DOWNLOAD_DIR
    assert list(downloads.iterdir()) == [], "rejected download must not be kept"
with Sandbox() as (root, fake):  # server sends more than announced
    fake.publish("1.2.0", size=100)
    run_check(root, fake)
    assert lines(root, "open.log") == [] and "更新到 1.2.0 失败" in lines(root, "notify.log")[0]
with Sandbox() as (root, fake):  # correct checksum, hostile content
    hostile = build_package("1.2.0", extra=[(f"{FOLDER}/link", b"/Users", stat.S_IFLNK | 0o777)])
    fake.publish("1.2.0", hostile)
    run_check(root, fake)
    assert lines(root, "open.log") == [] and "不允许的文件类型" in lines(root, "notify.log")[0]
    assert list((root / "support" / uc.DOWNLOAD_DIR).iterdir()) == []
with Sandbox() as (root, fake):  # correct checksum, somebody else's extension
    fake.publish("1.2.0", build_package("1.2.0", identifier="com.example.other"))
    run_check(root, fake)
    assert lines(root, "open.log") == [] and "更新包不是本扩展" in lines(root, "notify.log")[0]
with Sandbox() as (root, fake):  # a download that 404s
    fake.publish("1.2.0")
    del fake.files[f"/dl/v1.2.0/{NAME}"]
    run_check(root, fake)
    assert lines(root, "open.log") == [] and "下载更新失败" in lines(root, "notify.log")[0]

# One check at a time.
with Sandbox() as (root, fake):
    fake.publish("1.2.0")
    (root / "support").mkdir()
    import fcntl

    with (root / "support" / uc.LOCK_FILE).open("a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        result = run_check(root, fake)
    assert "正在进行" in result.stdout and fake.requests == [] and lines(root, "open.log") == []

# Old downloads are cleaned up; recent ones and symlinks are handled safely.
with tempfile.TemporaryDirectory() as td:
    folder = Path(td)
    old, fresh = folder / "old.popclipextz", folder / "fresh.popclipextz"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    stale_dir = folder / "dl-stale"
    stale_dir.mkdir()
    victim = folder.parent / "prune-victim.txt"
    victim.write_text("keep", encoding="utf-8")
    link = folder / "old-link"
    link.symlink_to(victim)
    long_ago = time.time() - uc.KEEP_DOWNLOADS - 100
    for item in (old, stale_dir):
        os.utime(item, (long_ago, long_ago))
    os.utime(link, (long_ago, long_ago), follow_symlinks=False)
    uc.prune_downloads(folder, time.time())
    assert not old.exists() and not stale_dir.exists() and not link.is_symlink()
    assert fresh.exists() and victim.read_text(encoding="utf-8") == "keep"
    victim.unlink()

# The checker is silent about itself: nothing is printed to a PopClip user except via
# feedback, and a bad invocation is refused.
bad = subprocess.run(
    ["/usr/bin/python3", str(EXTENSION / "update_check.py"), "--install"],
    capture_output=True, text=True, check=False,
)
assert bad.returncode == 2

# --- feedback.py: what is really handed to osascript ----------------------------

calls: list[list[str]] = []
answer = {"out": "更新\n", "code": 0}


def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    calls.append(command)
    return subprocess.CompletedProcess(command, answer["code"], stdout=answer["out"], stderr="")


real_run = feedback.subprocess.run
saved_env = dict(os.environ)
try:
    feedback.subprocess.run = fake_run
    os.environ.pop("BETTBOX_TEST_MODE", None)
    hostile_text = 'x" & (do shell script "touch /tmp/pwned") & "'
    feedback.notify(hostile_text)
    command = calls[-1]
    assert command[0] == "/usr/bin/osascript"
    script_lines = [command[i + 1] for i, part in enumerate(command) if part == "-e"]
    assert script_lines == list(feedback.NOTIFY_SCRIPT)
    assert command[-2:] == [hostile_text, feedback.TITLE]  # text only ever travels as an argument
    assert all(hostile_text not in part for part in script_lines)

    buttons = (uc.BUTTON_IGNORE, uc.BUTTON_LATER, uc.BUTTON_UPDATE)
    assert feedback.ask("新版本", buttons) == "更新"
    command = calls[-1]
    assert [command[i + 1] for i, part in enumerate(command) if part == "-e"] == list(feedback.DIALOG_SCRIPT)
    assert command[-5:] == ["新版本", feedback.TITLE, *buttons]
    answer["out"] = "\n"  # gave up (timeout)
    assert feedback.ask("新版本", buttons) is None
    answer["out"] = "Evil\n"
    assert feedback.ask("新版本", buttons) is None
    answer["out"], answer["code"] = "更新\n", 1  # user closed it / osascript failed
    assert feedback.ask("新版本", buttons) is None

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("no osascript")

    feedback.subprocess.run = explode
    feedback.notify("must not raise")
    assert feedback.ask("x", buttons) is None
finally:
    feedback.subprocess.run = real_run
    os.environ.clear()
    os.environ.update(saved_env)

# The AppleScript has to be valid; compile it without running it.
osacompile = Path("/usr/bin/osacompile")
if osacompile.exists():
    for script in (feedback.NOTIFY_SCRIPT, feedback.DIALOG_SCRIPT):
        with tempfile.TemporaryDirectory() as td:
            compiled = subprocess.run(
                [str(osacompile), "-o", str(Path(td) / "x.scpt"), *[a for line in script for a in ("-e", line)]],
                capture_output=True, text=True, check=False,
            )
            assert compiled.returncode == 0, compiled.stderr

# --- the packaged Config.plist ---------------------------------------------------

config = plistlib.loads((EXTENSION / "Config.plist").read_bytes())
option = config["Options"][0]
assert option["Option Identifier"] == "autoupdatecheck"
assert "POPCLIP_OPTION_" + option["Option Identifier"].upper() == uc.OPTION_ENV
assert option["Option Type"] == "boolean" and option["Option Default Value"] is True
assert uc.parse_version(config["Extension Version"]) is not None
assert config["Extension Identifier"] == IDENTIFIER == uc.EXTENSION_ID

print("all update checker tests passed")
