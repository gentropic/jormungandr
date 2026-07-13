// Drive the jorm UI end-to-end against the live sim node.
import { readFileSync } from 'node:fs';
import { chromium } from 'playwright';

// JORM_URL lets us route around WSL2's localhost relay, which drops out
// intermittently; the sim always answers on the distro's own IP.
const BASE = process.env.JORM_URL || 'http://localhost:8000';
const SHOTS = process.env.SHOTS;
const results = [];
const S_blinkyStillThere = gs => gs.some(g => g.id === 'blinky');
const ok = (name, cond) => {
  results.push([cond ? 'ok' : 'FAIL', name]);
  console.log((cond ? '  ok: ' : 'FAIL: ') + name);
  if (!cond) process.exitCode = 1;
};

// reset: demo guests installed + running, whatever state the node was left in
const TOKEN = process.env.JORM_TOKEN || 'dev-token';
const HDRS = { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' };
const ex = p => readFileSync(new URL('../examples/' + p, import.meta.url), 'utf8');
// hermetic: wipe and reinstall, so config state from a prior run can't make
// "set unit_f=true" a no-op that never goes pending
const DEMO = ['blinky', 'echoer', 'pinger', 'thermo'];
for (const g of DEMO) {
  await fetch(`${BASE}/api/guests/${g}/stop`, { method: 'POST', headers: HDRS }).catch(() => {});
  await fetch(`${BASE}/api/guests/${g}`, { method: 'DELETE', headers: HDRS }).catch(() => {});
  await fetch(`${BASE}/api/guests`, { method: 'POST', headers: HDRS, body: JSON.stringify({
    manifest: JSON.parse(ex(g + '/manifest.json')),
    files: { 'main.py': ex(g + '/main.py') } }) }).catch(() => {});
}
for (const g of DEMO) {
  await fetch(`${BASE}/api/guests/${g}/start`, {
    method: 'POST', headers: HDRS }).catch(() => {});
}
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
const busText = await page.textContent('#busterm');
ok('bus monitor streams pinger traffic', busText.includes('pinger/tick'));
ok('bus monitor streams echoer traffic', busText.includes('echoer/tock'));
await page.waitForFunction(() =>
  document.querySelector('#busterm').textContent.includes('$sys/clock/tick'),
  null, { timeout: 3000 });
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
await fetch(`${BASE}/api/node/reboot`, { method: 'POST', headers: HDRS });
await page.waitForFunction(() =>
  document.getElementById('app').classList.contains('stale'), null, { timeout: 10000 });
ok('node dies → stale mode within budget (grey + banner)', true);
ok('link shows down', await page.$eval('#link', el => el.className === 'down'));
if (SHOTS) await page.screenshot({ path: SHOTS + '/5-stale-mode.png' });

await browser.close();
console.log('\n' + (process.exitCode ? 'UI VERIFY: FAILURES' : 'UI VERIFY: ALL PASS'));
