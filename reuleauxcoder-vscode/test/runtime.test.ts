import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {record} from '@reuleauxcoder/client';
import {CoreRuntime, CoreFailure} from '../src/core/runtime.js';
import {backend, backendScript, python, until} from './helpers.js';
import {minimumCoreVersion, minimumEditorApiVersion} from '../src/core/compatibility.js';

test('real core streams Unicode, owns sent drafts, and survives view listeners detaching', async t => {
  const b = await backend(); t.after(() => b.close());
  const session = b.session; session.draftText = 'hello';
  session.submit('first', 'hello', [], b.client.state.session_generation);
  assert.equal(session.transcript.cells[0].text, 'hello');
  session.draftText = 'new draft';
  const view = () => {}; session.on('change', view); session.off('change', view);
  await until(() => session.transcript.cells.some(cell => cell.role === 'assistant' && cell.text.includes('Completed 中文')));
  assert.equal(session.draftText, 'new draft'); assert(!b.client.peer.closed);
  assert.equal(session.transcript.cells.filter(cell => cell.role === 'user').length, 1);
  await until(() => !b.client.state.running);
  assert.equal(session.transcript.cells.filter(cell => cell.role === 'assistant').length, 1);
  assert.equal(session.transcript.cells[0].status, 'applied');
});

for (const newline of ['\n', '\r\n']) {
  test(`native diff preserves ${newline === '\n' ? 'LF' : 'CRLF'} through review and approval`, async t => {
    const b = await backend(newline); t.after(() => b.close());
    b.session.submit('edit-submission', 'edit', [], b.client.state.session_generation);
    await until(() => b.client.interactions.length);
    const request = b.client.interactions[0].request; const document = request.documents[0];
    assert.equal(document.before, null); assert.equal(document.after, null);
    assert.equal(await b.client.reviewDocument(request.request_id, document.id, 'before'), `old = 1${newline}`);
    assert.equal(await b.client.reviewDocument(request.request_id, document.id, 'after'), `new = 1${newline}`);
    assert.equal(await readFile(join(b.cwd, 'example.py'), 'utf8'), `old = 1${newline}`);
    b.client.answer(request.request_id, record('ReviewResponse', {approved: true, action: 'allow_once'}));
    await until(() => !b.client.state.running);
    assert.equal(await readFile(join(b.cwd, 'example.py'), 'utf8'), `new = 1${newline}`);
    await assert.rejects(b.client.reviewDocument(request.request_id, document.id, 'before'), /no longer active/);
  });
}

test('unsaved documents block policy-allowed core edits and release after save', async t => {
  const b = await backend(); t.after(() => b.close()); const path = join(b.cwd, 'example.py');
  // Windows editor/file URIs may disagree on drive and filename casing.
  const reportedPath = process.platform === 'win32' ? path.toUpperCase() : path;
  await b.client.peer.request('runtime.editor_documents', {revision: 1, paths: [reportedPath]});
  const completed: unknown[] = [];
  b.client.on('completed', result => completed.push(result));
  b.session.submit('dirty-edit', 'auto-edit', [], b.client.state.session_generation);
  await until(() => completed.length === 1 && !b.client.state.running);
  assert(b.session.transcript.cells.some(cell => cell.text.includes('Unsaved editor changes')));
  assert.equal(await readFile(path, 'utf8'), 'old = 1\n');
  await b.client.peer.request('runtime.editor_documents', {revision: 2, paths: []});
  b.session.submit('saved-edit', 'auto-edit', [], b.client.state.session_generation);
  await until(() => completed.length === 2);
  assert.equal(await readFile(path, 'utf8'), 'new = 1\n', JSON.stringify(completed[1]));
});

test('missing executable is classified separately and startup is single-flight', async () => {
  const runtime = new CoreRuntime();
  const options = {cwd: process.cwd(), commands: [{command: 'rcoder-no-such-executable-for-test', args: []}]};
  const first = runtime.start(options); assert.equal(first, runtime.start(options));
  await assert.rejects(first, error => error instanceof CoreFailure && error.kind === 'missing'); runtime.dispose();
});

test('closing during initialization cancels the handshake and reaps the child', async () => {
  const runtime = new CoreRuntime();
  const starting = runtime.start({cwd: process.cwd(), commands: [{command: process.execPath, args: ['-e', 'setInterval(() => {}, 1000)', '--']}], startupTimeout: 10000});
  const rejection = assert.rejects(starting);
  await runtime.shutdown(); await rejection;
  assert.equal(runtime.client, undefined); runtime.dispose();
});

test('same-release legacy core is blocked before ready and can reconnect after updating', async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder compatibility 中文-'));
  const runtime = new CoreRuntime();
  t.after(async () => {await runtime.shutdown(); runtime.dispose(); await rm(cwd, {recursive: true, force: true});});
  let synchronized = false; let ready = false;
  runtime.on('ready', () => {ready = true;});
  await assert.rejects(runtime.start({cwd, commands: [{command: python, args: [backendScript, '--legacy-core']}], beforeReady: async () => {synchronized = true;}}),
    error => error instanceof CoreFailure && error.kind === 'incompatible' && error.message.includes('integration revision'));
  assert.equal(synchronized, false); assert.equal(ready, false); assert.equal(runtime.client, undefined);
  const client = await runtime.start({cwd, commands: [{command: python, args: [backendScript]}]});
  assert.equal(client.info.core_version, minimumCoreVersion);
  assert.equal(client.info.editor_api_version, minimumEditorApiVersion);
  assert.equal(ready, true);
});
