import assert from 'node:assert/strict';
import test from 'node:test';
import {runInNewContext} from 'node:vm';
import {build} from 'esbuild';
import {resolve} from 'node:path';

test('browser bundle supports messages, interactions, byte uploads and independent view disposal', async t => {
  const bundle = await build({
    stdin: {contents: `export * from '@reuleauxcoder/client';`, resolveDir: resolve('.')},
    bundle: true, platform: 'browser', format: 'iife', globalName: 'Protocol', write: false, metafile: true,
  });
  assert(Object.keys(bundle.metafile.inputs).every(path => path === '<stdin>' || path.replaceAll('\\', '/').includes('reuleauxcoder-client/dist/')),
    'the browser entry must not pull in TUI code or external dependencies');
  // No process, require, Buffer, filesystem, streams or Node event emitter.
  const browser: any = {setTimeout, clearTimeout, performance, btoa};
  runInNewContext(bundle.outputFiles[0].text, browser);
  const {MessagePeer, RuntimeClient, record, decode, emptyState} = browser.Protocol;
  const frontend: any = new MessagePeer((message: any) => queueMicrotask(() => backend.receive(structuredClone(message))));
  const backend: any = new MessagePeer((message: any) => queueMicrotask(() => frontend.receive(structuredClone(message))));
  const client = new RuntimeClient(frontend);
  t.after(() => {client.close(); backend.close();});
  let initializedProfile: any;
  let shutdowns = 0;
  backend.methods.set('initialize', ({profile}: any) => {
    initializedProfile = decode(profile);
    return {version: 1, image_uploads: true, catalog: {actions: []}, state: {...emptyState, revision: 1}};
  });
  backend.methods.set('runtime.shutdown', () => {shutdowns++; return null;});
  await client.initialize({ui_id: 'desktop', display_name: 'Desktop', capabilities: ['text_input']});
  assert.equal(initializedProfile.ui_id, 'desktop');

  const interaction = backend.request('interaction.request', {kind: 'confirm', request: record('ConfirmRequest', {request_id: 'ask', title: 'Continue?'})});
  await new Promise(resolve => client.once('interactions', resolve));
  client.answer('ask', record('ConfirmResponse', {confirmed: true, cancelled: false}));
  assert.equal(decode(await interaction).confirmed, true);

  const bytes = new Uint8Array([255, 0, 128, 65, 66]);
  const received: number[] = [];
  let cancelled = false;
  backend.methods.set('images.begin', ({name, size_bytes}: any) => {
    assert.equal(name, 'blob.png'); assert.equal(size_bytes, bytes.length);
    return {upload_id: 'upload', chunk_bytes: 2};
  });
  backend.methods.set('images.append', ({offset, data}: any) => {
    assert.equal(offset, received.length);
    received.push(...Array.from(atob(data), character => character.charCodeAt(0)));
    return received.length;
  });
  backend.methods.set('images.complete', () => ({attachment_id: 'image'}));
  backend.methods.set('images.cancel', () => {cancelled = true; return null;});
  const image = await client.uploadImage({name: 'blob.png', size: bytes.length, read: async (offset: number, length: number) => bytes.slice(offset, offset + length)});
  assert.equal(image.attachment_id, 'image');
  assert.deepEqual(received, [...bytes]); assert(cancelled);

  let views = 0;
  const view = () => views++;
  client.on('state', view);
  backend.notify('runtime.state', {state: {...emptyState, revision: 2}});
  await new Promise(resolve => client.once('state', resolve));
  client.off('state', view); // Unmounting a view leaves the host-owned client connected.
  backend.notify('runtime.state', {state: {...emptyState, revision: 3}});
  await new Promise(resolve => client.once('state', resolve));
  assert.equal(views, 1); assert.equal(shutdowns, 0); assert(!frontend.closed);
  const pending = frontend.request('pending');
  client.close();
  await assert.rejects(pending, /closed/i);
  assert.equal(shutdowns, 0);
});
