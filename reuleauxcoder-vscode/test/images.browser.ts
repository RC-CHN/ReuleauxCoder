import assert from 'node:assert/strict';
import test from 'node:test';
import {createServer} from 'node:http';
import {readFile, mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {chromium} from 'playwright';
import {webviewHtml} from '../src/webview/html.js';
import type {WebRequest} from '../src/shared.js';
import {backend, until} from './helpers.js';

test('real core: paste, send, enlarge, reload and edit scoped permissions in both languages', {timeout: 90000}, async t => {
  const server = createServer(async (req, res) => {
    const path = new URL(req.url!, 'http://localhost').pathname;
    if (path === '/webview.js' || path === '/webview.css') {
      res.setHeader('Content-Type', path.endsWith('.js') ? 'text/javascript' : 'text/css');
      res.end(await readFile(resolve('dist' + path))); return;
    }
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(webviewHtml({language: path.includes('zh') ? 'zh-CN' : 'en', nonce: 'images', script: '/webview.js', style: '/webview.css', cspSource: "'self'"}));
  });
  await new Promise<void>(done => server.listen(0, '127.0.0.1', done));
  t.after(() => new Promise<void>(done => server.close(() => done())));
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}); t.after(() => browser.close());
  await mkdir(resolve('../artifacts/vscode-concept'), {recursive: true});
  for (const language of ['en', 'zh']) {
    const b = await backend();
    const submitAction = b.client.submitAction.bind(b.client);
    let finishApproval: (() => void) | undefined; let delayApproval = true;
    b.client.submitAction = async (...args) => {
      const result = await submitAction(...args);
      if (args[0] === 'approval.show' && delayApproval) {delayApproval = false; await new Promise<void>(done => {finishApproval = done;});}
      return result;
    };
    const page = await browser.newPage({viewport: {width: 340, height: 850}});
    const errors: string[] = []; let revision = 0; let open = true; let failPreview = false;
    page.on('pageerror', error => errors.push(error.message));
    const publish = async () => {
      if (open) await page.evaluate(encoded => window.postMessage({kind: 'snapshot', snapshot: JSON.parse(encoded)}, '*'), JSON.stringify({...b.session.snapshot(), revision: ++revision}));
    };
    const changed = () => {void publish().catch(() => {});}; b.session.on('change', changed);
    try {
      await page.exposeFunction('bridge', async (request: WebRequest) => {
        const data = request.data ?? {}; let result: unknown; let error: string | undefined;
        try {
          switch (request.action) {
            case 'ready': result = {...b.session.snapshot(), revision: ++revision}; break;
            case 'draft': b.session.draftText = data.text; break;
            case 'send': b.session.submit(data.id, data.text, data.items, data.generation); break;
            case 'remove': b.session.remove(data.id); break;
            case 'upload.begin': result = await b.session.uploads!.begin('browser', data.name, data.size, data.image); break;
            case 'upload.append': result = await b.session.uploads!.append('browser', data.id, data.offset, data.data); break;
            case 'upload.complete': {const item = await b.session.uploads!.complete('browser', data.id); b.session.add(item); result = item.id; break;}
            case 'upload.cancel': await b.session.uploads!.cancel('browser', data.id); break;
            case 'image.preview':
              if (failPreview) {failPreview = false; error = 'Temporary preview failure'; break;}
              result = await b.session.imagePreview(data.attachmentId, data.variantId); break;
            case 'command.open': await b.session.commands.open(data.actionId); break;
            case 'command.policy': await b.session.commands.policy(data.surfaceId, data.path); break;
            case 'command.close': b.session.commands.dismiss(data.surfaceId); break;
            default: throw new Error('Unexpected browser request: ' + request.action);
          }
        } catch (reason) {error = String(reason); errors.push(`${request.action}: ${error}`);}
        await page.evaluate(message => window.postMessage(message, '*'), {kind: 'response', id: request.id, result, error});
      });
      await page.addInitScript({content: 'window.acquireVsCodeApi = () => ({postMessage: value => void window.bridge(value), getState: () => null, setState: () => {}});'});
      await page.goto(`http://127.0.0.1:${(server.address() as {port: number}).port}/${language}`);
      // Also cover sending a pasted image with no accompanying text.
      await page.locator('#composer').fill(language === 'zh' ? '' : 'Inspect this image');
      // Clipboard event runs the actual webview paste handler and byte upload pipeline.
      await page.locator('#composer').evaluate(node => {
        const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 360;
        const ctx = canvas.getContext('2d')!; ctx.fillStyle = '#24292e'; ctx.fillRect(0, 0, 640, 360);
        ctx.fillStyle = '#e4b77f'; ctx.fillRect(40, 48, 6, 52); ctx.font = 'bold 30px sans-serif'; ctx.fillText('Clipboard preview', 64, 84);
        ctx.fillStyle = '#a4acb3'; ctx.font = '18px sans-serif'; ctx.fillText('An image shared in this conversation', 64, 125);
        for (let i = 0; i < 3; i++) {ctx.fillStyle = ['#49675a', '#506b82', '#93784d'][i]; ctx.fillRect(40 + i * 190, 168, 172, 146);}
        const raw = atob(canvas.toDataURL('image/png').split(',')[1]);
        const data = new DataTransfer(); data.items.add(new File([Uint8Array.from(raw, char => char.charCodeAt(0))], 'clipboard.png', {type: 'image/png'}));
        node.dispatchEvent(new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: data}));
      });
      await page.locator('#composer').press('Enter');
      assert.equal(await page.locator('#composer').inputValue(), '');
      const tile = page.locator('#transcript .image-thumbnail'); await tile.waitFor();
      await page.waitForFunction(() => document.querySelector<HTMLImageElement>('#transcript .image-thumbnail img')?.naturalWidth === 640);
      await until(() => b.session.transcript.cells.some(cell => cell.role === 'user' && cell.status === 'applied'));
      await page.screenshot({path: resolve(`../artifacts/vscode-concept/image-inline-${language}.png`)});
      await tile.click(); const viewer = page.locator('.image-viewer'); await viewer.waitFor();
      await page.getByRole('button', {name: language === 'zh' ? '实际尺寸' : 'Actual size', exact: true}).click();
      assert(await page.locator('.image-viewport.actual-size').isVisible());
      await page.getByRole('button', {name: language === 'zh' ? '适应窗口' : 'Fit to view', exact: true}).click();
      await page.screenshot({path: resolve(`../artifacts/vscode-concept/image-viewer-${language}.png`)});
      await page.keyboard.press('Escape'); assert(await viewer.isHidden());
      assert(await tile.evaluate(node => node === document.activeElement));
      failPreview = true; await page.reload();
      await page.locator('#transcript .image-thumbnail.unavailable').waitFor();
      await tile.click(); await page.locator('.image-viewport img').waitFor();
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.querySelector<HTMLImageElement>('#transcript .image-thumbnail img')?.naturalWidth === 640);
      assert.equal(await page.locator('#transcript .image-thumbnail.unavailable').count(), 0);
      // These are distinct matching scopes, even though their tool names are identical.
      await b.client.submitAction('approval.set', {target: 'tool=edit_file', action: 'deny'});
      await b.client.submitAction('approval.set', {target: 'source=builtin,tool=edit_file', action: 'require_approval'});
      await page.locator('#permissions').click(); await page.locator('.policy-tool select').first().waitFor();
      await until(() => finishApproval); finishApproval!(); finishApproval = undefined;
      await page.waitForFunction(() => document.querySelector('#workbench')?.getAttribute('aria-busy') === 'false');
      await page.locator('.policy-scopes button').last().click();
      assert.equal(await page.locator('.policy-tool select').first().isDisabled(), false, 'Scope changes must use the latest busy state');
      await page.locator('.policy-scopes button').first().click();
      await page.locator('[data-policy-filter]').fill('edit_file');
      const rows = page.locator('.policy-tool:visible'); assert.equal(await rows.count(), 2);
      for (const row of await rows.all()) {
        assert.equal(await row.locator('label').textContent(), language === 'zh' ? '修改文件' : 'edit_file');
        assert(!(await row.innerText()).includes('source='));
        assert(!(await row.innerText()).includes('tool='));
      }
      const builtin = rows.filter({has: page.locator('.policy-target-scope', {hasText: language === 'zh' ? '内置工具' : 'Built-in tools'})});
      const all = rows.filter({has: page.locator('.policy-target-scope', {hasText: language === 'zh' ? '所有来源' : 'All sources'})});
      assert.equal(await builtin.getAttribute('data-policy'), 'require_approval');
      assert.equal(await all.getAttribute('data-policy'), 'deny');
      await builtin.locator('summary').click(); assert.match(await builtin.locator('code').innerText(), /source=builtin,tool=edit_file/);
      await builtin.locator('summary').click();
      await builtin.locator('select').selectOption({label: language === 'zh' ? '自动允许' : 'Allow automatically'});
      await page.waitForFunction(() => [...document.querySelectorAll('.policy-tool')].some(row => row.getAttribute('data-policy') === 'allow'));
      assert.equal(await all.getAttribute('data-policy'), 'deny');
      await page.locator('.policy-scopes button').last().click();
      assert.equal(await builtin.getAttribute('data-policy'), 'inherit');
      await builtin.locator('select').selectOption({label: language === 'zh' ? '禁止执行' : 'Block'});
      await page.waitForFunction(() => document.querySelector('.policy-scopes button:last-child')?.getAttribute('aria-pressed') === 'true' && [...document.querySelectorAll('.policy-tool')].some(row => row.getAttribute('data-policy') === 'deny'));
      await until(async () => (await readFile(resolve(b.cwd, '.rcoder/config.yaml'), 'utf8')).includes('deny'));
      await page.locator('.policy-scopes button').first().click();
      assert.equal(await builtin.getAttribute('data-policy'), 'allow');
      await page.emulateMedia({reducedMotion: 'reduce'});
      await page.screenshot({path: resolve(`../artifacts/vscode-concept/permissions-scoped-${language}.png`)});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.locator('[data-policy-filter]').fill('no-such-tool'); assert.equal(await rows.count(), 0);
      await page.locator('[data-policy-filter]').press('Escape');
      await tile.click(); assert(await viewer.isVisible());
      const sessionId = b.client.state.session_id;
      await b.client.submitAction('sessions.new', {});
      await viewer.waitFor({state: 'hidden'}); assert.equal(await tile.count(), 0);
      await b.client.submitAction('sessions.resume', {target: sessionId});
      await until(() => b.session.transcript.cells.some(cell => cell.images?.length));
      await page.waitForFunction(() => document.querySelector<HTMLImageElement>('#transcript .image-thumbnail img')?.naturalWidth === 640);
      assert.deepEqual(errors, []);
    } finally {finishApproval?.(); open = false; b.session.off('change', changed); await page.close(); await b.close();}
  }
});
