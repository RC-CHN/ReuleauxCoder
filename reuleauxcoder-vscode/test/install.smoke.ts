import assert from 'node:assert/strict';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {installCore, managedCommand} from '../src/core/install.js';
import {CoreRuntime} from '../src/core/runtime.js';
import {python} from './helpers.js';

const root = await mkdtemp(join(tmpdir(), 'rcoder-packaged-smoke-'));
const runtime = new CoreRuntime();
runtime.on('log', text => process.stdout.write(text));
try {
  const command = await installCore(root, resolve('dist/core'), python, text => process.stdout.write(text));
  assert.deepEqual(await managedCommand(root), command);
  const config = join(root, 'config.yaml');
  await writeFile(config, 'models:\n  profiles:\n    test:\n      model: test-model\n      api_key: test-only-key\n      base_url: http://127.0.0.1:1/v1\nlsp:\n  enabled: false\n');
  const client = await runtime.start({cwd: root, commands: [{...command, args: [...command.args, '--config', config]}]});
  assert(client.info.editor_documents && client.info.review_documents && client.info.submission_ids);
  await runtime.shutdown();
  console.log('PASS installed packaged core initializes editor protocol and shuts down cleanly');
} finally {runtime.dispose(); await rm(root, {recursive: true, force: true});}
