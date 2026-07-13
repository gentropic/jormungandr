// The node shell: geas on @gcu/term, with the node's flash as its filesystem.
//
// Lazily loaded — the Shell tab fetches this on first open, so a phone showing
// the dashboard never pays for a shell it will not use. The main UI stays ~21 KB
// gzipped; this is ~240 KB more, once, cached.
//
// Nothing here runs on the MCU. geas is JavaScript and runs in the browser; the
// node's side of it is nine HTTP methods (/api/fs) and the API it already had.
// That is the whole argument against a Unix on the chip, made concrete: the shell
// is a client, and the node stays a node.
import { Terminal, DomRenderer, Input } from './term.js';
import { createShell, createTermAdapter, makeLineEditor } from './geas.js';

const A = {
  reset: '\x1b[0m', dim: '\x1b[90m', red: '\x1b[31m', green: '\x1b[32m',
  yellow: '\x1b[33m', blue: '\x1b[34m', orange: '\x1b[93m', bold: '\x1b[1m',
};

// ── the VFS: geas's nine methods, over the node's flash ────────────────────
function nodeVfs(api) {
  const p = path => String(path || '/').replace(/^\/+/, '');
  // The root is the empty path, and '/api/fs/' with a trailing slash is a 404 —
  // so the root has to lose the slash the rest of the paths need to keep.
  const url = path => (p(path) ? '/api/fs/' + p(path) : '/api/fs');
  const dec = new TextDecoder();

  const get = async path => api('GET', url(path), undefined, 'raw');

  return {
    async readFile(path, opts) {
      const body = await get(path);
      if (typeof body !== 'string') throw new Error(`${path}: is a directory`);
      return (opts && opts.encoding === null) ? new TextEncoder().encode(body) : body;
    },
    async writeFile(path, data) {
      const body = typeof data === 'string' ? data : dec.decode(data);
      await api('PUT', url(path), body, 'raw');
    },
    async readdir(path) {
      const r = await api('GET', url(path));
      if (!r.dir) throw new Error(`${path}: not a directory`);
      return r.entries.map(e => e.name);
    },
    async stat(path) {
      // The root has no name, so it cannot be a path segment — and `ls /` stats
      // before it reads. Answer for it here rather than inventing an endpoint
      // whose only job is to say "yes, the flash is a directory".
      const r = p(path)
        ? await api('POST', `/api/fs/${p(path)}?op=stat`)
        : { dir: true, size: 0 };
      return {
        isFile: () => !r.dir, isDirectory: () => r.dir,
        size: r.size, mode: r.dir ? 0o40755 : 0o100644,
      };
    },
    async mkdir(path) { await api('POST', `/api/fs/${p(path)}?op=mkdir`); },
    async rmdir(path) { await api('DELETE', url(path)); },
    async unlink(path) { await api('DELETE', url(path)); },
    async rename(from, to) {
      await api('POST', `/api/fs/${p(from)}?op=rename`, { to: p(to) });
    },
    async glob(pattern) {
      // one directory deep is enough for a flash with four directories in it
      const star = pattern.lastIndexOf('/');
      const dir = star > 0 ? pattern.slice(0, star) : '';
      const pat = star > 0 ? pattern.slice(star + 1) : pattern;
      const rx = new RegExp('^' + pat.replace(/[.+^${}()|[\]\\]/g, '\\$&')
        .replace(/\*/g, '.*').replace(/\?/g, '.') + '$');
      let names;
      try { names = await this.readdir(dir || '/'); } catch (e) { return []; }
      return names.filter(n => rx.test(n)).map(n => (dir ? dir + '/' + n : n));
    },
  };
}

