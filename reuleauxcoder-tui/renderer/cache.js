/** Weighted LRU: oversized entries are computed normally but never retained. */
export class BoundedCache {
  entries = new Map();
  weight = 0;
  constructor(maxEntries, maxWeight, weigh = key => key.length + 1) {
    this.maxEntries = maxEntries;
    this.maxWeight = maxWeight;
    this.weigh = weigh;
  }
  get size() {return this.entries.size;}
  get(key) {
    const entry = this.entries.get(key);
    if (entry === undefined) return undefined;
    this.entries.delete(key);
    this.entries.set(key, entry);
    return entry.value;
  }
  set(key, value) {
    const previous = this.entries.get(key);
    if (previous) {this.weight -= previous.weight; this.entries.delete(key);}
    const weight = this.weigh(key, value);
    if (weight > this.maxWeight) return;
    this.entries.set(key, {value, weight}); this.weight += weight;
    while (this.size > this.maxEntries || this.weight > this.maxWeight) {
      const oldest = this.entries.keys().next().value;
      this.weight -= this.entries.get(oldest).weight;
      this.entries.delete(oldest);
    }
  }
}

// Accounting units conservatively include character/style records; they are not
// a claim about V8 heap bytes. Both entry count and retained payload are bounded.
export function styledWeight(line, characters) {
  let weight = line.length;
  for (const char of characters) {
    weight += 32 + char.value.length;
    for (const style of char.styles) weight += 16 + style.code.length;
  }
  return weight;
}
