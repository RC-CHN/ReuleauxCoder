import assert from 'node:assert/strict';
import test from 'node:test';
import {markdown, safe} from '../src/ui/format.js';
import stringWidth from 'string-width';

test('automatic links render once without reparsing their own labels', () => {
  for (const text of ['http://192.0.2.1:8080/status', 'https://example.com', 'www.example.com', 'user@example.com']) {
    assert.equal(safe(markdown(text)), text);
  }
  assert.equal(safe(markdown('<https://example.com>')), 'https://example.com');
});

test('parsed inline tokens preserve formatted links, references, lists and tables', () => {
  const text = '## **Checks**\n\n- [**Service**][service]\n- *Status*: http://192.0.2.1/status\n\n'
    + '| Field | Value |\n| --- | --- |\n| URL | https://example.com |\n\n[service]: https://example.com/service';
  const rendered = markdown(text);
  assert.match(rendered, /\x1b\[1mService\x1b\[22m/);
  assert.match(safe(rendered), /• Service \(https:\/\/example.com\/service\)/);
  assert.match(safe(rendered), /• Status: http:\/\/192.0.2.1\/status/);
  assert.match(safe(rendered), /URL\s+│ https:\/\/example.com/);
});

test('table columns wrap Unicode and styled cells without moving separators, and honor alignment', () => {
  const source = '| **Name** | Value |\n| :--- | ---: |\n| 中文👩🏽‍💻中文👩🏽‍💻中文 | `42` |\n| escaped \\| pipe | 7 |';
  const lines = safe(markdown(source, 33, true)).split('\n');
  assert(lines.every(line => stringWidth(line) <= 33));
  const rows = lines.filter(line => line.includes(' │ '));
  assert(rows.every(line => stringWidth(line.split(' │ ')[0]) === 15));
  assert(rows.some(line => line.endsWith('42')));
  assert(rows.some(line => line.includes('escaped | pipe')));
  assert.match(markdown(source, 33, true), /\x1b\[1mName/);
});

test('long table cells fold with an omission count and expand without losing their tail', () => {
  const text = '| Field | Description |\n| --- | --- |\n| name | ' + 'detail '.repeat(100) + 'FINAL |';
  const compact = safe(markdown(text, 45));
  assert.match(compact, /… \+\d+/);
  assert(!compact.includes('FINAL'));
  const full = safe(markdown(text, 45, true));
  assert(full.includes('FINAL'));
  assert(!full.includes('… +'));
  assert(compact.split('\n').length < full.split('\n').length);
  for (const width of [12, 25, 80]) {
    const rendered = safe(markdown(text, width, true));
    assert(rendered.split('\n').every(line => stringWidth(line) <= width));
  }
  const narrow = safe(markdown(text, 12));
  assert(narrow.includes('Field:'));
  assert(narrow.includes('Description:'));
  assert(!narrow.includes(' │ '));
  const header = '| ' + 'Header '.repeat(40) + 'LAST |\n| --- |\n';
  assert(!safe(markdown(header, 20)).includes('LAST'));
  assert(safe(markdown(header, 20, true)).includes('LAST'));
  assert(!safe(markdown('| Key | Value |\n| --- | --- |\n| line | one<br>two |', 40)).includes('<br>'));
});
