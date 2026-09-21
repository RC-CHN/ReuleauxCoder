// Build the self-contained frontend shipped in Python wheels and sdists.
import {build} from 'esbuild';
import {mkdir, readFile, readdir, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {patchRenderer} from './patch-renderer.mjs';

await patchRenderer();
const root = fileURLToPath(new URL('..', import.meta.url));
const destination = resolve(root, '../reuleauxcoder/_tui');
const pkg = JSON.parse(await readFile(resolve(root, 'package.json'), 'utf8'));
const nodeMajor = /^>=(\d+)$/.exec(pkg.engines.node)?.[1];
if (!nodeMajor) throw new Error('Expected a Node engine requirement of the form >=MAJOR');
await mkdir(destination, {recursive: true});
const result = await build({
  absWorkingDir: root,
  entryPoints: ['src/cli.tsx'],
  outfile: resolve(destination, 'cli.mjs'),
  bundle: true,
  platform: 'node',
  // Ink lazily imports its optional devtools; exclude that development module
  // so the installed production bundle has no external npm dependencies.
  plugins: [{name: 'no-devtools', setup(builder) {
    builder.onResolve({filter: /^\.\/devtools\.js$/}, args =>
      args.importer.replaceAll('\\', '/').endsWith('/ink/build/reconciler.js')
        ? {path: 'devtools', namespace: 'disabled-devtools'} : undefined);
    builder.onLoad({filter: /.*/, namespace: 'disabled-devtools'}, () => ({contents: 'export {};'}));
  }}],
  format: 'esm',
  target: `node${nodeMajor}`,
  define: {'process.env.NODE_ENV': '"production"', 'process.env.DEV': 'false'},
  banner: {js: 'import {createRequire as bundleRequire} from "node:module"; const require = bundleRequire(import.meta.url);'},
  minify: true,
  legalComments: 'eof',
  metafile: true,
});
await writeFile(resolve(destination, 'manifest.json'), JSON.stringify({node_major: Number(nodeMajor)}) + '\n');

// Include the license texts of bundled packages, alongside esbuild's legal comments.
const packages = new Set();
for (const input of Object.keys(result.metafile.inputs)) {
  const match = /^(.*node_modules\/(?:@[^/]+\/)?[^/]+)\//.exec(input);
  if (match) packages.add(resolve(root, match[1]));
}
let notices = '';
for (const directory of [...packages].sort()) {
  const metadata = JSON.parse(await readFile(resolve(directory, 'package.json'), 'utf8'));
  const files = (await readdir(directory)).filter(name => /^(license|licence|copying)(\.|$)/i.test(name));
  notices += `\n--- ${metadata.name}@${metadata.version} (${metadata.license}) ---\n`;
  if (!files.length) {
    // yoga-layout's npm archive omits its root license; vendored from the
    // matching tag: https://github.com/facebook/yoga/blob/v3.2.1/LICENSE
    notices += await readFile(resolve(root, 'licenses', `${metadata.name}.txt`), 'utf8') + '\n';
  }
  for (const file of files.sort()) notices += await readFile(resolve(directory, file), 'utf8') + '\n';
}
await writeFile(resolve(destination, 'THIRD_PARTY_NOTICES.txt'), notices);
console.log(`Bundled TUI for Node >=${nodeMajor} into ${destination}`);
