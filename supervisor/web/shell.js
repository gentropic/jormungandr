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
import { A, nodeVfs, jormBuiltins } from './jorm-pack.js';

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
