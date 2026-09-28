import type {ChatCell} from '../shared.js';
import {t, type MessageKey} from '../i18n.js';
import {coreMessage} from '../core-messages.js';
import type {ImageGallery} from './images.js';
import {reveal} from './motion.js';

/** Sent drafts stay next to the composer until the core proves application. */
export class PendingInputs {
  private entries = new Map<string, {node: HTMLDetailsElement; summary: HTMLElement; status: HTMLElement; text: HTMLElement; detail: HTMLElement; gallery: HTMLElement; images: string; signature: string; retry: HTMLButtonElement; restore: HTMLButtonElement}>();
  constructor(private root: HTMLElement, private gallery: ImageGallery, private retry: (id: string) => void, private restore: (id: string) => void) {}

  update(cells: ChatCell[], canRestore: (cell: ChatCell) => boolean): void {
    this.root.hidden = !cells.length;
    const ids = new Set(cells.map(cell => cell.id));
    for (const [id, entry] of this.entries) if (!ids.has(id)) {entry.node.remove(); this.entries.delete(id);}
    let position = this.root.firstChild;
    for (const cell of cells) {
      let entry = this.entries.get(cell.id);
      if (!entry) {
        const node = document.createElement('details'); node.className = 'pending-input'; node.dataset.id = cell.id;
        const heading = document.createElement('summary');
        const status = document.createElement('span'); status.className = 'pending-state';
        const summary = document.createElement('span'); summary.className = 'pending-preview';
        heading.append(status, summary);
        const body = document.createElement('div'); body.className = 'pending-body';
        const text = document.createElement('div'), gallery = document.createElement('div'), detail = document.createElement('div'); detail.className = 'detail';
        const retry = document.createElement('button'); retry.className = 'retry'; retry.textContent = t('Retry'); retry.addEventListener('click', () => this.retry(cell.id));
        const restore = document.createElement('button'); restore.textContent = t('Return to draft'); restore.addEventListener('click', () => this.restore(cell.id));
        body.append(text, gallery, detail, retry, restore); node.append(heading, body);
        entry = {node, summary, status, text, detail, gallery, images: '', signature: '', retry, restore}; this.entries.set(cell.id, entry);
        this.root.append(node); reveal(node);
      }
      if (position !== entry.node) this.root.insertBefore(entry.node, position);
      position = entry.node.nextSibling;
      const restorable = canRestore(cell), signature = JSON.stringify([cell, restorable]);
      if (entry.signature === signature) continue;
      entry.signature = signature;
      const label = cell.status === 'not-applied' ? t('Not applied') : cell.status === 'queued' || cell.status === 'accepted' ? t('Waiting to apply') : t((cell.status ?? 'sending') as MessageKey);
      if (entry.node.dataset.status !== cell.status && ['rejected', 'unconfirmed', 'not-applied'].includes(cell.status ?? '')) entry.node.open = true;
      entry.node.dataset.status = cell.status;
      entry.status.textContent = label;
      entry.summary.textContent = cell.text || t('Image');
      entry.summary.title = cell.text;
      entry.text.textContent = cell.text;
      entry.detail.textContent = cell.status === 'not-applied' ? t('The task ended before this message was applied. Return it to the draft to send again.') : coreMessage(cell.detail ?? '');
      entry.detail.hidden = !entry.detail.textContent;
      entry.retry.hidden = !['rejected', 'unconfirmed'].includes(cell.status ?? '');
      entry.restore.hidden = !restorable;
      const images = JSON.stringify(cell.images ?? []);
      if (images !== entry.images) {entry.images = images; entry.gallery.replaceChildren(); if (cell.images?.length) this.gallery.draw(entry.gallery, cell.images);}
    }
  }
  reset(): void {this.root.replaceChildren(); this.entries.clear();}
}
