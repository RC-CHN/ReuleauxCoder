import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import type {HostSnapshot} from '../src/shared.js';
import {browserHarness} from './browser-harness.js';
import {overview} from './webview-fixture.js';
import {backend, until} from './helpers.js';

test('live workbench: clocks, readable facts, stable disclosures and motion in both languages', {timeout: 40000}, async () => {
  await mkdir(resolve('../artifacts/vscode-concept'), {recursive: true});
  for (const language of ['en', 'zh-CN']) {
    // Deliberately use a remote host clock five hours ahead of the browser.
    const hostTime = Date.now() + 5 * 3600_000;
    const data = structuredClone(overview); data.activity = 'Reasoning'; data.goal = null;
    data.processes = [{id: 'proc', command: 'npm test', state: 'running', elapsed: 6, observedAt: hostTime - 90_000, output: 'Checking the implementation…'}];
    data.diagnostics = [{path: 'src/main.ts', errors: 2, warnings: 1}];
    const state: HostSnapshot = {hostId: 'live', revision: 1, draftRevision: 0, phase: 'ready', generation: 0, environment: 'SSH', workspace: '/project', model: 'test', running: true, sampledAt: hostTime, steering: {queued: 0, pending: false, stopping: false, supported: true}, overview: data, reviews: [], cells: [{id: 'reply', role: 'assistant', text: 'Checking the implementation and preserving **existing behavior**.'}], draftItems: [], draftText: ''};
    const h = await browserHarness(() => state, undefined, language, 360);
    const {page} = h;
    const publish = async () => {state.revision++; await h.publish();};
    try {
      assert.equal(await page.locator('#activity').getAttribute('data-state'), 'thinking');
      assert(await page.locator('#activity').isVisible());
      await page.locator('#overview-toggle').click();
      const clock = page.locator('#overview [data-elapsed-key="process:proc"]');
      assert.equal(await clock.textContent(), '1:36');
      const processButton = page.locator('[data-overview-action="process:proc"]');
      await processButton.focus();
      await page.waitForTimeout(2200);
      assert.match(await clock.textContent() ?? '', /^1:(?:3[89]|4\d)$/);
      assert(await processButton.evaluate(node => node === document.activeElement), 'Clock ticks must not replace controls or steal focus');
      await page.locator('.git-file').first().click(); assert(h.requests.some(request => request.action === 'openFile'));
      assert.notEqual(await page.locator('.git-added').first().evaluate(node => getComputedStyle(node).color), await page.locator('.git-deleted').first().evaluate(node => getComputedStyle(node).color));
      await page.locator('#overview-toggle').click();
      state.overview!.activity = 'Writing'; await publish();
      await page.waitForFunction(() => document.querySelector('#activity')?.getAttribute('data-state') === 'responding');
      // A late model delta cannot hide a request waiting for the user.
      state.reviews = [{id: 'pending', title: 'edit_file', summary: 'Review this change', documents: []}]; await publish();
      await page.waitForFunction(() => document.querySelector('#activity')?.getAttribute('data-state') === 'waiting');
      state.reviews = []; state.steering!.queued = 1; state.overview!.activity = 'shell'; await publish();
      await page.locator('#steer').waitFor({state: 'visible'});
      assert.equal(await page.locator('#activity').getAttribute('data-state'), 'tool');
      await page.locator('#overview-toggle').click();
      await page.locator('#overview').evaluate(node => {node.scrollTop = 0;});
      await page.waitForTimeout(200);
      await page.screenshot({path: resolve(`../artifacts/vscode-concept/live-workbench-${language}.png`), animations: 'disabled'});
      await page.evaluate(() => {
        document.body.classList.add('vscode-light');
        for (const [name, value] of Object.entries({'sideBar-background': '#f4f3ef', 'editor-background': '#fffdf9', 'input-background': '#fffefa', foreground: '#293238', descriptionForeground: '#626d74', 'panel-border': '#d3d4cf', 'textLink-foreground': '#326b92'})) document.documentElement.style.setProperty(`--vscode-${name}`, value);
      });
      await page.locator('.git-counts').scrollIntoViewIfNeeded();
      await page.screenshot({path: resolve(`../artifacts/vscode-concept/live-overview-light-${language}.png`), animations: 'disabled'});
      assert.equal(await page.locator('.git-added').first().evaluate(node => getComputedStyle(node).color), 'rgb(57, 113, 67)', 'Missing theme colors need a legible light fallback');
      // The native-style command panel shares the process clock and freezes on exit.
      state.commandSurface = {id: 1, feature: 'processes', busy: false, canBack: false, panel: {view_type: 'process_session:proc', title: 'npm test', body: 'npm test\nrunning · 6.0s · local/pipe', items: [], children: [], filterable: false, keep_open_on_submit: true, return_to_parent_on_submit: false}};
      await publish();
      const panelClock = page.locator('#workbench [data-elapsed-key="process:proc"]');
      await panelClock.waitFor({state: 'visible'});
      assert.equal(await panelClock.textContent(), await clock.textContent());
      state.overview!.processes[0] = {...state.overview!.processes[0], state: 'exited', elapsed: 105, observedAt: hostTime};
      state.overview!.goal = {...structuredClone(overview.goal!), status: 'paused', time_used_seconds: 65};
      await publish();
      await page.waitForFunction(() => document.querySelector('#workbench .elapsed')?.textContent === '1:45');
      assert.equal(await page.locator('#goal-strip .elapsed').textContent(), '1:05');
      await page.waitForTimeout(1100);
      assert.equal(await panelClock.textContent(), '1:45');
      assert.equal(await page.locator('#goal-strip .elapsed').textContent(), '1:05');
      state.overview!.processes[0].state = 'running'; state.commandSurface = undefined; await publish();
      state.phase = 'failed'; await publish();
      await page.waitForFunction(() => document.querySelector('#activity')?.getAttribute('data-state') === 'failed');
      const frozen = await clock.textContent(); await page.waitForTimeout(1100);
      assert.equal(await clock.textContent(), frozen, 'Connection loss must stop estimating elapsed time');
      await page.emulateMedia({reducedMotion: 'reduce'});
      assert.equal(await page.locator('.activity-mark i').first().evaluate(node => getComputedStyle(node).animationName), 'none');
      assert(await page.locator('body').evaluate(node => node.scrollWidth <= innerWidth), 'The narrow workbench must not overflow horizontally');
      assert.deepEqual(h.errors, []);
    } finally {await h.close();}
  }
});

