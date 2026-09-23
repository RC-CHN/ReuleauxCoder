/** Small host-neutral event source shared by the RPC peer and client. */
type Listener = (...args: any[]) => void;
export class Events {
  private listeners = new Map<string | symbol, {fn: Listener; once: boolean}[]>();
  on(name: string | symbol, fn: Listener): this {return this.add(name, fn, false);}
  once(name: string | symbol, fn: Listener): this {return this.add(name, fn, true);}
  private add(name: string | symbol, fn: Listener, once: boolean): this {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), {fn, once}]);
    return this;
  }
  off(name: string | symbol, fn: Listener): this {
    const remaining = (this.listeners.get(name) ?? []).filter(item => item.fn !== fn);
    if (remaining.length) this.listeners.set(name, remaining);
    else this.listeners.delete(name);
    return this;
  }
  removeListener(name: string | symbol, fn: Listener): this {return this.off(name, fn);}
  emit(name: string | symbol, ...args: any[]): boolean {
    const listeners = this.listeners.get(name) ?? [];
    for (const item of [...listeners]) {
      if (item.once) this.off(name, item.fn);
      item.fn(...args);
    }
    return listeners.length > 0;
  }
}
