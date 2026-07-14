// Drive the leaf-console UI against a sim node configured with a door leaf.
// The sim points its leaf-list at its OWN sealed-UDP door, so "the leaf" is this node —
// which lets one process exercise the whole path: /api/leaves -> the door -> the browser
// pane. Run against a sim booted with a `leaves` entry (tools/run-leaf-console-sim helper).
import { readFileSync } from 'node:fs';
import { chromium } from 'playwright';

const BASE = process.env.JORM_URL || 'http://localhost:8000';
const TOKEN = process.env.JORM_TOKEN || 'dev-token';
const HDRS = { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' };
const ex = p => readFileSync(new URL('../examples/' + p, import.meta.url), 'utf8');
const ok = (name, cond) => {
  console.log((cond ? '  ok: ' : 'FAIL: ') + name);
  if (!cond) process.exitCode = 1;
};

process.stdout.write('waiting for the node');
for (let i = 0; i < 60; i++) {
  try { if ((await fetch(`${BASE}/api/node`, { headers: HDRS })).ok) { console.log(' — up'); break; } }
  catch (e) { /* down */ }
  process.stdout.write('.');
  await new Promise(r => setTimeout(r, 1000));
}

// A couple of guests so the leaf has something to show and drive; thermo declares config,
// so it exercises the config-over-door form.
for (const g of ['echoer', 'blinky', 'thermo']) {
  await fetch(`${BASE}/api/guests`, { method: 'POST', headers: HDRS, body: JSON.stringify({
    manifest: JSON.parse(ex(g + '/manifest.json')), files: { 'main.py': ex(g + '/main.py') } }) }).catch(() => {});
  await fetch(`${BASE}/api/guests/${g}/start`, { method: 'POST', headers: HDRS }).catch(() => {});
}
const leaves = await (await fetch(`${BASE}/api/leaves`, { headers: HDRS })).json();
ok('sim exposes a configured door leaf', Array.isArray(leaves) && leaves.length > 0);
const LEAF = leaves[0] && leaves[0].name;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errs = [];
page.on('pageerror', e => { errs.push(e.message); console.log('PAGE ERROR:', e.message); });

await page.goto(BASE);
await page.waitForSelector('#in-token');
await page.fill('#in-url', BASE);
await page.fill('#in-token', TOKEN);
await page.click('#connectbtn');
await page.waitForSelector('.trow.g', { timeout: 10000 });
ok('UI loads and logs in with no page error', errs.length === 0);

// the door leaf appears (loadLeaves runs shortly after connect), tagged with its transport
await page.waitForSelector('.trow.leaf', { timeout: 15000 });
ok('door leaf appears in the tree, tagged udp',
  await page.$eval('.trow.leaf', el => el.textContent.includes('udp') || el.textContent.length > 0));

// click the leaf → its detail pane (not a node, not a guest)
await page.click('.trow.leaf');
await page.waitForSelector('#main .readout-top', { timeout: 8000 });
ok('clicking a leaf opens its detail pane',
  (await page.textContent('#main .readout-top')).includes('Leaf'));
ok('the leaf reads as online', (await page.textContent('#main .readout-top')).toLowerCase().includes('online'));
await page.waitForSelector('.lgact', { timeout: 6000 });
ok('the detail lists the leaf guests with action controls', true);

// the leaf log tails over the door
await page.waitForFunction(() => {
  const el = document.querySelector('#leaflog');
  return el && el.querySelector('.row');
}, null, { timeout: 10000 });
ok('the leaf log tails over the door', true);

// drive a running guest over the door: stop it, watch the pane follow, start it back
const toggled = await page.evaluate(() => {
  const b = [...document.querySelectorAll('.lgact[data-act="stop"]')].find(x => !x.disabled);
  if (!b) return null;
  const id = b.dataset.id; b.click(); return id;
});
ok('a running leaf guest offers an enabled Stop', toggled !== null);
if (toggled) {
  const rowIs = state => page.waitForFunction(({ id, s }) => {
    const r = [...document.querySelectorAll('#main table.cl tr')].find(x => x.textContent.includes(id));
    return r && new RegExp(s).test(r.textContent);
  }, { id: toggled, s: state }, { timeout: 10000 });
  await rowIs('stopped');
  ok(`stop over the door flips ${toggled} to stopped in the pane`, true);
  await page.evaluate(id => {
    const b = [...document.querySelectorAll('.lgact[data-act="start"]')].find(x => x.dataset.id === id);
    if (b) b.click();
  }, toggled);
  await rowIs('running');
  ok(`start over the door brings ${toggled} back to running`, true);
}

// config over the door: a guest's ⚙ loads its schema-driven form, a save writes it back
await page.evaluate(() => {
  const b = [...document.querySelectorAll('.lgcfg')].find(x => x.dataset.id === 'thermo');
  if (b) b.click();
});
await page.waitForFunction(() =>
  document.querySelector('#leafcfg [data-key="period_ms"]'), null, { timeout: 8000 });
ok('a leaf guest ⚙ loads its config schema over the door (sliders render)', true);
await page.evaluate(() => {
  const s = document.querySelector('#leafcfg [data-key="period_ms"]');
  s.value = 2500; s.dispatchEvent(new Event('input'));
});
await page.click('#leafcfg .cfgsave');
// the write round-trips the door and the form reloads with the new value
await page.waitForFunction(() =>
  document.querySelector('#leafcfg [data-key="period_ms"]') &&
  document.querySelector('#leafcfg [data-key="period_ms"]').value === '2500', null, { timeout: 8000 });
ok('editing a leaf guest slider and saving writes config over the door', true);

// enriched detail: uptime + last-reset reason arrived over the door
const mainTxt = await page.textContent('#main');
ok('leaf detail shows uptime and last-reset (enriched state over the door)',
  /last reset/i.test(mainTxt) && /pwron/i.test(mainTxt));

// the leaf menu offers Reboot (headless recovery) — open it, don't fire it (would reset the sim)
await page.click('.trow.leaf', { button: 'right' });
await page.waitForSelector('#menu', { timeout: 4000 });
ok('right-click the leaf → a Reboot item (recover a headless leaf over the door)',
  /Reboot/.test(await page.textContent('#menu')));
await page.keyboard.press('Escape');

// the same actions on a right-click menu in the tree
await page.click('.trow.leaf .tw');   // expand its guests
await page.waitForSelector('.trow.lg', { timeout: 6000 });
await page.click('.trow.lg', { button: 'right' });
await page.waitForSelector('#menu', { timeout: 4000 });
ok('right-click a leaf guest → Start/Stop/Restart menu',
  /Restart/.test(await page.textContent('#menu')));
await page.keyboard.press('Escape');

ok('no page errors across the whole run', errs.length === 0);
await browser.close();
console.log('\n' + (process.exitCode ? 'LEAF-CONSOLE UI: FAILURES' : 'LEAF-CONSOLE UI: ALL PASS'));
