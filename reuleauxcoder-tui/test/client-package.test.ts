import assert from 'node:assert/strict';
import test from 'node:test';
import {cp, mkdir, mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {build} from 'esbuild';

const exec = promisify(execFile);

test('built client works in an isolated consumer without TUI or Node browser types', async t => {
  const root = await mkdtemp(join(tmpdir(), 'rcoder-client-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const pkg = join(root, 'node_modules/@reuleauxcoder/client');
  await mkdir(pkg, {recursive: true});
  await cp(resolve('../reuleauxcoder-client/package.json'), join(pkg, 'package.json'));
  await cp(resolve('../reuleauxcoder-client/dist'), join(pkg, 'dist'), {recursive: true});
  await writeFile(join(root, 'package.json'), JSON.stringify({type: 'module'}));
  await writeFile(join(root, 'consumer.mjs'), `
    import assert from 'node:assert/strict';
    import {PassThrough} from 'node:stream';
    import {RuntimeClient, MessagePeer, RpcError} from '@reuleauxcoder/client';
    import {RpcPeer, RpcError as NodeRpcError, attachImageFile} from '@reuleauxcoder/client/node';
    assert.equal(RpcError, NodeRpcError);
    const peer = new RpcPeer(new PassThrough(), new PassThrough());
    assert(peer instanceof MessagePeer);
    const client = new RuntimeClient(peer);
    assert.equal(typeof attachImageFile, 'function');
    client.close();
    assert(peer.closed);
  `);
  await exec(process.execPath, ['consumer.mjs'], {cwd: root});
  await writeFile(join(root, 'browser.ts'), `
    import {RuntimeClient, MessagePeer, type UIProfile, type PendingInteraction} from '@reuleauxcoder/client';
    const profile: UIProfile = {ui_id: 'browser', display_name: 'Browser', capabilities: ['text_input']};
    const client = new RuntimeClient(new MessagePeer(message => globalThis.postMessage(message)));
    const interactions: PendingInteraction[] = client.interactions;
    void client.initialize(profile);
    void interactions;
  `);
  await writeFile(join(root, 'tsconfig.json'), JSON.stringify({
    compilerOptions: {target: 'ES2023', module: 'NodeNext', moduleResolution: 'NodeNext', strict: true, noEmit: true, types: [], lib: ['ES2023', 'DOM']},
    include: ['browser.ts'],
  }));
  await exec(process.execPath, [resolve('node_modules/typescript/bin/tsc'), '-p', join(root, 'tsconfig.json')], {cwd: root});
});

test('the Node adapter entry cannot be bundled into a browser frontend', async () => {
  await assert.rejects(build({
    stdin: {contents: `export * from '@reuleauxcoder/client/node';`, resolveDir: resolve('.')},
    bundle: true, platform: 'browser', write: false, logLevel: 'silent',
  }), /Could not resolve "@reuleauxcoder\/client\/node"/);
});
