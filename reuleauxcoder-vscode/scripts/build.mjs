import {build} from 'esbuild';
import {mkdir, copyFile} from 'node:fs/promises';
await mkdir('dist', {recursive: true});
await Promise.all([
  build({entryPoints: ['src/extension.ts'], bundle: true, platform: 'node', target: 'node20', format: 'cjs', external: ['vscode'], outfile: 'dist/extension.cjs', sourcemap: true}),
  build({entryPoints: ['src/webview/main.ts'], bundle: true, platform: 'browser', target: 'es2022', outfile: 'dist/webview.js', sourcemap: true}),
  copyFile('src/webview/style.css', 'dist/webview.css'),
  copyFile('../LICENSE', 'LICENSE'),
]);
