import {open} from 'node:fs/promises';
import {homedir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import type {Key} from 'ink';
import type {ImageReference} from '@reuleauxcoder/client';
import {edit, graphemes, type Editor} from './editor.js';

export interface DraftImage {label: string; image: ImageReference}

export function pastedPath(text: string): string | null {
  let path = text.trim();
  if (!path || path.length > 4096 || /[\r\n\0]/.test(path)) return null;
  const quoted = ['"', "'"].includes(path[0]) && path.at(-1) === path[0];
  if (quoted) path = path.slice(1, -1);
  if (path.startsWith('file://')) {try {return fileURLToPath(path);} catch {return null;}}
  if (/^[a-z]:[\\/]/i.test(path) || path.startsWith('\\\\')) {
    if (process.platform === 'linux' && (process.env.WSL_DISTRO_NAME || process.env.WSL_INTEROP) && !path.startsWith('\\\\')) return `/mnt/${path[0].toLowerCase()}/${path.slice(3).replaceAll('\\', '/')}`;
    return path;
  }
  if (!quoted) path = path.replace(/\\([ \t'"\\])/g, '$1');
  return path.startsWith('~/') ? join(homedir(), path.slice(2)) : path;
}

export async function pastedImagePath(text: string): Promise<string | null> {
  const path = pastedPath(text);
  if (!path) return null;
  try {
    const file = await open(path, 'r');
    try {
      if (!(await file.stat()).isFile()) return null;
      const header = Buffer.alloc(12);
      await file.read(header, 0, header.length, 0);
      return header.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])) || header[0] === 255 && header[1] === 216 && header[2] === 255 || header.toString('ascii', 0, 4) === 'RIFF' && header.toString('ascii', 8, 12) === 'WEBP' ? path : null;
    } finally {await file.close();}
  } catch {return null;}
}

export function replaceSpan(value: Editor, start: number, end: number, text: string): Editor {
  const before = graphemes(value.text.slice(0, start)).length;
  const removed = graphemes(value.text.slice(start, end)).length;
  const added = graphemes(text).length;
  return {text: value.text.slice(0, start) + text + value.text.slice(end), cursor: value.cursor <= before ? value.cursor : value.cursor >= before + removed ? value.cursor + added - removed : before + added};
}

export function editImageDraft(value: Editor, input: string, key: Partial<Key>, images: DraftImage[], width = 80): Editor {
  for (const {label} of images) {
    const index = value.text.indexOf(label);
    if (index < 0) continue;
    const start = graphemes(value.text.slice(0, index)).length, end = start + label.length;
    if (key.backspace && value.cursor > start && value.cursor <= end || key.delete && value.cursor >= start && value.cursor < end) return replaceSpan(value, index, index + label.length, '');
    if (key.leftArrow && value.cursor > start && value.cursor <= end) return {...value, cursor: start};
    if (key.rightArrow && value.cursor >= start && value.cursor < end) return {...value, cursor: end};
  }
  return edit(value, input, key, width);
}
