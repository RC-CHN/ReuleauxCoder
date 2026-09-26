import type {PanelItem} from '@reuleauxcoder/client';
import {isChinese, t} from './i18n.js';

export function localizedSkillText(values: [string, string][] | undefined, fallback: string): string {
  const entries = new Map((Array.isArray(values) ? values : []).filter(entry => Array.isArray(entry) && typeof entry[0] === 'string' && typeof entry[1] === 'string' && entry[1].trim()).map(([locale, value]) => [locale.toLowerCase(), value.trim()]));
  return (isChinese() ? entries.get('zh-cn') ?? entries.get('zh') : undefined) ?? entries.get('en') ?? fallback;
}
export function skillText(item: PanelItem): {label: string; description: string} {
  return {label: localizedSkillText(item.details?.titles, item.label), description: localizedSkillText(item.details?.summaries, item.details?.description ?? item.description)};
}
export function skillSource(source: string): string {
  return source === 'builtin' ? t('Bundled') : source === 'user' ? t('Global') : source === 'project' ? t('Workspace') : source;
}
export function skillCategory(category: string): string {
  return category === 'development' ? t('Development') : category === 'documents' ? t('Documents and data') : category === 'writing' ? t('Writing skills') : category === 'configuration' ? t('Configuration') : t('Other skills');
}
