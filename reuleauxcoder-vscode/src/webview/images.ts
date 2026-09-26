import type {ImageReference} from '@reuleauxcoder/client';
import type {WebRequest} from '../shared.js';
import {t} from '../i18n.js';
import {icon} from './icons.js';
import {reveal} from './motion.js';

/** Fetch previews only when visible; snapshots contain references, never image bytes. */
export class ImageGallery {
  private cache = new Map<string, Promise<string>>();
  private epoch = 0;
  private dialog = document.createElement('dialog');
  private focus?: HTMLElement;
  private observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      this.observer.unobserve(entry.target);
      this.visible.get(entry.target)?.(); this.visible.delete(entry.target);
    }
  }, {rootMargin: '150px'});
  private visible = new Map<Element, () => void>();
  constructor(private request: (action: string, data?: WebRequest['data']) => Promise<any>) {
    this.dialog.className = 'image-viewer';
    this.dialog.addEventListener('click', event => {if (event.target === this.dialog) this.dialog.close();});
    this.dialog.addEventListener('close', () => {this.dialog.replaceChildren(); if (this.focus?.isConnected) this.focus.focus();});
    document.body.append(this.dialog);
  }
  reset(): void {this.epoch++; this.cache.clear(); this.observer.disconnect(); this.visible.clear(); this.dialog.close();}
  draw(parent: HTMLElement, images: ImageReference[], compact = false): void {
    // Snapshot redraws can discard offscreen tiles before their observer fires.
    for (const node of this.visible.keys()) if (!node.isConnected) {this.observer.unobserve(node); this.visible.delete(node);}
    const gallery = document.createElement('div'); gallery.className = compact ? 'image-gallery compact' : 'image-gallery';
    for (const image of images) {
      const tile = document.createElement('button'); tile.type = 'button'; tile.className = 'image-thumbnail';
      tile.setAttribute('aria-label', t('Enlarge {0}', image.name)); tile.title = t('Enlarge {0}', image.name);
      const picture = document.createElement('img'); picture.alt = image.name; picture.decoding = 'async';
      const caption = document.createElement('span'); caption.textContent = image.name;
      const status = document.createElement('small'); status.textContent = t('Loading image…');
      tile.append(picture, caption, status); gallery.append(tile);
      const epoch = this.epoch;
      const load = async () => {
        try {const source = await this.load(image); if (epoch === this.epoch && tile.isConnected) {picture.src = source; status.textContent = `${image.width} × ${image.height}`; tile.classList.add('loaded'); tile.classList.remove('unavailable');}}
        catch {if (epoch === this.epoch && tile.isConnected) {status.textContent = t('Preview unavailable · click to retry'); tile.classList.add('unavailable');}}
      };
      this.visible.set(tile, () => void load()); this.observer.observe(tile);
      tile.addEventListener('click', () => {void load(); void this.open(image, tile);});
    }
    parent.append(gallery);
  }
  private load(image: ImageReference): Promise<string> {
    const key = image.variant_id;
    let pending = this.cache.get(key);
    if (!pending) {
      pending = this.request('image.preview', {attachmentId: image.attachment_id, variantId: image.variant_id});
      this.cache.set(key, pending!);
      if (this.cache.size > 48) this.cache.delete(this.cache.keys().next().value!);
      void pending!.catch(() => {if (this.cache.get(key) === pending) this.cache.delete(key);});
    }
    return pending!;
  }
  private async open(image: ImageReference, origin: HTMLElement): Promise<void> {
    const epoch = this.epoch;
    this.focus = origin; this.dialog.replaceChildren();
    const content = document.createElement('section'); content.className = 'image-viewer-content';
    const head = document.createElement('header');
    const title = document.createElement('strong'); title.textContent = image.name;
    const close = document.createElement('button'); close.type = 'button'; close.append(icon('close')); close.setAttribute('aria-label', t('Close image preview')); close.title = t('Close image preview'); close.addEventListener('click', () => this.dialog.close());
    head.append(title, close);
    const viewport = document.createElement('div'); viewport.className = 'image-viewport';
    const status = document.createElement('p'); status.setAttribute('role', 'status'); status.textContent = t('Loading image…'); viewport.append(status);
    const footer = document.createElement('footer');
    const info = document.createElement('span'); info.textContent = t('Sent image · {0} × {1}', image.width, image.height);
    const zoom = document.createElement('button'); zoom.type = 'button'; zoom.textContent = t('Actual size'); zoom.hidden = true;
    zoom.addEventListener('click', () => {const actual = viewport.classList.toggle('actual-size'); zoom.textContent = t(actual ? 'Fit to view' : 'Actual size'); zoom.setAttribute('aria-pressed', String(actual));});
    footer.append(info, zoom); content.append(head, viewport, footer); this.dialog.append(content);
    this.dialog.setAttribute('aria-label', t('Image preview')); if (!this.dialog.open) this.dialog.showModal(); close.focus(); reveal(content);
    try {
      const source = await this.load(image);
      if (epoch !== this.epoch || !content.isConnected || !this.dialog.open) return;
      const picture = document.createElement('img'); picture.src = source; picture.alt = image.name;
      viewport.replaceChildren(picture); zoom.hidden = false;
    } catch {if (epoch === this.epoch && content.isConnected) status.textContent = t('Image preview is unavailable.');}
  }
}
