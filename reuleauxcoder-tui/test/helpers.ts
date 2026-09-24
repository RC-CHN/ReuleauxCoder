import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';
import {RpcPeer} from '@reuleauxcoder/client/node';
import {RuntimeClient} from '@reuleauxcoder/client';
import {tuiProfile} from '../src/profile.js';
import {TuiController} from '../src/state/controller.js';
import {InputHistory} from '../src/state/history.js';

export const python = process.env.RCODER_TUI_PYTHON ?? fileURLToPath(new URL(process.platform === 'win32' ? '../../.venv/Scripts/python.exe' : '../../.venv/bin/python', import.meta.url));

export async function until(predicate: () => unknown, message = 'condition', timeout = 5000) {
  const deadline = Date.now() + timeout;
  while (!predicate()) {if (Date.now() > deadline) throw new Error(`Timed out: ${message}`); await delay(10);}
}
export async function backend() {
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder-tui-'));
  const child = spawn(python, [fileURLToPath(new URL('./backend.py', import.meta.url))], {cwd, stdio: 'pipe'});
  let errors = '';
  child.stderr.on('data', chunk => {errors += chunk;});
  const exited = new Promise<void>(resolve => child.once('close', () => resolve()));
  const peer = new RpcPeer(child.stdout, child.stdin);
  child.once('error', error => peer.close(error));
  const client = new RuntimeClient(peer);
  const controller = new TuiController(client, new InputHistory(join(cwd, 'history.jsonl')));
  try {await client.initialize(tuiProfile);}
  catch (error) {child.kill(); throw new Error(String(error) + errors);}
  return {client, controller, peer, cwd, child, errors: () => errors, async close() {
    await client.shutdown(); controller.dispose();
    const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
    await exited; clearTimeout(timer);
    try {
      if (child.exitCode !== 0) throw new Error(`Backend exit ${child.exitCode}: ${errors}`);
    } finally {await rm(cwd, {recursive: true, force: true, maxRetries: 5, retryDelay: 100});}
  }};
}
