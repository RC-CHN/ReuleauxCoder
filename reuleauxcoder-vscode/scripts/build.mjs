import {build} from 'esbuild';
import {mkdir, copyFile, readFile, readdir, writeFile} from 'node:fs/promises';
await mkdir('dist', {recursive: true});
const [, webview] = await Promise.all([
  build({entryPoints: ['src/extension.ts'], bundle: true, platform: 'node', target: 'node20', format: 'cjs', external: ['vscode'], outfile: 'dist/extension.cjs', sourcemap: true}),
  build({entryPoints: ['src/webview/main.ts'], bundle: true, platform: 'browser', target: 'es2022', outfile: 'dist/webview.js', sourcemap: true, metafile: true}),
  copyFile('src/webview/style.css', 'dist/webview.css'),
  copyFile('../LICENSE', 'LICENSE'),
]);
const packages = new Set(Object.keys(webview.metafile.inputs).map(path => path.match(/^node_modules\/(?:@[^/]+\/)?[^/]+/)?.[0]).filter(Boolean));
const notices = [];
for (const path of [...packages].sort()) {
  const manifest = JSON.parse(await readFile(`${path}/package.json`, 'utf8'));
  const license = (await readdir(path)).find(file => /^licen[sc]e(?:[.-]|$)/i.test(file));
  if (!license) throw new Error(`Missing bundled dependency license: ${manifest.name}`);
  notices.push(`${manifest.name} ${manifest.version}\n\n${await readFile(`${path}/${license}`, 'utf8')}`);
}
await writeFile('dist/THIRD_PARTY_NOTICES.txt', notices.join('\n\n---\n\n'));
