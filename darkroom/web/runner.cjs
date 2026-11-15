#!/usr/bin/env node
// darkroom web runner: reads one JSON job on stdin, drives a fresh headless Chromium through
// the job's steps, writes PNG frames into job.framesDir and a result JSON to job.resultPath.
//
// With --session it keeps the page open instead: the first stdin line opens the page (and runs
// `setup`), then each further line {"cmd": "shot", ...} runs one shot's steps on that page and
// answers with one stdout line "@@darkroom <result JSON>"; {"cmd": "close"} (or EOF) ends it.
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

const now = () => new Date().toISOString();
const newResult = () => ({ frames: [], names: [], consoleErrors: [], console: [], marks: {}, error: null, logs: [], warnings: [] });

// Open the browser and the page for a job: context, init scripts, routes, the first navigation
// and the ready wait. Returns a session whose shot() runs steps on that page. Console messages
// go to whichever result is current (`s.out`).
async function open(job, out) {
  const s = { job, out, browser: null, t: 1 };
  const { chromium } = loadPlaywright(job.cwd);
  // One timeout for every Playwright call that can time out (screenshots, clicks, taps,
  // navigation); Playwright's own 30 s default is too short for software WebGL on a busy box.
  const T = job.timeoutMs || 120000;
  s.browser = await chromium.launch({ args: job.launchArgs, timeout: T });
  const browser = s.browser;
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
  s.context = context;
  // initScript: runs in every page (and frame) before the page's own scripts, in order.
  for (const content of job.initScripts || []) await context.addInitScript({ content });
  context.setDefaultTimeout(T);
  context.setDefaultNavigationTimeout(Math.max(T, job.readyTimeout || 0));
  for (const pattern of job.block || []) await context.route(pattern, (r) => r.abort());
  const page = await context.newPage();
  s.page = page;
  // The whole console, for console.jsonl; errors (and uncaught exceptions) also in consoleErrors.
  page.on("pageerror", (e) => {
    const text = String(e.message || e);
    s.out.consoleErrors.push(text);
    s.out.console.push({ type: "pageerror", text, location: null, time: now() });
  });
  page.on("console", (m) => {
    let location = null;
    try { location = m.location(); } catch (e) { /* none */ }
    s.out.console.push({ type: m.type(), text: m.text(), location, time: now() });
    if (m.type() === "error") s.out.consoleErrors.push(m.text());
  });

  const kit = job.adapter === "strata-kit";
  const ready = job.ready || (kit ? "window.__strataKit" : "true");
  // After every navigation: the adapter's ready wait, then the shot's css.
  const afterLoad = async () => {
    await page.waitForFunction(ready, null, { timeout: job.readyTimeout, polling: 100 });
    if (job.css) await page.addStyleTag({ content: job.css });
    s.t = 1; // strata-kit clock, seconds
  };
  await page.goto(job.url, { waitUntil: job.waitUntil || "load", timeout: job.readyTimeout });
  await afterLoad();

  const drive = async (n, step) => {
    const dt = step || job.frameStep || 1 / 30;
    await page.evaluate(({ t0, n, dt }) => { for (let i = 1; i <= n; i++) window.__strataKit.drive(t0 + i * dt); }, { t0: s.t, n, dt });
    s.t += n * dt;
  };

  // The point to tap or click: [x, y], a selector's centre, or {at: [fx, fy], of: selector}
  // (fractions of the element's box). `force` skips the visibility wait.
  const pointOf = async (v, force, verb) => {
    if (Array.isArray(v)) return v;
    const sel = typeof v === "string" ? v : v.of;
    const [fx, fy] = typeof v === "string" ? [0.5, 0.5] : v.at;
    const el = page.locator(sel).first();
    if (!force) await el.waitFor({ state: "visible", timeout: T });
    const box = await el.boundingBox();
    if (!box) throw new Error(`${verb}: '${sel}' has no box on the page`);
    return [box.x + box.width * fx, box.y + box.height * fy];
  };

  // Wait until an element's box is the same over STABLE_POLLS consecutive animation frames.
  const STABLE_POLLS = 3;
  const stable = async (sel, timeout) => {
    const t0 = Date.now();
    const el = page.locator(sel).first();
    await el.waitFor({ state: "attached", timeout });
    let last = null, same = 0;
    for (;;) {
      const box = await el.boundingBox();
      const key = box ? [box.x, box.y, box.width, box.height].map((x) => x.toFixed(2)).join() : null;
      same = key !== null && key === last ? same + 1 : 0;
      if (same >= STABLE_POLLS) return;
      last = key;
      if (Date.now() - t0 > timeout) throw new Error(`stable: '${sel}' was still moving (or had no box) after ${timeout} ms`);
      await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r())));
    }
  };

  // One shot on this page: its steps, then (for a still) the final frame. Fills `out`.
  s.shot = async (shotJob, out) => {
    s.out = out;
    const clip = shotJob.crop ? { x: shotJob.crop[0], y: shotJob.crop[1], width: shotJob.crop[2], height: shotJob.crop[3] } : undefined;
    const capture = async (name) => {
      const file = path.join(shotJob.framesDir, String(out.frames.length).padStart(5, "0") + ".png");
      await page.screenshot({ path: file, clip, animations: "allow", caret: "initial", timeout: T });
      out.frames.push(file);
      out.names.push(name || null);
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
          const [x, y] = await pointOf(st.tap, st.force, "tap");
          if (dev.touch) await page.touchscreen.tap(x, y);
          else await page.mouse.click(x, y);
        } else if ("click" in st) {
          if (typeof st.click === "string") await page.click(st.click, { timeout: T, force: !!st.force });
          else { const [x, y] = await pointOf(st.click, st.force, "click"); await page.mouse.click(x, y); }
        } else if ("stable" in st) await stable(st.stable, st.timeout || 30000);
        else if ("wait" in st) await page.waitForTimeout(st.wait);
        else if ("waitFor" in st) await waitTrue(page, st.waitFor, scope, st.timeout || 30000, "waitFor");
        else if ("frames" in st) {
          if (kit) await drive(st.frames, st.step);
          else for (let i = 0; i < st.frames; i++) await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r())));
        } else if ("camera" in st) await page.evaluate((c) => Object.assign(window.__strataKit.cam, c), st.camera);
        else if ("css" in st) await page.addStyleTag({ content: st.css });
        else if ("capture" in st) await capture(typeof st.capture === "string" ? st.capture : null);
        else if ("mark" in st) out.marks[st.mark] = out.frames.length;
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
          if (!met) out.warnings.push(`until '${st.until}' still false after ${max} pass(es); carried on`);
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
    const scope = { vars: shotJob.vars || {} };
    await run(shotJob.setup || [], scope);
    await run(shotJob.steps || [], scope);
    const named = out.names.some((n) => n);
    if (shotJob.kind === "still" && named) {
      // Named captures: keep just those, one still each.
      const keep = out.names.map((n, k) => (n ? k : -1)).filter((k) => k >= 0);
      const seen = new Set();
      for (const k of keep) {
        if (seen.has(out.names[k])) throw new Error(`capture name '${out.names[k]}' used twice; each named capture writes its own file`);
        seen.add(out.names[k]);
      }
      out.frames = keep.map((k) => out.frames[k]);
      out.names = keep.map((k) => out.names[k]);
    } else if (shotJob.kind === "still" || (out.frames.length === 0 && shotJob.kind !== "setup")) {
      if (kit && shotJob.kind === "still" && !(shotJob.steps || []).length) await drive(6);
      if (shotJob.kind === "still") { out.frames = []; out.names = []; }
      await capture(null);
    }
  };
  s.close = async () => { await context.close().catch(() => {}); await browser.close().catch(() => {}); };
  return s;
}

