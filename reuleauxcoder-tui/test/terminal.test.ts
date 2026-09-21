import test from 'node:test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {fileURLToPath} from 'node:url';
import {python} from './helpers.js';

test('launcher owns a real PTY and restores the terminal on exit', {skip: process.platform === 'win32'}, async () => {
  await promisify(execFile)(python, [fileURLToPath(new URL('./terminal_smoke.py', import.meta.url)), process.execPath], {timeout: 30_000});
});

test('--no-mouse preserves native selection and terminal restoration', {skip: process.platform === 'win32'}, async () => {
  await promisify(execFile)(python, [fileURLToPath(new URL('./terminal_smoke.py', import.meta.url)), process.execPath], {
    timeout: 30_000, env: {...process.env, RCODER_TUI_TEST_NO_MOUSE: '1'},
  });
});

test('bundled TUI runs menus, chat and approvals without node_modules', {skip: process.platform === 'win32'}, async () => {
  await promisify(execFile)(python, [
    fileURLToPath(new URL('./terminal_smoke.py', import.meta.url)), process.execPath,
    fileURLToPath(new URL('../../reuleauxcoder/_tui/cli.mjs', import.meta.url)),
  ], {timeout: 30_000});
});