// ── the jorm builtins: the verbs, as commands geas can pipe ────────────────
function jormBuiltins(api, ctx) {
  const out = (c, s) => c.stdout(s + '\n');
  const need = (argv, what) => {
    if (!argv[1]) throw new Error(`which ${what}?`);
    return argv[1];
  };

  return {
    async guests(argv, c) {
      const gs = await api('GET', '/api/guests');
      if (!gs.length) return out(c, A.dim + 'no guests installed' + A.reset), 0;
      for (const g of gs) {
        const col = g.state === 'running' ? A.green : g.state === 'crashed' ? A.red
          : g.state === 'unresponsive' ? A.yellow : A.dim;
        out(c, `${A.dim}${String(g.num).padEnd(5)}${A.reset}${g.id.padEnd(12)} ` +
          `${col}${g.state.padEnd(13)}${A.reset}${A.dim}${g.status || ''}` +
          `${g.suspected ? '  ⚠ suspected' : ''}${A.reset}`);
      }
      return 0;
    },
    async start(argv, c) {
      const r = await api('POST', `/api/guests/${need(argv, 'guest')}/start`);
      out(c, `${argv[1]}: ${A.green}${r.state}${A.reset}`);
      ctx.refresh();
      return 0;
    },
    async stop(argv, c) {
      const r = await api('POST', `/api/guests/${need(argv, 'guest')}/stop`);
      out(c, `${argv[1]}: ${r.state}`);
      ctx.refresh();
      return 0;
    },
    async restart(argv, c) {
      const r = await api('POST', `/api/guests/${need(argv, 'guest')}/restart`);
      out(c, `${argv[1]}: ${A.green}${r.state}${A.reset}`);
      ctx.refresh();
      return 0;
    },
    async claims(argv, c) {
      const t = await api('GET', '/api/claims');
      if (t.reserved_pins.length)
        out(c, A.dim + 'reserved: pin ' + t.reserved_pins.join(', ') + A.reset);
      for (const p of t.pins)
        out(c, `pin ${String(p.pin).padEnd(4)} ${A.blue}${p.mode.padEnd(11)}${A.reset}${p.owners.join(' · ')}`);
      for (const e of t.i2c)
        out(c, `i2c ${e.bus}/0x${e.addr.toString(16)}  ${e.owner}`);
      if (!t.pins.length && !t.i2c.length) out(c, A.dim + 'nothing passed through' + A.reset);
      return 0;
    },
    async jlog(argv, c) {
      const got = await api('GET', '/api/node/log?n=' + (argv[1] || 30));
      for (const l of got.lines) {
        const col = l.level === 'error' ? A.red : l.level === 'sys' ? A.blue : '';
        const t = l.ts ? new Date(l.ts * 1000).toTimeString().slice(0, 8)
          : '+' + (l.up || 0).toFixed(0) + 's';
        out(c, `${A.dim}${t}${A.reset} ${col}${l.level.padEnd(5)}${A.reset} ${l.text}`);
      }
      return 0;
    },
    async pub(argv, c) {
      if (!argv[1]) throw new Error('pub <topic> [json]');
      let msg = argv.slice(2).join(' ');
      try { msg = JSON.parse(msg); } catch (e) { if (!msg) msg = null; }
      const r = await api('POST', '/api/bus/publish', {
        topic: argv[1],
        msg: (msg && typeof msg === 'object') ? { ...msg, origin: 'ui' } : msg,
      });
      out(c, `published ${A.blue}${argv[1]}${A.reset} → ${r.delivered} subscriber(s)`);
      return 0;
    },
    async sub(argv, c) {
      const filter = argv[1] || '#';
      out(c, A.dim + `watching ${filter} — any key stops` + A.reset);
      await new Promise(resolve => {
        const off = ctx.watch(filter, (topic, msg) => {
          const ui = msg && typeof msg === 'object' && msg.origin === 'ui';
          out(c, `${ui ? A.orange + '▸ ' : A.blue}${topic.padEnd(26)}${A.reset}` +
            JSON.stringify(msg));
        });
        ctx.interrupt = () => { off(); ctx.interrupt = null; resolve(); };
      });
      return 0;
    },
    async retained(argv, c) {
      const t = await api('GET', '/api/bus/retained');
      for (const topic of Object.keys(t).sort())
        out(c, `${A.blue}${topic.padEnd(28)}${A.reset}${JSON.stringify(t[topic])}`);
      return 0;
    },
    async jlib(argv, c) {
      const rows = await api('GET', '/api/lib');
      if (!rows.length) return out(c, A.dim + 'the library store is empty' + A.reset), 0;
      for (const r of rows)
        out(c, `${r.name.padEnd(16)}${A.dim}${String(r.bytes).padStart(6)} B  ` +
          `imported by: ${r.imported_by.join(', ') || '—'}${A.reset}`);
      return 0;
    },
    async node(argv, c) {
      const n = await api('GET', '/api/node');
      out(c, `${A.bold}${n.hostname}${A.reset} ${A.dim}· ${n.board}${A.reset}`);
      out(c, `  heap free ${A.green}${(n.heap_free / 1048576).toFixed(2)} MB${A.reset}` +
        `  ·  up ${(n.uptime_ms / 1000).toFixed(0)} s  ·  ${n.cluster}`);
      out(c, `  clock: ${n.clock.synced ? A.green + n.clock.source
        : A.yellow + 'UNSYNCED — timestamps are uptime'}${A.reset}`);
      return 0;
    },
    async temp(argv, c) {
      out(c, ctx.state.temp == null ? A.dim + 'no reading yet' + A.reset
        : `${A.green}${ctx.state.temp} °C${A.reset}`);
      return 0;
    },
    async reboot(argv, c) {
      await api('POST', '/api/node/reboot');
      out(c, A.yellow + 'rebooting…' + A.reset);
      return 0;
    },
    async jhelp(argv, c) {
      out(c, `${A.bold}geas${A.reset} — POSIX syntax, pipes, globs, ${A.dim}for/if/while${A.reset}.`);
      out(c, `${A.dim}The node's flash is the filesystem: ls /guests, cat /lib/*.py${A.reset}`);
      out(c, '');
      out(c, `${A.bold}jorm verbs${A.reset}`);
      for (const [v, d] of [
        ['guests', 'list installed guests'],
        ['start|stop|restart <id>', 'lifecycle'],
        ['claims', 'who owns which pin'],
        ['pub <topic> [json]', 'inject a message'],
        ['sub <filter>', 'watch the bus (any key stops)'],
        ['retained', 'the retained bus table'],
        ['jlog [n]', "the supervisor's own log"],
        ['jlib', 'the shared library store'],
        ['node · temp · reboot', 'the node itself'],
      ]) out(c, `  ${A.orange}${v.padEnd(26)}${A.reset}${A.dim}${d}${A.reset}`);
      out(c, '');
      out(c, `${A.dim}and the coreutils geas brings: ls cat grep sed cut sort wc find …${A.reset}`);
      return 0;
    },
  };
}

