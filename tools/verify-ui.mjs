// Drive the jorm UI end-to-end against the live sim node.
import { readFileSync } from 'node:fs';
import { chromium } from 'playwright';

// JORM_URL lets us route around WSL2's localhost relay, which drops out
// intermittently; the sim always answers on the distro's own IP.
const BASE = process.env.JORM_URL || 'http://localhost:8000';
const SHOTS = process.env.SHOTS;
const results = [];
// An ESP32 with eight guests running, mid-GC, with a browser hammering it, will
// occasionally take longer than ten seconds to accept a TCP connection. That is a
// busy node, not a failed one — the same distinction ota.py had to learn. Retry
// once before believing the worst.
const _fetch = globalThis.fetch;
globalThis.fetch = async (url, opts) => {
  try {
    return await _fetch(url, opts);
  } catch (e) {
    await new Promise(r => setTimeout(r, 2000));
    return await _fetch(url, opts);
  }
};

const S_blinkyStillThere = gs => gs.some(g => g.id === 'blinky');
const refreshTree = p => p.reload().then(() => p.waitForSelector('.trow.g'));
const ok = (name, cond) => {
  results.push([cond ? 'ok' : 'FAIL', name]);
  console.log((cond ? '  ok: ' : 'FAIL: ') + name);
  if (!cond) process.exitCode = 1;
};

