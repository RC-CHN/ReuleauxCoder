import {FRAME_MS} from '../ui/motion.js';

export interface SelectionViewport {offset: number; height: number; total: number; perItem: number; follow: boolean}

/** Retarget a short scroll; wheel bursts accumulate distance, never queued animations. */
export class ScrollMotion {
  private timer?: ReturnType<typeof setInterval>;
  private scope?: object;
  private target = 0;

  constructor(private readonly changed: () => void) {}

  move(scope: object, delta: number, read: () => number, write: (row: number) => void, current: () => boolean, maximum = Infinity) {
    const from = read();
    const continuing = this.timer && this.scope === scope && Math.sign(this.target - from) === Math.sign(delta);
    const target = Math.max(0, Math.min(maximum, (continuing ? this.target : from) + delta));
    this.cancel();
    if (from === target) return;
    this.scope = scope; this.target = target;
    const started = performance.now();
    // A key or wheel event moves the first row immediately; easing handles the rest.
    let last = from + Math.sign(target - from);
    write(last); this.changed();
    if (last === target) return;
    this.timer = setInterval(() => {
      // Navigation, resize and layout corrections invalidate the old row coordinates.
      if (!current() || read() !== last) {this.cancel(); return;}
      const progress = Math.min(1, (performance.now() - started) / 120);
      const row = Math.round(from + (target - from) * (1 - (1 - progress) ** 3));
      if (row !== last) {last = row; write(row); this.changed();}
      if (progress === 1) this.cancel();
    }, FRAME_MS);
  }

  cancel() {clearInterval(this.timer); this.timer = undefined; this.scope = undefined;}
}
