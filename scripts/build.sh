#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_dir=${script_dir:h}
extension_dir="$repo_dir/BettboxDomain.popclipext"
dist_dir="$repo_dir/dist"
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT

mkdir -p "$dist_dir"

xcrun swiftc -O -target arm64-apple-macos12.0 \
  "$extension_dir/bettbox_quit.swift" \
  -o "$build_dir/bettbox_quit-arm64"
xcrun swiftc -O -target x86_64-apple-macos12.0 \
  "$extension_dir/bettbox_quit.swift" \
  -o "$build_dir/bettbox_quit-x86_64"
lipo -create \
  "$build_dir/bettbox_quit-arm64" \
  "$build_dir/bettbox_quit-x86_64" \
  -output "$extension_dir/bettbox_quit"
chmod 755 "$extension_dir/bettbox_quit"

plutil -lint "$extension_dir/Config.plist"
python3 -m py_compile \
  "$extension_dir/add_domain.py" \
  "$extension_dir/inspect_domain.py" \
  "$extension_dir/scan_web_domains.py" \
  "$extension_dir/bettbox_rule_test.py"

find "$extension_dir" -type d -name __pycache__ -prune -exec rm -rf {} +
rm -f "$dist_dir/BettboxDomain.popclipextz"
ditto -c -k --keepParent --norsrc --noextattr --noqtn --noacl \
  "$extension_dir" "$dist_dir/BettboxDomain.popclipextz"

echo "Built $dist_dir/BettboxDomain.popclipextz"
