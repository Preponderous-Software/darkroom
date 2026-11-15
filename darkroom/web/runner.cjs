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
  throw new Error("playwright not found. Searched: " +
    [process.env.DARKROOM_PLAYWRIGHT && `$DARKROOM_PLAYWRIGHT (${process.env.DARKROOM_PLAYWRIGHT})`,
     `NODE_PATH (${process.env.NODE_PATH || "unset"})`, cwd && `${cwd}/node_modules and its parents`].filter(Boolean).join("; ") +
    ". Fix it once with `darkroom setup-web --link /path/to/node_modules` (a node_modules that has playwright) " +
    "or `darkroom setup-web --install`, or set NODE_PATH or DARKROOM_PLAYWRIGHT.");
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

// The paced clock (clock.engine "darkroom"), installed as an init script before the page's own
// scripts. setTimeout, setInterval, requestAnimationFrame, performance.now, Date.now and
// `new Date()` all read one clock. Until freeze() it follows real time (timers are real timers,
// tracked); after it, time stands still and only advance(ms) moves it: due timers run in order of
// their due time, then every pending animation frame runs once, with the new time.
function installClock(cfg) {
  const W = window;
  if (W.__darkroomClock) return;
  const real = {
    setTimeout: W.setTimeout.bind(W), clearTimeout: W.clearTimeout.bind(W),
    raf: W.requestAnimationFrame.bind(W), now: performance.now.bind(performance), Date: W.Date,
  };
  const RD = real.Date;
  const dateBase = RD.now() - real.now();
  let frozen = false, vt = 0, nextId = 1, seq = 0, rafs = new Map();
  const timers = new Map();
  const nowV = () => (frozen ? vt : real.now());
  const report = (e) => { if (W.reportError) W.reportError(e); else real.setTimeout(() => { throw e; }, 0); };
  const call = (fn, args) => { try { fn.apply(W, args); } catch (e) { report(e); } };
  const arm = (t) => { t.real = real.setTimeout(() => fire(t.id), Math.max(0, t.due - real.now())); };
  function fire(id) {
    const t = timers.get(id);
    if (!t) return;
    if (t.every == null) timers.delete(id);
    else { t.due = Math.max(t.due + t.every, frozen ? 0 : real.now()); t.seq = ++seq; if (!frozen) arm(t); }
    call(t.fn, t.args);
  }
  function add(fn, delay, args, repeat) {
    if (typeof fn !== "function") { const code = String(fn); fn = () => (0, eval)(code); }
    const d = Math.max(0, Number(delay) || 0);
    const t = { id: nextId++, fn, args, due: nowV() + d, every: repeat ? Math.max(1, d) : null, seq: ++seq, real: null };
    timers.set(t.id, t);
    if (!frozen) arm(t);
    return t.id;
  }
  function clear(id) {
    const t = timers.get(id);
    if (!t) return;
    timers.delete(id);
    if (t.real != null) real.clearTimeout(t.real);
  }
  W.setTimeout = function setTimeout(fn, delay, ...args) { return add(fn, delay, args, false); };
  W.setInterval = function setInterval(fn, delay, ...args) { return add(fn, delay, args, true); };
  W.clearTimeout = function clearTimeout(id) { clear(id); };
  W.clearInterval = function clearInterval(id) { clear(id); };
  W.requestAnimationFrame = function requestAnimationFrame(cb) {
    const id = nextId++;
    rafs.set(id, cb);
    if (!frozen) real.raf((ts) => { if (!frozen && rafs.delete(id)) call(cb, [ts]); });
    return id;
  };
  W.cancelAnimationFrame = function cancelAnimationFrame(id) { rafs.delete(id); };
  performance.now = function now() { return nowV(); };
  const dateNow = () => Math.round(dateBase + nowV());
  function Date(...a) {
    if (!new.target) return new RD(dateNow()).toString();
    return Reflect.construct(RD, a.length ? a : [dateNow()], new.target);
  }
  Date.prototype = RD.prototype;
  Date.now = dateNow;
  Date.parse = RD.parse;
  Date.UTC = RD.UTC;
  W.Date = Date;
  // A task boundary, so promise callbacks queued by one timer run before the next timer.
  const yieldTask = () => new Promise((r) => { const ch = new MessageChannel(); ch.port1.onmessage = () => r(); ch.port2.postMessage(0); });
  function freeze() {
    if (frozen) return;
    vt = real.now();
    frozen = true;
    for (const t of timers.values()) if (t.real != null) { real.clearTimeout(t.real); t.real = null; }
  }
  async function advance(ms) {
    freeze();
    const target = vt + ms;
    for (let guard = 0; ; guard++) {
      let next = null;
      for (const t of timers.values()) {
        if (t.due <= target && (!next || t.due < next.due || (t.due === next.due && t.seq < next.seq))) next = t;
      }
      if (!next) break;
      if (guard > 100000) throw new Error("paced clock: over 100000 timers ran in one step (a zero-delay timer loop?)");
      vt = Math.max(vt, next.due);
      fire(next.id);
      await yieldTask();
    }
    vt = target;
    const due = rafs;
    rafs = new Map();
    for (const cb of due.values()) call(cb, [vt]);
    await yieldTask();
    return vt;
  }
  Object.defineProperty(W, "__darkroomClock", {
    value: { freeze, advance, get now() { return nowV(); }, get frozen() { return frozen; } },
  });
  if (cfg.start === "load") freeze();
}
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
  // A paced clock goes in first, so the page's own scripts (and other init scripts) see it.
  const clock = job.clock && job.clock.mode === "paced" ? job.clock : null;
  s.clock = clock;
  if (clock && clock.engine === "playwright") {
    if (!context.clock) throw new Error("clock.engine 'playwright' needs Playwright 1.45 or newer (page.clock); use engine 'darkroom'");
    await context.clock.install();
    if (clock.start === "load") await context.clock.pauseAt(Date.now());
  } else if (clock) {
    await context.addInitScript({ content: `(${installClock})(${JSON.stringify(clock)});` });
  }
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
    // A paced page's own timers may be stopped, so poll from here rather than in the page.
    if (clock) await waitTrue(page, ready, {}, job.readyTimeout, "ready");
    else await page.waitForFunction(ready, null, { timeout: job.readyTimeout, polling: 100 });
    if (job.css) await page.addStyleTag({ content: job.css });
    s.t = 1; // strata-kit clock, seconds
    if (clock && clock.start !== "load") await freezeClock();
  };
  // Stop the paced clock where it is (start "ready"); from here only frames steps move it.
  const freezeClock = async () => {
    if (clock.engine === "playwright") {
      // pauseAt cannot go back, and the page's time flows while this call travels: pause a
      // little ahead (timers due in the gap run), further ahead if that was still too soon.
      for (const ahead of [20, 100, 500, 2000]) {
        try { await page.clock.pauseAt((await page.evaluate(() => Date.now())) + ahead); return; } catch (e) {
          if (!/past/.test(String(e)) || ahead === 2000) throw e;
        }
      }
    } else await page.evaluate(() => window.__darkroomClock.freeze());
  };
  // Step the paced clock n times by `step` seconds: due timers, then one round of animation frames.
  const pace = async (n, step) => {
    const ms = (step || clock.step) * 1000;
    if (clock.engine === "playwright") { for (let i = 0; i < n; i++) await page.clock.runFor(ms); return; }
    await page.evaluate(async ({ n, ms }) => { for (let i = 0; i < n; i++) await window.__darkroomClock.advance(ms); }, { n, ms });
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
      // A paced page's animation frames wait for frames steps: poll on wall-clock time instead.
      if (clock) await page.waitForTimeout(50);
      else await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r())));
    }
  };

  // The pointer's last position, for relative moves ({"mouse": "move", "by": [dx, dy]}).
  s.mouse = [0, 0];
  const moveTo = async (p, steps) => { await page.mouse.move(p[0], p[1], { steps: steps || 1 }); s.mouse = [p[0], p[1]]; };

  // A crop measured from an element ({"of": css, "pad": px}), clamped to the viewport.
  const measureCrop = async (crop) => {
    const el = page.locator(crop.of).first();
    await el.waitFor({ state: "visible", timeout: T });
    const box = await el.boundingBox();
    if (!box) throw new Error(`crop: '${crop.of}' has no box on the page`);
    const pad = crop.pad || 0, vp = page.viewportSize() || { width: dev.width, height: dev.height };
    const x0 = Math.max(0, Math.floor(box.x - pad)), y0 = Math.max(0, Math.floor(box.y - pad));
    const x1 = Math.min(vp.width, Math.ceil(box.x + box.width + pad)), y1 = Math.min(vp.height, Math.ceil(box.y + box.height + pad));
    if (x1 <= x0 || y1 <= y0) throw new Error(`crop: '${crop.of}' (plus pad ${pad}) is outside the ${vp.width}x${vp.height} viewport`);
    return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
  };

  // One shot on this page: its steps, then (for a still) the final frame. Fills `out`.
  s.shot = async (shotJob, out) => {
    s.out = out;
    const crop = shotJob.crop;
    // A measured crop is taken at the shot's first capture and kept, so every frame is one size.
    let clip = Array.isArray(crop) ? { x: crop[0], y: crop[1], width: crop[2], height: crop[3] } : undefined;
    const capture = async (name) => {
      if (crop && !Array.isArray(crop) && !clip) {
        clip = await measureCrop(crop);
        out.crop = { of: crop.of, pad: crop.pad || 0, rect: [clip.x, clip.y, clip.width, clip.height] };
      }
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
        } else if ("mouse" in st) {
          if (st.at !== undefined) await moveTo(await pointOf(st.at, st.force, "mouse"), st.steps);
          else if (st.by) await moveTo([s.mouse[0] + st.by[0], s.mouse[1] + st.by[1]], st.steps);
          if (st.mouse === "down") await page.mouse.down({ button: st.button || "left" });
          else if (st.mouse === "up") await page.mouse.up({ button: st.button || "left" });
        } else if ("drag" in st) {
          const d = st.drag, button = d.button || "left";
          const from = await pointOf(d.from, st.force, "drag.from");
          await moveTo(from);
          await page.mouse.down({ button });
          await moveTo(await pointOf(d.to, st.force, "drag.to"), d.steps || 10);
          await page.mouse.up({ button });
        } else if ("wheel" in st) {
          if (st.at !== undefined) await moveTo(await pointOf(st.at, st.force, "wheel"));
          await page.mouse.wheel(st.wheel.dx || 0, st.wheel.dy || 0);
        } else if ("stable" in st) await stable(st.stable, st.timeout || 30000);
        else if ("wait" in st) await page.waitForTimeout(st.wait);
        else if ("waitFor" in st) await waitTrue(page, st.waitFor, scope, st.timeout || 30000, "waitFor");
        else if ("frames" in st) {
          if (kit) await drive(st.frames, st.step);
          else if (clock) await pace(st.frames, st.step);
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
