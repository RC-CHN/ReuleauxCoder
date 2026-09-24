import assert from 'node:assert/strict';
import test from 'node:test';
import {fileReference, fileReferences} from '../src/file-links.js';

test('workspace references preserve relative, encoded and Windows paths with locations', () => {
  assert.deepEqual(fileReference('AGENT.md'), {path: 'AGENT.md'});
  assert.deepEqual(fileReference('docs/guide/setup.md#L12-L18'), {path: 'docs/guide/setup.md', line: 12, column: 1, endLine: 18});
  assert.deepEqual(fileReference('src/main.ts:42:3'), {path: 'src/main.ts', line: 42, column: 3, endLine: 42});
  assert.deepEqual(fileReference('C:\\work\\src\\main.ts:8'), {path: 'C:\\work\\src\\main.ts', line: 8, column: 1, endLine: 8});
  assert.deepEqual(fileReference('docs/My%20Notes%20%E4%B8%AD%E6%96%87.md#heading'), {path: 'docs/My Notes 中文.md'});
  assert.deepEqual(fileReference('Dockerfile'), {path: 'Dockerfile'});
  assert.deepEqual(fileReference('.github/workflows/ci.yml'), {path: '.github/workflows/ci.yml'});
  assert.deepEqual([...('AGENT.md、README_CN.md ✓ docs/nested/readme.md:12'.matchAll(fileReferences))].map(match => match[0]), ['AGENT.md', 'README_CN.md', 'docs/nested/readme.md:12']);
  assert.deepEqual([...('See README.md. Keep unknown.md.backup unchanged.'.matchAll(fileReferences))].map(match => match[0]), ['README.md']);
});
test('file references reject URL schemes, control characters and invalid positions', () => {
  for (const value of ['https://agent.md/', 'http://example.com/file.py', 'command:open.md', 'javascript:alert.md', 'file:///tmp/a.md', 'command%3Aopen.md', 'a%00.md', 'a.md:0', 'a.md#L4-L2', 'a.md:999999999999999999', 'v0.9.3']) assert.equal(fileReference(value), undefined, value);
});
