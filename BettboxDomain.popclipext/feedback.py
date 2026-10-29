#!/usr/bin/python3
"""User-visible feedback for the extension: macOS notifications and one dialog.

PopClip's ``show-status`` only draws a tick or a cross, so anything that needs
words (why a rule was not added, that a new version exists) goes through here.

Exactly two AppleScript statements are ever used, ``display notification`` and
``display dialog``. No other application is scripted and no mouse or keyboard
input is generated. Text is passed as ``argv`` and never spliced into the
script.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


OSASCRIPT = "/usr/bin/osascript"
TITLE = "Bettbox 域名覆写"
DIALOG_TIMEOUT = 300

NOTIFY_SCRIPT = (
    "on run argv",
    "display notification (item 1 of argv) with title (item 2 of argv)",
    "end run",
)
DIALOG_SCRIPT = (
    "on run argv",
    "set answer to display dialog (item 1 of argv) with title (item 2 of argv)"
    " buttons {item 3 of argv, item 4 of argv, item 5 of argv}"
    " default button (item 5 of argv) with icon note"
    f" giving up after {DIALOG_TIMEOUT}",
    'if gave up of answer then return ""',
    "return button returned of answer",
    "end run",
)


def test_mode() -> bool:
    return os.environ.get("BETTBOX_TEST_MODE") == "1"


def _osascript(script: tuple[str, ...], *args: str, timeout: float) -> str | None:
    command = [OSASCRIPT]
    for line in script:
        command += ["-e", line]
    command += list(args)
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def notify(message: str, title: str = TITLE) -> None:
    """Show a macOS notification. Best effort: never raises."""
    if test_mode():
        path = os.environ.get("BETTBOX_TEST_NOTIFY_PATH")
        if path:
            with Path(path).open("a", encoding="utf-8") as handle:
                handle.write(f"{title}\t{message}\n")
        return
    _osascript(NOTIFY_SCRIPT, message[:240], title, timeout=10)


def ask(
    message: str,
    buttons: tuple[str, str, str],
    title: str = TITLE,
) -> str | None:
    """Show a three-button dialog; the last button is the default.

    Returns the label of the button the user chose, or None if the dialog timed out,
    was dismissed or could not be shown.
    """
    if test_mode():
        choice = os.environ.get("BETTBOX_TEST_DIALOG_CHOICE")
        return choice if choice in buttons else None
    answer = _osascript(
        DIALOG_SCRIPT, message, title, *buttons, timeout=DIALOG_TIMEOUT + 30
    )
    return answer if answer in buttons else None
