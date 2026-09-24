import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {translate, setLocale, t, errorText, type MessageKey} from '../src/i18n.js';
import chinese from '../l10n/bundle.l10n.zh-cn.json' with {type: 'json'};

test('English fallback, Chinese locale selection and substitution preserve dynamic text', () => {
  setLocale('zh-cn'); assert.equal(t('Start core'), '启动核心');
  assert.equal(translate('zh-TW', 'Start core'), '启动核心');
  assert.equal(translate('de', 'Start core'), 'Start core');
  assert.equal(translate('zh-cn', 'Install Reuleaux core on {0}', 'SSH: build-host'), '在SSH: build-host安装 Reuleaux 核心');
  assert.equal(errorText(new Error('model/provider output')), 'model/provider output'); setLocale('en');
});
test('all translations preserve placeholders and both manifest catalogs cover every label', async () => {
  for (const [source, translated] of Object.entries(chinese)) {
    assert(translated.trim(), source);
    assert.deepEqual([...translated.matchAll(/\{\d+\}/g)].map(match => match[0]).sort(), [...source.matchAll(/\{\d+\}/g)].map(match => match[0]).sort(), source);
    assert.equal(translate('en', source as MessageKey), source);
  }
  const manifest = await readFile(new URL('../package.json', import.meta.url), 'utf8');
  const en = JSON.parse(await readFile(new URL('../package.nls.json', import.meta.url), 'utf8'));
  const zh = JSON.parse(await readFile(new URL('../package.nls.zh-cn.json', import.meta.url), 'utf8'));
  assert.deepEqual(Object.keys(en).sort(), Object.keys(zh).sort());
  for (const [, key] of manifest.matchAll(/%([\w.]+)%/g)) {assert(en[key], key); assert(zh[key], key);}
});
