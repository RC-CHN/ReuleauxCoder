import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import type {ChatCell, HostSnapshot} from '../src/shared.js';
import {browserHarness} from './browser-harness.js';
import {foldToolCells} from '../src/core/tool-groups.js';

test('tool inspection: preserve disclosure state, lazy bodies, streaming selections and all-details control', {timeout: 30000}, async () => {
  const tool: ChatCell = {id: 'read', role: 'tool', title: 'read_file', status: 'complete', text: '{"ok":true,"lines":[1,2]}', detail: '{"file_path":"src/main.ts","start_line":1}'};
  const reasoning: ChatCell = {id: 'think', role: 'reasoning', text: 'Keep **existing behavior**.\n\n```ts\nconst stable = true;\n```\n\nMore reasoning'};
  const group: ChatCell = {id: 'group:read', role: 'tool-group', status: 'running', text: '2 次工具调用 · 1 已完成 · 1 执行中', members: [tool, reasoning, {id: 'shell', role: 'tool', title: 'shell', status: 'running', text: 'starting', detail: '{"command":"npm test"}'}]};
  const state: HostSnapshot = {hostId: 'tools', revision: 1, draftRevision: 0, phase: 'ready', generation: 0, environment: 'Local', workspace: '/project', model: 'test', running: true, reviews: [], cells: [group], draftItems: [], draftText: ''};
  const h = await browserHarness(() => state, undefined, 'zh-CN');
  const {page} = h;
  const publish = async () => {state.revision++; await h.publish();};
  try {
      const disclosure = page.locator('[data-tool-id="group:read"]');
      assert.equal(await disclosure.getAttribute('open'), null);
      assert.equal(await page.locator('.tool-output, .markdown').count(), 0, 'Collapsed tool bodies should not be rendered');
      await disclosure.locator(':scope > summary').click();
      await page.locator('[data-tool-id="read"] > summary').click();
      assert.equal(await page.locator('[data-tool-id="read"] dt').first().textContent(), 'file_path');
      assert((await page.locator('[data-tool-id="read"] .tool-output').textContent())?.includes('\n  "ok": true'));
      await page.locator('[data-tool-id="think"] > summary').click();
      const code = await page.locator('[data-tool-id="think"] .code-block').elementHandle();
      reasoning.text += ' updated'; group.members![2].text += '\nmore output'; await publish();
      await page.waitForFunction(() => document.querySelector('[data-tool-id="think"] .body')?.textContent?.includes('updated'));
      assert(await page.locator('[data-tool-id="think"] .code-block').evaluate((node, before) => node === before, code));
      assert(await page.locator('[data-tool-id="read"]').evaluate(node => (node as HTMLDetailsElement).open));
      await page.locator('#expand-all').click();
      assert(await page.locator('.tool-disclosure').evaluateAll(nodes => nodes.every(node => (node as HTMLDetailsElement).open)));
      await page.locator('#expand-all').click();
      assert(await page.locator('.tool-disclosure').evaluateAll(nodes => nodes.every(node => !(node as HTMLDetailsElement).open)));
      // A failed member breaks out of its group and opens once for inspection.
      state.cells = [{...tool, id: 'failure', status: 'failed', text: 'Permission denied: src/main.ts'}]; await publish();
      await page.locator('[data-tool-id="failure"] .tool-output').waitFor({state: 'visible'});
      await page.locator('[data-tool-id="failure"] > summary').click(); await publish();
      assert.equal(await page.locator('[data-tool-id="failure"]').getAttribute('open'), null);

    // Moving a previously expanded flat call into a new group keeps it open.
    state.cells = [tool]; await publish();
    await page.locator('[data-tool-id="read"] > summary').click();
    state.cells = [group]; await publish();
    await page.locator('[data-tool-id="group:read"] [data-tool-id="read"] .tool-output').waitFor({state: 'visible'});
    await mkdir(resolve('../artifacts/vscode-concept'), {recursive: true});
    await page.screenshot({path: resolve('../artifacts/vscode-concept/tools-expanded-zh.png'), animations: 'disabled'});
    // Repeated process checks retain every result, including after another snapshot.
    state.cells = foldToolCells([1, 2, 3].map(index => ({id: `poll-${index}`, role: 'tool', title: 'shell_session', status: 'complete', detail: '{"action":"poll","session_id":"proc"}', text: `result ${index}`})));
    await publish();
    await page.locator('#expand-all').click();
    await page.locator('[data-tool-id="poll-1:check:poll-3"] .tool-output').waitFor({state: 'visible'});
    assert.deepEqual(await page.locator('.tool-output').allTextContents(), ['result 1', 'result 2', 'result 3']);
    await publish();
    assert.deepEqual(await page.locator('.tool-output').allTextContents(), ['result 1', 'result 2', 'result 3']);
    state.cells = [{...tool, id: 'empty-tool', status: 'running', text: ''}]; await publish();
    await page.waitForFunction(() => document.querySelector('[data-tool-id="empty-tool"] .tool-output')?.textContent === '等待输出…');
    state.cells[0].status = 'complete'; await publish();
    await page.waitForFunction(() => document.querySelector('[data-tool-id="empty-tool"] .tool-output')?.textContent === '无输出');
    assert.deepEqual(h.errors, []);
  } finally {await h.close();}
});