// ── mount ─────────────────────────────────────────────────────────────────
export function mountShell(host, ctx) {
  // term.css contracts for a .termhost wrapper around .screen + the focus
  // textarea, and says plainly not to override its structural declarations.
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

  const cols = Math.max(40, Math.min(140, Math.floor(host.clientWidth / 7.9) || 80));
  const term = new Terminal(cols, 26);
  const renderer = new DomRenderer(term, screen, { cssVarTheme: true });
  const input = new Input(term, screen, hidden, renderer);

  let raf = 0, blink = performance.now();
  const tick = now => {
    if (now - blink > 530) { renderer.cursorOn = !renderer.cursorOn; blink = now; }
    renderer.render();
    raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);

  // geas's own adapter for this exact terminal, and its own line editor —
  // history, backspace, echo. The one I hand-wrote was doing geas's job.
  const adapter = createTermAdapter({ terminal: term });
  const readLine = makeLineEditor(adapter);

  // A terminal is not a file. geas's builtins end lines with LF, as any Unix
  // program should; a VT needs CRLF, or every line starts where the last one
  // ended and the output walks diagonally off the screen. The translation
  // belongs here, at the boundary between a program's stdout and a piece of
  // glass — not in geas, which is right, and not in term, which is also right.
  const CR = String.fromCharCode(13), LF = String.fromCharCode(10);
  const write = s => adapter.write(
    String(s).split(CR + LF).join(LF).split(LF).join(CR + LF));

  const shell = createShell({
    vfs: nodeVfs(ctx.api),
    cwd: '/',
    env: { HOME: '/', PS1: '', NODE: (ctx.state.node && ctx.state.node.hostname) || 'jorm' },
    stdout: write,
    stderr: s => write(`${A.red}${s}${A.reset}`),
    readLine,
    builtins: jormBuiltins(ctx.api, ctx),
  });

  const hostname = () => (ctx.state.node && ctx.state.node.hostname) || 'jorm';
  const banner = () => {
    write(`${A.bold}${hostname()}${A.reset} ${A.dim}— geas on @gcu/term. ` +
      `'jhelp' for the jorm verbs, 'help' for the shell.${A.reset}\r\n`);
    write(`${A.dim}the node's flash is the filesystem — try: ls -l /guests${A.reset}\r\n\r\n`);
  };

  const hist = ctx.state.shhist || (ctx.state.shhist = []);
  let running = true;

  (async function repl() {
    banner();
    while (running) {
      const { line, eof } = await readLine({
        prompt: `${A.orange}${hostname()}${A.reset} ${A.dim}▸${A.reset} `,
        onHistory: hist,
      });
      if (eof) break;
      const src = (line || '').trim();
      if (!src) continue;
      hist.push(src);
      try {
        await shell.exec(src);
      } catch (e) {
        write(`${A.red}✗ ${e.message}${A.reset}\r\n`);
      }
    }
  })();

  return () => {
    running = false;
    cancelAnimationFrame(raf);
    input.dispose();
    renderer.dispose();
    term.dispose();
  };
}
