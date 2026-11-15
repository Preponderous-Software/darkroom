#!/usr/bin/env node
// darkroom web runner: reads one JSON job on stdin, drives a fresh headless Chromium through
// the job's steps, writes PNG frames into job.framesDir and a result JSON to job.resultPath.
//
// Playwright is resolved from (in order) $DARKROOM_PLAYWRIGHT, $NODE_PATH, the manifest's
// node_modules, and this script's own. One browser per job: a crash costs one attempt.
"use strict";
const fs = require("fs");
const path = require("path");
const { createRequire } = require("module");

function loadPlaywright(cwd) {
  const tries = [];
  if (process.env.DARKROOM_PLAYWRIGHT) tries.push(() => require(process.env.DARKROOM_PLAYWRIGHT));
  tries.push(() => require("playwright"));
  if (cwd) tries.push(() => createRequire(path.join(cwd, "package.json"))("playwright"));
  for (const t of tries) { try { return t(); } catch (e) { /* next */ } }
  throw new Error("playwright not found: set NODE_PATH to a node_modules that has it, or npm i playwright in the project");
}

function readStdin() {
  return new Promise((resolve) => { let s = ""; process.stdin.on("data", (d) => (s += d)); process.stdin.on("end", () => resolve(s)); });
}

// Compile a js step: an expression ("a + 1") or statements ("x = 1; return x").
const PAGE_EVAL = async ({ code, scope }) => {
  const keys = Object.keys(scope);
  let fn;
  try { fn = new Function(...keys, `return (async () => (${code}))();`); }
  catch (e) { fn = new Function(...keys, `return (async () => { ${code} })();`); }
  return await fn(...keys.map((k) => scope[k]));
};

(async () => {
  const job = JSON.parse(await readStdin());
  const result = { frames: [], consoleErrors: [], marks: {}, error: null, logs: [] };
  const write = () => fs.writeFileSync(job.resultPath, JSON.stringify(result, null, 2));
  let browser;
  try {
    const { chromium } = loadPlaywright(job.cwd);
    browser = await chromium.launch({ args: job.launchArgs });
    const dev = job.device;
    const baseUA = await (async () => { const p = await browser.newPage(); const ua = await p.evaluate(() => navigator.userAgent); await p.close(); return ua; })();
    // A fresh context per take (nothing persists between takes); "monitor" in the user
    // agent so sites that count visits can skip this one.
    const context = await browser.newContext({
      viewport: { width: dev.width, height: dev.height },
      deviceScaleFactor: dev.dpr || 1, hasTouch: !!dev.touch, isMobile: !!dev.mobile,
      userAgent: job.userAgent || baseUA.replace("HeadlessChrome", "Chrome") + " darkroom-monitor",
    });
    for (const pattern of job.block || []) await context.route(pattern, (r) => r.abort());
    const page = await context.newPage();
    page.on("pageerror", (e) => result.consoleErrors.push(String(e.message || e)));
    page.on("console", (m) => { if (m.type() === "error") result.consoleErrors.push(m.text()); });

    await page.goto(job.url, { waitUntil: job.waitUntil || "load", timeout: job.readyTimeout });
    const kit = job.adapter === "strata-kit";
    const ready = job.ready || (kit ? "window.__strataKit" : "true");
    await page.waitForFunction(ready, null, { timeout: job.readyTimeout, polling: 100 });
    if (job.css) await page.addStyleTag({ content: job.css });

    let t = 1; // strata-kit clock, seconds
    const drive = async (n) => {
      await page.evaluate(({ t0, n }) => { for (let i = 1; i <= n; i++) window.__strataKit.drive(t0 + i / 30); }, { t0: t, n });
      t += n / 30;
    };
    const clip = job.crop ? { x: job.crop[0], y: job.crop[1], width: job.crop[2], height: job.crop[3] } : undefined;
    const capture = async () => {
      const file = path.join(job.framesDir, String(result.frames.length).padStart(5, "0") + ".png");
      await page.screenshot({ path: file, clip, animations: "allow", caret: "initial" });
      result.frames.push(file);
    };

    async function run(steps, scope) {
      for (const st of steps) {
        if ("js" in st) await page.evaluate(PAGE_EVAL, { code: st.js, scope });
        else if ("key" in st) {
          const a = st.action || "press";
          if (a === "down") await page.keyboard.down(st.key);
          else if (a === "up") await page.keyboard.up(st.key);
          else await page.keyboard.press(st.key);
        } else if ("type" in st) await page.keyboard.type(st.type);
        else if ("tap" in st) {
          if (dev.touch) await page.touchscreen.tap(st.tap[0], st.tap[1]);
          else await page.mouse.click(st.tap[0], st.tap[1]);
        } else if ("click" in st) await page.click(st.click);
        else if ("wait" in st) await page.waitForTimeout(st.wait);
        else if ("waitFor" in st) await page.waitForFunction(PAGE_EVAL, { code: st.waitFor, scope }, { timeout: st.timeout || 30000, polling: 100 });
        else if ("frames" in st) {
          if (kit) await drive(st.frames);
          else for (let i = 0; i < st.frames; i++) await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r())));
        } else if ("camera" in st) await page.evaluate((c) => Object.assign(window.__strataKit.cam, c), st.camera);
        else if ("css" in st) await page.addStyleTag({ content: st.css });
        else if ("capture" in st) await capture();
        else if ("mark" in st) result.marks[st.mark] = result.frames.length;
        else if ("repeat" in st) {
          const v = st.var || "i";
          for (let i = 0; i < st.repeat; i++) {
            await run(st.steps || [], { ...scope, [v]: i, n: st.repeat, u: st.repeat > 1 ? i / (st.repeat - 1) : 0 });
          }
        } else throw new Error("unknown step " + JSON.stringify(st));
      }
    }
    const scope = { vars: job.vars || {} };
    await run(job.setup || [], scope);
    await run(job.steps || [], scope);
    if (job.kind === "still" || result.frames.length === 0) {
      if (kit && job.kind === "still" && !(job.steps || []).length) await drive(6);
      if (job.kind === "still") { result.frames = []; }
      await capture();
    }
    await context.close();
  } catch (e) {
    result.error = String((e && e.stack) || e).split("\n").slice(0, 6).join("\n");
  } finally {
    if (browser) await browser.close().catch(() => {});
    write();
    process.exit(result.error ? 1 : 0);
  }
})();
