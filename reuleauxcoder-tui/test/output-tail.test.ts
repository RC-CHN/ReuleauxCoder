import assert from 'node:assert/strict';
import test from 'node:test';
import {OutputTail} from '../src/state/output-tail.js';
import {SessionStore} from '../src/state/session.js';
import {decode, record} from '../src/protocol/wire.js';
import {safe} from '../src/ui/format.js';
import {toolGroupRows} from '../src/ui/tool-groups.js';

test('live previews retain the last three lines across fragments and trailing whitespace', () => {
  const tail = new OutputTail();
  let text = '';
  for (const chunk of ['one\ntwo\nthree\nfour', '\n\n \t', 'five\n', '中文🙂', '\n', 'next']) {
    text += chunk; tail.append(chunk);
    assert.equal(tail.text, safe(text).trimEnd().split('\n').slice(-3).join('\n'));
  }
  const previous = tail.text;
  tail.append(' \n'.repeat(100_000));
  assert.equal(tail.text, previous, 'trailing whitespace does not evict visible output');
  tail.append('last');
  assert.equal(tail.text, ' \n \nlast');
});

test('CSI and OSC controls split across events never enter the preview', () => {
  const text = 'old\n\x1b[31mred\x1b[0m\n\x1b]0;hidden\nwindow title\x07中文\n\x1b]8;;https://example.test\x1b\\link\x1b]8;;\x1b\\';
  for (const size of [1, 2, 7, text.length]) {
    const tail = new OutputTail();
    for (let offset = 0; offset < text.length; offset += size) tail.append(text.slice(offset, offset + size));
    assert.equal(tail.text, safe(text).trimEnd().split('\n').slice(-3).join('\n'));
  }
});

test('very long lines and control sequences keep bounded preview state', () => {
  const tail = new OutputTail();
  tail.append('🙂'.repeat(100_000) + 'latest');
  assert(tail.text.length <= 8192);
  assert(tail.text.isWellFormed());
  assert(tail.text.endsWith('latest'));
  tail.append('\x1b]0;' + 'hidden'.repeat(100_000));
  tail.append('\x1b\\\nfinished');
  assert(tail.text.length <= 8192);
  assert(!tail.text.includes('hidden'));
  assert(tail.text.endsWith('finished'));
});

test('rendering and resizing a streaming tool only use its preview, preserving full output', () => {
  const session = new SessionStore();
  const event = (name: string, fields: any) => session.runtime({payload: decode(record(name, fields))});
  event('ToolCallStarted', {tool_call_id: 'tool', tool_name: 'shell', arguments: {command: 'build'}});
  const body = 'old output\n'.repeat(100_000) + 'last one\nlast two\nlast three\n';
  event('ToolOutputDelta', {tool_call_id: 'tool', text: body});
  const cell = session.cells[0], tail = cell.outputTail!;
  assert.equal(cell.body, body);
  assert.equal(tail.scannedChars, body.length);
  assert.equal(tail.text, 'last one\nlast two\nlast three');
  // Accessing the full output from the folding path is a regression, regardless of timing.
  Object.defineProperty(cell, 'body', {configurable: true, get() {throw new Error('full output read by preview');}});
  for (const width of [30, 80, 120]) {
    assert(safe(toolGroupRows([cell], width).join('\n')).includes('last three'));
  }
  Object.defineProperty(cell, 'body', {configurable: true, writable: true, value: body});
  event('ToolOutputDelta', {tool_call_id: 'tool', text: 'new line'});
  assert.equal(tail.scannedChars, body.length + 'new line'.length);
  assert.equal(cell.body, body + 'new line');
  event('ToolCallFinished', {tool_call_id: 'tool', tool_name: 'shell', outcome: {status: 'succeeded', stdout: body}});
  assert.equal(cell.outputTail, undefined);
  assert(cell.body.includes(body));
  assert(cell.details.includes('old output'));
});
