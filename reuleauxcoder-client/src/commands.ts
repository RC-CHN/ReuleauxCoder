import type {Action} from './wire.js';

const normalize = (text: string) => text.trim().replace(/^\/+/, '').toLowerCase().replace(/\s+/g, ' ');
export function slashNames(action: Action): string[] {
  return action.triggers.filter(trigger => trigger.kind === 'slash').map(trigger => normalize(trigger.value).replace(/\s+[<\[].*$/, ''));
}

/** Exact commands outrank command prefixes; words outrank incidental substrings. */
export function commandRank(query: string, commands: string[], description: string): number {
  const search = normalize(query);
  if (!search) return 0;
  const names = commands.map(normalize);
  if (names.includes(search)) return 0;
  if (names.some(name => name.startsWith(search + ' '))) return 1;
  if (names.some(name => name.startsWith(search))) return 2;
  const text = normalize(description), terms = search.split(' '), words = text.split(/[\s._/-]+/);
  if (terms.every(term => words.some(word => word.startsWith(term)))) return 3;
  return terms.every(term => text.includes(term)) ? 4 : Infinity;
}

/** The short /model <profile> syntax remains valid, but needs no duplicate menu row. */
export function menuActions(actions: Action[]): Action[] {
  const hasMain = actions.some(action => action.action_id === 'model.use_main');
  const scope = (action: Action) => /\[(global|workspace)\]/.test(action.description) ? 2 : action.preview ? 0 : 1;
  return actions.filter(action => !(hasMain && action.action_id === 'model.switch')).sort((left, right) => scope(left) - scope(right));
}
