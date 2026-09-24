import assert from 'node:assert/strict';
import test from 'node:test';
import {createServer} from 'node:http';
import {readFile, mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {chromium} from 'playwright';
import {webviewHtml} from '../src/webview/html.js';
import type {HostSnapshot, WebRequest} from '../src/shared.js';

test('actual bilingual webview: immediate steering, retry, paste/upload and narrow layout', {timeout: 60000}, async () => {
  const server = createServer(async (req, res) => {
    const pathname = new URL(req.url!, 'http://localhost').pathname;
    if (pathname === '/webview.js' || pathname === '/webview.css') {
      res.setHeader('Content-Type', pathname.endsWith('.js') ? 'text/javascript' : 'text/css');
      res.end(await readFile(resolve('dist' + pathname))); return;
    }
    const language = pathname.includes('zh') ? 'zh-CN' : 'en';
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(webviewHtml({language, nonce: 'browser-test', script: '/webview.js', style: '/webview.css', cspSource: "'self'"}));
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = (server.address() as {port: number}).port;
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    for (const language of ['en', 'zh']) {
      const page = await browser.newPage({viewport: {width: 340, height: 850}});
      const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
      let state: HostSnapshot = {hostId: 'host-1', revision: 1, draftRevision: 0, phase: 'ready', generation: 0, environment: 'SSH: workbench', workspace: '/workspace/project', model: 'test-model', running: false, cells: [], reviews: [], draftItems: [], draftText: ''};
      const messages: WebRequest[] = []; let failSend = false; let uploaded = Buffer.alloc(0);
      const publish = async () => {state.revision++; await page.evaluate(snapshot => window.postMessage({kind: 'snapshot', snapshot}, '*'), state);};
      await page.exposeFunction('bridge', async (request: WebRequest) => {
        messages.push(request); let result: unknown = null; let error: string | undefined;
        const data = request.data!;
        switch (request.action) {
          case 'ready': result = state; break;
          case 'send':
            await delay(150);
            if (failSend) {failSend = false; error = 'Temporary bridge failure'; break;}
            if (!state.cells.some(cell => cell.id === data.id)) state.cells.push({id: data.id, role: 'user', text: data.text, status: 'applied'});
            state.draftItems = state.draftItems.filter(item => !data.items.includes(item.id)); await publish(); break;
          case 'draft': state.draftText = data.text; break;
          case 'upload.begin': uploaded = Buffer.alloc(0); result = {id: 'test-upload', chunk: 32}; break;
          case 'upload.append': await delay(15); assert.equal(data.offset, uploaded.length); uploaded = Buffer.concat([uploaded, Buffer.from(data.data, 'base64')]); result = uploaded.length; break;
          case 'upload.complete': state.draftItems.push({id: 'attachment-1', name: 'paste.png', kind: 'image'}); result = 'attachment-1'; await publish(); break;
        }
        await page.evaluate(message => window.postMessage(message, '*'), {kind: 'response', id: request.id, result, error});
      });
      await page.addInitScript({content: `let saved = JSON.parse(sessionStorage.getItem('vscode-state') || 'null'); window.acquireVsCodeApi = () => ({postMessage: value => void window.bridge(value), getState: () => saved, setState: value => {saved = value; sessionStorage.setItem('vscode-state', JSON.stringify(value));}});`});
      await page.goto(`http://127.0.0.1:${port}/${language}`);
      assert.deepEqual(errors, []);
      await page.waitForFunction(() => document.querySelector('#environment')!.textContent?.includes('SSH: workbench'));
      await page.waitForFunction(() => !document.querySelector<HTMLButtonElement>('#send')!.disabled);
      assert.equal(await page.locator('#composer').getAttribute('aria-label'), language === 'zh' ? '消息' : 'Message');
      await page.locator('#composer').fill('ordinary <img src=x onerror=alert(1)>');
      await page.locator('#composer').press('Enter');
      assert.equal(await page.locator('#composer').inputValue(), '');
      assert.equal(await page.locator('.cell.user').count(), 1);
      assert.equal(await page.locator('.cell img').count(), 0);
      await page.locator('#composer').fill('new draft');
      await page.waitForFunction(() => document.querySelector('.cell.user .meta')!.textContent?.includes('sending') === false);
      assert.equal(await page.locator('#composer').inputValue(), 'new draft');
      state.running = true; await publish();
      await page.locator('#composer').fill('steering 中文 👩🏽‍💻');
      await page.locator('#composer').press('Enter');
      assert.equal(await page.locator('#composer').inputValue(), '');
      assert.equal(await page.locator('.cell.user').count(), 2);
      await page.locator('#composer').press('Enter');
      await delay(250);
      assert.equal(messages.filter(message => message.action === 'send').length, 2);

      failSend = true;
      await page.locator('#composer').fill('retry input'); await page.locator('#composer').press('Enter');
      await page.locator('.retry').waitFor();
      const failedId = messages.filter(message => message.action === 'send').at(-1)!.data!.id;
      await page.reload(); await page.locator('.retry').waitFor();
      await page.locator('.retry').click(); await delay(250);
      assert.equal(messages.filter(message => message.action === 'send').at(-1)!.data!.id, failedId);
      assert.equal(await page.locator('.cell.user').count(), 3);

      const expected = Buffer.from('clipboard bytes 中文'.repeat(20));
      await page.locator('#composer').evaluate((node, values) => {
        const clipboardData = new DataTransfer(); clipboardData.items.add(new File([new Uint8Array(values)], 'paste.png', {type: 'image/png'}));
        node.dispatchEvent(new ClipboardEvent('paste', {clipboardData, bubbles: true, cancelable: true}));
      }, [...expected]);
      await page.locator('#composer').fill('send while uploading'); await page.locator('#composer').press('Enter');
      assert.equal(await page.locator('#composer').inputValue(), '');
      assert.equal(await page.locator('.cell.user').count(), 4);
      await page.locator('#composer').fill('keep this next draft');
      await page.waitForFunction(() => document.querySelectorAll('.cell.user').length === 4 && !document.querySelectorAll('.cell.user')[3].querySelector('.meta span'));
      assert.equal(await page.locator('#composer').inputValue(), 'keep this next draft');
      assert.deepEqual(messages.filter(message => message.action === 'send').at(-1)!.data!.items, ['attachment-1']);
      assert.deepEqual(uploaded, expected);
      assert(messages.filter(message => message.action === 'upload.append').length > 1);
      state.cells.push({id: 'assistant', role: 'assistant', text: language === 'zh' ? '已检查修改。请在中央原生 diff 中审阅并批准。' : 'The proposal is ready. Review and approve it in the native diff editor.'});
      state.reviews = [{id: 'review', title: 'edit_file', summary: '', documents: [{id: 'doc', path: 'src/runtime.ts'}]}];
      await publish();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await mkdir(resolve('../artifacts/vscode-concept'), {recursive: true});
      await page.screenshot({path: resolve(`../artifacts/vscode-concept/extension-${language}.png`)});
      await page.setViewportSize({width: 1100, height: 850});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const old = {...state, revision: 0, phase: 'failed'};
      await page.evaluate(snapshot => window.postMessage({kind: 'snapshot', snapshot}, '*'), old);
      assert(await page.locator('#setup').isHidden());
      assert.deepEqual(errors, []);
      await page.close();
    }
  } finally {await browser.close(); await new Promise<void>(resolve => server.close(() => resolve()));}
});
