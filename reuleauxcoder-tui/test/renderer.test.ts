import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp, mkdir, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import Output, {OutputCaches} from '../node_modules/ink/build/output.js';
import Original from '../node_modules/ink/build/output.rcoder-original.js';
import {BoundedCache} from '../renderer/cache.js';
import {patchRenderer} from '../scripts/patch-renderer.mjs';
import {styledCharsFromTokens, styledCharsToString, tokenize} from '@alcalzone/ansi-tokenize';
import {serializeStyledChars} from '../renderer/serialize.js';

const strings = ['', 'plain', '中文 👩🏽‍💻 é', '\x1b[31mred', '\x1b[1;2mstrong dim\x1b[22m normal', '\x1b[38;2;12;34;56mRGB\x1b[48;5;21m背景\x1b[0m', '\x1b]8;;https://example.com\x07link\x1b]8;;\x07'];

test('style fast path preserves exact upstream transitions and final closures', () => {
  const codes = ['', '\x1b[1m', '\x1b[2m', '\x1b[1;2m', '\x1b[22m', '\x1b[31m', '\x1b[39m', '\x1b[48;5;123m', '\x1b[38;2;1;2;3m', '\x1b[7m', '\x1b[0m', '\x1b]8;;https://example.com\x1b\\', '\x1b]8;;\x1b\\'];
  for (const left of codes) for (const right of codes) for (const body of strings) {
    const chars = styledCharsFromTokens(tokenize(`${left}same style 中文${right}${body}tail`));
    assert.equal(serializeStyledChars(chars), styledCharsToString(chars));
    // Reconstructed cells need value equality, not array/object identity.
    const copies = chars.map(char => ({...char, styles: char.styles.map(style => ({...style}))}));
    assert.equal(serializeStyledChars(copies), styledCharsToString(chars));
  }
  assert.equal(serializeStyledChars([]), '');
});

test('renderer installation is repeatable and rejects modified or upgraded upstream files', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'rcoder-renderer-'));
  t.after(() => rm(directory, {recursive: true, force: true}));
  const ink = join(directory, 'node_modules/ink');
  await mkdir(join(ink, 'build'), {recursive: true});
  await writeFile(join(ink, 'package.json'), JSON.stringify({version: '7.1.1'}));
  for (const name of ['output', 'renderer']) {
    await writeFile(join(ink, `build/${name}.js`), await readFile(new URL(`../node_modules/ink/build/${name}.rcoder-original.js`, import.meta.url)));
  }
  await patchRenderer(directory);
  const first = await readFile(join(ink, 'build/output.js'), 'utf8');
  await patchRenderer(directory);
  assert.equal(await readFile(join(ink, 'build/output.js'), 'utf8'), first);
  await writeFile(join(ink, 'build/output.rcoder-original.js'), 'modified');
  await assert.rejects(patchRenderer(directory), /refusing to patch/);
  assert.equal(await readFile(join(ink, 'build/output.js'), 'utf8'), first);
  await writeFile(join(ink, 'package.json'), JSON.stringify({version: '7.2.0'}));
  await assert.rejects(patchRenderer(directory), /Review renderer adapter/);
});

test('renderer caches stay bounded, refresh recency and bypass oversized payloads', () => {
  const cache = new BoundedCache(2, 10);
  cache.set('a', 0); cache.set('bb', 2);
  assert.equal(cache.get('a'), 0);
  cache.set('ccc', 3);
  assert.equal(cache.get('bb'), undefined);
  cache.set('a', 4);
  assert.equal(cache.weight, 6);
  cache.set('too large to retain', 1);
  assert.equal(cache.size, 2);
  for (let i = 0; i < 1000; i++) cache.set(String(i), i);
  assert(cache.weight <= 10 && cache.size <= 2);
});

test('unchanged composed rows are reused while width, placement, write order and transforms invalidate them', () => {
  const caches = new OutputCaches();
  let parsed = 0, value = 'A';
  const originalParse = caches.getStyledChars.bind(caches);
  caches.getStyledChars = (text: string) => {parsed++; return originalParse(text);};
  const draw = (width = 20, x = 0, reversed = false) => {
    const options = {width, height: 3};
    const actual = new Output({...options, caches}), expected = new Original(options);
    const writes = [[x, 0, '中文 styled'], [x + 1, 0, 'over'], [0, 1, 'same'], [0, 2, 'dynamic']] as const;
    for (const [left, top, text] of reversed ? [...writes].reverse() : writes) {
      const transform = {transformers: text === 'dynamic' ? [(line: string) => line + value] : []};
      actual.write(left, top, text, transform); expected.write(left, top, text, transform);
    }
    const result = actual.get();
    assert.deepEqual(result, expected.get());
    return result;
  };
  draw(); assert.equal(parsed, 4);
  draw(); assert.equal(parsed, 4, 'cache hit bypasses cell composition, not only parsing');
  value = 'B'; draw(); assert.equal(parsed, 5, 'only the transformed row changed');
  draw(21); assert.equal(parsed, 9, 'width affects the initial grid');
  draw(21, 2); assert.equal(parsed, 11, 'placement changes only the overlaid row');
  draw(21, 2, true); assert.equal(parsed, 13, 'paint order is significant');
  for (let i = 0; i < 700; i++) {value = String(i); draw();}
  assert(caches.rows.size <= 256 && caches.rows.weight <= caches.rows.maxWeight);
});

test('cross-frame caches preserve upstream bytes for Unicode, styles, clipping and overlapping writes', () => {
  const caches = new OutputCaches();
  let seed = 17;
  const random = (n: number) => {seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed % n;};
  for (let frame = 0; frame < 1000; frame++) {
    const dimensions = {width: 3 + random(28), height: 1 + random(8)};
    const actual = new Output({...dimensions, caches});
    const expected = new Original(dimensions);
    for (let i = 0; i < 12; i++) {
      const clip = {x1: random(3), x2: dimensions.width - random(3), y1: 0, y2: dimensions.height};
      if (i % 3 === 0) {actual.clip(clip); expected.clip(clip);}
      const x = random(dimensions.width), y = random(dimensions.height);
      const text = strings[random(strings.length)] + (i % 4 === 0 ? '\nnext 中文' : '');
      const options = {transformers: i % 5 === 0 ? [(line: string, index: number) => `\x1b[7m${index}:${line}\x1b[27m`] : []};
      actual.write(x, y, text, options); expected.write(x, y, text, options);
      if (i % 3 === 0) {actual.unclip(); expected.unclip();}
    }
    assert.deepEqual(actual.get(), expected.get(), `frame ${frame}`);
  }
  const parsed = caches.getStyledChars('reused 中文');
  assert.strictEqual(caches.getStyledChars('reused 中文'), parsed);
  assert(caches.styledChars.weight <= caches.styledChars.maxWeight);
  assert(caches.widths.weight <= caches.widths.maxWeight);
  assert(caches.blockWidths.weight <= caches.blockWidths.maxWeight);
  assert(caches.rows.weight <= caches.rows.maxWeight);
  assert.notStrictEqual(new OutputCaches().getStyledChars('reused 中文'), parsed, 'separate terminals do not share retained cells');
  for (let i = 0; i < 800; i++) caches.getStyledChars(`\x1b[31m${i}:` + 'x'.repeat(80));
  const oversized = 'x'.repeat(70000);
  caches.getStyledChars(oversized);
  assert.equal(caches.styledChars.get(oversized), undefined);
  assert(caches.styledChars.size <= 512 && caches.styledChars.weight <= caches.styledChars.maxWeight);
});
