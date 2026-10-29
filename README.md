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

## 构建

```bash
./scripts/build.sh
python3 tests/test_bettbox_domain.py
```

构建脚本会生成 Apple Silicon 与 Intel 通用的 `bettbox_quit`，并把扩展打包到：

```text
dist/BettboxDomain.popclipextz
```

## 隐私与安全

- 不读取或上传订阅 URL、代理节点凭据、UUID 或私钥。
- 所有 Bettbox 配置修改都在本机完成，并在写入前创建备份。
- “捕获网页域名”会访问用户所选择的网页并执行其 JavaScript；输出只保留域名、HTTP 状态和请求类型。

## License

MIT
