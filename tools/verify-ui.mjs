// Drive the jorm UI end-to-end against the live sim node.
import { chromium } from 'playwright';

const BASE = 'http://localhost:8000';
const SHOTS = process.env.SHOTS;
const results = [];
const ok = (name, cond) => {
  results.push([cond ? 'ok' : 'FAIL', name]);
  console.log((cond ? '  ok: ' : 'FAIL: ') + name);
  if (!cond) process.exitCode = 1;
};

// reset: all three demo guests running, whatever state the node was left in
for (const g of ['blinky', 'echoer', 'pinger']) {
  await fetch(`${BASE}/api/guests/${g}/start`, {
    method: 'POST', headers: { Authorization: 'Bearer dev-token' },
  }).catch(() => {});
}

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
await page.fill('#in-token', 'dev-token');
await page.click('#connectbtn');

// node summary renders live
await page.waitForSelector('.trow.g', { timeout: 8000 });
ok('login → tree renders guests', (await page.$$('.trow.g')).length === 3);
ok('hostchip = jorm-c510', (await page.textContent('#hostchip')) === 'jorm-c510');
await page.waitForFunction(() => document.querySelector('#gval').textContent === '3/3');
ok('masthead guests 3/3', true);
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
await page.click('.trow.g:has-text("echoer")');
await page.click('.tab[data-tab="console"]');
await page.waitForFunction(() =>
  document.querySelectorAll('#conterm .row').length >= 2, null, { timeout: 5000 });
ok('guest console streams history over WS',
  (await page.textContent('#conterm')).includes('running'));

// lifecycle: stop blinky, watch the tree flip, start it again
await page.click('.trow.g:has-text("blinky")');
await page.click('.tab[data-tab="overview"]');
await page.waitForSelector('[data-act="stop"]');
await page.click('[data-act="stop"]');
await page.waitForFunction(() => {
  const row = [...document.querySelectorAll('.trow.g')]
    .find(r => r.textContent.includes('blinky'));
  return row && row.querySelector('.st').textContent === '○';
}, null, { timeout: 6000 });
ok('stop blinky → tree glyph flips to ○ via $sys state', true);
await page.click('[data-act="start"]');
await page.waitForFunction(() => {
  const row = [...document.querySelectorAll('.trow.g')]
    .find(r => r.textContent.includes('blinky'));
  return row && row.querySelector('.st').textContent === '●';
}, null, { timeout: 6000 });
ok('start blinky → glyph back to ●', true);
if (SHOTS) await page.screenshot({ path: SHOTS + '/3-guest-overview.png' });

// claims
await page.click('.trow:not(.g)');
await page.click('.tab[data-tab="claims"]');
await page.waitForFunction(() => {
  const el = document.querySelector('#claimsbody');
  return el && !el.textContent.includes('loading');
}, null, { timeout: 5000 });
ok('claims table shows pin 2 → blinky',
  (await page.textContent('#claimsbody')).includes('blinky'));

// theme toggle (headless prefers light, real desktops vary — test the flip)
const themeBefore = await page.evaluate(() => document.documentElement.dataset.theme);
await page.click('#themebtn');
const themeAfter = await page.evaluate(() => document.documentElement.dataset.theme);
ok(`theme flips (${themeBefore} → ${themeAfter})`,
  themeAfter !== themeBefore && ['dark', 'light'].includes(themeAfter));
if (SHOTS) await page.screenshot({ path: SHOTS + '/4-other-theme.png' });
await page.click('#themebtn');

// staleness: kill the node, the instrument must grey out — no fake data
await page.evaluate(base => fetch(base + '/api/node/reboot', {
  method: 'POST', headers: { Authorization: 'Bearer dev-token' } }), BASE);
await page.waitForFunction(() =>
  document.getElementById('app').classList.contains('stale'), null, { timeout: 10000 });
ok('node dies → stale mode within budget (grey + banner)', true);
ok('link shows down', await page.$eval('#link', el => el.className === 'down'));
if (SHOTS) await page.screenshot({ path: SHOTS + '/5-stale-mode.png' });

await browser.close();
console.log('\n' + (process.exitCode ? 'UI VERIFY: FAILURES' : 'UI VERIFY: ALL PASS'));
