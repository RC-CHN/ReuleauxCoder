import {t} from '../i18n.js';
import {randomUUID} from 'node:crypto';
import {decode, type RuntimeClient} from '@reuleauxcoder/client';

interface Upload {id: string; owner: string; backend: string; namespace: 'images' | 'attachments'; generation: number; size: number; offset: number; chunk: number; name: string; busy: boolean}
/** Acknowledged, bounded browser chunks are forwarded without whole-file buffering. */
export class Uploads {
  private active?: Upload;
  constructor(private client: RuntimeClient) {}
  async begin(owner: string, name: string, size: number, image: boolean) {
    if (this.active) throw new Error(t('Another attachment is uploading. Wait or cancel it first.'));
    if (typeof name !== 'string' || name.length > 255 || /[/\\\0]/.test(name) || !Number.isSafeInteger(size) || size < 0 || size > 64 * 1024 * 1024) throw new Error(t('Invalid attachment, or larger than 64 MiB.'));
    if (image && !this.client.state.support_modal?.includes('image')) throw new Error(t('The selected model does not support images. Choose a vision model first.'));
    const generation = this.client.state.session_generation;
    const namespace = image ? 'images' : 'attachments';
    const upload: Upload = {id: randomUUID(), owner, backend: '', namespace, generation, size, offset: 0, chunk: 0, name, busy: true}; this.active = upload;
    try {
      const result = await this.client.peer.request(`${namespace}.begin`, {name, size_bytes: size, session_id: this.client.state.session_id, session_generation: generation}) as any;
      upload.backend = result.upload_id;
      if (this.active !== upload || generation !== this.client.state.session_generation) {await this.client.peer.request(`${namespace}.cancel`, {upload_id: upload.backend}); throw new Error(t('Session or attachment changed during upload.'));}
      if (!Number.isSafeInteger(result.chunk_bytes) || result.chunk_bytes < 1 || result.chunk_bytes > 262144) throw new Error('Invalid attachment chunk limit.');
      upload.chunk = result.chunk_bytes; upload.busy = false;
      return {id: upload.id, chunk: upload.chunk};
    } catch (error) {
      if (this.active === upload) await this.cancel(owner, upload.id);
      throw error;
    }
  }
  private current(owner: string, id: string): Upload {
    const upload = this.active;
    if (!upload || upload.owner !== owner || upload.id !== id || upload.generation !== this.client.state.session_generation) throw new Error(t('This upload has expired.'));
    if (upload.busy) throw new Error(t('The previous attachment operation is still pending.'));
    return upload;
  }
  async append(owner: string, id: string, offset: number, data: string): Promise<number> {
    const upload = this.current(owner, id);
    if (offset !== upload.offset || typeof data !== 'string' || data.length > Math.ceil(upload.chunk / 3) * 4 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(data)) throw new Error('Invalid attachment chunk.');
    const length = Buffer.byteLength(data, 'base64');
    if (!length || length > upload.chunk || offset + length > upload.size) throw new Error('Attachment chunk exceeds its declared size.');
    upload.busy = true;
    try {
      const next = await this.client.peer.request(`${upload.namespace}.append`, {upload_id: upload.backend, offset, data});
      if (this.active !== upload || upload.generation !== this.client.state.session_generation) throw new Error(t('Session changed during upload.'));
      if (next !== offset + length) throw new Error('Invalid attachment acknowledgement.');
      return upload.offset = next as number;
    } finally {upload.busy = false;}
  }
  async complete(owner: string, id: string) {
    const upload = this.current(owner, id);
    if (upload.offset !== upload.size) throw new Error(t('Attachment upload is incomplete.'));
    upload.busy = true;
    try {
      const reference = decode(await this.client.peer.request(`${upload.namespace}.complete`, {upload_id: upload.backend}));
      if (this.active !== upload || upload.generation !== this.client.state.session_generation) throw new Error(t('Session changed during upload.'));
      return {id: upload.id, name: upload.name, kind: upload.namespace === 'images' ? 'image' as const : 'file' as const, reference};
    } finally {await this.cancel(owner, id);}
  }
  async cancel(owner?: string, id?: string): Promise<void> {
    const upload = this.active;
    if (!upload || owner && owner !== upload.owner || id && id !== upload.id) return;
    this.active = undefined;
    if (upload.backend && !this.client.peer.closed) await this.client.peer.request(`${upload.namespace}.cancel`, {upload_id: upload.backend});
  }
}
