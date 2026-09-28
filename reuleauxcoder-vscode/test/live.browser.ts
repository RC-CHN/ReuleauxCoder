import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import type {HostSnapshot} from '../src/shared.js';
import {browserHarness} from './browser-harness.js';
import {overview} from './webview-fixture.js';
import {backend, until} from './helpers.js';
import {decode} from '@reuleauxcoder/client';

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

test('pending input keeps selections during progress and keyboard focus when controls disappear', {timeout: 30000}, async () => {
  for (const language of ['en', 'zh-CN']) {
    const state: HostSnapshot = {hostId: 'pending', revision: 1, draftRevision: 0, phase: 'ready', generation: 0, environment: 'Local', workspace: '/project', model: 'test', running: true, steering: {queued: 0, pending: false, stopping: false, supported: true}, reviews: [], cells: [], pendingInputs: [{id: 'guide', role: 'user', text: 'Check configuration before proceeding', status: 'uploading', detail: '10%'}], draftItems: [], draftText: ''};
    const h = await browserHarness(() => state, undefined, language);
    const {page} = h;
    const publish = async () => {state.revision++; await h.publish();};
    try {
      const composer = page.locator('#composer'), summary = page.locator('#pending-inputs summary');
      await composer.fill('Next draft');
      await composer.evaluate(node => (node as HTMLTextAreaElement).setSelectionRange(2, 5));
      await summary.click();
      // Selecting a sent message to copy must survive attachment progress and admission.
      await page.locator('.pending-body > div').first().evaluate(node => {
        const range = document.createRange(); range.selectNodeContents(node);
        const selection = window.getSelection()!; selection.removeAllRanges(); selection.addRange(range);
      });
      state.pendingInputs![0].detail = '90%'; await publish();
      await page.waitForFunction(() => document.querySelector('.pending-body .detail')?.textContent === '90%');
      assert.equal(await page.evaluate(() => window.getSelection()?.toString()), state.pendingInputs![0].text);
      state.pendingInputs![0].status = 'queued'; state.pendingInputs![0].detail = ''; state.steering!.queued = 1; await publish();
      await page.locator('#steer').waitFor({state: 'visible'});
      assert.equal(await page.evaluate(() => window.getSelection()?.toString()), state.pendingInputs![0].text);
      assert(await page.locator('#pending-inputs details').evaluate(node => (node as HTMLDetailsElement).open));
      await page.locator('#steer').focus(); await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.activeElement?.id === 'composer');
      assert.equal(await composer.inputValue(), 'Next draft');
      assert.deepEqual(await composer.evaluate(node => [(node as HTMLTextAreaElement).selectionStart, (node as HTMLTextAreaElement).selectionEnd]), [2, 5]);
      await summary.focus();
      state.cells.push({...state.pendingInputs![0], status: 'applied'}); state.pendingInputs = []; state.steering!.queued = 0; await publish();
      await page.locator('#pending-inputs').waitFor({state: 'hidden'});
      assert(await composer.evaluate(node => node === document.activeElement), 'Applying a focused queue entry returns focus to the draft');
      state.pendingInputs = [{id: 'second', role: 'user', text: 'Another instruction', status: 'rejected'}]; await publish();
      await page.locator('#pending-inputs .retry').click();
      state.pendingInputs[0].status = 'sending'; await publish();
      await page.locator('#pending-inputs .retry').waitFor({state: 'hidden'});
      assert(await summary.evaluate(node => node === document.activeElement), 'Retry keeps keyboard navigation in the pending entry');
      const other = page.locator('#attach'); await other.focus();
      assert(await other.evaluate(node => node === document.activeElement));
      state.pendingInputs = []; state.steering!.queued = 0; await publish();
      await page.locator('#pending-inputs').waitFor({state: 'hidden'});
      assert(await other.evaluate(node => node === document.activeElement), 'Background application must not steal focus from another control');
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
    if (request.action === 'restorePending') return b.session.restorePending(data.id);
    if (request.action === 'stop') return b.client.peer.request('runtime.stop');
    return null;
  }, 'zh-CN');
  const applicationSnapshots: HostSnapshot[] = [];
  const changed = () => {
    const state = b.session.snapshot();
    if (state.cells.some(cell => cell.role === 'user' && cell.text === '先检查配置')) applicationSnapshots.push(state);
    void h.publish().catch(() => {});
  }; b.session.on('change', changed);
  try {
    await h.page.locator('#composer').fill('wait'); await h.page.locator('#composer').press('Enter');
    await until(() => b.client.state.running);
    await until(() => b.session.transcript.cells.some(cell => cell.text === 'wait'));
    await h.page.locator('#composer').fill('先检查配置'); await h.page.locator('#composer').press('Enter');
    await h.page.locator('#steer').waitFor({state: 'visible'});
    assert.equal(await h.page.locator('#composer').inputValue(), '');
    assert.equal(await h.page.locator('#transcript .cell.user').count(), 1, 'Queued input must not be a transcript placeholder');
    await h.page.locator('#pending-inputs summary').click();
    assert.match(await h.page.locator('#pending-inputs .pending-body').textContent() ?? '', /先检查配置/);
    await b.client.peer.request('test.stream_output', {text: '之前的回复'});
    await h.page.waitForFunction(() => document.querySelector('#transcript')?.textContent?.includes('之前的回复'));
    assert(await h.page.locator('#pending-inputs details').evaluate(node => (node as HTMLDetailsElement).open), 'Updates preserve the queue disclosure');
    await h.page.reload();
    await h.page.locator('#pending-inputs summary').waitFor();
    assert.equal(await h.page.locator('#transcript .cell.user').count(), 1, 'Reopening a view does not promote queued input');
    await h.page.screenshot({path: resolve('../artifacts/vscode-concept/steering-pending-zh-CN.png'), animations: 'disabled'});
    await h.page.locator('#composer').fill('下一条尚未发送');
    await h.page.locator('#steer').click();
    await until(() => b.client.state.interrupt_pending);
    assert(!b.client.state.stopping); assert(await h.page.locator('#steer').isDisabled());
    assert.equal(await h.page.locator('#composer').inputValue(), '下一条尚未发送');
    assert.equal((await b.session.promoteSteering()).outcome, 'already_promoted');
    assert.equal(await b.client.peer.request('test.drain_steering'), 1);
    await h.page.waitForFunction(() => document.querySelectorAll('#transcript .cell.user').length === 2);
    await h.page.locator('#pending-inputs').waitFor({state: 'hidden'});
    assert(applicationSnapshots.length > 0);
    assert(applicationSnapshots.every(state => state.overview?.queued === 0), 'Overview must clear the queued count at application, before a later runtime snapshot');
    await b.client.peer.request('test.stream_output', {text: '收到补充后的回复'});
    await h.page.waitForFunction(() => document.querySelectorAll('#transcript .cell.assistant').length === 2);
    assert.deepEqual((await h.page.locator('#transcript .cell .body').allTextContents()).map(text => text.trim()), ['wait', '之前的回复', '先检查配置', '收到补充后的回复']);
    const history = decode(await b.client.peer.request('test.messages'));
    assert.deepEqual(history.filter((message: any) => ['user', 'assistant'].includes(message.role)).map((message: any) => message.content), ['wait', '之前的回复', '先检查配置', '收到补充后的回复']);
    assert.equal((await b.session.promoteSteering()).outcome, 'nothing_pending');
    assert(!b.client.state.stopping);
    await h.page.locator('#composer').fill('这条尚未应用'); await h.page.locator('#composer').press('Enter');
    await until(() => b.client.state.queued_steering.length === 1);
    await h.page.locator('#composer').fill('下一条尚未发送');
    await h.page.locator('#stop').click(); await until(() => !b.client.state.running);
    await h.page.locator('#steering-status').waitFor({state: 'hidden'});
    assert.equal(await h.page.locator('#composer').inputValue(), '下一条尚未发送');
    await h.page.waitForFunction(() => document.querySelector('#pending-inputs .pending-state')?.textContent === '未应用');
    assert.equal(await h.page.locator('#transcript .cell.user').count(), 2);
    await h.page.getByRole('button', {name: '退回草稿', exact: true}).click();
    await h.page.waitForFunction(() => document.querySelector<HTMLTextAreaElement>('#composer')?.value === '下一条尚未发送\n这条尚未应用');
    await h.page.locator('#pending-inputs').waitFor({state: 'hidden'});
    assert.deepEqual(h.errors, []);
  } finally {b.session.off('change', changed); await h.close(); await b.close();}
});
