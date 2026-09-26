import assert from 'node:assert/strict';
import test from 'node:test';
import {createServer} from 'node:http';
import {readFile, mkdir, writeFile} from 'node:fs/promises';
import {resolve, join} from 'node:path';
import {chromium} from 'playwright';
import {webviewHtml} from '../src/webview/html.js';
import {copySkillToWorkspace} from '../src/core/skill-files.js';
import type {WebRequest} from '../src/shared.js';
import {backend, until} from './helpers.js';

test('skill journey: discover, filter, inspect, toggle, copy and reload with real core metadata', {timeout: 90000}, async t => {
  const b = await backend(); t.after(() => b.close());
  const submitAction = b.client.submitAction.bind(b.client);
  let finishOpen: (() => void) | undefined;
  b.client.submitAction = async (...args) => {
    const result = await submitAction(...args);
    if (args[0] === 'skills.show') await new Promise<void>(done => {finishOpen = done;});
    return result;
  };
  for (const [name, extra] of [['plain-skill', ''], ['partial-skill', 'metadata:\n  rcoder.display-name.zh-CN: 部分翻译\n  rcoder.icon: future-icon\n  rcoder.summary: []\n  rcoder.category: unknown\n']]) {
    const dir = join(b.cwd, '.rcoder/skills', name); await mkdir(dir, {recursive: true});
    await writeFile(join(dir, 'SKILL.md'), `---\nname: ${name}\ndescription: Custom standard skill\n${extra}---\nInstructions`);
  }
  await b.client.submitAction('skills.reload', {});
  const server = createServer(async (req, res) => {
    const path = new URL(req.url!, 'http://localhost').pathname;
    if (path === '/webview.js' || path === '/webview.css') {
      res.setHeader('Content-Type', path.endsWith('.js') ? 'text/javascript' : 'text/css');
      res.end(await readFile(resolve('dist' + path))); return;
    }
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(webviewHtml({language: path.includes('zh') ? 'zh-CN' : 'en', nonce: 'skills', script: '/webview.js', style: '/webview.css', cspSource: "'self'"}));
  });
  await new Promise<void>(done => server.listen(0, '127.0.0.1', done));
  t.after(() => new Promise<void>(done => server.close(() => done())));
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}); t.after(() => browser.close());
  await mkdir(resolve('../artifacts/vscode-concept'), {recursive: true});
  for (const language of ['en', 'zh']) {
    b.session.commands.close();
    const page = await browser.newPage({viewport: {width: 340, height: 850}});
    const errors: string[] = []; const actions: string[] = []; let revision = 0; let open = true;
    let finishReload: (() => void) | undefined;
    page.on('pageerror', error => errors.push(error.message));
    const publish = async () => {
      if (open) await page.evaluate(encoded => window.postMessage({kind: 'snapshot', snapshot: JSON.parse(encoded)}, '*'), JSON.stringify({...b.session.snapshot(), revision: ++revision}));
    };
    const changed = () => {void publish().catch(error => {if (open) errors.push(String(error));});};
    b.session.on('change', changed);
    try {
      await page.exposeFunction('bridge', async (request: WebRequest) => {
        const data = request.data ?? {}; let result: unknown; let error: string | undefined;
        actions.push(request.action);
        try {
          switch (request.action) {
            case 'ready': result = {...b.session.snapshot(), revision: ++revision}; break;
            case 'draft': b.session.draftText = data.text; break;
            case 'command.open': await b.session.commands.open(data.actionId); break;
            case 'command.select': await b.session.commands.select(data.surfaceId, data.index); break;
            case 'command.close': b.session.commands.dismiss(data.surfaceId); break;
            case 'skill.reload':
              await b.session.commands.reloadSkills(data.surfaceId);
              await new Promise<void>(done => {finishReload = done;}); break;
            case 'skill.open': assert(b.session.commands.skill(data.surfaceId, data.name).details?.location); break;
            case 'skill.copy': {
              const item = b.session.commands.skill(data.surfaceId, data.name);
              await copySkillToWorkspace(b.cwd, item.id!, item.details!.location);
              await b.client.submitAction('skills.reload', {}); break;
            }
            default: throw new Error('Unexpected browser request: ' + request.action);
          }
        } catch (reason) {error = String(reason); errors.push(`${request.action}: ${error}`); t.diagnostic(`${request.action}: ${error}`);}
        await page.evaluate(message => window.postMessage(message, '*'), {kind: 'response', id: request.id, result, error});
      });
      await page.addInitScript({content: 'window.acquireVsCodeApi = () => ({postMessage: value => void window.bridge(value), getState: () => null, setState: () => {}});'});
      await page.goto(`http://127.0.0.1:${(server.address() as {port: number}).port}/${language}`);
      await page.locator('#commands').click();
      await page.locator('.menu-search').fill(language === 'zh' ? '技能' : 'skills');
      await page.locator('[data-action="skills.show"][role="option"]').click();
      const search = page.locator('.skill-search'); await search.waitFor();
      // Force the first panel projection to render before its action reply.
      await until(() => finishOpen); finishOpen!(); finishOpen = undefined;
      await page.waitForFunction(() => document.querySelector('#workbench')?.getAttribute('aria-busy') === 'false');
      await page.evaluate(() => Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {}))));
      await page.screenshot({path: resolve(`../artifacts/vscode-concept/skills-${language}.png`)});
      // Name and localized title both find the same backend-owned entry.
      await search.fill(language === 'zh' ? '演示文稿' : 'pptx');
      const pptx = page.locator('[data-skill="pptx"]'); await pptx.waitFor();
      const selectBefore = actions.filter(action => action === 'command.select').length;
      await pptx.locator('.skill-name').click(); await pptx.locator('.skill-details').waitFor();
      assert.equal(actions.filter(action => action === 'command.select').length, selectBefore);
      assert.equal(await pptx.locator('[role="switch"]').getAttribute('aria-checked'), 'true');
      await pptx.locator('[data-row="skill.open:pptx"]').click(); assert(actions.includes('skill.open'));
      await page.waitForFunction(() => [...document.querySelectorAll<HTMLButtonElement>('.skill-actions button')].every(button => !button.disabled));
      await page.evaluate(() => Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {}))));
      await page.screenshot({path: resolve(`../artifacts/vscode-concept/skills-detail-${language}.png`)});
      // Supply the same host theme tokens VS Code injects; retain production layout.
      await page.evaluate(() => {
        document.body.classList.add('vscode-light');
        for (const [key, value] of Object.entries({'sideBar-background': '#f4f3ef', 'editor-background': '#fffdf9', 'input-background': '#fffefa', foreground: '#293238', descriptionForeground: '#626d74', 'panel-border': '#d3d4cf', 'list-hoverBackground': '#edece6'})) document.documentElement.style.setProperty(`--vscode-${key}`, value);
      });
      await page.waitForFunction(() => getComputedStyle(document.querySelector('.skill-name')!).color === 'rgb(41, 50, 56)' && getComputedStyle(document.querySelector('.skill-search')!).backgroundColor === 'rgb(255, 254, 250)');
      await page.screenshot({path: resolve(`../artifacts/vscode-concept/skills-light-${language}.png`)});
      await page.evaluate(() => {
        document.body.classList.remove('vscode-light');
        for (const key of ['sideBar-background', 'editor-background', 'input-background', 'foreground', 'descriptionForeground', 'panel-border', 'list-hoverBackground']) document.documentElement.style.removeProperty(`--vscode-${key}`);
      });
      await page.emulateMedia({reducedMotion: 'reduce'});
      const togglePosition = await pptx.locator('[role="switch"]').boundingBox();
      await pptx.locator('[role="switch"]').click();
      await page.waitForFunction(() => {const toggle = document.querySelector<HTMLButtonElement>('[data-skill="pptx"] [role="switch"]'); return toggle?.getAttribute('aria-checked') === 'false' && !toggle.disabled;});
      assert.equal(await search.inputValue(), language === 'zh' ? '演示文稿' : 'pptx');
      assert(await pptx.locator('.skill-details').isVisible());
      assert.equal(await page.evaluate(() => (document.activeElement as HTMLElement)?.dataset.row), 'skill.toggle:pptx');
      assert.deepEqual(await pptx.locator('[role="switch"]').boundingBox(), togglePosition);
      await pptx.locator('[role="switch"]').click();
      await page.waitForFunction(() => document.querySelector('[data-skill="pptx"] [role="switch"]')?.getAttribute('aria-checked') === 'true');
      await page.emulateMedia({reducedMotion: 'no-preference'});
      await search.fill('plain-skill');
      assert.equal(await page.locator('[data-skill="plain-skill"] .skill-title').textContent(), `plain-skill${language === 'zh' ? '工作区' : 'Workspace'}`);
      await page.locator('[data-row="skill.source:builtin"]').click();
      assert(await page.getByText(language === 'zh' ? '没有匹配的技能，试试其他名称或来源。' : 'No matching skills. Try another name or source.', {exact: true}).isVisible());
      await page.locator('[data-row="skill.source:project"]').click();
      assert(await page.locator('[data-skill="plain-skill"]').isVisible());
      await search.fill('partial-skill');
      assert((await page.locator('[data-skill="partial-skill"] .skill-title').textContent())!.includes(language === 'zh' ? '部分翻译' : 'partial-skill'));
      assert.equal(await page.locator('[data-skill="partial-skill"] .skill-copy small').textContent(), 'Custom standard skill');
      await page.locator('[data-row="skill.source:"]').click();
      await search.fill(language === 'zh' ? 'docx' : 'pptx');
      await page.locator('[data-row="skill.source:builtin"]').click();
      const target = language === 'zh' ? 'docx' : 'pptx'; const row = page.locator(`[data-skill="${target}"]`);
      if (await row.locator('.skill-name').getAttribute('aria-expanded') !== 'true') await row.locator('.skill-name').click();
      await row.locator(`[data-row="skill.copy:${target}"]`).click();
      await page.waitForFunction(name => document.querySelector(`[data-skill="${name}"] .skill-source`)?.getAttribute('data-source') === 'project', target);
      assert.equal(await page.locator('[data-row="skill.source:project"]').getAttribute('aria-pressed'), 'true');
      assert.equal(await row.locator(`[data-row="skill.copy:${target}"]`).count(), 0);
      assert(await readFile(join(b.cwd, '.rcoder/skills', target, 'SKILL.md'), 'utf8'));
      await page.locator('[data-row="skill.reload"]').click();
      await until(() => finishReload);
      assert.equal(await search.inputValue(), target);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      // Press the key at the user's actual focus while the reload reply is delayed.
      await page.keyboard.press('Escape'); await page.locator('#workbench').waitFor({state: 'hidden'});
      await until(() => !b.session.commands.surface);
      finishReload!();
      await page.waitForFunction(() => document.querySelector('[data-row="skill.reload"]')?.getAttribute('data-pending') === 'false');
      assert(await page.locator('#workbench').isHidden(), 'A delayed reload reply must not reopen the panel');
      assert.deepEqual(errors, []);
    } finally {finishOpen?.(); finishReload?.(); open = false; b.session.off('change', changed); await page.close();}
  }
});
