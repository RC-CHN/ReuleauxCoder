import assert from 'node:assert/strict';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import test from 'node:test';
import {ConfigurationProcess} from '../src/core/configuration-process.js';
import {ConfigurationEditor} from '../src/core/configuration-editor.js';
import {WorkspaceSession} from '../src/core/session.js';
import {python} from './helpers.js';

const valid = 'app:\n  api_key: offline-test\n  model: test\n';
test('native buffer changes invalidate checks, including edits while a probe is pending', {timeout: 30000}, async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder config buffers-')), file = join(cwd, 'explicit 中文.yaml');
  await writeFile(file, valid);
  const editor = new ConfigurationEditor(() => {});
  const options = {cwd, env: {...process.env, HOME: join(cwd, 'home'), USERPROFILE: join(cwd, 'home')}, commands: [{command: python, args: ['-m', 'reuleauxcoder', '--config', file, '-p', 'never execute']}]};
  t.after(async () => {await editor.close(); await rm(cwd, {recursive: true, force: true});});
  await editor.open(options);
  assert.equal(editor.source('explicit'), file);
  assert.equal((await editor.process.client!.inspect()).runtime, null);
  editor.setDocuments([{scope: 'explicit', content: 'app: [broken'}]);
  assert.equal((await editor.check())!.valid, false);
  assert.equal(await readFile(file, 'utf8'), valid);
  editor.setDocuments([{scope: 'explicit', content: valid + '# unsaved\n'}]);
  assert.equal(editor.state!.validation, undefined);
  assert.equal((await editor.check())!.buffer_check, true);
  const client = editor.process.client!, check = client.check.bind(client);
  let release!: () => void;
  client.check = async options => {await new Promise<void>(resolve => {release = resolve;}); return check(options);};
  const pending = editor.check();
  while (!release) await new Promise(resolve => setTimeout(resolve, 10));
  editor.setDocuments([{scope: 'explicit', content: 'ui: [changed'}]); release();
  await assert.rejects(pending, /changed during/);
  assert.equal(editor.state!.validation, undefined);
  client.check = check;
  editor.setDocuments([]);
  assert.equal((await editor.check())!.valid, true);
  assert.equal(await readFile(file, 'utf8'), valid);
});

test('management spawn is single-flight, cancellable and bounded; absent binaries can fall back', {timeout: 30000}, async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder recovery lifecycle-'));
  const process = new ConfigurationProcess();
  t.after(async () => {await process.close(); await rm(cwd, {recursive: true, force: true});});
  const options = {cwd, env: {...globalThis.process.env, HOME: join(cwd, 'home'), USERPROFILE: join(cwd, 'home')}, commands: [{command: join(cwd, 'absent-core'), args: []}, {command: python, args: ['-m', 'reuleauxcoder']}]};
  const first = process.start(options); assert.equal(process.start(options), first);
  const client = await first; assert((await client.describe()).capabilities?.includes('buffer_checks'));
  await process.close(); assert(client.peer.closed);
  const cancelled = process.start(options); const rejected = assert.rejects(cancelled, /closed/);
  await process.close(); await rejected;
  const stalled = {cwd, startupTimeout: 100, commands: [{command: globalThis.process.execPath, args: ['-e', 'setInterval(() => {}, 1000)', '--']}]};
  await assert.rejects(process.start(stalled), /timed out/);
  assert.equal(process.client, undefined);
  // A released core without the new capability handshake must be updated.
  const outdated = "process.stdin.once('data', data => {const {id} = JSON.parse(data); console.log(JSON.stringify({jsonrpc:'2.0',id,result:{api_version:1,mode:'configuration',capabilities:[]}}));});";
  await assert.rejects(process.start({cwd, commands: [{command: globalThis.process.execPath, args: ['-e', outdated, '--']}]}), /Update the core/);
});

test('disposing a workspace during recovery cleanup cannot launch a late core', async () => {
  const session = new WorkspaceSession(tmpdir(), 'test');
  let finish!: () => void; let started = false;
  session.configuration.close = () => new Promise(resolve => {finish = resolve;});
  session.runtime.start = async () => {started = true; throw new Error('unexpected spawn');};
  const start = session.start({cwd: tmpdir(), commands: []});
  const finishStart = finish;
  session.dispose(); finishStart(); finish();
  await assert.rejects(start, /cancelled/); assert.equal(started, false);
});
