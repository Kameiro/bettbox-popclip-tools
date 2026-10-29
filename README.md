# Bettbox Domain Tools for PopClip

一个面向 macOS Bettbox 用户的 PopClip 扩展，提供三个域名操作：

- **加入 Bettbox**：把所选域名写入当前 profile 的覆写规则，目标是该 profile 里名称含“手动选择”的策略组（如 `🚀 手动选择`、`☝️ 手动选择`，会自动识别）。
- **检查域名**：检查覆写规则是否命中，并通过当前系统网络路径执行 HTTP(S) 诊断。
- **捕获网页域名**：用无头 Chromium 加载页面，列出页面实际请求的域名和失败状态。

## 下载与安装

1. 从 [Releases](../../releases/latest) 下载 `BettboxDomain.popclipextz`。
2. 双击安装包并允许 PopClip 安装扩展。
3. 在浏览器或其他应用中选择一个域名或完整 HTTP(S) URL，然后选择相应操作。

## 要求

- macOS 12 或更高版本，Apple Silicon 或 Intel Mac
- PopClip 2024.5（内部版本 4151）或更高版本
- Bettbox 安装在 `/Applications/Bettbox.app`（支持 1.18.x，以及把配置存放在 `config.json` 的 1.19 及更新版本）
- 当前 Bettbox profile 中存在一个名称含“手动选择”的 `select` 策略组（插件从该 profile 的订阅文件里读取组名）
- “捕获网页域名”还需要 Node.js 和 Playwright：

  ```bash
  npm install -g playwright
  npx playwright install chromium
  ```

  首次运行会检查 Playwright 与 Chromium。缺少依赖时，PopClip 会显示安装提醒，并把对应命令复制到剪贴板。

“加入 Bettbox”会先备份配置，再以原子方式写入规则。Bettbox 1.19 起把 `~/Library/Application Support/com.appshub.bettbox/config.json` 作为主存储（启动时优先读取它），偏好设置里的 `flutter.config` 只是镜像；插件与新版 Bettbox 保持一致：先读 `config.json`（缺失或没有 profile 时才回退到偏好设置），写入时两处都写（`config.json` 先写），备份和失败回滚也覆盖两处。若规则指向的手动选择组后来被改名，插件会把已有规则一并改指向现在的组名。由于 Bettbox 1.18.3 没有公开的核心重载接口，该操作会请求 Bettbox 正常退出并重新启动，以确保 Mihomo 加载新规则。启动使用 macOS 后台模式，不会切换到 Bettbox 窗口；旧 Core 的延后清理不会阻塞新实例启动。

## 失败提示

PopClip 的 ✓ / ✗ 只表示动作有没有成功启动，不显示文字，所以结果会用 macOS 通知告诉你：

- 输入无效、找不到“手动选择”组等能立即判断的问题：PopClip 显示 ✗，并弹出通知说明原因。
- “加入 Bettbox”在后台完成（Bettbox 重启、规则回读）后：弹出通知“已加入 Bettbox 并恢复 Mihomo”，失败则弹出“未能加入 …：原因”。
- “检查域名”“捕获网页域名”失败时同样显示 ✗ 并弹出通知。

通知由 macOS 的 `osascript` 发出；如果没有看到通知，请在“系统设置 → 通知”里检查“脚本编辑器”是否被允许。

## 更新

从 1.1.0 起，插件每天最多检查一次 GitHub 上是否有新版本（在你使用任一动作之后，后台进行，不会拖慢动作）：

1. 有更高版本时弹出对话框：**更新** / **稍后提醒** / **忽略此版本**。不会静默替换。
2. 点“更新”后，插件下载 Release 里的 `BettboxDomain.popclipextz`，核对 GitHub 公布的 SHA-256，并检查包内容（路径都在扩展目录内、没有符号链接、扩展标识和版本与公告一致）。任何一项不符就放弃，并通知你，当前版本不受影响。
3. 通过检查后交给 PopClip，由 PopClip 自己弹出安装确认，确认后原位替换旧版。
4. “稍后提醒”会在下一次检查（约 24 小时后）再问；“忽略此版本”则直到出现更高的版本才再提示。

关闭检查（关闭后不会有任何网络请求），任选其一：

- 在 PopClip 偏好设置 → 扩展 里打开本扩展的设置，取消勾选“自动检查插件更新”。
- 或创建一个空文件，不依赖 PopClip 的设置界面：

  ```bash
  touch "$HOME/Library/Application Support/com.appshub.bettbox/popclip-bettbox-domain.no-update-check"
  ```

  删除该文件即可恢复检查。

手动检查（相当于“立即检查”，忽略 24 小时间隔和“忽略此版本”）：

```bash
/usr/bin/python3 "$HOME/Library/Application Support/PopClip/Extensions/BettboxDomain.popclipext/update_check.py" --check --force
```

已安装 1.0.2 或更早版本的用户没有检查功能，需要手动安装一次 1.1.0（双击新的安装包即可），之后才会收到更新提示。

## 构建

```bash
./scripts/build.sh
python3 tests/test_bettbox_domain.py
python3 tests/test_update_check.py
```

构建脚本会生成 Apple Silicon 与 Intel 通用的 `bettbox_quit`，并把扩展打包到：

```text
dist/BettboxDomain.popclipextz
```

## 隐私与安全

- 不读取或上传订阅 URL、代理节点凭据、UUID 或私钥。
- 所有 Bettbox 配置修改都在本机完成，并在写入前创建备份。
- 插件唯一的网络请求是更新检查：每天最多一次，向 `api.github.com` 查询本仓库最新 Release（公开接口，不带令牌）。GitHub 能看到的只有你的 IP 和 User-Agent（`bettbox-popclip-tools/<插件版本>`）；不发送域名、规则或 Bettbox 配置。可在扩展设置中关闭。
- 更新包的校验值与安装包来自同一个 GitHub 账号的 Release：它能防住传输损坏和中间人，但无法防止账号本身被盗后发布恶意版本。安装前 PopClip 的确认界面是最后一道关口。
- 只有两条 AppleScript 语句被使用：`display notification`（通知）和 `display dialog`（更新对话框）。不控制其他应用，不模拟点击或按键；测试会检查这一点。
- “捕获网页域名”会访问用户所选择的网页并执行其 JavaScript；输出只保留域名、HTTP 状态和请求类型。

## License

MIT
