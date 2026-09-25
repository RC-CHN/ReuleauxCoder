import assert from 'node:assert/strict';
import test from 'node:test';
import {createServer} from 'node:http';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {chromium} from 'playwright';
import {webviewHtml} from '../src/webview/html.js';
import {ConfigurationRecovery} from '../src/core/configuration-recovery.js';
import {setLocale} from '../src/i18n.js';
import type {HostSnapshot, WebRequest} from '../src/shared.js';
import {python} from './helpers.js';

test('human recovery journey: diagnose, edit, recheck, preview, handle conflicts, restore and explicitly restart', {timeout: 60000}, async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder browser recovery 中文-'));
  const file = join(cwd, 'config 中文.yaml');
  const original = 'app:\n  api_key: local-test-only\n  model: local-model\n';
  const options = {cwd, env: {...process.env, HOME: join(cwd, 'home'), USERPROFILE: join(cwd, 'home')}, commands: [{command: python, args: ['-m', 'reuleauxcoder', '--config', file]}]};
  const recovery = new ConfigurationRecovery(() => {void publish?.();});
  let publish: (() => Promise<void>) | undefined;
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
  t.after(async () => {publish = undefined; await recovery.close(); await new Promise<void>(resolve => server.close(() => resolve())); await rm(cwd, {recursive: true, force: true}); setLocale('en');});
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  t.after(() => browser.close());
  for (const language of ['en', 'zh']) {
    setLocale(language); await writeFile(file, original);
    await recovery.open(options);
    const client = recovery.process.client!;
    const record = await client.prepare({scope: 'explicit', changes: [{path: '/ui/verbosity', value: 'debug'}]});
    await client.apply(record.id); await recovery.close();
    await writeFile(file, 'app: [broken');
    const page = await browser.newPage({viewport: {width: 340, height: 850}});
    const pageErrors: string[] = []; page.on('pageerror', error => pageErrors.push(error.message));
    let state: HostSnapshot = {hostId: 'journey', revision: 0, draftRevision: 0, phase: 'failed', generation: 0, environment: 'SSH: workspace', workspace: cwd, model: '', running: false, cells: [], reviews: [], draftItems: [], draftText: '', error: {kind: 'startup', message: 'Invalid configuration'}};
    let opened = '', dirty = false, restarts = 0;
    publish = async () => {state = {...state, revision: state.revision + 1, recovery: recovery.state}; await page.evaluate(encoded => window.postMessage({kind: 'snapshot', snapshot: JSON.parse(encoded)}, '*'), JSON.stringify(state));};
    await page.exposeFunction('bridge', async (request: WebRequest) => {
      let result: unknown, error: string | undefined;
      const data = request.data ?? {};
      try {
        switch (request.action) {
          case 'ready': result = state; break;
          case 'configuration.open': await recovery.open(options); break;
          case 'configuration.file': opened = recovery.source(data.scope); break;
          case 'configuration.check': await recovery.check(); break;
          case 'configuration.select': await recovery.select(data.id); break;
          case 'configuration.apply': await recovery.apply(data.id, data.offline === true, dirty ? [file] : []); break;
          case 'start': restarts++; await recovery.close(); state.phase = 'ready'; state.error = undefined; break;
          default: throw new Error('Unexpected action: ' + request.action);
        }
      } catch (reason) {error = (reason as Error).message;}
      await publish!();
      await page.evaluate(message => window.postMessage(message, '*'), {kind: 'response', id: request.id, result, error});
    });
    await page.addInitScript({content: 'window.acquireVsCodeApi = () => ({postMessage: value => void window.bridge(value), getState: () => null, setState: () => {}});'});
    await page.goto(`http://127.0.0.1:${(server.address() as {port: number}).port}/${language}`);
    const panel = page.locator('#configuration-recovery');
    await page.locator('[data-command="configuration.open"]').click();
    await panel.locator('.recovery-diagnostic').first().waitFor();
    assert(!await page.locator('#composer').isVisible(), 'Repair should not compete with an unusable chat composer');
    await panel.locator('.recovery-source:visible button').click(); assert.equal(opened, file);
    // File editing itself is covered in the native VS Code host; resume from a saved edit.
    await writeFile(opened, original);
    const check = panel.getByRole('button', {name: language === 'zh' ? '重新检查' : 'Check again', exact: true});
    await check.click();
    const start = panel.getByRole('button', {name: language === 'zh' ? '启动核心' : 'Start core', exact: true});
    await start.waitFor(); assert.equal(restarts, 0, 'Checking must not start a task');
    assert((await panel.textContent())?.includes(language === 'zh' ? '配置检查通过' : 'Configuration checks passed'));
    // The alternate human path uses saved history when manual repair is unavailable.
    await writeFile(file, 'app: [broken again'); await check.click();
    const history = panel.locator('.recovery-history');
    await history.getByRole('button').first().click();
    await panel.locator('.recovery-preview').waitFor();
    const restore = panel.getByRole('button', {name: language === 'zh' ? '恢复此配置' : 'Restore this configuration', exact: true});
    await page.waitForFunction(() => document.querySelectorAll('.recovery-checks .check-passed').length === 2);
    assert(await restore.isDisabled());
    const offline = panel.getByRole('checkbox'); await offline.check();
    dirty = true; await restore.click();
    await page.waitForFunction(() => !!document.querySelector('.recovery-error')?.textContent?.match(/unsaved|未保存/));
    assert.equal(await readFile(file, 'utf8'), 'app: [broken again');
    dirty = false; await writeFile(file, 'app: [external edit'); await restore.click();
    await page.waitForFunction(() => !!document.querySelector('.recovery-error')?.textContent?.match(/changed on disk|磁盘上的配置已变化/));
    await check.click();
    await page.waitForFunction(() => !document.querySelector('.recovery-preview'));
    await history.getByRole('button').first().click();
    await page.waitForFunction(() => document.querySelectorAll('.recovery-checks .check-passed').length === 2);
    assert.equal(await offline.isChecked(), false, 'A new preview must require a new offline choice');
    assert(!await page.locator('#notice').isVisible(), 'Resolved errors must not remain under a new preview');
    assert(await restore.isDisabled());
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path: join(tmpdir(), `rcoder-recovery-${language}.png`)});
    await offline.check(); await restore.click();
    await start.waitFor();
    assert.equal(await readFile(file, 'utf8'), original);
    assert.equal(restarts, 0, 'Restoration must not restart the Agent automatically');
    await start.click();
    await page.locator('#setup').waitFor({state: 'hidden'});
    assert.equal(restarts, 1); assert.deepEqual(pageErrors, []);
    assert(await page.locator('#composer').isVisible());
    publish = undefined; await page.close();
  }
});
