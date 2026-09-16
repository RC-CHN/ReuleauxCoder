#!/usr/bin/env node
import React from 'react';
import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {parseArgs} from 'node:util';
import {renderTerminal} from './ui/render.js';
import {RpcPeer} from './protocol/peer.js';
import {RuntimeClient} from './protocol/client.js';
import {TuiController} from './state/controller.js';
import {InputHistory} from './state/history.js';
import {App} from './ui/App.js';
import {safe} from './ui/format.js';
import {configureTheme, DEFAULT_THEME, presets} from './ui/theme.js';
import {loadTheme} from './ui/theme-config.js';
import {RENDER_FPS} from './ui/motion.js';

async function main() {
  const {values, positionals} = parseArgs({allowPositionals: true, options: {
    help: {type: 'boolean', short: 'h'}, cwd: {type: 'string'}, config: {type: 'string', short: 'c'},
    model: {type: 'string', short: 'm'}, resume: {type: 'string', short: 'r'},
    python: {type: 'string'}, backend: {type: 'string'}, theme: {type: 'string'}, 'no-alt-screen': {type: 'boolean'},
  }});
  if (values.help) {
    process.stdout.write(`ReuleauxCoder React TUI\n\nUsage: rcoder-tui [options]\n       rcoder-tui --backend EXECUTABLE -- [backend arguments…]\n\n  --cwd PATH          Local working directory (default: current directory)\n  --config PATH       Python backend configuration\n  --model NAME        Override model\n  --resume ID         Resume a saved session\n  --python PATH       Python executable (default: repository .venv/python, then python3)\n  --backend PROGRAM   Launch a custom stdio backend, e.g. ssh\n  --theme NAME|PATH   ${Object.keys(presets).join(' / ')}, or a JSON theme file (default: ${DEFAULT_THEME})\n  --no-alt-screen     Render in the main terminal buffer\n\nOpen / or Ctrl+P for command menus. F1 opens keyboard help.\n`);
    return;
  }
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error('The TUI needs a terminal. Use rcoder --prompt for non-interactive output.');
  if (!values.backend && positionals.length) throw new Error('Backend arguments require --backend.');
  if (values.backend && (values.config || values.model || values.resume || values.python)) throw new Error('With --backend, pass backend options after --.');
  const cwd = resolve(values.cwd ?? process.cwd());
  configureTheme(await loadTheme(values.theme, cwd));
  const repositoryPython = fileURLToPath(new URL(process.platform === 'win32' ? '../../.venv/Scripts/python.exe' : '../../.venv/bin/python', import.meta.url));
  const executable = values.backend ?? values.python ?? (existsSync(repositoryPython) ? repositoryPython : process.platform === 'win32' ? 'python' : 'python3');
  const args = values.backend ? positionals : ['-m', 'reuleauxcoder', '--rpc-stdio', ...['config', 'model', 'resume'].flatMap(name => {
    const value = values[name as 'config' | 'model' | 'resume'];
    return value ? [`--${name}`, name === 'config' ? resolve(value) : value] : [];
  })];
  const history = new InputHistory(resolve(cwd, '.rcoder/tui-history.jsonl'));
  await history.load();
  const child = spawn(executable, args, {cwd, stdio: ['pipe', 'pipe', 'pipe']});
  const peer = new RpcPeer(child.stdout, child.stdin);
  const client = new RuntimeClient(peer);
  const controller = new TuiController(client, history);
  let stderr = '';
  const exited = new Promise<void>(resolveExit => {child.once('exit', () => resolveExit()); child.once('error', () => resolveExit());});
  child.once('error', error => peer.close(error));
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk: string) => {
    stderr += chunk;
    controller.session.notice(safe(chunk), 'backend');
  });
  let lastRenderSample = -Infinity;
  const app = renderTerminal(<App controller={controller} alternateScreen={!values['no-alt-screen']}/>, {
    alternateScreen: !values['no-alt-screen'], incrementalRendering: true,
    exitOnCtrlC: false, maxFps: RENDER_FPS, interactive: true,
    onRender: ({renderTime}) => {
      // Sample render timings without sending an RPC for every animation frame.
      const now = performance.now();
      if (controller.session.connected && !peer.closed && now - lastRenderSample >= 1000) {
        lastRenderSample = now;
        client.recordPerformance(renderTime);
      }
    },
  });
  const terminate = () => {void controller.finish();};
  process.once('SIGTERM', terminate);
  process.once('SIGHUP', terminate);
  try {
    await client.initialize();
    client.resize(controller.rows, controller.columns);
    controller.startRefresh();
    const saved = await app.waitUntilExit();
    if (saved) process.stdout.write(`Saved session: ${safe(String(saved))}\n`);
    if (controller.session.fatal) throw new Error(controller.session.fatal);
  } catch (error) {
    app.unmount();
    throw new Error((error as Error).message + (stderr ? '\n' + safe(stderr) : ''), {cause: error});
  } finally {
    process.off('SIGTERM', terminate); process.off('SIGHUP', terminate);
    app.unmount(); controller.dispose();
    try {await client.shutdown();}
    finally {
      const timer = setTimeout(() => {child.kill('SIGTERM');}, 2000);
      const killTimer = setTimeout(() => {child.kill('SIGKILL');}, 4000);
      await exited;
      clearTimeout(timer); clearTimeout(killTimer);
    }
  }
}

main().catch(error => {process.stderr.write(`rcoder-tui: ${safe(error.message)}\n`); process.exitCode = 1;});
