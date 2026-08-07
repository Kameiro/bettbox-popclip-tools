#!/usr/bin/env node

const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function loadPlaywright() {
  const candidates = [
    "playwright",
    path.join(__dirname, "node_modules", "playwright"),
    "/usr/local/lib/node_modules/playwright",
    "/opt/homebrew/lib/node_modules/playwright",
    path.join(
      os.homedir(),
      ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright",
    ),
  ];

  try {
    const npm = path.join(path.dirname(process.execPath), "npm");
    if (fs.existsSync(npm)) {
      const root = execFileSync(npm, ["root", "-g"], {
        encoding: "utf8",
        timeout: 3000,
      }).trim();
      if (root) candidates.push(path.join(root, "playwright"));
    }
  } catch {}

  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (error) {
      if (error?.code !== "MODULE_NOT_FOUND") throw error;
    }
  }
  throw new Error(
    "找不到 Playwright；请先运行 npm install -g playwright && npx playwright install chromium",
  );
}

const { chromium } = loadPlaywright();

const LOAD_TIMEOUT_MS = 18000;
const CAPTURE_AFTER_LOAD_MS = 6000;

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function hostFromUrl(value) {
  try {
    const parsed = new URL(value);
    if (!["http:", "https:", "ws:", "wss:"].includes(parsed.protocol)) return null;
    return parsed.hostname.toLowerCase().replace(/\.$/, "");
  } catch {
    return null;
  }
}

async function capture(targetUrl) {
  const browserInfo = { name: "Chromium 无头模式" };
  const browser = await chromium.launch({
    headless: true,
    args: [
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-features=Translate",
      "--mute-audio",
    ],
  });
  let context;

  try {
    context = await browser.newContext({
      viewport: { width: 1100, height: 800 },
      serviceWorkers: "allow",
    });
    const page = await context.newPage();
    const domains = new Map();
    const requests = new Map();
    let mainStatus = null;
    let totalRequests = 0;
    let totalFailed = 0;
    const startedAt = Date.now();

    function domainEntry(host) {
      if (!domains.has(host)) {
        domains.set(host, { host, requests: 0, failed: 0, types: new Set(), statuses: new Set() });
      }
      return domains.get(host);
    }

    context.on("request", (request) => {
      const host = hostFromUrl(request.url());
      if (!host) return;
      const type = request.resourceType() || "other";
      const entry = domainEntry(host);
      entry.requests += 1;
      entry.types.add(type);
      requests.set(request, { host, type });
      totalRequests += 1;
    });
    context.on("response", (response) => {
      const request = response.request();
      const saved = requests.get(request);
      const host = saved?.host || hostFromUrl(response.url());
      if (!host) return;
      const status = response.status();
      domainEntry(host).statuses.add(status);
      if (request.resourceType() === "document" && request.frame() === page.mainFrame()) {
        mainStatus = status;
      }
    });
    context.on("requestfailed", (request) => {
      const saved = requests.get(request);
      if (!saved) return;
      domainEntry(saved.host).failed += 1;
      totalFailed += 1;
    });
    function watchWebSockets(currentPage) {
      currentPage.on("websocket", (socket) => {
        const host = hostFromUrl(socket.url());
        if (!host) return;
        const entry = domainEntry(host);
        entry.requests += 1;
        entry.types.add("websocket");
        totalRequests += 1;
      });
    }
    watchWebSockets(page);
    context.on("page", watchWebSockets);

    try {
      await page.goto(targetUrl, { waitUntil: "load", timeout: LOAD_TIMEOUT_MS });
    } catch (error) {
      if (!/Timeout/i.test(String(error?.message || error))) throw error;
    }
    await page.waitForTimeout(CAPTURE_AFTER_LOAD_MS);

    const finalUrl = page.url() || targetUrl;
    const targetHost = hostFromUrl(targetUrl);
    const items = [...domains.values()]
      .map((entry) => ({
        host: entry.host,
        requests: entry.requests,
        failed: entry.failed,
        types: [...entry.types].sort(),
        statuses: [...entry.statuses].sort((a, b) => a - b),
      }))
      .sort((a, b) => {
        if (a.host === targetHost) return -1;
        if (b.host === targetHost) return 1;
        return a.host.localeCompare(b.host);
      });

    return {
      browser: browserInfo.name,
      target_url: targetUrl,
      final_url: finalUrl,
      main_status: mainStatus,
      capture_ms: Date.now() - startedAt,
      total_requests: totalRequests,
      total_failed: totalFailed,
      domains: items,
    };
  } finally {
    try {
      if (context) await Promise.race([context.close(), delay(3000)]);
    } finally {
      await Promise.race([browser.close(), delay(5000)]);
    }
  }
}

async function main() {
  const targetUrl = process.argv[2];
  if (!targetUrl) throw new Error("缺少目标 URL");
  return capture(targetUrl);
}

main()
  .then((result) => {
    process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0));
  })
  .catch((error) => {
    process.stderr.write(`${error?.message || "网页捕获失败"}\n`, () => process.exit(1));
  });
