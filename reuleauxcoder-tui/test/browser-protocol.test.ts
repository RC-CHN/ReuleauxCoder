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
    return {version: 1, image_uploads: true, attachment_uploads: true, catalog: {actions: []}, state: {...emptyState, revision: 1}};
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

  received.length = 0;
  cancelled = false;
  backend.methods.set('attachments.begin', ({name, size_bytes}: any) => {
    assert.equal(name, 'blob.bin'); assert.equal(size_bytes, bytes.length);
    return {upload_id: 'upload', chunk_bytes: 2};
  });
  backend.methods.set('attachments.append', backend.methods.get('images.append'));
  backend.methods.set('attachments.complete', () => record('AttachmentReference', {attachment_id: 'file', name: 'blob.bin', mime_type: 'application/octet-stream', size_bytes: bytes.length, path: '.rcoder/attachments/session/file/blob.bin'}));
  backend.methods.set('attachments.cancel', backend.methods.get('images.cancel'));
  const source = {name: 'blob.bin', size: bytes.length, read: async (offset: number, length: number) => bytes.slice(offset, offset + length)};
  const attachment = await client.uploadAttachment(source);
  assert.equal(attachment.attachment_id, 'file');
  assert.equal(attachment.path, '.rcoder/attachments/session/file/blob.bin');
  assert.deepEqual(received, [...bytes]); assert(cancelled);

  await assert.rejects(client.uploadAttachment({...source, size: 64 * 1024 * 1024 + 1, read: () => {throw new Error('Must not read oversized files');}}), /64 MiB/);
  cancelled = false;
  await assert.rejects(client.uploadAttachment({...source, read: async () => new Uint8Array(3)}), /source chunk/);
  assert(cancelled);
  cancelled = false;
  backend.methods.set('attachments.append', () => 0);
  await assert.rejects(client.uploadAttachment(source), /acknowledgement/);
  assert(cancelled);
  client.info.attachment_uploads = false;
  await assert.rejects(client.uploadAttachment(source), /does not support/);

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
