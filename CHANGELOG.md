# Changelog

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
