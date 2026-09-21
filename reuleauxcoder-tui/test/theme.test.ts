import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp, mkdir, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {loadTheme} from '../src/ui/theme-config.js';
import {fit, paint, resolveTheme} from '../src/ui/theme.js';
import sliceAnsi from 'slice-ansi';
import stringWidth from 'string-width';

test('cached row fitting preserves ANSI closures and Unicode clipping across widths', () => {
  const samples = ['plain', '中文👩🏽‍💻é', '\x1b[31mopen color', '\x1b[44m\x1b[31mred\x1b[39m', paint.selected(paint.bold('Selected 中文')), ''];
  for (let repeat = 0; repeat < 3; repeat++) for (const text of samples) for (const width of [0, 1, 4, 20, 100]) {
    const clipped = sliceAnsi(text, 0, width);
    assert.equal(fit(text, width), clipped + ' '.repeat(Math.max(0, width - stringWidth(clipped))));
  }
  for (let i = 0; i < 2000; i++) fit(paint.accent(`row-${i}`), 80);
  assert.equal(fit('plain', 8), 'plain   ', 'eviction preserves results');
});

test('project theme defaults, CLI preset and custom file have explicit precedence', async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder-theme-'));
  t.after(() => rm(cwd, {recursive: true, force: true}));
  assert.deepEqual(await loadTheme(undefined, cwd), resolveTheme('workbench'));
  await mkdir(join(cwd, '.rcoder'));
  await writeFile(join(cwd, '.rcoder/tui-theme.json'), JSON.stringify({extends: 'ember', accent: '#12ABef'}));
  const project = await loadTheme(undefined, cwd);
  assert.equal(project.accent, '#12ABef');
  assert.equal(project.warning, resolveTheme('ember').warning);
  assert.equal(project.secondary, resolveTheme('ember').secondary);
  assert.deepEqual(await loadTheme('terminal', cwd), resolveTheme('terminal'));
  assert.deepEqual(await loadTheme('ocean', cwd), resolveTheme('ocean'));
  await writeFile(join(cwd, 'custom.json'), JSON.stringify({extends: 'ocean', selectionText: 'white'}));
  assert.equal((await loadTheme('custom.json', cwd)).selectionText, 'white');
  await assert.rejects(loadTheme('missing.json', cwd), /ENOENT/);
});

test('invalid theme configuration fails with its file and field, before terminal startup', async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'rcoder-theme-'));
  t.after(() => rm(cwd, {recursive: true, force: true}));
  await writeFile(join(cwd, 'theme.json'), '{"accent":"not-a-color"}');
  await assert.rejects(loadTheme('theme.json', cwd), /theme\.json: Invalid accent color/);
  assert.throws(() => resolveTheme({extends: 'missing'}), /Unknown theme/);
  assert.throws(() => resolveTheme({acccent: 'red'}), /Unknown theme color: acccent/);
  assert.throws(() => resolveTheme({accent: '\x1b[2J'}), /Invalid accent color/);
});
