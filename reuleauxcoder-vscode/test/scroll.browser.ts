import assert from 'node:assert/strict';
import test from 'node:test';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {chromium} from 'playwright';
import {webviewHtml} from '../src/webview/html.js';
import type {HostSnapshot, WebRequest} from '../src/shared.js';

declare global {interface Window {scrollTest: {fixture: HostSnapshot; tick: number; chunks: number; stable: Element[]}}}

test('streaming: read history without snapping, preserve Markdown blocks and resume following', {timeout: 30000}, async () => {
  const server = createServer(async (req, res) => {
    const path = new URL(req.url!, 'http://localhost').pathname;
    if (path === '/webview.js' || path === '/webview.css') {
      res.setHeader('Content-Type', path.endsWith('.js') ? 'text/javascript' : 'text/css');
      res.end(await readFile(resolve('dist' + path))); return;
    }
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(webviewHtml({language: 'en', nonce: 'scroll', script: '/webview.js', style: '/webview.css', cspSource: "'self'"}));
  });
  await new Promise<void>(done => server.listen(0, '127.0.0.1', done));
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    const page = await browser.newPage({viewport: {width: 420, height: 900}});
    const errors: string[] = [], requests: WebRequest[] = [];
    page.on('pageerror', error => errors.push(error.message));
    const block = '## Check\n\nRead `src/main.ts` and keep this paragraph selected.\n\n- Inspect the changes\n- Verify the behavior\n\n```ts\nconst result = 42;\n```\n\n';
    const state: HostSnapshot = {hostId: 'scroll', revision: 1, draftRevision: 0, phase: 'ready', generation: 0, environment: 'Local', workspace: '/workspace/project', model: 'test', running: true, reviews: [], draftItems: [], draftText: '', cells: Array.from({length: 40}, (_, i) => ({id: `history-${i}`, role: i % 2 ? 'assistant' : 'user', text: i % 2 ? block : 'Inspect this implementation.'}))};
    state.cells.push({id: 'stream', role: 'assistant', text: block.repeat(100) + '[Guide][target]\n\nStreaming tail.'});
    await page.exposeFunction('bridge', async (request: WebRequest) => {
      requests.push(request);
      await page.evaluate(encoded => window.postMessage(JSON.parse(encoded), '*'), JSON.stringify({kind: 'response', id: request.id, result: request.action === 'ready' ? state : null}));
    });
    await page.addInitScript({content: 'window.acquireVsCodeApi = () => ({postMessage: value => void window.bridge(value), getState: () => null, setState: () => {}});'});
    await page.goto(`http://127.0.0.1:${(server.address() as {port: number}).port}`);
    await page.locator('[data-id="stream"] .code-block').last().waitFor({state: 'attached'});
    const bottom = () => page.locator('#transcript').evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight);
    assert(await bottom() <= 2);
    await page.evaluate(encoded => {
      const fixture: HostSnapshot = JSON.parse(encoded);
      const harness = window.scrollTest = {fixture, tick: 0, chunks: 0, stable: [] as Element[]};
      harness.stable = [...document.querySelectorAll('[data-id="stream"] .markdown > *')].slice(0, -1);
      const paragraph = document.querySelector('[data-id="stream"] .markdown > p')!;
      const range = document.createRange(); range.selectNodeContents(paragraph);
      window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
      harness.tick = window.setInterval(() => {
        fixture.cells.at(-1)!.text += ` streamed-${++harness.chunks}`; fixture.revision++;
        window.postMessage({kind: 'snapshot', snapshot: fixture}, '*');
      }, 32);
    }, JSON.stringify(state));
    await page.waitForFunction(() => window.scrollTest.chunks >= 4);
    // A small real wheel gesture must immediately leave follow mode, even within 70 px.
    await page.mouse.move(180, 520); await page.mouse.wheel(0, -40);
    await page.waitForFunction(() => {const node = document.querySelector('#transcript')!; return node.scrollHeight - node.scrollTop - node.clientHeight > 20;});
    const readingTop = await page.locator('#transcript').evaluate(node => node.scrollTop);
    const chunks = await page.evaluate(() => window.scrollTest.chunks);
    await page.waitForFunction(count => window.scrollTest.chunks >= count + 12, chunks);
    assert(Math.abs(await page.locator('#transcript').evaluate(node => node.scrollTop) - readingTop) <= 1, 'output must not move the reading position');
    await page.locator('#new-output').waitFor({state: 'visible'});
    assert(await page.evaluate(() => window.scrollTest.stable.every((node, i) => node === document.querySelectorAll('[data-id="stream"] .markdown > *')[i])), 'completed Markdown blocks must retain their DOM');
    assert.equal(await page.evaluate(() => window.getSelection()?.toString()), 'Read src/main.ts and keep this paragraph selected.');
    // Read further back, then explicitly return to the latest output.
    await page.mouse.wheel(0, -500); await page.waitForTimeout(100);
    assert(await bottom() > 500);
    await page.locator('#new-output').click();
    await page.waitForTimeout(150); assert(await bottom() <= 2);
    // Scrolling down to the actual bottom also resumes following.
    await page.mouse.move(180, 520);
    await page.mouse.wheel(0, -100); await page.locator('#new-output').waitFor({state: 'visible'});
    await page.mouse.wheel(0, 10000); await page.waitForTimeout(200);
    assert(await bottom() <= 2); assert(await page.locator('#new-output').isHidden());
    await page.evaluate(() => {
      const harness = window.scrollTest; clearInterval(harness.tick);
      // A definition can change an earlier block; incremental DOM must still resolve it.
      harness.fixture.cells.at(-1)!.text += '\n\n[target]: https://example.com/guide\n';
      harness.fixture.revision++; window.postMessage({kind: 'snapshot', snapshot: harness.fixture}, '*');
    });
    const link = page.locator('[data-id="stream"] a').filter({hasText: 'Guide'});
    await link.click();
    assert(requests.some(request => request.action === 'openLink' && request.data?.url === 'https://example.com/guide'));
    // An unchanged host snapshot while reading does not advertise new output.
    await page.mouse.move(180, 520); await page.mouse.wheel(0, -200); await page.waitForTimeout(100);
    await page.evaluate(() => {const state = window.scrollTest.fixture; state.revision++; window.postMessage({kind: 'snapshot', snapshot: state}, '*');});
    await page.waitForTimeout(100); assert(await page.locator('#new-output').isHidden());
    // Shrinking/replacing a reply removes obsolete Markdown blocks.
    await page.evaluate(() => {const state = window.scrollTest.fixture; state.cells.at(-1)!.text = 'Replacement reply'; state.revision++; window.postMessage({kind: 'snapshot', snapshot: state}, '*');});
    await page.waitForFunction(() => document.querySelector('[data-id="stream"] .body')?.textContent?.trim() === 'Replacement reply');
    assert.equal(await page.locator('[data-id="stream"] .code-block').count(), 0);
    // Incremental parsing must match a fresh render through unfinished nested syntax,
    // later link definitions, edits in the middle and deletion of the entire reply.
    let markdown = '# Heading\n\n[guide][ref]\n\n- one\n  - nested\n\n```ts\nconst value =';
    const variants = [markdown, markdown += ' 42;\n```\n\n| A | B |\n| - | - |\n| 1 |', markdown += ' 2 |\n\n[ref]: https://example.com\n\n![hidden](https://example.com/pixel.png)\n\n<script>unsafe</script>', markdown.replace('one', 'changed'), ''];
    for (const [i, text] of variants.entries()) {
      await page.evaluate(({text, i}) => {
        const state = window.scrollTest.fixture;
        state.cells = state.cells.filter(cell => !cell.id.startsWith('fresh-'));
        state.cells.find(cell => cell.id === 'stream')!.text = text;
        state.cells.push({id: `fresh-${i}`, role: 'assistant', text}); state.revision++;
        window.postMessage({kind: 'snapshot', snapshot: state}, '*');
      }, {text, i});
      await page.locator(`[data-id="fresh-${i}"]`).waitFor({state: 'attached'});
      assert.equal(await page.locator('[data-id="stream"] .body').innerHTML(), await page.locator(`[data-id="fresh-${i}"] .body`).innerHTML());
      assert.equal(await page.locator('[data-id="stream"] img, [data-id="stream"] script').count(), 0);
    }
    assert.deepEqual(errors, []);
  } finally {await browser.close(); server.closeAllConnections(); await new Promise<void>(done => server.close(() => done()));}
});
