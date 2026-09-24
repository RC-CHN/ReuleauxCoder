import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp, mkdir, readFile, rm, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {environmentPython, installCore, managedCommand, pythonCandidates, run} from '../src/core/install.js';

test('installer selects host Python paths and rejects a corrupt bundle without changing managed state', async t => {
  const storage = await mkdtemp(join(tmpdir(), 'rcoder-install-test-')); t.after(() => rm(storage, {recursive: true, force: true}));
  assert.deepEqual(pythonCandidates('', 'win32')[0], {command: 'py', args: ['-3']});
  assert.equal(environmentPython('/env', 'win32'), join('/env', 'Scripts', 'python.exe'));
  assert.deepEqual(pythonCandidates('/custom/python'), [{command: '/custom/python', args: []}]);
  assert.equal(await managedCommand(storage), undefined);
  await writeFile(join(storage, 'core.json'), JSON.stringify({python: process.execPath}));
  const old = await readFile(join(storage, 'core.json'), 'utf8');
  const bundle = join(storage, 'bundle'); await mkdir(bundle);
  await writeFile(join(bundle, 'core.whl'), 'corrupt');
  await writeFile(join(bundle, 'manifest.json'), JSON.stringify({wheel: 'core.whl', sha256: '0'.repeat(64)}));
  await assert.rejects(installCore(storage, bundle, '', () => {}), /checksum/);
  assert.equal(await readFile(join(storage, 'core.json'), 'utf8'), old);
  assert.equal((await managedCommand(storage))?.command, process.execPath);
});

test('installer cancellation waits for the child to exit and argv stays literal', async () => {
  const controller = new AbortController(); let output = '';
  const pending = run({command: process.execPath, args: []}, ['-e', 'console.log(process.argv[1]); setInterval(() => {}, 1000)', '$(literal); `argument`'], text => {output += text; controller.abort();}, controller.signal);
  await assert.rejects(pending, error => error instanceof Error && error.name === 'AbortError');
  assert(output.includes('$(literal); `argument`'));
});
