import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {attachFile} from '@reuleauxcoder/client/node';
import {backend} from './helpers.js';

test('file uploads cross real stdio in bounded chunks and preserve same-name attachments', async t => {
  const b = await backend(); t.after(() => b.close());
  assert(b.client.info.attachment_uploads);
  const bytes = Buffer.alloc(600_000);
  for (let i = 0; i < bytes.length; i++) bytes[i] = i % 256;
  let reads = 0;
  const first = await b.client.uploadAttachment({
    name: '资料.bin', size: bytes.length,
    async read(offset, length) {
      assert(length <= 256 * 1024); reads++;
      return bytes.subarray(offset, offset + length);
    },
  });
  assert.equal(reads, 3);
  assert(first.path.startsWith(`.rcoder/attachments/${b.client.state.session_id}/`));
  assert.equal(first.size_bytes, bytes.length);
  assert.deepEqual(await readFile(join(b.cwd, first.path)), bytes);
  const path = join(b.cwd, '资料.bin');
  await writeFile(path, 'second');
  const second = await attachFile(b.client, path);
  assert.notEqual(second.attachment_id, first.attachment_id);
  assert.equal(await readFile(join(b.cwd, second.path), 'utf8'), 'second');
  const empty = await b.client.uploadAttachment({name: 'empty.txt', size: 0, read: async () => {throw new Error('Empty file must not be read');}});
  assert.equal((await readFile(join(b.cwd, empty.path))).length, 0);
});
