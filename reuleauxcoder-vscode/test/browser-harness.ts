import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {chromium} from 'playwright';
import {webviewHtml} from '../src/webview/html.js';
import type {HostSnapshot, WebRequest} from '../src/shared.js';

export async function browserHarness(snapshot: () => HostSnapshot, handle: (request: WebRequest) => Promise<unknown> = async () => null, language = 'en', width = 420) {
  const server = createServer(async (req, res) => {
    const path = new URL(req.url!, 'http://localhost').pathname;
    if (path === '/webview.js' || path === '/webview.css') {
      res.setHeader('Content-Type', path.endsWith('.js') ? 'text/javascript' : 'text/css'); res.end(await readFile(resolve('dist' + path))); return;
    }
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(webviewHtml({language, nonce: 'journey', script: '/webview.js', style: '/webview.css', cspSource: "'self'"}));
  });
  await new Promise<void>(done => server.listen(0, '127.0.0.1', done));
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  const page = await browser.newPage({viewport: {width, height: 950}});
  const errors: string[] = [], requests: WebRequest[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const close = async () => {await browser.close(); server.closeAllConnections(); await new Promise<void>(done => server.close(() => done()));};
  try {
    await page.exposeFunction('bridge', async (request: WebRequest) => {
      requests.push(request); let result: unknown = null, error: string | undefined;
      try {result = request.action === 'ready' ? snapshot() : await handle(request);} catch (reason) {error = String(reason);}
      if (!page.isClosed()) await page.evaluate(encoded => window.postMessage(JSON.parse(encoded), '*'), JSON.stringify({kind: 'response', id: request.id, result, error}));
    });
    await page.addInitScript({content: 'window.acquireVsCodeApi = () => ({postMessage: value => void window.bridge(value), getState: () => null, setState: () => {}});'});
    await page.goto(`http://127.0.0.1:${(server.address() as {port: number}).port}`);
    await page.waitForFunction(() => !document.querySelector<HTMLButtonElement>('#send')!.disabled);
    return {page, requests, errors, close, async publish() {await page.evaluate(encoded => window.postMessage({kind: 'snapshot', snapshot: JSON.parse(encoded)}, '*'), JSON.stringify(snapshot()));}};
  } catch (error) {await close(); throw error;}
}
