/** Optional rendering benchmark; run after npm run build. No model or workspace changes. */
import {writeFile} from 'node:fs/promises';
import {browserHarness} from '../test/browser-harness.js';
import type {ChatCell, HostSnapshot} from '../src/shared.js';

const results = [];
for (const scenario of ['closed-tools', 'open-tools', 'large-code-fence']) {
  const members: ChatCell[] = Array.from({length: 240}, (_, index) => ({id: `tool-${index}`, role: 'tool', title: 'read_file', status: 'complete', detail: JSON.stringify({file_path: `src/module${index}.ts`}), text: 'Retained tool output\n'.repeat(100)}));
  const cells: ChatCell[] = [{id: 'tools', role: 'tool-group', text: '240 tool calls · 240 completed', members}, {id: 'stream', role: 'assistant', text: scenario === 'large-code-fence' ? '```ts\n' + 'const result = inspect("source");\n'.repeat(2400) : 'Streaming response'}];
  const state: HostSnapshot = {hostId: 'profile', revision: 1, draftRevision: 0, phase: 'ready', generation: 0, environment: 'Local', workspace: '/profile', running: true, model: 'fixture', reviews: [], draftItems: [], draftText: '', cells};
  const h = await browserHarness(() => state);
  try {
    const {page} = h;
    // tsx preserves names in serialized browser functions with this helper.
    await page.evaluate('window.__name = value => value');
    if (scenario === 'open-tools') await page.locator('#expand-all').click();
    await page.waitForTimeout(300);
    const cdp = await page.context().newCDPSession(page); await cdp.send('Performance.enable');
    const start = await cdp.send('Performance.getMetrics');
    const measurement = await page.evaluate(async encoded => {
      const state = JSON.parse(encoded), frames: number[] = [], tasks: number[] = [];
      let previous = performance.now(), frame = 0, updates = 0, mutations = 0;
      function tick(now: number) {frames.push(now - previous); previous = now; frame = requestAnimationFrame(tick);}
      frame = requestAnimationFrame(tick);
      const observer = new PerformanceObserver(entries => tasks.push(...entries.getEntries().map(entry => entry.duration)));
      observer.observe({entryTypes: ['longtask']});
      const changes = new MutationObserver(entries => {mutations += entries.reduce((sum, entry) => sum + entry.addedNodes.length + entry.removedNodes.length, 0);});
      changes.observe(document.querySelector('#transcript')!, {childList: true, subtree: true});
      const started = performance.now();
      await new Promise<void>(done => {
        const timer = setInterval(() => {
          state.cells.at(-1).text += '\n// More streamed text'; state.revision++;
          window.postMessage({kind: 'snapshot', snapshot: state}, '*');
          if (++updates === 100) {clearInterval(timer); setTimeout(done, 100);}
        }, 32);
      });
      cancelAnimationFrame(frame); observer.disconnect(); changes.disconnect(); frames.sort((a, b) => a - b);
      return {elapsedMs: performance.now() - started, updates, frames: frames.length, frameP95: frames[Math.floor(frames.length * .95)], maxFrame: frames.at(-1), longTasks: tasks.length, mutations, domNodes: document.querySelectorAll('*').length};
    }, JSON.stringify(state));
    const end = await cdp.send('Performance.getMetrics');
    const durations = Object.fromEntries(['ScriptDuration', 'LayoutDuration', 'RecalcStyleDuration', 'TaskDuration'].map(name => [name, (end.metrics.find(metric => metric.name === name)?.value ?? 0) - (start.metrics.find(metric => metric.name === name)?.value ?? 0)]));
    results.push({scenario, ...measurement, ...durations, errors: h.errors});
  } finally {await h.close();}
}
const report = JSON.stringify(results, null, 2) + '\n';
if (process.argv[2]) await writeFile(process.argv[2], report);
console.log(report);
