import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';
import {WorkspaceSession} from '../src/core/session.js';

export const python = process.env.RCODER_TEST_PYTHON ?? fileURLToPath(new URL(process.platform === 'win32' ? '../../.venv/Scripts/python.exe' : '../../.venv/bin/python', import.meta.url));
export const backendScript = fileURLToPath(new URL('./backend.py', import.meta.url));
export async function until(predicate: () => unknown | Promise<unknown>, timeout = 5000): Promise<void> {
  const deadline = Date.now() + timeout;
  while (!await predicate()) {if (Date.now() >= deadline) throw new Error('Condition timed out.'); await delay(10);}
}
export async function backend() {
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder-vscode-'));
  await writeFile(join(cwd, 'example.py'), 'old = 1\n', 'utf8');
  const session = new WorkspaceSession(cwd, 'Test host');
  let log = ''; session.on('log', text => {log += text;});
  try {await session.start({cwd, commands: [{command: python, args: [backendScript]}]});}
  catch (error) {session.dispose(); throw new Error(String(error) + '\n' + log);}
  return {session, client: session.requireClient(), cwd, async close() {await session.client?.interrupt(); await session.shutdown(); session.dispose(); await rm(cwd, {recursive: true, force: true});}};
}
