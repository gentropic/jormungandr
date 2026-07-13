// The node shell, on a real terminal.
//
// Lazily loaded: the Shell tab fetches this (and term.js) on first open, so a
// phone that only wants the dashboard never pays for a terminal it did not ask
// for. The main UI stays ~21 KB gzipped; this costs what it costs, once, cached.
//
// It is deliberately not a Unix. The verbs are jorm's, the "filesystem" is the
// node's API, and there is no process table because there is no process table —
// there are guests, and the supervisor owns them. The full POSIX shell (geas) is
// the next surface, and it binds to the same API through a VFS of three methods.
import { Terminal, DomRenderer, Input } from './term.js';

const C = {
  reset: '\x1b[0m',
  dim: '\x1b[90m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  orange: '\x1b[93m',
  bold: '\x1b[1m',
};

const HELP = [
  ['guests', 'list installed guests'],
  ['start|stop|restart <id>', 'lifecycle'],
  ['rm <id>', 'remove a stopped guest'],
  ['cat <id> [file]', "read a guest's source"],
  ['claims', 'who owns which pin'],
  ['log [n]', "the supervisor's own log"],
  ['retained', 'the retained bus table'],
  ['pub <topic> [json]', 'inject a message'],
  ['sub <filter>', 'watch the bus (any key stops)'],
  ['lib', 'the shared library store'],
  ['node', 'board, heap, uptime, clock'],
  ['temp', 'the MCU temperature, in Celsius'],
  ['reboot', 'reboot the node'],
  ['clear', 'clear the screen'],
];
const VERBS = HELP.map(h => h[0].split(/[ |]/)[0])
  .concat(['stop', 'restart', 'help']);

export function mountShell(host, ctx) {
  // term.css contracts for a .termhost wrapper around .screen + the focus
  // textarea, and it says plainly not to override the structural declarations.
  // I did (overflow, padding, a border straight on .screen) and the renderer
  // measured a zero-height cell, so the whole terminal collapsed to one line.
  // Style the box around it; leave the contract alone.
  const wrap = document.createElement('div');
  wrap.className = 'termhost';
  const screen = document.createElement('div');
  screen.className = 'screen';
  const hidden = document.createElement('textarea');
  hidden.setAttribute('autocapitalize', 'off');
  hidden.setAttribute('autocomplete', 'off');
  hidden.setAttribute('spellcheck', 'false');
  wrap.append(screen, hidden);
  host.replaceChildren(wrap);

  const cols = Math.max(40, Math.min(120, Math.floor(host.clientWidth / 8.4) || 80));
  const term = new Terminal(cols, 24);
  // cssVarTheme: the renderer re-reads --gcu-term-* every frame, and the UI maps
  // those onto Switchboard's --au-*. So the terminal is basalt in the dark and
  // equipment gray in the light, without knowing either word.
  const renderer = new DomRenderer(term, screen, { cssVarTheme: true });
  const input = new Input(term, screen, hidden, renderer);

  let raf = 0;
  let blink = performance.now();
  const tick = now => {
    if (now - blink > 530) { renderer.cursorOn = !renderer.cursorOn; blink = now; }
    renderer.render();
    raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);

  const w = s => term.write(s);
  const say = (s = '') => w(s + '\r\n');
  const err = s => say(`${C.red}✗ ${s}${C.reset}`);

  const host_ = () => (ctx.state.node && ctx.state.node.hostname) || 'jorm';
  const prompt = () => w(`${C.orange}${host_()}${C.reset} ${C.dim}▸${C.reset} `);

  // ── the line editor. Input hands us keystrokes the way a pty does; echoing
  //    them, and knowing what backspace means, is the shell's job — not the
  //    terminal's. This is the part a <input type=text> was quietly doing for us.
  let line = '';
  let cur = 0;
  const hist = ctx.state.shhist || (ctx.state.shhist = []);
  let hpos = hist.length;
  let busy = false;
  let interrupt = null;

  const redraw = () => {
    w('\r\x1b[2K');
    prompt();
    w(line);
    if (cur < line.length) w(`\x1b[${line.length - cur}D`);
  };

  const banner = () => {
    say(`${C.bold}${host_()}${C.reset} ${C.dim}— the jorm verbs. 'help' lists them.${C.reset}`);
    say(`${C.dim}not a Unix, on purpose: the node's shell is its API${C.reset}`);
    say();
  };

  term.onText(async data => {
    for (const ch of data) {
      if (busy) {
        if (interrupt) interrupt();   // any key stops a stream
        continue;
      }
      if (ch === '\r' || ch === '\n') {
        say();
        const cmd = line.trim();
        line = ''; cur = 0;
        if (cmd) {
          hist.push(cmd);
          hpos = hist.length;
          busy = true;
          try { await run(cmd); }
          catch (e) { err(e.message); }
          busy = false;
        }
        prompt();
      } else if (ch === '\x7f' || ch === '\b') {
        if (cur > 0) { line = line.slice(0, cur - 1) + line.slice(cur); cur--; redraw(); }
      } else if (ch === '\x03') {           // ctrl-c
        say('^C'); line = ''; cur = 0; prompt();
      } else if (ch === '\x0c') {           // ctrl-l
        w('\x1b[2J\x1b[H'); prompt(); w(line);
      } else if (ch === '\t') {
        const hit = VERBS.filter(v => v.startsWith(line));
        if (hit.length === 1) { line = hit[0] + ' '; cur = line.length; redraw(); }
        else if (hit.length > 1) { say(); say(C.dim + hit.join('  ') + C.reset); redraw(); }
      } else if (ch === '\x1b') {
        // an escape sequence arrives whole in this chunk; handled below
      } else if (ch >= ' ') {
        line = line.slice(0, cur) + ch + line.slice(cur); cur++;
        redraw();
      }
    }
    // arrows come as CSI sequences in the same chunk
    if (data.includes('\x1b[A') && !busy) {
      if (hpos > 0) { line = hist[--hpos] || ''; cur = line.length; redraw(); }
    } else if (data.includes('\x1b[B') && !busy) {
      hpos = Math.min(hpos + 1, hist.length);
      line = hist[hpos] || ''; cur = line.length; redraw();
    } else if (data.includes('\x1b[D') && !busy) {
      if (cur > 0) { cur--; w('\x1b[1D'); }
    } else if (data.includes('\x1b[C') && !busy) {
      if (cur < line.length) { cur++; w('\x1b[1C'); }
    }
  });

  // ── the verbs ───────────────────────────────────────────────────────────
  const api = ctx.api;

  async function run(input_) {
    const [verb, ...rest] = input_.split(/\s+/);
    const arg = rest[0];
    const need = () => { if (!arg) throw new Error('which guest?'); return arg; };

    switch (verb) {
      case 'help':
        for (const [cmd, what] of HELP)
          say(`  ${C.orange}${cmd.padEnd(26)}${C.reset}${C.dim}${what}${C.reset}`);
        return;
      case 'clear':
        w('\x1b[2J\x1b[H');
        return;

      case 'guests': {
        const gs = await api('GET', '/api/guests');
        if (!gs.length) return say(C.dim + 'no guests installed' + C.reset);
        for (const g of gs) {
          const col = g.state === 'running' ? C.green
            : g.state === 'crashed' ? C.red
            : g.state === 'unresponsive' ? C.yellow : C.dim;
          say(`  ${C.dim}${String(g.num).padEnd(5)}${C.reset}` +
            `${g.id.padEnd(12)} ${col}${g.state.padEnd(13)}${C.reset}` +
            `${C.dim}${g.status || ''}${g.suspected ? '  ⚠ suspected' : ''}${C.reset}`);
        }
        return;
      }
      case 'start': case 'stop': case 'restart': {
        const r = await api('POST', `/api/guests/${need()}/${verb}`);
        say(`${arg}: ${C.green}${r.state}${C.reset}`);
        return;
      }
      case 'rm':
        await api('DELETE', '/api/guests/' + need());
        say(`removed ${arg}`);
        ctx.refresh();
        return;
      case 'cat': {
        const file = rest[1] || 'main.py';
        const body = await api('GET', `/api/guests/${need()}/files/${file}`, undefined, true);
        for (const l of String(body).split('\n')) say(l);
        return;
      }
      case 'claims': {
        const t = await api('GET', '/api/claims');
        if (t.reserved_pins.length)
          say(C.dim + 'reserved: pin ' + t.reserved_pins.join(', ') + C.reset);
        if (!t.pins.length && !t.i2c.length)
          return say(C.dim + 'nothing passed through' + C.reset);
        for (const p of t.pins)
          say(`  pin ${String(p.pin).padEnd(4)} ${C.blue}${p.mode.padEnd(11)}${C.reset}${p.owners.join(' · ')}`);
        for (const e of t.i2c)
          say(`  i2c ${e.bus}/0x${e.addr.toString(16)}  ${e.owner}`);
        return;
      }
      case 'log': {
        const got = await api('GET', '/api/node/log?n=' + (arg || 30));
        for (const l of got.lines) {
          const col = l.level === 'error' ? C.red : l.level === 'sys' ? C.blue : '';
          const t = l.ts ? new Date(l.ts * 1000).toTimeString().slice(0, 8)
            : '+' + (l.up || 0).toFixed(0) + 's';
          say(`${C.dim}${t}${C.reset} ${col}${l.level.padEnd(5)}${C.reset} ${l.text}`);
        }
        return;
      }
      case 'retained': {
        const t = await api('GET', '/api/bus/retained');
        for (const topic of Object.keys(t).sort())
          say(`  ${C.blue}${topic.padEnd(28)}${C.reset}${JSON.stringify(t[topic])}`);
        return;
      }
      case 'pub': {
        if (!arg) throw new Error('pub <topic> [json]');
        let msg = rest.slice(1).join(' ');
        try { msg = JSON.parse(msg); } catch (e) { if (!msg) msg = null; }
        const r = await api('POST', '/api/bus/publish',
          { topic: arg, msg: (msg && typeof msg === 'object') ? { ...msg, origin: 'ui' } : msg });
        say(`published ${C.blue}${arg}${C.reset} → ${r.delivered} subscriber(s)`);
        return;
      }
      case 'sub': {
        const filter = arg || '#';
        say(C.dim + `watching ${filter} — any key stops` + C.reset);
        await new Promise(resolve => {
          const off = ctx.watch(filter, (topic, msg) => {
            const ui = msg && typeof msg === 'object' && msg.origin === 'ui';
            say(`${ui ? C.orange + '▸ ' : C.blue}${topic.padEnd(26)}${C.reset}` +
              JSON.stringify(msg));
          });
          interrupt = () => { off(); interrupt = null; say(C.dim + '— stopped' + C.reset); resolve(); };
        });
        return;
      }
      case 'lib': {
        const rows = await api('GET', '/api/lib');
        if (!rows.length) return say(C.dim + 'the library store is empty' + C.reset);
        for (const r of rows)
          say(`  ${r.name.padEnd(16)}${C.dim}${String(r.bytes).padStart(6)} B  ` +
            `imported by: ${r.imported_by.join(', ') || '—'}${C.reset}`);
        return;
      }
      case 'node': {
        const n = await api('GET', '/api/node');
        say(`${C.bold}${n.hostname}${C.reset} ${C.dim}· ${n.board}${C.reset}`);
        say(`  heap free ${C.green}${(n.heap_free / 1048576).toFixed(2)} MB${C.reset}` +
          `  ·  up ${(n.uptime_ms / 1000).toFixed(0)} s  ·  ${n.cluster}`);
        say(`  clock: ${n.clock.synced ? C.green + n.clock.source
          : C.yellow + 'UNSYNCED — timestamps are uptime'}${C.reset}`);
        return;
      }
      case 'temp':
        say(ctx.state.temp == null ? C.dim + 'no reading yet' + C.reset
          : `${C.green}${ctx.state.temp} °C${C.reset}`);
        return;
      case 'reboot':
        await api('POST', '/api/node/reboot');
        say(C.yellow + 'rebooting…' + C.reset);
        return;
      default:
        throw new Error(`unknown: ${verb} — try 'help'`);
    }
  }

  banner();
  prompt();
  hidden.focus();

  return () => {           // dispose: the Shell tab is leaving
    cancelAnimationFrame(raf);
    input.dispose();
    renderer.dispose();
    term.dispose();
  };
}
