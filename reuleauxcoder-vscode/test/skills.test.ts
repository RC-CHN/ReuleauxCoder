import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp, mkdir, writeFile, readFile, rm, symlink} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import type {PanelItem} from '@reuleauxcoder/client';
import {skillText, skillCategory, localizedSkillText} from '../src/skill-text.js';
import {skillIcon} from '../src/webview/icons.js';
import {setLocale} from '../src/i18n.js';
import {copySkillToWorkspace} from '../src/core/skill-files.js';
import {backend, until} from './helpers.js';

test('optional presentation falls back to standard fields and generic symbols', () => {
  const row: PanelItem = {label: 'custom-skill', description: 'Standard description', current: true, action: null};
  try {
    for (const locale of ['en', 'zh-CN']) {
      setLocale(locale); assert.deepEqual(skillText(row), {label: row.label, description: row.description});
      assert.equal(localizedSkillText(undefined, 'fallback'), 'fallback');
      assert.equal(localizedSkillText([['en', '']], 'fallback'), 'fallback');
      assert.equal(skillIcon('unrecognized-icon'), 'skills'); assert.equal(skillIcon('constructor'), 'skills');
    }
    const details = {source: 'project', location: '/tmp/SKILL.md', description: 'Trigger', titles: [['zh-cn', '中文名称']] as [string, string][], summaries: [['en', 'English summary']] as [string, string][], icon: '', category: ''};
    assert.deepEqual(skillText({...row, details}), {label: '中文名称', description: 'English summary'});
    assert.equal(skillCategory('future-category'), '其他技能');
    setLocale('en'); assert.deepEqual(skillText({...row, details}), {label: 'custom-skill', description: 'English summary'});
  } finally {setLocale('en');}
});

test('workspace copy preserves full resources and refuses collisions, path escape and links', async t => {
  const root = await mkdtemp(join(tmpdir(), 'rcoder-skill-files 中文-')); t.after(() => rm(root, {recursive: true, force: true}));
  const source = join(root, 'source'); const workspace = join(root, 'workspace');
  await mkdir(join(source, 'references'), {recursive: true}); await mkdir(workspace);
  await writeFile(join(source, 'SKILL.md'), '---\nname: example\ndescription: Standard\n---\nBody');
  await writeFile(join(source, 'references', 'guide.md'), 'Reference 中文'); await writeFile(join(source, 'LICENSE'), 'License');
  const result = await copySkillToWorkspace(workspace, 'example', join(source, 'SKILL.md'));
  assert.equal(await readFile(join(workspace, '.rcoder/skills/example/references/guide.md'), 'utf8'), 'Reference 中文');
  assert.equal(await readFile(join(workspace, '.rcoder/skills/example/LICENSE'), 'utf8'), 'License');
  await writeFile(result, 'User edit');
  await assert.rejects(copySkillToWorkspace(workspace, 'example', join(source, 'SKILL.md')), /already exists/);
  assert.equal(await readFile(result, 'utf8'), 'User edit');
  await assert.rejects(copySkillToWorkspace(workspace, '../escape', join(source, 'SKILL.md')), /Invalid skill/);
  try {await symlink(join(source, 'LICENSE'), join(source, 'link'));}
  catch (error) {if ((error as NodeJS.ErrnoException).code === 'EPERM') {t.diagnostic('Symlink creation unavailable on this host'); return;} throw error;}
  await assert.rejects(copySkillToWorkspace(workspace, 'linked', join(source, 'SKILL.md')), /symbolic links/);
  await rm(join(source, 'link'));
  const other = join(root, 'other'); await mkdir(other);
  const linkedWorkspace = join(root, 'linked-workspace'); await mkdir(linkedWorkspace); await symlink(other, join(linkedWorkspace, '.rcoder'), 'junction');
  await assert.rejects(copySkillToWorkspace(linkedWorkspace, 'example', join(source, 'SKILL.md')), /inside this workspace/);
});

test('real core projects dynamic skill metadata and rejects stale or invented file actions', async t => {
  const b = await backend(); t.after(() => b.close());
  const commands = b.session.commands;
  await commands.open('skills.show'); await until(() => commands.surface?.panel?.view_type === 'skills' && !commands.surface.busy);
  let surface = commands.surface!;
  const pptx = commands.skill(surface.id, 'pptx');
  assert.equal(pptx.details!.source, 'builtin'); assert.equal(new Map(pptx.details!.titles).get('zh-cn'), '演示文稿');
  assert.throws(() => commands.skill(surface.id, '../../config.yaml'), /no longer available/);
  const custom = join(b.cwd, '.rcoder/skills/plain-skill'); await mkdir(custom, {recursive: true});
  await writeFile(join(custom, 'SKILL.md'), '---\nname: plain-skill\ndescription: Only standard fields\n---\nUse normally');
  const oldId = surface.id;
  await commands.reloadSkills(surface.id); await until(() => commands.surface?.panel?.items.some(item => item.id === 'plain-skill') && !commands.surface.busy);
  assert.throws(() => commands.skill(oldId, 'pptx'), /panel changed/);
  surface = commands.surface!;
  const plain = commands.skill(surface.id, 'plain-skill');
  assert.equal(plain.details!.source, 'project'); assert.deepEqual(plain.details!.titles, []);
  const index = surface.panel!.items.findIndex(item => item.id === 'plain-skill');
  await commands.select(surface.id, index); await until(() => commands.surface?.panel?.items.find(item => item.id === 'plain-skill')?.current === false);
  commands.close(); assert.throws(() => commands.skill(surface.id, 'plain-skill'), /panel changed/);
});
