import type {Action, Json, Parameter} from '@reuleauxcoder/client';

export interface Menu {name: string; title: string; description: string; group: string; order: number; actions: Action[]}
const FEATURE_INFO: Record<string, {title: string; description: string; group: string; order: number}> = {
  goal: {title: 'Goal', description: 'Keep working toward an objective', group: 'Work', order: 1},
  sessions: {title: 'Session history', description: 'Continue a previous conversation', group: 'Work', order: 2},
  processes: {title: 'Processes', description: 'Inspect output and manage running processes', group: 'Work', order: 3},
  'subagent.jobs': {title: 'Agents', description: 'Track delegated tasks and results', group: 'Work', order: 4},
  skills: {title: 'Skills', description: 'Browse reusable capabilities', group: 'Tools & skills', order: 5},
  mcp: {title: 'MCP servers', description: 'Manage connected tools', group: 'Tools & skills', order: 6},
  model: {title: 'Models', description: 'Choose models for this session or workspace', group: 'Preferences', order: 7},
  mode: {title: 'Mode', description: 'Choose how the agent works', group: 'Preferences', order: 8},
  thinking: {title: 'Reasoning', description: 'Reasoning visibility and effort', group: 'Preferences', order: 9},
  approval: {title: 'Permissions', description: 'Review approval rules and session grants', group: 'Preferences', order: 10},
  shell: {title: 'Shell', description: 'Choose a shell on the workspace host', group: 'Preferences', order: 11},
  system: {title: 'Workspace', description: 'Context, configuration and session tools', group: 'Workspace', order: 12},
};
const fallback = (feature: string, order: number) => ({title: humanize(feature), description: 'Workspace actions', group: 'More', order});

export function menusFromCatalog(actions: Action[]): Menu[] {
  const menus = new Map<string, Menu>();
  for (const action of actions) {
    const trigger = action.triggers.find(item => item.kind === 'slash');
    if (!trigger) throw new Error(`Action has no menu entry: ${action.action_id}`);
    const name = trigger.value.split(/\s+/)[0];
    const info = FEATURE_INFO[action.feature_id] ?? fallback(action.feature_id, 99);
    if (!menus.has(name)) menus.set(name, {name, ...info, actions: []});
    menus.get(name)!.actions.push(action);
  }
  return [...menus.values()].sort((a, b) => a.order - b.order || a.name.localeCompare(b.name));
}
export function menuSearchText(menu: Menu): string {
  return [menu.name, menu.title, menu.description, menu.group, ...menu.actions.flatMap(action => [action.description, action.action_id, ...action.triggers.map(trigger => trigger.value)])].join(' ').toLowerCase();
}
export const humanize = (value: string) => value.replace(/_/g, ' ').replace(/^./, letter => letter.toUpperCase());
export const actionLabel = (action: Action) => action.description.replace(/^(?:\[[^\]]+\]\s*)+/, '');
export const defaults = (action: Action): {[key: string]: Json} => Object.fromEntries(action.parameters.filter(item => !item.required).map(item => [item.name, item.default]));

export function fieldValue(parameter: Parameter, input: string): Json {
  if (!input.trim()) {
    if (parameter.required) throw new Error(`${humanize(parameter.name)} is required`);
    return parameter.default;
  }
  if (parameter.kind === 'integer') {
    const value = Number(input);
    if (!Number.isSafeInteger(value)) throw new Error('Enter a whole number');
    return value;
  }
  if (parameter.kind === 'boolean') {
    if (input === 'true') return true;
    if (input === 'false') return false;
    if (parameter.nullable && input === 'auto') return null;
    throw new Error(parameter.nullable ? 'Choose true, false or auto' : 'Choose true or false');
  }
  return input;
}
