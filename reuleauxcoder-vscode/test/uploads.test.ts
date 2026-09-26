import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {join} from 'node:path';
import {decode} from '@reuleauxcoder/client';
import {backend, until} from './helpers.js';

test('browser bytes arrive on the workspace host with bounded ownership and offsets', async t => {
  const b = await backend(); t.after(() => b.close());
  const uploads = b.session.uploads!;
  const content = Buffer.from('Local clipboard 中文\n'.repeat(20000));
  const upload = await uploads.begin('view-a', 'notes.txt', content.length, false);
  await assert.rejects(uploads.begin('view-b', 'other.txt', 1, false), /Another attachment/);
  await assert.rejects(uploads.append('view-b', upload.id, 0, 'YQ=='), /expired/);
  await assert.rejects(uploads.append('view-a', upload.id, 1, 'YQ=='), /Invalid attachment/);
  for (let offset = 0; offset < content.length;) offset = await uploads.append('view-a', upload.id, offset, content.subarray(offset, offset + upload.chunk).toString('base64'));
  const result = await uploads.complete('view-a', upload.id);
  assert.deepEqual(await readFile(join(b.cwd, result.reference.path)), content);
  assert.equal(result.kind, 'file');
});

test('disposing a view cancels its upload and cannot cancel another owner', async t => {
  const b = await backend(); t.after(() => b.close()); const uploads = b.session.uploads!;
  const upload = await uploads.begin('view-a', 'partial.txt', 5, false);
  await uploads.cancel('view-b');
  assert.equal(await uploads.append('view-a', upload.id, 0, Buffer.from('abc').toString('base64')), 3);
  await uploads.cancel('view-a');
  await assert.rejects(uploads.complete('view-a', upload.id), /expired/);
  assert(!b.client.peer.closed);
});

test('clipboard image upload reaches a real core turn and survives retry with the same submission ID', async t => {
  const b = await backend(); t.after(() => b.close());
  const bytes = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAFElEQVR4nGP8//8/AwwwMSABFA4Aby0DAyMYAwQAAAAASUVORK5CYII=', 'base64');
  const upload = await b.session.uploads!.begin('clipboard', 'paste.png', bytes.length, true);
  await b.session.uploads!.append('clipboard', upload.id, 0, bytes.toString('base64'));
  const image = await b.session.uploads!.complete('clipboard', upload.id);
  assert.equal(image.kind, 'image'); assert.equal(image.reference.width, 4); assert.equal(image.reference.height, 3);
  b.session.add(image); assert.equal(b.session.draftItems.length, 1);
  const preview = await b.session.imagePreview(image.reference.attachment_id, image.reference.variant_id);
  assert.match(preview, /^data:image\/png;base64,/);
  await assert.rejects(b.session.imagePreview('unknown', image.reference.variant_id), /no longer available/);
  const submit = b.client.submit.bind(b.client);
  const attempts: unknown[] = [];
  b.client.submit = async (...args) => {
    attempts.push(structuredClone(args));
    if (attempts.length === 1) throw new Error('Temporary transport failure');
    return submit(...args);
  };
  b.session.submit('clipboard-image', 'Inspect this image', [image.id], b.client.state.session_generation);
  await until(() => b.session.transcript.cells.some(cell => cell.id === 'clipboard-image' && cell.status === 'unconfirmed'));
  await b.session.submissions!.retry('clipboard-image');
  await until(() => b.session.transcript.cells.some(cell => cell.id === 'clipboard-image' && ['applied', 'rejected'].includes(cell.status!)));
  assert.equal(b.session.transcript.cells.find(cell => cell.id === 'clipboard-image')?.status, 'applied');
  assert.deepEqual(attempts[0], attempts[1]);
  await until(() => !b.client.state.running);
  const messages = decode(await b.client.peer.request('test.messages'));
  const users = messages.filter((message: any) => message.role === 'user');
  assert.equal(users.length, 1);
  assert.equal(users[0].content.find((part: any) => part.type === 'image').attachment_id, image.reference.attachment_id);
  assert.equal(b.session.transcript.cells.filter(cell => cell.id === 'clipboard-image').length, 1);
  assert.equal(b.session.draftItems.length, 0);
  assert.equal(b.session.transcript.cells.find(cell => cell.id === 'clipboard-image')?.images?.[0].variant_id, image.reference.variant_id);
  assert.equal(await b.session.imagePreview(image.reference.attachment_id, image.reference.variant_id), preview);
});

test('clipboard images keep their session when sent as steering', async t => {
  const b = await backend(); t.after(() => b.close());
  b.session.submit('working', 'wait', [], b.client.state.session_generation);
  await until(() => b.client.state.running);
  const bytes = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAFElEQVR4nGP8//8/AwwwMSABFA4Aby0DAyMYAwQAAAAASUVORK5CYII=', 'base64');
  const upload = await b.session.uploads!.begin('clipboard', 'steering.png', bytes.length, true);
  await b.session.uploads!.append('clipboard', upload.id, 0, bytes.toString('base64'));
  const image = await b.session.uploads!.complete('clipboard', upload.id);
  b.session.add(image);
  b.session.submit('steering-image', 'Also check this', [image.id], b.client.state.session_generation);
  await until(() => b.session.transcript.cells.some(cell => cell.id === 'steering-image' && ['queued', 'rejected'].includes(cell.status!)));
  assert.equal(b.session.transcript.cells.find(cell => cell.id === 'steering-image')?.status, 'queued');
  await b.client.peer.request('test.drain_steering');
  await until(() => b.session.transcript.cells.some(cell => cell.id === 'steering-image' && cell.status === 'applied'));
  const messages = decode(await b.client.peer.request('test.messages'));
  assert(messages.some((message: any) => Array.isArray(message.content) && message.content.some((part: any) => part.type === 'image' && part.attachment_id === image.reference.attachment_id)));
});