// reset: demo guests installed + running, whatever state the node was left in
const TOKEN = process.env.JORM_TOKEN || 'dev-token';
const HDRS = { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' };
const ex = p => readFileSync(new URL('../examples/' + p, import.meta.url), 'utf8');
// This suite ends by killing the node (the stale-mode drill), so the next run
// starts against a board that is still booting — and every setup call would be
// swallowed by a .catch(). Wait for the node before assuming there is one.
process.stdout.write('waiting for the node');
for (let i = 0; i < 60; i++) {
  try {
    const r = await fetch(`${BASE}/api/node`, { headers: HDRS });
    if (r.ok) { console.log(' — up'); break; }
  } catch (e) { /* still down */ }
  process.stdout.write('.');
  await new Promise(r => setTimeout(r, 2000));
}

// hermetic: wipe and reinstall, so config state from a prior run can't make
// a later write a no-op that never goes pending
const DEMO = ['blinky', 'echoer', 'pinger', 'thermo'];
for (const g of DEMO) {
  await fetch(`${BASE}/api/guests/${g}/stop`, { method: 'POST', headers: HDRS }).catch(() => {});
  await fetch(`${BASE}/api/guests/${g}`, { method: 'DELETE', headers: HDRS }).catch(() => {});
  await fetch(`${BASE}/api/guests`, { method: 'POST', headers: HDRS, body: JSON.stringify({
    manifest: JSON.parse(ex(g + '/manifest.json')),
    files: { 'main.py': ex(g + '/main.py') } }) }).catch(() => {});
}
for (const g of DEMO) {
  const r = await fetch(`${BASE}/api/guests/${g}/start`, {
    method: 'POST', headers: HDRS }).catch(() => null);
  if (!r || !r.ok) console.log(`  (warning: ${g} did not start — checks that need it will fail)`);
}
await new Promise(r => setTimeout(r, 1500));   // let them publish before we look
const consoleHas = async (g, text) => {
  const r = await fetch(`${BASE}/api/guests/${g}/console?n=100`, { headers: HDRS });
  return JSON.stringify(await r.json()).includes(text);
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
page.on('pageerror', e => console.log('PAGE ERROR:', e.message));
page.on('response', async r => {
  if (r.url().endsWith('/api/bus/publish'))
    console.log('  publish →', r.status(), await r.text());
});

await page.goto(BASE);
// login
await page.waitForSelector('#in-token');
await page.fill('#in-url', BASE);
await page.fill('#in-token', TOKEN);
await page.click('#connectbtn');

// The node may carry guests this harness did not install (a beacon, a vitals).
// Count what is there rather than assume — a test that only passes on an empty
// node is a test that fails the moment the node is being used for something.
const installed = (await (await fetch(`${BASE}/api/guests`, { headers: HDRS })).json()).length;
const runningNow = () => fetch(`${BASE}/api/guests`, { headers: HDRS })
  .then(r => r.json()).then(gs => gs.filter(g => g.state === 'running').length);

await page.waitForSelector('.trow.g', { timeout: 8000 });
await page.waitForFunction(n =>
  document.querySelectorAll('.trow.g').length === n, installed, { timeout: 8000 });
ok(`login → tree renders all ${installed} guests`, true);
ok('hostchip = jorm-c510', (await page.textContent('#hostchip')) === 'jorm-c510');
const running = await runningNow();
await page.waitForFunction(t =>
  document.querySelector('#gval').textContent === t,
  `${running}/${installed}`, { timeout: 8000 });
ok(`masthead guests ${running}/${installed}`, true);
await page.waitForFunction(() =>
  document.querySelector('#heapval').textContent !== '—', null, { timeout: 8000 });
ok('heap gauge fed by $sys/heap', true);
await page.waitForFunction(() =>
  document.querySelector('#tick').classList.contains('lit'), null, { timeout: 3000 });
ok('tick dot lit by received ticks', true);
ok('link up', await page.$eval('#link', el => el.className === 'up'));
if (SHOTS) await page.screenshot({ path: SHOTS + '/1-node-summary-dark.png' });

// bus monitor
await page.click('.tab[data-tab="bus"]');
await page.waitForFunction(() =>
  document.querySelector('#busterm').textContent.includes('pinger/tick'),
  null, { timeout: 6000 });
// Wait, don't peek. A snapshot taken the instant the first row lands is a
// race — echoer republishes what pinger sends, so it is always a beat behind,
// and on a busy node that beat is longer than the snapshot. Assertions that
// only pass on a quiet node are assertions that lie about a busy one.
const busHas = t => page.waitForFunction(
  s => document.querySelector('#busterm').textContent.includes(s), t,
  { timeout: 20000 });
await busHas('pinger/tick');
ok('bus monitor streams pinger traffic', true);
await busHas('echoer/tock');
ok('bus monitor streams echoer traffic', true);
await busHas('$sys/clock/tick');
ok('bus monitor sees $sys traffic too', true);
// inject with origin: ui → orange line
await page.fill('#pubtopic', 'cmd/pinger/demo');
await page.fill('#pubmsg', '{"origin": "ui", "note": "human tap"}');
await page.click('#pubbtn');
await page.waitForFunction(() =>
  document.querySelector('#busterm .row.uiline'), null, { timeout: 10000 });
ok('origin: ui line marked in action orange', true);
if (SHOTS) await page.screenshot({ path: SHOTS + '/2-bus-monitor.png' });

// guest console
await page.click('.trow.g:has-text("(echoer)")');
await page.click('.tab[data-tab="console"]');
await page.waitForFunction(() =>
  document.querySelectorAll('#conterm .row').length >= 2, null, { timeout: 5000 });
ok('guest console streams history over WS',
  (await page.textContent('#conterm')).includes('running'));

// lifecycle: stop blinky, watch the tree flip, start it again
await page.click('.trow.g:has-text("(blinky)")');
await page.click('.tab[data-tab="overview"]');
await page.waitForSelector('[data-act="stop"]');
await page.click('[data-act="stop"]');
await page.waitForFunction(() => {
  const row = [...document.querySelectorAll('.trow.g')]
    .find(r => r.textContent.includes('(blinky)'));
  return row && row.querySelector('.st').textContent === '○';
}, null, { timeout: 6000 });
ok('stop blinky → tree glyph flips to ○ via $sys state', true);
await page.click('[data-act="start"]');
await page.waitForFunction(() => {
  const row = [...document.querySelectorAll('.trow.g')]
    .find(r => r.textContent.includes('(blinky)'));
  return row && row.querySelector('.st').textContent === '●';
}, null, { timeout: 6000 });
ok('start blinky → glyph back to ●', true);
if (SHOTS) await page.screenshot({ path: SHOTS + '/3-guest-overview.png' });

// claims
await page.click('.trow.grp');
await page.click('.tab[data-tab="claims"]');
await page.waitForFunction(() => {
  const el = document.querySelector('#claimsbody');
  return el && !el.textContent.includes('loading');
}, null, { timeout: 5000 });
ok('claims table shows pin 2 → blinky',
  (await page.textContent('#claimsbody')).includes('blinky'));

// ── M3: the declared panel on the dashboard wall ─────────────────────────
await page.click('.trow.grp');
await page.click('.tab[data-tab="dashboard"]');
await page.waitForSelector('[data-panel="thermo"]', { timeout: 6000 });
await page.waitForFunction(() =>
  /[\d]/.test(document.getElementById('w-thermo-0').textContent), null, { timeout: 8000 });
ok('dashboard renders thermo gauge with a live number', true);
await page.waitForFunction(() =>
  document.getElementById('w-thermo-0-bar').style.width !== '', null, { timeout: 4000 });
ok('gauge bar tracks the bound value', true);

// panel slider → set topic → guest console (origin: ui)
await page.evaluate(() => {
  const el = document.getElementById('w-thermo-2');
  el.value = 700;
  el.dispatchEvent(new Event('change'));
});
let sawSet = false;
for (let i = 0; i < 20 && !sawSet; i++) {
  await page.waitForTimeout(300);
  sawSet = await consoleHas('thermo', 'period -> 700 (origin: ui)');
}
ok('panel slider commands its guest (origin: ui)', sawSet);

// ── M3: config form — live apply + pending-restart amber ─────────────────
await page.click('.trow.g:has-text("(thermo)")');
await page.click('.tab[data-tab="config"]');
await page.waitForSelector('[data-key="period_ms"]', { timeout: 6000 });
await page.evaluate(() => {
  document.querySelector('[data-key="period_ms"]').value = 1500;   // live
  document.querySelector('[data-key="gauge_max_c"]').value = 80;   // pending restart
});
await page.click('#cfgsave');
await page.waitForFunction(() =>
  document.querySelector('#cfgbody') &&
  document.querySelector('#cfgbody').textContent.includes('pending restart'),
  null, { timeout: 6000 });
ok('config saved: live applied, non-live badged pending restart', true);
let sawCfg = false;
for (let i = 0; i < 10 && !sawCfg; i++) {
  await page.waitForTimeout(300);
  sawCfg = await consoleHas('thermo', 'config: period_ms -> 1500');
}
ok('live config write streamed to hal.config.watch', sawCfg);

// ── M3: panels outlive their guests — frozen, not vanished ───────────────
await fetch(`${BASE}/api/guests/thermo/stop`, { method: 'POST', headers: HDRS });
await page.click('.trow.grp');
await page.click('.tab[data-tab="dashboard"]');
await page.waitForFunction(() => {
  const el = document.querySelector('[data-panel="thermo"]');
  return el && el.className.includes('stale-p') && el.textContent.includes('frozen');
}, null, { timeout: 8000 });
ok('stopped guest → panel grays and freezes last values', true);
await fetch(`${BASE}/api/guests/thermo/start`, { method: 'POST', headers: HDRS });

// ── the tree folds (Proxmox shape: Datacenter → node → guests) ───────────
await page.click('.trow[data-tw="node"] .tw');
await page.waitForFunction(() =>
  document.querySelectorAll('.trow.g').length === 0, null, { timeout: 3000 });
ok('collapsing the node hides its guests', true);
await page.click('.trow[data-tw="node"] .tw');
await page.waitForFunction(n =>
  document.querySelectorAll('.trow.g').length === n, installed, { timeout: 3000 });
ok('expanding brings them back', true);
const firstRow = await page.evaluate(() => {
  const row = document.querySelector('.trow.g');
  return row ? row.textContent : '(no guest row)';
});
ok(`guests carry a number — ${firstRow}`, /\d{3}/.test(firstRow));

// ── context menus (Proxmox is a right-click program) ─────────────────────
await page.click('.trow.grp');
await page.click('.tab[data-tab="summary"]');
const guestRow = '.trow.g:has-text("(blinky)")';
await page.click(guestRow, { button: 'right' });
await page.waitForSelector('#menu', { timeout: 4000 });
const menuText = await page.textContent('#menu');
ok('right-click a guest → menu with its number',
  /\d{3}.*blinky/.test(menuText) && menuText.includes('Restart'));
ok('menu is state-aware (blinky runs, so Start is disabled)',
  await page.evaluate(() => {
    const b = [...document.querySelectorAll('#menu button')].find(x => x.textContent.includes('Start'));
    return b && b.disabled;
  }));
ok('a running guest cannot be Removed from the menu',
  await page.evaluate(() => {
    const b = [...document.querySelectorAll('#menu button')].find(x => x.textContent.includes('Remove'));
    return b && b.disabled;
  }));
await page.keyboard.press('Escape');
ok('Escape closes the menu', (await page.$('#menu')) === null);

// destructive items arm rather than fire — on a guest that could actually go
await fetch(`${BASE}/api/guests/blinky/stop`, { method: 'POST', headers: HDRS });
await page.waitForFunction(() => {
  const row = [...document.querySelectorAll('.trow.g')].find(r => r.textContent.includes('(blinky)'));
  return row && row.querySelector('.st').textContent === '○';
}, null, { timeout: 6000 });
await page.click(guestRow, { button: 'right' });
await page.waitForSelector('#menu');
await page.evaluate(() =>
  [...document.querySelectorAll('#menu button')].find(x => x.textContent.includes('Remove')).click());
ok('Remove arms instead of removing',
  (await page.textContent('#menu')).includes('for good?'));
await page.keyboard.press('Escape');
ok('and the guest is still there', !!S_blinkyStillThere(await (await fetch(
  `${BASE}/api/guests`, { headers: HDRS })).json()));
await fetch(`${BASE}/api/guests/blinky/start`, { method: 'POST', headers: HDRS });

await page.click('.trow.grp', { button: 'right' });
await page.waitForSelector('#menu', { timeout: 4000 });
const nodeMenuText = await page.textContent('#menu');
ok('right-click the node → node actions',
  nodeMenuText.includes('Bus monitor') && nodeMenuText.includes('Maintenance mode'));
await page.keyboard.press('Escape');

// a menu action actually acts
await page.click(guestRow, { button: 'right' });
await page.waitForSelector('#menu');
await page.evaluate(() =>
  [...document.querySelectorAll('#menu button')].find(x => x.textContent.includes('Console')).click());
await page.waitForSelector('#conterm', { timeout: 5000 });
ok('menu → Console opens the console tab', true);

// ── the console runs both ways ───────────────────────────────────────────
await fetch(`${BASE}/api/guests`, { method: 'POST', headers: HDRS, body: JSON.stringify({
  manifest: JSON.parse(ex('parrot/manifest.json')),
  files: { 'main.py': ex('parrot/main.py') } }) }).catch(() => {});
await fetch(`${BASE}/api/guests/parrot/start`, { method: 'POST', headers: HDRS }).catch(() => {});
await page.waitForTimeout(1200);
await refreshTree(page);
await page.click('.trow.g:has-text("(parrot)")');
await page.click('.tab[data-tab="console"]');
await page.waitForSelector('#conin', { timeout: 5000 });
ok('a guest that listens gets a live input line',
  await page.evaluate(() => !document.getElementById('conin').disabled));
await page.fill('#conin', 'echo typed from the browser');
await page.keyboard.press('Enter');
await page.waitForFunction(() =>
  document.querySelector('#conterm').textContent.includes('typed from the browser'),
  null, { timeout: 8000 });
ok('typing into the console reaches the guest, and it answers', true);

// a guest that reads nothing says so, rather than swallowing input
await page.click('.trow.g:has-text("(blinky)")');
await page.click('.tab[data-tab="console"]');
await page.waitForSelector('#conin', { timeout: 5000 });
ok('a deaf guest disables its input and says why',
  await page.evaluate(() => {
    const el = document.getElementById('conin');
    return el.disabled && /reads no input/.test(el.placeholder);
  }));

// ── the node shell: a real terminal, fetched from the node's own flash ───
await page.click('.trow.grp');
const tShell = Date.now();
await page.click('.tab[data-tab="shell"]');
await page.waitForSelector('.screen', { timeout: 20000 });
ok(`@gcu/term mounts from the node's flash (${Date.now() - tShell} ms cold)`, true);
ok('and the rows have height — a terminal you cannot see is not a terminal',
  await page.evaluate(() =>
    document.querySelector('.screen').getBoundingClientRect().height > 100));

const shellSays = t => page.waitForFunction(
  s => document.querySelector('.screen').textContent.includes(s), t, { timeout: 20000 });
// Wait for the prompt before typing. A shell that is not reading loses what you
// type at it — which is true of every shell, and was true of this harness: it
// fired the next command while the last was still printing, then blamed the
// shell for the keystrokes it had thrown on the floor.
const LF = String.fromCharCode(10);
const atPrompt = () => page.waitForFunction(lf => {
  const rows = document.querySelector('.screen').textContent
    .split(lf).map(r => r.trimEnd()).filter(Boolean);
  const last = rows[rows.length - 1];
  // ed has its own prompt. A waiter that only knows the shell's will hang
  // forever inside the editor, which is a fine way to learn what ed is.
  return rows.length > 0 && (last.endsWith('▸') || last.endsWith('*') || last === '*');
}, LF, { timeout: 20000 });
const shellRun = async cmd => {
  await atPrompt();
  await page.keyboard.type(cmd);
  await page.keyboard.press('Enter');
};
await page.click('.screen');
await shellRun('guests');
await shellSays('parrot');
ok('`guests` lists them, in the terminal', true);

// ── geas: the node's flash IS the filesystem ────────────────────────────
await shellRun('ls /');
await shellSays('guests');
ok('`ls /` lists the ESP32 flash', true);

await shellRun('cat /guests/blinky/main.py | wc -l');
await shellSays('5');
ok('a pipe — cat | wc -l — over the flash, on an ESP32', true);

await shellRun('for g in /guests/*; do echo "seen $g"; done');
await shellSays('seen /guests/blinky');
ok('a for-loop with a glob over the flash', true);

// and the two guards, from the shell that would most like to break them
await shellRun('cat /settings.json');
await shellSays('not readable here');
ok('the shell cannot read the wifi psk', true);

await shellRun('rm /main.py');
await shellSays('managed by OTA');
ok('the shell cannot brick the node — that door has a lock on the inside', true);

await page.keyboard.type('temp');
await page.keyboard.press('Enter');
await page.waitForFunction(() =>
  /\d+ °C/.test(document.querySelector('.screen').textContent), null, { timeout: 12000 });
ok('`temp` reads the MCU sensor, in Celsius', true);

await shellRun('nonsense');
// geas is POSIX: an unrecognised command is "not found" and exits 127. That is
// the shell's answer, not ours, and it is the right one.
await shellSays('command not found');
ok('an unknown command fails the way a shell fails', true);

// the terminal is themed by Switchboard, not by itself
ok('the terminal takes its palette from --au-*',
  await page.evaluate(() => {
    const cs = getComputedStyle(document.querySelector('.screen'));
    const bg = cs.getPropertyValue('--gcu-term-bg').trim();
    const surf = getComputedStyle(document.documentElement)
      .getPropertyValue('--au-surface-deep').trim();
    return bg.includes('au-surface-deep') || bg === surf || bg.startsWith('var(');
  }));

// ── ed, and the Files tab: editing a guest ON the node ───────────────────
// ed comes free inside geas, and it reads the flash through the same VFS.
await page.click('.trow.grp');
await page.click('.tab[data-tab="shell"]');
await page.waitForSelector('.screen');
await shellRun('ed /guests/blinky/main.py');
// ed prints a byte count and nothing else — that is ed. ,p prints the buffer.
await shellRun(',p');
await shellSays('hal.sleep_ms');
ok('ed reads a guest off the flash — it was in geas all along', true);
await shellRun('q');

// the Files tab: the same loop, for people who do not speak ed
await page.click('.trow.g:has-text("(blinky)")');
await page.click('.tab[data-tab="files"]');
await page.waitForSelector('#editor', { timeout: 10000 });
await page.waitForFunction(() =>
  document.getElementById('editor').value.includes('hal.pin'), null, { timeout: 10000 });
ok('the Files tab opens a guest source from the node', true);

const before = await page.inputValue('#editor');
const mark = '# edited on the node, from a browser';
await page.fill('#editor', before + LF + mark + LF);
await page.click('#fsaverun');
await page.waitForTimeout(3000);
const saved = await (await fetch(`${BASE}/api/fs/guests/blinky/main.py`, { headers: HDRS })).text();
ok('save & restart writes it to flash and restarts the guest',
  saved.includes(mark));
// put it back the way we found it
await fetch(`${BASE}/api/fs/guests/blinky/main.py`, {
  method: 'PUT', headers: HDRS, body: before });
await fetch(`${BASE}/api/guests/blinky/restart`, { method: 'POST', headers: HDRS })
  .catch(() => {});

// ── the tree reads as a tree ─────────────────────────────────────────────
ok('each level indents deeper than its parent',
  await page.evaluate(() => {
    const px = s => parseFloat(getComputedStyle(document.querySelector(s)).paddingLeft);
    return px('.trow.root') < px('.trow.grp') && px('.trow.grp') < px('.trow.g');
  }));

// ── rename the cluster ───────────────────────────────────────────────────
await page.click('.trow.root', { button: 'right' });
await page.waitForSelector('#menu');
await page.evaluate(() =>
  [...document.querySelectorAll('#menu button')].find(x => x.textContent.includes('Rename')).click());
await page.waitForSelector('#clustername', { timeout: 3000 });
await page.fill('#clustername', 'Gentropic');
await page.keyboard.press('Enter');
await page.waitForFunction(() =>
  document.querySelector('.trow.root') &&
  document.querySelector('.trow.root').textContent.includes('Gentropic'), null, { timeout: 6000 });
ok('the cluster is renameable, and it sticks', true);
const persisted = await (await fetch(`${BASE}/api/node`, { headers: HDRS })).json();
ok('and the node persisted it', persisted.cluster === 'Gentropic');

// ── the phone ────────────────────────────────────────────────────────────
await page.setViewportSize({ width: 390, height: 844 });   // a phone, not a guess
await page.waitForTimeout(400);
ok('phone: the tree hides and a drawer button appears',
  await page.evaluate(() => {
    const t = document.querySelector('.tree').getBoundingClientRect();
    const b = document.getElementById('drawerbtn');
    return t.right <= 1 && getComputedStyle(b).display !== 'none';
  }));
await page.click('#drawerbtn');
await page.waitForTimeout(400);
ok('phone: the drawer opens',
  await page.evaluate(() => document.querySelector('.tree').getBoundingClientRect().left >= 0));
// an open drawer must not cover the button that closes it
ok('phone: ☰ stays reachable with the drawer open',
  await page.evaluate(() => {
    const b = document.getElementById('drawerbtn').getBoundingClientRect();
    const at = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
    return at && at.id === 'drawerbtn';
  }));
await page.click('#scrim', { position: { x: 340, y: 500 } });
await page.waitForTimeout(400);
ok('phone: tapping the scrim dismisses the drawer',
  await page.evaluate(() => document.querySelector('.tree').getBoundingClientRect().right <= 1));
await page.click('#drawerbtn');
await page.waitForTimeout(400);
await page.click('.trow.g');
await page.waitForTimeout(400);
ok('phone: selecting closes the drawer',
  await page.evaluate(() => document.querySelector('.tree').getBoundingClientRect().right <= 1));
ok('phone: the page never scrolls sideways',
  await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
await page.setViewportSize({ width: 1400, height: 900 });

// theme toggle (headless prefers light, real desktops vary — test the flip)
const themeBefore = await page.evaluate(() => document.documentElement.dataset.theme);
await page.click('#themebtn');
const themeAfter = await page.evaluate(() => document.documentElement.dataset.theme);
ok(`theme flips (${themeBefore} → ${themeAfter})`,
  themeAfter !== themeBefore && ['dark', 'light'].includes(themeAfter));
if (SHOTS) await page.screenshot({ path: SHOTS + '/4-other-theme.png' });
await page.click('#themebtn');

// staleness: kill the node, the instrument must grey out — no fake data
// The node resets while answering, so the socket dies mid-reply. A node that
// hangs up while telling you it is rebooting is not a failure — it is the point.
await fetch(`${BASE}/api/node/reboot`, { method: 'POST', headers: HDRS })
  .catch(() => {});
await page.waitForFunction(() =>
  document.getElementById('app').classList.contains('stale'), null, { timeout: 10000 });
ok('node dies → stale mode within budget (grey + banner)', true);
ok('link shows down', await page.$eval('#link', el => el.className === 'down'));
if (SHOTS) await page.screenshot({ path: SHOTS + '/5-stale-mode.png' });

await browser.close();
console.log('\n' + (process.exitCode ? 'UI VERIFY: FAILURES' : 'UI VERIFY: ALL PASS'));
