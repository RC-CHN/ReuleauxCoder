import assert from 'node:assert/strict';
import test from 'node:test';
import {createServer} from 'node:http';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {chromium} from 'playwright';
import {webviewHtml} from '../src/webview/html.js';
import {ConfigurationEditor} from '../src/core/configuration-editor.js';
import {setLocale} from '../src/i18n.js';
import type {HostSnapshot, WebRequest} from '../src/shared.js';
import {python} from './helpers.js';

test('human configuration journey: choose scope, edit buffers, check, save and explicitly start', {timeout: 60000}, async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder browser config 中文-')), file = join(cwd, 'config 中文.yaml');
  const original = '# preserved\napp:\n  api_key: local-test-only\n  model: local-model\n';
  const options = {cwd, env: {...process.env, HOME: join(cwd, 'home'), USERPROFILE: join(cwd, 'home')}, commands: [{command: python, args: ['-m', 'reuleauxcoder', '--config', file]}]};
  let publish: (() => Promise<void>) | undefined;
  const editor = new ConfigurationEditor(() => {void publish?.();});
  const server = createServer(async (req, res) => {
    const path = new URL(req.url!, 'http://localhost').pathname;
    if (path === '/webview.js' || path === '/webview.css') {
      res.setHeader('Content-Type', path.endsWith('.js') ? 'text/javascript' : 'text/css');
      res.end(await readFile(resolve('dist' + path))); return;
    }
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(webviewHtml({language: path.includes('zh') ? 'zh-CN' : 'en', nonce: 'journey', script: '/webview.js', style: '/webview.css', cspSource: "'self'"}));
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined;
  t.after(async () => {
    publish = undefined; setLocale('en');
    // A Windows filesystem cleanup error must not skip closing Chromium and
    // leave the test process alive after reporting a failed hook.
    try {await browser?.close();}
    finally {
      try {await editor.close();}
      finally {
        server.closeAllConnections();
        await new Promise<void>(resolve => server.close(() => resolve()));
        await rm(cwd, {recursive: true, force: true, maxRetries: 5, retryDelay: 100});
      }
    }
  });
  browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  for (const language of ['en', 'zh']) {
    setLocale(language); await writeFile(file, 'app: [broken');
    const page = await browser.newPage({viewport: {width: 340, height: 850}});
    const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
    let state: HostSnapshot = {hostId: 'journey', revision: 0, draftRevision: 0, phase: 'failed', generation: 0, environment: 'SSH: workspace', workspace: cwd, model: '', running: false, cells: [], reviews: [], draftItems: [], draftText: '', error: {kind: 'startup', message: 'Invalid configuration'}};
    let opened = '', restarts = 0, buffer = '';
    publish = async () => {state = {...state, revision: state.revision + 1, configuration: editor.state}; await page.evaluate(encoded => window.postMessage({kind: 'snapshot', snapshot: JSON.parse(encoded)}, '*'), JSON.stringify(state));};
    await page.exposeFunction('bridge', async (request: WebRequest) => {
      let result: unknown, error: string | undefined;
      const data = request.data ?? {};
      try {
        switch (request.action) {
          case 'ready': result = state; break;
          case 'configuration.open': await editor.open(options); break;
          case 'configuration.file': opened = editor.source(data.scope); break;
          case 'configuration.check': await editor.check(); break;
          case 'configuration.save': await writeFile(file, buffer); editor.setDocuments([], true); await editor.check(); break;
          case 'configuration.restart': restarts++; await editor.close(); state.phase = 'ready'; state.error = undefined; break;
          default: throw new Error('Unexpected action: ' + request.action);
        }
      } catch (reason) {error = (reason as Error).message;}
      await publish!();
      await page.evaluate(message => window.postMessage(message, '*'), {kind: 'response', id: request.id, result, error});
    });
    await page.addInitScript({content: 'window.acquireVsCodeApi = () => ({postMessage: value => void window.bridge(value), getState: () => null, setState: () => {}});'});
    await page.goto(`http://127.0.0.1:${(server.address() as {port: number}).port}/${language}`);
    const panel = page.locator('#configuration-editor');
    await page.locator('[data-command="configuration.open"]').click();
    await panel.locator('.configuration-diagnostic').first().waitFor();
    assert(!await page.locator('#composer').isVisible());
    assert((await panel.textContent())?.includes(language === 'zh' ? '工作区设置覆盖全局' : 'Workspace settings override global'));
    // Explicit configuration diagnostics offer the correct source directly.
    await panel.locator('.configuration-diagnostic button').click(); assert.equal(opened, file);
    // Native editor interactions are covered by the VS Code host test.
    buffer = original; editor.setDocuments([{scope: 'explicit', content: buffer}]); await publish!();
    const check = panel.getByRole('button', {name: language === 'zh' ? '检查配置' : 'Check configuration', exact: true});
    await check.click();
    await page.waitForFunction(() => document.querySelectorAll('.configuration-checks .check-passed').length === 2);
    const start = panel.getByRole('button', {name: language === 'zh' ? '启动核心' : 'Start core', exact: true});
    const save = panel.getByRole('button', {name: language === 'zh' ? '保存配置文件' : 'Save configuration files', exact: true});
    assert(await save.isEnabled()); assert(await start.isDisabled());
    assert.equal(await readFile(file, 'utf8'), 'app: [broken');
    buffer = 'ui:\n  verbositty: debug\n'; editor.setDocuments([{scope: 'explicit', content: buffer}]); await publish!();
    await page.waitForFunction(() => !document.querySelector('.configuration-checks'));
    assert(await save.isDisabled(), 'Editing invalidates the prior passing check');
    await check.click(); await panel.locator('.configuration-diagnostic').first().waitFor();
    assert(await start.isDisabled());
    buffer = original; editor.setDocuments([{scope: 'explicit', content: buffer}]); await check.click();
    await page.waitForFunction(() => document.querySelectorAll('.configuration-checks .check-passed').length === 2);
    await save.click();
    await page.waitForFunction(() => !document.querySelector<HTMLButtonElement>('[data-control="configuration.restart"]')!.disabled);
    assert.equal(restarts, 0); assert.equal(await readFile(file, 'utf8'), original);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path: join(tmpdir(), `rcoder-configuration-${language}.png`)});
    await start.click(); await panel.waitFor({state: 'hidden'});
    assert.equal(restarts, 1); assert.deepEqual(errors, []); assert(await page.locator('#composer').isVisible());
    publish = undefined; await page.close();
  }
});
