import assert from 'node:assert/strict';
import test from 'node:test';
import {createServer} from 'node:http';
import {readFile, mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {chromium} from 'playwright';
import {webviewHtml} from '../src/webview/html.js';
import type {HostSnapshot, WebRequest} from '../src/shared.js';
import {catalog, panels, overview} from './webview-fixture.js';
import {writePreview} from './webview-preview.js';
import {pathToFileURL} from 'node:url';

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
      state.catalog = catalog;
      const messages: WebRequest[] = []; let failSend = false; let uploaded = Buffer.alloc(0); let surfaceId = 0;
      const publish = async () => {state.revision++; await page.evaluate(encoded => window.postMessage({kind: 'snapshot', snapshot: JSON.parse(encoded)}, '*'), JSON.stringify(state));};
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
          case 'models': data.actionId = 'model.show';
          // The browser fixture supplies protocol data; real command dispatch is tested in commands.test.ts.
          case 'command.open': {
            const action = catalog.find(item => item.action_id === data.actionId)!;
            state.commandSurface = {id: ++surfaceId, feature: action.feature_id, busy: false, canBack: false, ...(action.parameters.length ? {action} : {panel: structuredClone(panels[action.action_id])})}; await publish(); break;
          }
          case 'command.select': {
            const panel = state.commandSurface!.panel!; const item = panel.items[data.index]; const child = panel.children.find(([id]) => id === item.label)?.[1];
            if (child) state.commandSurface = {...state.commandSurface!, id: ++surfaceId, canBack: true, panel: child};
            else if (item.action?.action_id.startsWith('skills.')) {item.current = !item.current; state.commandSurface!.id = ++surfaceId;}
            else state.commandSurface = undefined;
            await publish(); break;
          }
          case 'command.submit': case 'command.close': state.commandSurface = undefined; await publish(); break;
          case 'command.back': state.commandSurface = {...state.commandSurface!, id: ++surfaceId, canBack: false, panel: panels['model.show']}; await publish(); break;
          case 'answer': state.interactions = state.interactions?.filter(item => item.id !== data.id); await publish(); break;
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

      // Mouse and search are first-class entry points; the current draft survives navigation.
      await page.locator('#commands').click();
      await page.locator('[data-action="model.show"][role="option"]').click();
      await page.locator('.panel-rows button').first().click();
      await page.waitForFunction(() => document.querySelectorAll('.panel-rows button').length === 2);
      assert.equal(await page.locator('.panel-rows button').count(), 2);
      assert.equal(await page.locator('#composer').inputValue(), 'keep this next draft');
      await page.locator('.workbench-heading button').first().click();
      await page.locator('.workbench-heading button').last().click();
      await page.locator('#composer').fill('/goal create');
      const beforeCommand = messages.filter(message => message.action === 'command.open').length;
      await page.locator('#composer').dispatchEvent('keydown', {key: 'Enter', isComposing: true});
      assert.equal(messages.filter(message => message.action === 'command.open').length, beforeCommand);
      await page.locator('#composer').press('Enter');
      await page.locator('.command-form textarea').fill('A clear objective');
      await publish(); assert.equal(await page.locator('.command-form textarea').inputValue(), 'A clear objective');
      await page.locator('.command-form button[type="submit"]').click();
      await page.waitForFunction(() => document.querySelector('#workbench')!.hasAttribute('hidden'));
      assert.deepEqual(messages.filter(message => message.action === 'command.submit').at(-1)!.data!.values, {objective: 'A clear objective'});
      assert.equal(await page.locator('#composer').inputValue(), '');
      await page.locator('#composer').fill('/goal');
      await page.locator('#composer').press('Shift+Enter');
      assert.equal(await page.locator('#composer').inputValue(), '/goal\n');
      assert(await page.locator('#workbench').isHidden());
      await page.locator('#composer').fill('/no-match');
      await page.locator('#composer').press('Escape');
      assert.equal(await page.locator('#composer').inputValue(), '/no-match');
      await page.locator('#composer').fill('keep this next draft');
      await page.locator('#commands').click();
      await page.locator('.menu-search').fill(language === 'zh' ? '技能' : 'skills');
      await page.locator('[data-action="skills.show"][role="option"]').click();
      assert.equal(await page.locator('[role="switch"]').getAttribute('aria-checked'), 'true');
      await page.locator('[role="switch"]').click();
      await page.waitForFunction(() => document.querySelector('[role="switch"]')?.getAttribute('aria-checked') === 'false');
      await page.locator('.workbench-heading button').last().click();

      await page.locator('.context-strip [data-action="approval.show"]').click();
      await page.locator('.panel-rows button').first().click();
      await page.waitForFunction(() => document.querySelector('.permission-steps .active')?.textContent?.startsWith('2'));
      await page.locator('.panel-rows button').first().click();
      await page.waitForFunction(() => document.querySelectorAll('.permission-row').length === 4);
      assert.equal(await page.locator('.permission-row[aria-current="true"]').count(), 1);
      await page.locator('.permission-row[data-policy="require_approval"]').click();
      await page.waitForFunction(() => document.querySelector('#workbench')!.hasAttribute('hidden'));

      state.interactions = [{id: 'secret', kind: 'input_text', title: 'Authentication', message: 'Enter a secret', secret: true, allowEmpty: false, allowCancel: true}]; await publish();
      await page.locator('.answer-form input').fill('not-persisted'); await publish();
      assert.equal(await page.locator('.answer-form input').inputValue(), 'not-persisted');
      assert(!(await page.evaluate(() => sessionStorage.getItem('vscode-state')!)).includes('not-persisted'));
      await page.locator('.answer-form button').click();
      await page.waitForFunction(() => !document.querySelector('.answer-form'));
      assert.equal(messages.filter(message => message.action === 'answer').at(-1)!.data!.value, 'not-persisted');
      assert.equal(await page.locator('#composer').inputValue(), 'keep this next draft');

      state.cells.push({id: 'markdown', role: 'assistant', text: '## Ready to review\n\n**Input stays responsive.**\n\n- Commands open beside the composer\n- Reviews open in the editor\n\n| Check | Result |\n| --- | --- |\n| Steering | Passed |\n\n```ts\nconst ready = true;\n```\n\n[Docs](https://example.com/docs) [bad](javascript:alert(1)) [command](command:workbench.action.closeWindow)\n\n<img src=x onerror=alert(1)> ![remote](https://example.com/tracker.png)'});
      await publish();
      assert.equal(await page.locator('.markdown h2').count(), 1); assert.equal(await page.locator('.markdown table').count(), 1); assert.equal(await page.locator('.markdown pre code').textContent(), 'const ready = true;\n');
      assert.equal(await page.locator('.markdown img, .markdown script, .markdown a[href^="command:"], .markdown a[href^="javascript:"]').count(), 0);
      await page.locator('.markdown a[href="https://example.com/docs"]').click();
      assert.equal(messages.filter(message => message.action === 'openLink').at(-1)!.data!.url, 'https://example.com/docs');
      state.overview = structuredClone(overview); state.mode = 'code';
      state.cells.push({id: 'assistant', role: 'assistant', text: language === 'zh' ? '已检查修改。请在中央原生 diff 中审阅并批准。' : 'The proposal is ready. Review and approve it in the native diff editor.'});
      state.reviews = [{id: 'review', title: 'edit_file', summary: language === 'zh' ? '让输入立即响应，并把命令操作留在会话中。' : 'Keep input responsive and command controls within the conversation.', documents: [{id: 'doc', path: 'src/runtime.ts'}], grants: [{id: 'scope-one', label: 'This session', description: 'Allow edits to this file for this session', broad: false}]}];
      await publish();
      await page.locator('.review-file').click(); assert.equal(messages.filter(message => message.action === 'review').at(-1)!.data!.documentId, 'doc');
      await page.locator('.review-alternatives summary').click();
      await page.locator('input[value="scope-one"]').check();
      await page.locator('.review-decision .primary').click();
      assert.equal(messages.filter(message => message.action === 'approve').at(-1)!.data!.scopeId, 'scope-one');
      await page.locator('.review-alternatives summary').click();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await mkdir(resolve('../artifacts/vscode-concept'), {recursive: true});
      await delay(200); await page.screenshot({path: resolve(`../artifacts/vscode-concept/extension-${language}.png`)});
      await page.setViewportSize({width: 1100, height: 850});
      await delay(150); await page.screenshot({path: resolve(`../artifacts/vscode-concept/workbench-${language}.png`)});
      await page.locator('#commands').click(); await delay(150); await page.screenshot({path: resolve(`../artifacts/vscode-concept/commands-${language}.png`)});
      await page.locator('.workbench-heading button').last().click();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const old = {...state, revision: 0, phase: 'failed'};
      await page.evaluate(encoded => window.postMessage({kind: 'snapshot', snapshot: JSON.parse(encoded)}, '*'), JSON.stringify(old));
      assert(await page.locator('#setup').isHidden());
      assert.deepEqual(errors, []);
      await page.close();
      const preview = await browser.newPage({viewport: {width: 1200, height: 1000}});
      await preview.goto(pathToFileURL(await writePreview(language)).toString());
      await preview.waitForFunction(() => document.querySelector('.attention-card'));
      await delay(200); await preview.screenshot({path: resolve(`../artifacts/vscode-concept/preview-${language}.png`)});
      await preview.locator('#commands').click(); await delay(150); await preview.screenshot({path: resolve(`../artifacts/vscode-concept/preview-menu-${language}.png`)});
      await preview.locator('.workbench-heading button').last().click();
      await preview.locator('.context-strip [data-action="approval.show"]').click();
      await preview.locator('.panel-rows button').first().click();
      await preview.waitForFunction(() => document.querySelector('.permission-steps .active')?.textContent?.startsWith('2'));
      await preview.locator('.panel-rows button').first().click();
      await preview.waitForFunction(() => document.querySelectorAll('.permission-row').length === 4);
      await delay(150); await preview.screenshot({path: resolve(`../artifacts/vscode-concept/preview-permissions-${language}.png`)});
      await preview.locator('.workbench-heading button').first().click();
      await preview.waitForFunction(() => document.querySelector('.permission-steps .active')?.textContent?.startsWith('2'));
      await preview.locator('.workbench-heading button').last().click();
      await preview.evaluate(() => {
        document.body.classList.add('vscode-light');
        for (const [name, value] of Object.entries({'sideBar-background': '#f4f3ef', 'editor-background': '#fffdf9', 'input-background': '#fffefa', foreground: '#293238', descriptionForeground: '#626d74', 'panel-border': '#d3d4cf', 'button-background': '#795323', 'button-foreground': '#ffffff', 'button-hoverBackground': '#8c6434', 'charts-orange': '#986826', focusBorder: '#aa7c42', 'list-hoverBackground': '#edece6', 'textLink-foreground': '#326b92', 'input-placeholderForeground': '#777f83'})) document.documentElement.style.setProperty(`--vscode-${name}`, value);
      });
      assert(await preview.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await preview.screenshot({path: resolve(`../artifacts/vscode-concept/preview-light-${language}.png`)});
      await preview.setViewportSize({width: 340, height: 900});
      assert(await preview.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await preview.screenshot({path: resolve(`../artifacts/vscode-concept/preview-sidebar-${language}.png`)});
      await preview.close();
    }
  } finally {await browser.close(); await new Promise<void>(resolve => server.close(() => resolve()));}
});
