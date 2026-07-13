// `jorm shell` — the same shell, in your terminal.
//
// Not a second shell. geas is the shell; this is a second front-end for it. The
// VFS and the jorm builtins come from supervisor/web/jorm-pack.js, the same file
// the browser loads, so a verb learned here works there and a bug fixed there is
// fixed here. Writing a REPL in Python instead would have been the third
// implementation of a shell we already had — and the second one to drift.
//
//   JORM_URL=... JORM_TOKEN=... node tools/geas-cli.mjs
//   JORM_URL=... JORM_TOKEN=... node tools/geas-cli.mjs -c 'ls /guests'
import { createInterface } from 'node:readline/promises';
import process, { stdin, stdout, stderr, argv, env, exit } from 'node:process';

import { createShell } from '../supervisor/web/geas.js';
import { A, nodeVfs, jormBuiltins } from '../supervisor/web/jorm-pack.js';

const URL_ = (env.JORM_URL || 'http://jorm-c510.local').replace(/\/+$/, '');
const TOKEN = env.JORM_TOKEN || '';
if (!TOKEN) {
  stderr.write('jorm: no token (set JORM_TOKEN)\n');
  exit(1);
}

// the same api() shape the browser's shell is handed, over node's fetch
async function api(method, path, body, raw) {
  const opts = { method, headers: {
    Authorization: 'Bearer ' + TOKEN,
    // keep-alive sockets keep node alive after the work is done; a one-shot
    // command should end when the command ends
    Connection: 'close',
  } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = raw ? 'application/octet-stream' : 'application/json';
    opts.body = raw ? body : JSON.stringify(body);
  }
  const r = await fetch(URL_ + path, opts);
  const text = await r.text();
  if (!r.ok) {
    let msg = text;
    try { msg = JSON.parse(text).error; } catch (e) { /* not json */ }
    throw new Error(msg);
  }
  if (raw && method === 'GET') {
    try { return JSON.parse(text); } catch (e) { return text; }   // dirs answer JSON
  }
  try { return JSON.parse(text); } catch (e) { return text; }
}

// `sub` needs a live bus. Node has had a global WebSocket since 21, so the
// terminal gets the same streaming the browser does — and stops on any key.
const state = { node: null, temp: null };
const ctx = {
  api,
  state,
  refresh: () => {},
  interrupt: null,
  watch(filter, onMsg) {
    const ws = new WebSocket(
      URL_.replace(/^http/, 'ws') + '/api/bus?token=' + encodeURIComponent(TOKEN));
    ws.addEventListener('open', () =>
      ws.send(JSON.stringify({ op: 'sub', filters: [filter] })));
    ws.addEventListener('message', ev => {
      let f;
      try { f = JSON.parse(ev.data); } catch (e) { return; }
      if (f.topic !== undefined) onMsg(f.topic, f.msg);
    });
    return () => { try { ws.close(); } catch (e) { /* already gone */ } };
  },
};

// Colour is for a person, not for a pipe. `jorm shell -c guests | grep running`
// should get text, not escape codes — a CLI that paints its output into someone
// else's grep is a CLI that lies about what it produced.
const ESC = String.fromCharCode(27);
const plain = s => String(s).split(ESC).map((part, i) =>
  (i === 0 ? part : part.slice(part.indexOf('m') + 1))).join('');
const paint = stdout.isTTY ? (s => s) : plain;

const shell = createShell({
  vfs: nodeVfs(api),
  cwd: '/',
  env: { HOME: '/' },
  stdout: s => stdout.write(paint(s)),
  stderr: s => stderr.write(paint(A.red + s + A.reset)),
  builtins: jormBuiltins(api, ctx),
});

try {
  state.node = await api('GET', '/api/node');
  const t = await api('GET', '/api/bus/retained');
  if (t['$sys/temp']) state.temp = t['$sys/temp'].c;
} catch (e) {
  stderr.write(`jorm: cannot reach ${URL_} (${e.message})\n`);
  exit(1);
}

// one-shot: `jorm shell -c 'ls /guests | wc -l'` — which is what makes it
// scriptable, and the reason it is worth being a real shell rather than a menu
const dashC = argv.indexOf('-c');
if (dashC !== -1) {
  const src = argv.slice(dashC + 1).join(' ');
  const status = await shell.exec(src);
  // exec() returns {exitCode}, and records it on the shell — it does not return a
  // bare number. Reading it wrong meant `$?` inside geas was right all along while
  // we reported the wrong thing to the caller, which is the worst way to be wrong:
  // a script checking our exit code would have believed us.
  const code = (status && typeof status.exitCode === 'number') ? status.exitCode
    : (typeof status === 'number' ? status : shell.lastStatus);
  process.exitCode = typeof code === 'number' ? code : 0;
  // And do NOT call exit(). Any command that touched the network leaves an undici
  // socket still closing, and exit() tears the loop out from under it — libuv
  // asserts and the process dies with 127, which we would then hand to a script as
  // if the command had failed. Set the code and let node leave when it is finished.
  // (`Connection: close` in api() is what makes "when it is finished" be now.)
} else {

const host = state.node.hostname;
stdout.write(`${A.bold}${host}${A.reset} ${A.dim}— geas. 'jhelp' for the jorm verbs, ` +
  `'help' for the shell, ctrl-d to leave.${A.reset}\n`);
stdout.write(`${A.dim}the node's flash is the filesystem — try: ls -l /guests${A.reset}\n\n`);

const rl = createInterface({ input: stdin, output: stdout, terminal: true, historySize: 200 });
// stop a `sub` stream on any key, exactly as the browser's shell does
stdin.on('keypress', () => { if (ctx.interrupt) ctx.interrupt(); });

for (;;) {
  let line;
  try {
    line = await rl.question(`${A.orange}${host}${A.reset} ${A.dim}▸${A.reset} `);
  } catch (e) {
    break;                       // ctrl-c / ctrl-d
  }
  const src = line.trim();
  if (!src) continue;
  if (src === 'exit' || src === 'quit') break;
  try {
    await shell.exec(src);
  } catch (e) {
    stderr.write(`${A.red}✗ ${e.message}${A.reset}\n`);
  }
}
rl.close();
stdout.write(A.dim + 'bye.' + A.reset + '\n');

}
