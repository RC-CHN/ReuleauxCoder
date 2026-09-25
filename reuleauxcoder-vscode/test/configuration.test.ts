import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, mkdir, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import test from 'node:test';
import {ConfigurationClient} from '@reuleauxcoder/client';
import {RpcPeer} from '@reuleauxcoder/client/node';
import {python} from './helpers.js';

test('shared read-only client checks unsaved YAML and detects disk conflicts without creating state', {timeout: 30000}, async t => {
  const root = await mkdtemp(join(tmpdir(), 'rcoder config 中文-'));
  const home = join(root, 'home'), file = join(root, '.rcoder', 'config.yaml');
  await mkdir(join(root, '.rcoder')); await writeFile(file, 'app: [broken');
  const child = spawn(python, ['-m', 'reuleauxcoder', 'config', 'rpc', '--workspace', root], {
    cwd: root, env: {...process.env, HOME: home, USERPROFILE: home}, stdio: 'pipe', windowsHide: true,
  });
  let errors = ''; child.stderr.on('data', chunk => {errors += chunk.toString();});
  const closed = new Promise<number | null>(resolve => child.once('close', resolve));
  const peer = new RpcPeer(child.stdout, child.stdin); child.once('error', error => peer.close(error));
  t.after(async () => {peer.close(); child.kill(); await closed; await rm(root, {recursive: true, force: true});});
  const client = new ConfigurationClient(peer);
  assert.equal((await client.initialize()).api_version, 2);
  assert.deepEqual((await client.describe()).operations, ['describe', 'inspect', 'check']);
  const before = await client.inspect(); assert.equal(before.valid, false);
  assert.equal(before.diagnostics[0].code, 'invalid_yaml');
  const content = '# 中文项目\r\napp:\r\n  api_key: private-sdk-key\r\n';
  const result = await client.check({documents: [{scope: 'workspace', content}], base_revision: before.revision});
  assert.equal(result.valid, true); assert.equal(result.buffer_check, true);
  assert.equal(await readFile(file, 'utf8'), 'app: [broken');
  assert(!JSON.stringify(result).includes('private-sdk-key'));
  await writeFile(file, content);
  assert.equal((await client.inspect()).valid, true);
  assert.equal((await client.inspect()).runtime, null);
  await assert.rejects(client.check({base_revision: before.revision}), /changed on disk/);
  for (const op of ['prepare', 'apply', 'validate', 'history', 'revert', 'recover', 'editor_documents']) {
    await assert.rejects(peer.request('config.' + op), /[Mm]ethod/);
  }
  assert.equal(await readFile(file, 'utf8'), content);
  peer.close(); assert.equal(await closed, 0, errors);
});
