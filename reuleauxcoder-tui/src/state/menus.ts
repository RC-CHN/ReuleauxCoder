import type {Action, Json, Parameter} from '@reuleauxcoder/client';

export interface Menu {name: string; title: string; actions: Action[]}
export function menusFromCatalog(actions: Action[]): Menu[] {
  const menus = new Map<string, Menu>();
  for (const action of actions) {
    const trigger = action.triggers.find(item => item.kind === 'slash');
    if (!trigger) throw new Error(`Action has no menu entry: ${action.action_id}`);
    const name = trigger.value.split(/\s+/)[0];
    if (!menus.has(name)) menus.set(name, {name, title: humanize(name.slice(1)), actions: []});
    menus.get(name)!.actions.push(action);
  }
  return [...menus.values()].sort((a, b) => a.name.localeCompare(b.name));
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