const errorText = (e) => String((e && e.stack) || e).split("\n").slice(0, 6).join("\n");

async function single() {
  const job = JSON.parse(await readStdin());
  const result = newResult();
  let s = null;
  try {
    s = await open(job, result);
    await s.shot(job, result);
    await s.context.close();
  } catch (e) {
    result.error = errorText(e);
  } finally {
    if (s && s.browser) await s.browser.close().catch(() => {});
    fs.writeFileSync(job.resultPath, JSON.stringify(result, null, 2));
    process.exit(result.error ? 1 : 0);
  }
}

// --session: one page, many shots. Messages before the first shot (opening the page, setup)
// are reported with the first shot.
async function session() {
  const readline = require("readline");
  const rl = readline.createInterface({ input: process.stdin, terminal: false });
  const reply = (obj) => process.stdout.write("@@darkroom " + JSON.stringify(obj) + "\n");
  let s = null, pending = newResult(), code = 0;
  try {
    for await (const line of rl) {
      if (!line.trim()) continue;
      const msg = JSON.parse(line);
      if (!s) {
        try {
          s = await open(msg, pending);
          await s.shot({ kind: "setup", setup: msg.setup || [], steps: [], vars: msg.vars, framesDir: msg.framesDir }, pending);
          pending.frames = []; pending.names = []; pending.marks = {};
          reply({ ready: true, warnings: pending.warnings });
        } catch (e) {
          reply({ ready: false, error: errorText(e), consoleErrors: pending.consoleErrors, console: pending.console });
          code = 1;
          break;
        }
        continue;
      }
      if (msg.cmd === "close") break;
      const out = pending || newResult();
      pending = null;
      out.warnings = [];
      try { await s.shot(msg, out); } catch (e) { out.error = errorText(e); }
      reply(out);
    }
  } finally {
    if (s) await s.close();
    process.exit(code);
  }
}

(process.argv.includes("--session") ? session : single)();
