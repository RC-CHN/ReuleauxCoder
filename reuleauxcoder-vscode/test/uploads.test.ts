import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {join} from 'node:path';
import {backend} from './helpers.js';

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

test('clipboard image bytes are validated by the real core and referenced in the draft', async t => {
  const b = await backend(); t.after(() => b.close());
  const bytes = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAFElEQVR4nGP8//8/AwwwMSABFA4Aby0DAyMYAwQAAAAASUVORK5CYII=', 'base64');
  const upload = await b.session.uploads!.begin('clipboard', 'paste.png', bytes.length, true);
  await b.session.uploads!.append('clipboard', upload.id, 0, bytes.toString('base64'));
  const image = await b.session.uploads!.complete('clipboard', upload.id);
  assert.equal(image.kind, 'image'); assert.equal(image.reference.width, 4); assert.equal(image.reference.height, 3);
  b.session.add(image); assert.equal(b.session.draftItems.length, 1);
});
