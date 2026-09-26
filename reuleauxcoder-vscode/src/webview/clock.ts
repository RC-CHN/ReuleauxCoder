import type {HostSnapshot} from '../shared.js';
import {shortDuration, t} from '../i18n.js';

interface Sample {seconds: number; at: number; running: boolean}

/** Advance display clocks locally without polling the core or redrawing panels. */
export class LiveClock {
  private hostTime = Date.now();
  private localTime = performance.now();
  private owner = '';
  private samples = new Map<string, Sample>();
  private timer?: number;

  constructor() {
    document.addEventListener('visibilitychange', () => this.schedule());
  }

  update(state: HostSnapshot): void {
    const owner = `${state.hostId}:${state.generation}`;
    if (owner !== this.owner) {this.samples.clear(); this.owner = owner;}
    if (state.sampledAt !== undefined) {this.hostTime = state.sampledAt; this.localTime = performance.now();}
    if (state.phase !== 'ready') {
      for (const sample of this.samples.values()) if (sample.running) {sample.seconds += Math.max(0, this.now() - sample.at) / 1000; sample.running = false;}
      this.schedule(); return;
    }
    const keys = new Set<string>();
    for (const process of state.overview?.processes ?? []) {
      const key = `process:${process.id}`; keys.add(key);
      this.sample(key, process.elapsed, process.state === 'running', process.observedAt);
    }
    const goal = state.overview?.goal;
    if (goal) {keys.add('goal'); this.sample('goal', goal.time_used_seconds, state.phase === 'ready' && goal.status === 'active', state.overview?.goalObservedAt);}
    for (const key of this.samples.keys()) if (!keys.has(key)) this.samples.delete(key);
    this.schedule();
  }

  private now(): number {return this.hostTime + performance.now() - this.localTime;}
  private sample(key: string, seconds: number, running: boolean, at?: number): void {
    const previous = this.samples.get(key);
    if (previous?.seconds === seconds && previous.running === running && (at === undefined || previous.at === at)) return;
    this.samples.set(key, {seconds, running, at: at ?? this.now()});
  }
  element(key: string, seconds = 0): HTMLElement {
    const node = document.createElement('span'); node.className = 'elapsed'; node.dataset.elapsedKey = key;
    node.textContent = this.format(key, seconds); node.title = t('Elapsed time'); return node;
  }
  private format(key: string, fallback = 0): string {
    const sample = this.samples.get(key);
    const seconds = Math.max(0, Math.floor(sample ? sample.seconds + (sample.running ? Math.max(0, this.now() - sample.at) / 1000 : 0) : fallback));
    if (seconds < 60) return shortDuration(seconds);
    const minutes = Math.floor(seconds / 60), tail = String(seconds % 60).padStart(2, '0');
    return minutes < 60 ? `${minutes}:${tail}` : `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${tail}`;
  }
  private tick(): void {
    for (const node of document.querySelectorAll<HTMLElement>('[data-elapsed-key]')) {
      if (!this.samples.has(node.dataset.elapsedKey!)) continue;
      const text = this.format(node.dataset.elapsedKey!);
      if (node.textContent !== text) node.textContent = text;
    }
  }
  private schedule(): void {
    const active = !document.hidden && [...this.samples.values()].some(sample => sample.running);
    if (active && this.timer === undefined) {this.tick(); this.timer = window.setInterval(() => this.tick(), 1000);}
    else if (!active && this.timer !== undefined) {clearInterval(this.timer); this.timer = undefined; this.tick();}
  }
}
