import {appendFile, mkdir, readFile} from 'node:fs/promises';
import {dirname} from 'node:path';

export class InputHistory {
  entries: string[] = [];
  private writes: Promise<void> = Promise.resolve();
  private lastSaved?: string;
  private needsNewline = false;
  constructor(readonly path?: string) {}
  async load(): Promise<string | undefined> {
    if (!this.path) return;
    let text: string;
    try {text = await readFile(this.path, 'utf8');}
    catch (error) {if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error;}
    this.needsNewline = !!text && !text.endsWith('\n');
    this.entries = [];
    let skipped = 0;
    for (const line of text.split('\n')) {
      if (!line) continue;
      try {
        const value: unknown = JSON.parse(line);
        if (typeof value === 'string') {this.entries.push(value); continue;}
      } catch { /* Keep the readable entries on either side. */ }
      skipped++;
    }
    this.lastSaved = this.entries.at(-1);
    if (skipped) return `Skipped ${skipped} unreadable input history record(s).`;
  }
  async add(text: string): Promise<void> {
    if (this.entries.at(-1) !== text) this.entries.push(text);
    if (!this.path) return;
    const path = this.path;
    const write = this.writes.then(async () => {
      if (text === this.lastSaved) return;
      await mkdir(dirname(path), {recursive: true});
      await appendFile(path, (this.needsNewline ? '\n' : '') + JSON.stringify(text) + '\n', {mode: 0o600});
      this.needsNewline = false;
      this.lastSaved = text;
    });
    this.writes = write.catch(() => {this.needsNewline = true;});
    await write;
  }
}
