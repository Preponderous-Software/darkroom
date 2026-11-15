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

// Poll an expression (with the step's scope) inside the page until it is truthy. Playwright's
// waitForFunction does not await an async predicate (a Promise is truthy), so poll here.
async function waitTrue(page, code, scope, timeout, what) {
  const t0 = Date.now();
  for (;;) {
    if (await page.evaluate(PAGE_EVAL, { code, scope })) return;
    if (Date.now() - t0 > timeout) throw new Error(`${what}: '${code}' still false after ${timeout} ms`);
    await page.waitForTimeout(100);
  }
}

(async () => {
  const job = JSON.parse(await readStdin());
  const result = { frames: [], names: [], consoleErrors: [], marks: {}, error: null, logs: [], warnings: [] };
  const write = () => fs.writeFileSync(job.resultPath, JSON.stringify(result, null, 2));
  let browser;
  try {
    const { chromium } = loadPlaywright(job.cwd);
    // One timeout for every Playwright call that can time out (screenshots, clicks, taps,
    // navigation); Playwright's own 30 s default is too short for software WebGL on a busy box.
    const T = job.timeoutMs || 120000;
    browser = await chromium.launch({ args: job.launchArgs, timeout: T });
    const dev = job.device;
    const baseUA = await (async () => { const p = await browser.newPage(); const ua = await p.evaluate(() => navigator.userAgent); await p.close(); return ua; })();
    // A fresh context per take (nothing persists between takes); "monitor" in the user
    // agent so sites that count visits can skip this one.
    const context = await browser.newContext({
      ...(job.context || {}),
      viewport: { width: dev.width, height: dev.height },
      deviceScaleFactor: dev.dpr || 1, hasTouch: !!dev.touch, isMobile: !!dev.mobile,
      userAgent: job.userAgent || baseUA.replace("HeadlessChrome", "Chrome") + " darkroom-monitor",
    });
    // initScript: runs in every page (and frame) before the page's own scripts, in order.
    for (const content of job.initScripts || []) await context.addInitScript({ content });
    context.setDefaultTimeout(T);
    context.setDefaultNavigationTimeout(Math.max(T, job.readyTimeout || 0));
    for (const pattern of job.block || []) await context.route(pattern, (r) => r.abort());
    const page = await context.newPage();
    page.on("pageerror", (e) => result.consoleErrors.push(String(e.message || e)));
    page.on("console", (m) => { if (m.type() === "error") result.consoleErrors.push(m.text()); });

    const kit = job.adapter === "strata-kit";
    const ready = job.ready || (kit ? "window.__strataKit" : "true");
    let t = 1; // strata-kit clock, seconds
    // After every navigation: the adapter's ready wait, then the shot's css.
    const afterLoad = async () => {
      await page.waitForFunction(ready, null, { timeout: job.readyTimeout, polling: 100 });
      if (job.css) await page.addStyleTag({ content: job.css });
      t = 1;
    };
    await page.goto(job.url, { waitUntil: job.waitUntil || "load", timeout: job.readyTimeout });
    await afterLoad();

    const drive = async (n, step) => {
      const dt = step || job.frameStep || 1 / 30;
      await page.evaluate(({ t0, n, dt }) => { for (let i = 1; i <= n; i++) window.__strataKit.drive(t0 + i * dt); }, { t0: t, n, dt });
      t += n * dt;
    };
    const clip = job.crop ? { x: job.crop[0], y: job.crop[1], width: job.crop[2], height: job.crop[3] } : undefined;
    const capture = async (name) => {
      const file = path.join(job.framesDir, String(result.frames.length).padStart(5, "0") + ".png");
      await page.screenshot({ path: file, clip, animations: "allow", caret: "initial", timeout: T });
      result.frames.push(file);
      result.names.push(name || null);
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
          let x, y;
          if (typeof st.tap === "string") {
            const el = page.locator(st.tap).first();
            await el.waitFor({ state: "visible", timeout: T });
            const box = await el.boundingBox();
            if (!box) throw new Error(`tap: '${st.tap}' has no box on the page`);
            x = box.x + box.width / 2; y = box.y + box.height / 2;
          } else [x, y] = st.tap;
          if (dev.touch) await page.touchscreen.tap(x, y);
          else await page.mouse.click(x, y);
        } else if ("click" in st) await page.click(st.click, { timeout: T });
        else if ("wait" in st) await page.waitForTimeout(st.wait);
        else if ("waitFor" in st) await waitTrue(page, st.waitFor, scope, st.timeout || 30000, "waitFor");
        else if ("frames" in st) {
          if (kit) await drive(st.frames, st.step);
          else for (let i = 0; i < st.frames; i++) await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r())));
        } else if ("camera" in st) await page.evaluate((c) => Object.assign(window.__strataKit.cam, c), st.camera);
        else if ("css" in st) await page.addStyleTag({ content: st.css });
        else if ("capture" in st) await capture(typeof st.capture === "string" ? st.capture : null);
        else if ("mark" in st) result.marks[st.mark] = result.frames.length;
        else if ("goto" in st) {
          await page.goto(new URL(st.goto, page.url()).href, { waitUntil: job.waitUntil || "load", timeout: job.readyTimeout });
          await afterLoad();
        } else if ("reload" in st) {
          await page.reload({ waitUntil: job.waitUntil || "load", timeout: job.readyTimeout });
          await afterLoad();
        } else if ("until" in st) {
          // Repeat the steps until the expression holds (checked after each pass), at most
          // `max` passes, then `then` more passes.
          const v = st.var || "i", max = st.max, then = st.then || 0;
          let i = 0, met = false;
          while (i < max) {
            await run(st.steps || [], { ...scope, [v]: i, n: max, u: max > 1 ? i / (max - 1) : 0 });
            i++;
            if (await page.evaluate(PAGE_EVAL, { code: st.until, scope })) { met = true; break; }
          }
          if (!met) result.warnings.push(`until '${st.until}' still false after ${max} pass(es); carried on`);
          for (let k = 0; k < then; k++) {
            await run(st.steps || [], { ...scope, [v]: i + k, n: max, u: 1 });
          }
        } else if ("repeat" in st) {
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
    const named = result.names.some((n) => n);
    if (job.kind === "still" && named) {
      // Named captures: keep just those, one still each.
      const keep = result.names.map((n, k) => (n ? k : -1)).filter((k) => k >= 0);
      const seen = new Set();
      for (const k of keep) {
        if (seen.has(result.names[k])) throw new Error(`capture name '${result.names[k]}' used twice; each named capture writes its own file`);
        seen.add(result.names[k]);
      }
      result.frames = keep.map((k) => result.frames[k]);
      result.names = keep.map((k) => result.names[k]);
    } else if (job.kind === "still" || result.frames.length === 0) {
      if (kit && job.kind === "still" && !(job.steps || []).length) await drive(6);
      if (job.kind === "still") { result.frames = []; result.names = []; }
      await capture(null);
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
