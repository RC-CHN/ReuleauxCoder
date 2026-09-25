import assert from 'node:assert/strict';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import test from 'node:test';
import {ConfigurationProcess} from '../src/core/configuration-process.js';
import {ConfigurationRecovery} from '../src/core/configuration-recovery.js';
import {WorkspaceSession} from '../src/core/session.js';
import {python} from './helpers.js';

const valid = 'app:\n  api_key: offline-test\n  model: test\n';
test('independent recovery preserves explicit paths, rejects dirty/stale files and never starts an Agent', {timeout: 60000}, async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder recovery 中文-'));
  const file = join(cwd, 'explicit 中文.yaml');
  await writeFile(file, valid);
  let changes = 0;
  const recovery = new ConfigurationRecovery(() => changes++);
  const options = {cwd, env: {...process.env, HOME: join(cwd, 'home'), USERPROFILE: join(cwd, 'home')}, commands: [{command: python, args: ['-m', 'reuleauxcoder', '--config', file, '--model', 'ignored-runtime-override', '-p', 'do not run me']}]};
  t.after(async () => {await recovery.close(); await rm(cwd, {recursive: true, force: true});});
  await recovery.open(options);
  const client = recovery.process.client!;
  assert.equal(recovery.source('explicit'), file);
  assert.equal((await client.inspect()).runtime, null);
  const second = await client.prepare({scope: 'explicit', changes: [{path: '/ui/verbosity', value: 'debug'}]});
  await client.apply(second.id);
  await recovery.close();
  // Even malformed YAML and runtime-only arguments cannot prevent management startup.
  await writeFile(file, 'app: [broken');
  await recovery.open(options);
  assert.equal(recovery.state!.valid, false);
  assert.equal(recovery.state!.diagnostics[0].code, 'invalid_yaml');
  assert(recovery.state!.history.some(item => item.id === second.id));
  await assert.rejects(recovery.select('untrusted-record'), /history changed/);
  await recovery.select(second.id);
  assert(recovery.state!.validation!.checks.every(check => check.status === 'passed'));
  const id = recovery.state!.candidate!.id;
  await assert.rejects(recovery.apply(id, true, [file]), /unsaved changes/);
  assert.equal(await readFile(file, 'utf8'), 'app: [broken');
  await writeFile(file, 'app: [changed');
  await assert.rejects(recovery.apply(id, true, []), /changed on disk/);
  assert.equal(await readFile(file, 'utf8'), 'app: [changed');
  await recovery.check(); await recovery.select(second.id);
  await recovery.apply(recovery.state!.candidate!.id, true, []);
  assert.equal(await readFile(file, 'utf8'), valid);
  assert.equal(recovery.state!.valid, true);
  assert(recovery.state!.message?.includes('restored'));
  assert(changes >= 15);
  const peer = recovery.process.client!.peer;
  await recovery.close(); assert(peer.closed); assert.equal(recovery.process.client, undefined);
});

test('management spawn is single-flight, cancellable and bounded; absent binaries can fall back', {timeout: 30000}, async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder recovery lifecycle-'));
  const process = new ConfigurationProcess();
  t.after(async () => {await process.close(); await rm(cwd, {recursive: true, force: true});});
  const options = {cwd, env: {...globalThis.process.env, HOME: join(cwd, 'home'), USERPROFILE: join(cwd, 'home')}, commands: [{command: join(cwd, 'absent-core'), args: []}, {command: python, args: ['-m', 'reuleauxcoder']}]};
  const first = process.start(options); assert.equal(process.start(options), first);
  const client = await first; assert((await client.describe()).capabilities?.includes('reviewer_protection'));
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
  session.recovery.close = () => new Promise(resolve => {finish = resolve;});
  session.runtime.start = async () => {started = true; throw new Error('unexpected spawn');};
  const start = session.start({cwd: tmpdir(), commands: []});
  const finishStart = finish;
  session.dispose(); finishStart(); finish();
  await assert.rejects(start, /cancelled/); assert.equal(started, false);
});

test('online recovery verifies each affected model in bounded batches using actual configured requests', {timeout: 60000}, async t => {
  const {createServer} = await import('node:http');
  const requests: {model: string; max_tokens: number; reasoning_effort?: string}[] = [];
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = []; for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString()); requests.push(body);
    res.writeHead(200, {'Content-Type': 'text/event-stream'});
    res.end(`data: ${JSON.stringify({id: 'probe', object: 'chat.completion.chunk', created: 0, model: body.model, choices: [{index: 0, delta: {role: 'assistant', content: 'OK'}, finish_reason: 'stop'}]})}\n\ndata: [DONE]\n\n`);
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder recovery model tests-'));
  const recovery = new ConfigurationRecovery(() => {});
  t.after(async () => {await recovery.close(); await new Promise<void>(resolve => server.close(() => resolve())); await rm(cwd, {recursive: true, force: true});});
  const file = join(cwd, 'models.yaml');
  const document = {app: {api_key: 'local-test', base_url: `http://127.0.0.1:${(server.address() as {port: number}).port}/v1`, reasoning_effort: 'high'}, models: {active_main: 'p0', profiles: Object.fromEntries(Array.from({length: 9}, (_, i) => [`p${i}`, {model: `model-${i}`, max_tokens: 101 + i}]))}};
  await writeFile(file, JSON.stringify(document));
  await recovery.open({cwd, env: {...process.env, HOME: join(cwd, 'home'), USERPROFILE: join(cwd, 'home')}, commands: [{command: python, args: ['-m', 'reuleauxcoder', '--config', file]}]});
  const saved = await recovery.process.client!.prepare({scope: 'explicit', changes: [{path: '/ui/verbosity', value: 'debug'}]});
  await recovery.process.client!.apply(saved.id);
  await writeFile(file, 'app: [broken'); await recovery.check(); await recovery.select(saved.id);
  const id = recovery.state!.candidate!.id;
  await recovery.testModels(id); assert.equal(requests.length, 8);
  assert.equal(recovery.state!.validation!.checks.filter(check => check.check === 'model' && check.status === 'passed').length, 8);
  await recovery.testModels(id); assert.equal(requests.length, 9, 'The second click must only probe the remaining target');
  assert.equal(new Set(requests.map(item => item.model)).size, 9);
  for (const request of requests) {assert.equal(request.max_tokens, 101 + Number(request.model.split('-')[1])); assert.equal(request.reasoning_effort, 'high');}
  await recovery.testModels(id); assert.equal(requests.length, 9, 'Fresh successful probes should not consume tokens again');
  // Reusing the view after probe expiry must permit online testing again.
  for (const check of recovery.state!.validation!.checks) if (check.check === 'model') check.checked_at = 0;
  await recovery.testModels(id); assert.equal(requests.length, 17);
  await recovery.testModels(id); assert.equal(requests.length, 18);
  await recovery.apply(id, false, []);
  const restored = (await recovery.process.client!.inspect()).sources.find(source => source.scope === 'explicit')!.values;
  assert.deepEqual(restored.models, document.models);
  assert.equal(restored.ui, undefined);
});
