import {runTests} from '@vscode/test-electron';
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
  await runTests({
    vscodeExecutablePath: process.env.VSCODE_EXECUTABLE_PATH,
    version: process.env.VSCODE_TEST_VERSION ?? '1.106.0',
    extensionDevelopmentPath: resolve('.'), extensionTestsPath: resolve('dist/test/extension.cjs'),
    launchArgs: [workspace, '--user-data-dir', join(temporary, 'user'), '--extensions-dir', join(temporary, 'extensions'), '--disable-extensions', '--disable-workspace-trust', '--skip-welcome', '--skip-release-notes', '--no-sandbox', '--disable-gpu', ...(process.env.VSCODE_HEADLESS ? ['--ozone-platform=headless'] : [])],
  });
} finally {await rm(temporary, {recursive: true, force: true});}
