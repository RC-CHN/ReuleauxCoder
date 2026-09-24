import {downloadAndUnzipVSCode} from '@vscode/test-electron';
import {spawn} from 'node:child_process';
import {build} from 'esbuild';
import {mkdtemp, mkdir, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
const root = resolve('..');
const temporary = await mkdtemp(join(tmpdir(), 'rcoder-extension-test-'));
const workspace = join(temporary, 'workspace 中文 with spaces');
await mkdir(join(workspace, '.vscode'), {recursive: true});
await writeFile(join(workspace, 'example.py'), 'old = 1\n');
await writeFile(join(workspace, '.vscode', 'settings.json'), JSON.stringify({
  'reuleaux.autoStart': false,
  'reuleaux.corePath': process.env.RCODER_TEST_PYTHON ?? join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
  'reuleaux.coreArguments': [resolve('test/backend.py')],
  'reuleaux.openDiffAutomatically': true,
}));
await build({entryPoints: ['test/extension/index.ts'], bundle: true, platform: 'node', format: 'cjs', target: 'node20', external: ['vscode'], outfile: 'dist/test/extension.cjs'});
try {
  const executable = process.env.VSCODE_EXECUTABLE_PATH ?? await downloadAndUnzipVSCode({version: process.env.VSCODE_TEST_VERSION ?? '1.106.0'});
  // Launch the executable directly: test-electron's shell wrapper splits paths on Windows.
  const args = [workspace, '--user-data-dir', join(temporary, 'user'), '--extensions-dir', join(temporary, 'extensions'), '--disable-extensions', '--disable-updates', '--disable-workspace-trust', '--skip-welcome', '--skip-release-notes', '--no-sandbox', '--disable-gpu-sandbox', '--disable-gpu', `--extensionDevelopmentPath=${resolve('.')}`, `--extensionTestsPath=${resolve('dist/test/extension.cjs')}`, ...(process.env.VSCODE_HEADLESS ? ['--ozone-platform=headless'] : [])];
  await new Promise((resolve, reject) => {
    const child = spawn(executable, args, {stdio: 'inherit', shell: false});
    child.once('error', reject);
    child.once('close', (code, signal) => code === 0 ? resolve() : reject(new Error(`VS Code tests exited with ${signal ?? code}`)));
  });
} finally {await rm(temporary, {recursive: true, force: true, maxRetries: 5, retryDelay: 100});}