test('real core: send guidance, promote once, retain the next draft and stop separately', {timeout: 30000}, async () => {
  const b = await backend(); let revision = 0;
  const h = await browserHarness(() => ({...b.session.snapshot(), revision: ++revision}), async request => {
    const data = request.data ?? {};
    if (request.action === 'send') b.session.submit(data.id, data.text, data.items, data.generation);
    if (request.action === 'draft') b.session.draftText = data.text;
    if (request.action === 'steer') return b.session.promoteSteering();
    if (request.action === 'stop') return b.client.peer.request('runtime.stop');
    return null;
  }, 'zh-CN');
  const changed = () => {void h.publish().catch(() => {});}; b.session.on('change', changed);
  try {
    await h.page.locator('#composer').fill('wait'); await h.page.locator('#composer').press('Enter');
    await until(() => b.client.state.running);
    await h.page.locator('#composer').fill('先检查配置'); await h.page.locator('#composer').press('Enter');
    await h.page.locator('#steer').waitFor({state: 'visible'});
    assert.equal(await h.page.locator('#composer').inputValue(), '');
    await h.page.locator('#composer').fill('下一条尚未发送');
    await h.page.locator('#steer').click();
    await until(() => b.client.state.interrupt_pending);
    assert(!b.client.state.stopping); assert(await h.page.locator('#steer').isDisabled());
    assert.equal(await h.page.locator('#composer').inputValue(), '下一条尚未发送');
    assert.equal((await b.session.promoteSteering()).outcome, 'already_promoted');
    assert.equal(await b.client.peer.request('test.drain_steering'), 1);
    assert.equal((await b.session.promoteSteering()).outcome, 'nothing_pending');
    assert(!b.client.state.stopping);
    await h.page.locator('#stop').click(); await until(() => !b.client.state.running);
    await h.page.locator('#steering-status').waitFor({state: 'hidden'});
    assert.equal(await h.page.locator('#composer').inputValue(), '下一条尚未发送');
    assert.deepEqual(h.errors, []);
  } finally {b.session.off('change', changed); await h.close(); await b.close();}
});
