import React from 'react';
import {PassThrough, Writable} from 'node:stream';
import {setTimeout as delay} from 'node:timers/promises';
import {writeFile} from 'node:fs/promises';
import {parseArgs} from 'node:util';
import {cpus} from 'node:os';
import {App} from '../src/ui/App.js';
import {renderTerminal} from '../src/ui/render.js';
import {safe} from '../src/ui/format.js';
import {inputLayout} from '../src/ui/viewport.js';
import {editor, edit} from '../src/state/editor.js';
import {TuiController} from '../src/state/controller.js';
import {RuntimeClient} from '../src/protocol/client.js';
import {RpcPeer} from '../src/protocol/peer.js';
import {RENDER_FPS} from '../src/ui/motion.js';

const {values} = parseArgs({options: {json: {type: 'string'}, label: {type: 'string', default: 'working-tree'}}});
const round = (n: number) => Math.round(n * 100) / 100;
const percentile = (samples: number[], p: number) => round([...samples].sort((a, b) => a - b)[Math.min(samples.length - 1, Math.floor(samples.length * p))] ?? 0);
const results: Record<string, unknown>[] = [];
const marker = (index: number) => `NAV_${String(index).padStart(5, '0')}`;

// Each sample waits for newly exposed text, rather than counting an unrelated
// animation or cursor-only write as evidence that the input reached the screen.
for (const scene of ['wheel', 'arrows', 'typing', 'wheel-busy', 'typing-busy'] as const) {
  const inputKind = scene.replace('-busy', '');
  const c = new TuiController(new RuntimeClient(new RpcPeer(new PassThrough(), new PassThrough())));
  c.resize(40, 160); c.session.connected = true;
  if (scene.endsWith('-busy')) c.session.state = {...c.session.state, running: true};
  for (let i = 0; i < 10000; i++) c.session.add('user', 'You', `Message ${i}\nHistory body\n`);
  if (inputKind !== 'typing') {c.document('Navigation benchmark', Array.from({length: 5000}, (_, i) => `${marker(i)}: content 中文`).join('\n')); if (c.screen?.kind === 'document') c.screen.offset = 200;}
  else c.composer = editor('INPUT_');
  let measuring = false, pending: {expected: string; started: number; resolve(): void} | undefined;
  const latency: number[] = [], renders: number[] = [];
  let bytes = 0;
  const stdout = Object.assign(new Writable({write(chunk, _encoding, done) {
    if (measuring) {
      bytes += chunk.length;
      if (pending && safe(chunk.toString()).includes(pending.expected)) {
        const sample = pending; pending = undefined;
        latency.push(performance.now() - sample.started); sample.resolve();
      }
    }
    done();
  }}), {columns: 160, rows: 40, isTTY: true});
  const stdin = Object.assign(new PassThrough(), {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const app = renderTerminal(<App controller={c}/>, {stdout, stdin, stderr: stdout, interactive: true, exitOnCtrlC: false, patchConsole: false, incrementalRendering: true, maxFps: RENDER_FPS,
    onRender: ({renderTime}) => {if (measuring) renders.push(renderTime);},
  });
  let renderError: unknown;
  void app.waitUntilExit().catch(error => {renderError = error;});
  try {
    await delay(3500);
    if (renderError) throw renderError;
    const usage = process.cpuUsage(), started = performance.now();
    measuring = true;
    for (let i = 0; i < 40; i++) {
      const direction = i % 2 ? -1 : 1;
      let input: string, expected: string;
      if (inputKind === 'typing') {input = 'x'; expected = 'INPUT_' + 'x'.repeat(i + 1);}
      else {
        if (c.screen?.kind !== 'document') throw new Error('Missing document');
        const target = c.screen.offset + direction * (inputKind === 'wheel' ? 3 : 1);
        expected = marker(direction > 0 ? target + c.viewportRows - 1 : target);
        input = inputKind === 'wheel' ? `\x1b[<${direction > 0 ? 65 : 64};20;10M` : direction > 0 ? '\x1b[B' : '\x1b[A';
      }
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => {pending = undefined; reject(new Error(`${scene}: no frame showing ${expected}`));}, 3000);
        pending = {expected, started: performance.now(), resolve() {clearTimeout(timer); resolve();}};
        stdin.write(input);
      });
      await delay(16);
    }
    measuring = false;
    const elapsed = performance.now() - started, cpu = process.cpuUsage(usage);
    results.push({name: `raw-${scene}-to-content-frame`, samples: latency.length, p50_ms: percentile(latency, .5), p95_ms: percentile(latency, .95), max_ms: percentile(latency, 1), ink_render_p95_ms: percentile(renders, .95), render_count: renders.length, cpu_percent_one_core: round((cpu.user + cpu.system) / (elapsed * 10)), output_kib: round(bytes / 1024)});
  } finally {c.dispose(); app.unmount(); app.cleanup(); stdin.destroy(); c.client.peer.close();}
}

let draft = editor('中文 input 👩🏽‍💻 line\n'.repeat(500));
inputLayout(draft, 154, 4);
const times: number[] = [];
for (let i = 0; i < 1000; i++) {
  const started = performance.now();
  draft = edit(draft, '', i % 2 ? {downArrow: true} : {upArrow: true}, 154);
  inputLayout(draft, 154, 4);
  times.push(performance.now() - started);
}
results.push({name: '500-line-unicode-cursor-and-layout', samples: times.length, p50_ms: percentile(times, .5), p95_ms: percentile(times, .95), max_ms: percentile(times, 1)});
const report = {label: values.label, node: process.version, node_env: process.env.NODE_ENV ?? 'development', platform: process.platform, cpu: cpus()[0]?.model, terminal: {columns: 160, rows: 40}, render_limit_fps: RENDER_FPS, scope: 'Raw stdin through Ink to newly visible text in a synthetic terminal sink; excludes terminal emulator, GPU and SSH latency.', results};
const json = JSON.stringify(report, null, 2) + '\n';
if (values.json) await writeFile(values.json, json);
process.stdout.write(json);
