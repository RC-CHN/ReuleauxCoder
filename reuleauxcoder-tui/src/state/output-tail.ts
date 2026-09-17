import {safe} from '../ui/format.js';

const MAX_CHARS = 8192;

/** A bounded, sanitized live preview; the cell retains the complete raw output. */
export class OutputTail {
  private tail = '';
  private whitespace = '';
  private control: 'text' | 'escape' | 'csi' | 'osc' | 'osc-escape' = 'text';
  scannedChars = 0;
  get text() {return this.tail;}

  append(chunk: string) {
    this.scannedChars += chunk.length;
    const clean = this.clean(chunk), visible = clean.trimEnd();
    if (!visible) {
      this.whitespace = suffix(this.whitespace + clean);
      return;
    }
    let tail = suffix(this.tail + this.whitespace + visible);
    let start = tail.length;
    for (let count = 0; count < 3; count++) {
      start = tail.lastIndexOf('\n', start - 1);
      if (start < 0) break;
    }
    if (start >= 0) tail = tail.slice(start + 1);
    this.tail = tail;
    this.whitespace = suffix(clean.slice(visible.length));
  }

  private clean(chunk: string): string {
    const parts: string[] = [];
    let offset = 0;
    while (offset < chunk.length) {
      if (this.control === 'text') {
        const escape = chunk.indexOf('\x1b', offset);
        parts.push(safe(chunk.slice(offset, escape < 0 ? chunk.length : escape)));
        if (escape < 0) break;
        this.control = 'escape'; offset = escape + 1;
      } else if (this.control === 'escape') {
        if (chunk[offset] === '[') {this.control = 'csi'; offset++;}
        else if (chunk[offset] === ']') {this.control = 'osc'; offset++;}
        else this.control = 'text';
      } else if (this.control === 'csi') {
        const code = chunk.charCodeAt(offset);
        if (code >= 0x40 && code <= 0x7e) {this.control = 'text'; offset++;}
        else if (code >= 0x20 && code <= 0x3f) offset++;
        else this.control = 'text';
      } else if (this.control === 'osc') {
        const bell = chunk.indexOf('\x07', offset), escape = chunk.indexOf('\x1b', offset);
        if (bell >= 0 && (escape < 0 || bell < escape)) {this.control = 'text'; offset = bell + 1;}
        else if (escape >= 0) {this.control = 'osc-escape'; offset = escape + 1;}
        else break;
      } else {
        if (chunk[offset] === '\\') {this.control = 'text'; offset++;}
        else this.control = 'osc';
      }
    }
    return parts.join('');
  }
}

function suffix(text: string): string {
  let start = Math.max(0, text.length - MAX_CHARS);
  if (start && /[\uDC00-\uDFFF]/.test(text[start]) && /[\uD800-\uDBFF]/.test(text[start - 1])) start++;
  return text.slice(start);
}
