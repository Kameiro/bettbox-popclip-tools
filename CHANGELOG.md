# Changelog

## 1.1.0 - 2026-10-29

- New: update check. At most once a day, after any action, the extension asks GitHub's public releases API
  for the latest release. If it is newer, a dialog offers *更新 / 稍后提醒 / 忽略此版本*. On *更新* the package
  is downloaded, its SHA-256 (from the GitHub API) and contents are verified (paths inside the extension
  folder, no symlinks, matching identifier and announced version) and then handed to PopClip, which asks for
  the usual install confirmation. Nothing is installed silently; nothing about domains or Bettbox
  configuration is sent. Can be switched off with the new option *自动检查插件更新*.
  Extensions installed before 1.1.0 have no checker and need one manual install of this version.
- Fix: failures were invisible. Every action ended with exit code 0 and PopClip's `show-status` can only
  draw a tick, so a rule that was not added still showed ✓. Now failures that can be detected immediately
  exit non-zero (PopClip draws ✗) and show a notification with the reason; the background worker of
  *加入 Bettbox* posts a notification when it finishes or fails. *检查域名* and *捕获网页域名* likewise.
- New `feedback.py` is the only place that uses AppleScript, and only for `display notification` and
  `display dialog`; tests enforce that nothing else (System Events, key or mouse input) appears.
- Manual check: `update_check.py --check --force`.

## 1.0.2 - 2026-10-29

- Fix: with Bettbox 1.19+ the rule disappeared on launch ("Bettbox 启动后未保留该规则"). Bettbox now reads
  `config.json` first and mirrors it to the preferences plist; the extension only edited the plist. It now
  reads `config.json` first, writes both stores (primary first) and backs up / rolls back both.
- Resolve the manual-select group from the current profile (any `select` group whose name contains 手动选择)
  instead of a hard-coded `🚀 手动选择`; fail early, before Bettbox is asked to quit, if there is none.
- Repoint rules written earlier to a manual-select group that was renamed since (Mihomo rejects a rule whose
  target group does not exist).
- `检查域名` (rule tester) reads `config.json` first as well.
- Tests: model Bettbox 1.19 startup, renamed groups, YAML group detection, store precedence.

## 1.0.1 - 2026-08-14

- Launch Bettbox in the background after adding a rule.
- Continue once the Bettbox app exits instead of waiting for a delayed Core shutdown.
- Detect missing Playwright or Chromium and copy the required install command.

## 1.0.0 - 2026-08-07

- Add domains to Bettbox override rules through commands.
- Inspect static rule matching and live HTTP(S) connectivity.
- Capture domains requested by a JavaScript-enabled web page.
- Package a universal Apple Silicon and Intel helper binary.
- Remove machine-specific paths and bundle the rule-testing dependency.
