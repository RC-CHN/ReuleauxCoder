import type {Action} from '@reuleauxcoder/client';
import {t, errorText, coreText, type MessageKey} from '../i18n.js';
import type {IconName} from './icons.js';

const features: Record<string, [MessageKey, MessageKey, IconName, MessageKey]> = {
  goal: ['Goal', 'Keep working toward an objective', 'goal', 'Work'],
  model: ['Models', 'Choose models for this session or workspace', 'model', 'Preferences'],
  mode: ['Mode', 'Choose how the agent works', 'mode', 'Preferences'],
  thinking: ['Reasoning', 'Reasoning visibility and effort', 'skills', 'Preferences'],
  skills: ['Skills', 'Browse and enable reusable capabilities', 'skills', 'Tools & skills'],
  mcp: ['MCP servers', 'Manage connected tools', 'tools', 'Tools & skills'],
  sessions: ['Session history', 'Continue a previous conversation', 'history', 'Work'],
  processes: ['Processes', 'Inspect output and manage running processes', 'terminal', 'Work'],
  'subagent.jobs': ['Agents', 'Track delegated tasks and results', 'agents', 'Work'],
  shell: ['Shell', 'Choose a shell on the workspace host', 'terminal', 'Preferences'],
  approval: ['Permissions', 'Review session rules and workspace defaults', 'shield', 'Preferences'],
  system: ['Workspace', 'Context, configuration and session tools', 'settings', 'Workspace'],
};
export function featureInfo(feature: string) {
  const entry = features[feature];
  return entry ? {label: t(entry[0]), description: t(entry[1]), icon: entry[2], group: t(entry[3])} : {label: feature, description: '', icon: 'commands' as IconName, group: t('More')};
}
export function actionLabel(action: Action): string {
  const trigger = action.triggers.find(trigger => trigger.kind === 'slash')?.value ?? action.action_id;
  const description = action.description.replace(/^(?:\[[^\]]+\]\s*)+/, '');
  return errorText(description || trigger);
}
export function actionScope(action: Action): string {
  if (action.description.includes('[global]') || action.description.includes('[workspace]')) return t('Workspace default');
  if (action.description.includes('[session]')) return t('This session');
  return '';
}
export function panelText(text: string): string {
  return coreText(text);
}
export function commandItems(catalog: Action[], query: string): {action: Action; label: string; description: string; icon: IconName; group: string}[] {
  const search = query.replace(/^\//, '').trim().toLowerCase();
  const groups = new Map<string, Action[]>();
  for (const action of catalog) {const items = groups.get(action.feature_id) ?? []; items.push(action); groups.set(action.feature_id, items);}
  const result = [];
  for (const [feature, actions] of groups) {
    const info = featureInfo(feature);
    const primary = actions.find(action => action.preview && action.parameters.every(parameter => !parameter.required)) ?? actions[0];
    const candidates = search ? actions : feature === 'system' ? actions.filter(action => action.preview) : [primary];
    for (const action of candidates) {
      const main = action === primary && feature !== 'system';
      const label = main ? info.label : actionLabel(action);
      const description = main ? info.description : actionScope(action) || errorText(action.description);
      if (search && ![label, description, action.action_id, action.description, ...action.triggers.map(trigger => trigger.value)].join(' ').toLowerCase().includes(search)) continue;
      result.push({action, label, description, icon: info.icon, group: info.group});
    }
  }
  const order = ['goal', 'sessions', 'processes', 'subagent.jobs', 'skills', 'mcp', 'model', 'mode', 'thinking', 'approval', 'shell', 'system'];
  return result.sort((left, right) => (order.indexOf(left.action.feature_id) < 0 ? 99 : order.indexOf(left.action.feature_id)) - (order.indexOf(right.action.feature_id) < 0 ? 99 : order.indexOf(right.action.feature_id)));
}
