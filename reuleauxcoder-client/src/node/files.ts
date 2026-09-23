/** Frontend-local filesystem adapter; paths never cross the runtime protocol. */
import {open} from 'node:fs/promises';
import {basename, join} from 'node:path';
import {homedir} from 'node:os';
import type {AttachmentSource, RuntimeClient} from '../client.js';

export async function attachImageFile(client: RuntimeClient, path: string) {
  return withFile(path, source => client.uploadImage(source));
}

export async function attachFile(client: RuntimeClient, path: string) {
  return withFile(path, source => client.uploadAttachment(source));
}

async function withFile<T>(path: string, upload: (source: AttachmentSource) => Promise<T>): Promise<T> {
  if (path.startsWith('~/') || path.startsWith('~\\')) path = join(homedir(), path.slice(2));
  const file = await open(path, 'r');
  try {
    return await upload({
      name: basename(path), size: (await file.stat()).size,
      async read(offset, length) {
        const buffer = new Uint8Array(length);
        const {bytesRead} = await file.read(buffer, 0, length, offset);
        return buffer.subarray(0, bytesRead);
      },
    });
  } finally {await file.close();}
}
