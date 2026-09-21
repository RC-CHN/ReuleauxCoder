// A narrow, reproducible adapter for the pinned Ink renderer. Never patch an
// unknown upstream file: upgrades require reviewing the diff and oracle tests.
import {createHash} from 'node:crypto';
import {readFile, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const digest = text => createHash('sha256').update(text).digest('hex');
const replace = (text, from, to) => {
  if (text.split(from).length !== 2) throw new Error(`Renderer patch anchor changed: ${from}`);
  return text.replace(from, to);
};
const replaceBlock = (text, start, end, replacement) => {
  const first = text.indexOf(start), last = text.indexOf(end, first);
  if (first < 0 || last < first) throw new Error(`Renderer patch block changed: ${start}`);
  return replace(text, text.slice(first, last + end.length), replacement);
};

export function patchOutput(source) {
  let text = "import {BoundedCache, styledWeight} from '../../../renderer/cache.js';\n" + source;
  text = "import {composeRow} from '../../../renderer/compose-row.js';\n" + text;
  text = replace(text, 'class OutputCaches {', 'export class OutputCaches {');
  text = replace(text, 'widths = new Map();', 'widths = new BoundedCache(4096, 65536);');
  text = replace(text, 'blockWidths = new Map();', 'blockWidths = new BoundedCache(128, 65536);');
  text = replace(text, 'styledChars = new Map();', 'styledChars = new BoundedCache(512, 2 * 1024 * 1024, styledWeight);\n    rows = new BoundedCache(256, 2 * 1024 * 1024, (key, value) => key.length + value.length);');
  text = replace(text, 'caches = new OutputCaches();', 'caches;');
  text = replace(text, 'this.width = width;', 'this.caches = options.caches ?? new OutputCaches();\n        this.width = width;');
  text = replaceBlock(text, '            const row = [];', '            output.push(row);', '            output.push([]);');
  text = replaceBlock(text, '                    const characters = this.caches.getStyledChars(line);', "                    if (currentLine[offsetX]?.value === '') {\n                        currentLine[offsetX] = spaceCell;\n                    }\n                    offsetY++;", '                    currentLine.push({x, line});\n                    offsetY++;');
  text = replaceBlock(text, '            .map(line => {', '        })', '            .map(line => composeRow(line, this.width, this.caches))');
  return text;
}

export function patchRendererSource(source) {
  let text = replace(source, "import Output from './output.js';", "import Output, {OutputCaches} from './output.js';\nconst frameCaches = new WeakMap();");
  text = replace(text, 'const output = new Output({', 'let caches = frameCaches.get(node);\n        if (!caches) {caches = new OutputCaches(); frameCaches.set(node, caches);}\n        const output = new Output({\n            caches,');
  text = replace(text, 'staticOutput = new Output({', 'staticOutput = new Output({\n                caches,');
  return text;
}

export async function patchRenderer(directory = root) {
  const pkg = JSON.parse(await readFile(resolve(directory, 'node_modules/ink/package.json'), 'utf8'));
  if (pkg.version !== '7.1.1') throw new Error(`Review renderer adapter for Ink ${pkg.version}`);
  const files = [
    ['output', '54d62805875cc0644787f19038ac6adbd3a01aca367c048ec94f8822485fd2cf', patchOutput],
    ['renderer', '9e72b27731c38daac7e9f978e24f7bf1210c5cc26bf973e30f08c3ad4a9fe374', patchRendererSource],
  ];
  const plans = [];
  for (const [name, expected, transform] of files) {
    const target = resolve(directory, `node_modules/ink/build/${name}.js`);
    const original = resolve(directory, `node_modules/ink/build/${name}.rcoder-original.js`);
    const current = await readFile(target, 'utf8');
    const marker = `// ReuleauxCoder renderer adapter; upstream SHA256 ${expected}\n`;
    const source = current.startsWith(marker) ? await readFile(original, 'utf8') : current;
    if (digest(source) !== expected) throw new Error(`Unexpected Ink ${name}.js; refusing to patch`);
    plans.push({target, original, source, current, patched: marker + transform(source)});
  }
  for (const plan of plans) {
    if (plan.current === plan.patched) continue;
    // Keep the byte-exact upstream implementation as a differential test oracle.
    await writeFile(plan.original, plan.source);
    await writeFile(plan.target, plan.patched);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await patchRenderer();
