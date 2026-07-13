// A ticket must do three things, and the third is the one that matters.
import { chromium } from 'playwright';

const URL_ = 'http://127.0.0.1:8000';
const TOKEN = 'dev-token';

const mint = async () => {
  const r = await fetch(URL_ + '/api/auth/ticket', {
    method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN },
  });
  return (await r.json()).ticket;
};

let bad = 0;
const ok = (c, m) => { console.log(`  ${c ? 'ok' : 'FAIL'}: ${m}`); if (!c) bad++; };

const browser = await chromium.launch();
const ctx = await browser.newContext();          // a fresh browser: no localStorage
const page = await ctx.newPage();

// 1. a ticket logs you in without ever typing a token
const t = await mint();
await page.goto(`${URL_}/#t=${t}`);
await page.waitForTimeout(2500);
const chip = await page.textContent('#hostchip').catch(() => '');
ok(chip.includes('jorm-sim'), `landed logged in as ${chip.trim()} — no login form`);
ok(!(await page.$('#connectbtn')), 'the login form never appeared');

// 2. the ticket is gone from the URL — a copied link is not a credential
const url = page.url();
ok(!url.includes('#t=') && !url.includes(t), `fragment stripped: ${url}`);

// 3. and the ticket is spent. This is the whole point of not using the token.
const again = await fetch(URL_ + '/api/auth/redeem', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ ticket: t }),
});
ok(again.status === 401, `replaying the ticket is refused (${again.status})`);

// 4. and a made-up one never worked
const forged = await fetch(URL_ + '/api/auth/redeem', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ ticket: 'f'.repeat(32) }),
});
ok(forged.status === 401, `a forged ticket is refused (${forged.status})`);

// 5. the token still guards everything else
const naked = await fetch(URL_ + '/api/guests');
ok(naked.status === 401, 'the api is still shut without a token');

await browser.close();
console.log(bad ? `\nTICKET: ${bad} FAILED` : '\nTICKET: ALL PASS');
// Set the code; do not call exit(). fetch() leaves an undici socket closing, and
// tearing the loop out from under it makes libuv assert and the process die with
// 127 — which a CI script would read as "the tests failed". Same trap as geas-cli.
process.exitCode = bad ? 1 : 0;
