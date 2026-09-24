import {coreText, errorText, isChinese, t, type MessageKey} from './i18n.js';

const patterns = new Map<string, {prefix: string; pattern: RegExp; slots: number[]}>();
/** Match complete, known UI templates. Captured names and payloads remain verbatim. */
export function templateText(text: string, key: MessageKey, labels: readonly number[] = []): string | undefined {
  if (!isChinese()) return;
  let compiled = patterns.get(key);
  if (!compiled) {
    const slots: number[] = [];
    const pattern = key.split(/(\{\d+\})/).map(part => {
      if (/^\{\d+\}$/.test(part)) {slots.push(Number(part.slice(1, -1))); return '([\\s\\S]*?)';}
      return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }).join('');
    compiled = {prefix: key.split('{')[0], pattern: new RegExp(`^${pattern}$`), slots}; patterns.set(key, compiled);
  }
  if (!text.startsWith(compiled.prefix)) return;
  const match = compiled.pattern.exec(text); if (!match) return;
  const values: string[] = [];
  compiled.slots.forEach((slot, index) => {values[slot] = labels.includes(slot) ? errorText(match[index + 1]) : match[index + 1];});
  return t(key, ...values);
}

const notices: readonly [MessageKey, ...number[]][] = [
  ['Session saved: {0}'], ['Session auto-saved: {0}'], ['Resume with: rcoder -r {0}'], ['Resume previous with: /session {0}'],
  ["Switched session main model profile to '{0}' ({1})"], ["Switched session sub-agent model profile to '{0}' ({1})"],
  ["Set global main model profile to '{0}' ({1}) and saved to {2}"], ["Set global sub-agent model profile to '{0}' ({1}) and saved to {2}"],
  ["Unknown model profile '{0}'. Use /model to list available profiles."],
  ["Switched session mode to '{0}'"], ["Unknown mode '{0}'. Use /mode to list available modes."],
  ['Removed workspace approval rule and saved to {0}'], ['Updated workspace approval rule and saved to {0}'],
  ["No session approval rule for '{0}'."], ["No global approval rule for '{0}'."],
  ['Skill added: {0}'], ['Skill updated: {0}'], ['Skill removed: {0}'], ['Skill not found and skipped: {0}'],
  ["Skill '{0}' not found."], ["Skill '{0}' already {1}.", 1], ["Skill '{0}' {1}.", 1],
  ["MCP server '{0}' not found in config."], ["MCP server '{0}' is already {1} and is connecting.", 1], ["MCP server '{0}' is already {1}.", 1],
  ["Saved MCP server '{0}' to {1}"], ["MCP server '{0}' remains {1} in workspace config.", 1],
  ["MCP server '{0}' preference was saved, but runtime {1} failed. It will be retried on the next startup.", 1], ["MCP server '{0}' runtime {1} retry failed.", 1],
  ["MCP server '{0}' {1} and saved to {2}", 1], ["MCP server '{0}' {1}", 1],
  ['Context compacted: estimated {0} → {1} tokens.'], ['No eligible context to compact with {0}; estimated {1} tokens. Recent conversation was retained.'],
  ['Goal {0} · Tokens {1} / {2}', 0, 2], ['Reasoning display: {0}.', 0],
  ['Reasoning effort set to: [bold]{0}[/bold] (API: [dim]{1}[/dim] via [dim]{2}[/dim], was: {3}).', 0, 3],
  ["'{0}' is not available. Available values: {1}."], ["Sub-agent job '{0}' not found."],
  ['Job {0} completed.\n{1}'], ['Job {0} failed: {1}'], ['Job {0} status: {1}', 1], ['Sub-agent {0} failed: {1}', 0],
  ['Termination requested for {0} unresolved process session(s).'], ['Process {0} uses pipe mode; no input was sent.'],
  ['Process {0} is {1}; no input was sent.', 1], ['Hidden input to {0} was cancelled; no input was sent.'],
  ['Hidden input was sent to {0}; its value was not recorded.'], ['Hidden input was not confirmed for {0}: {1}'],
  ['Process operation was not confirmed: {0}'], ['Unknown process action: {0}'],
  ['Shell: {0}. Applies to new commands in this runtime.'], ['Could not discover shells: {0}'],
];

/** Apply only to notices/errors, never to assistant text, tool output or user input. */
export function coreMessage(text: string): string {
  if (!isChinese()) return text;
  for (const [key, ...labels] of notices) {const result = templateText(text, key, labels); if (result !== undefined) return result;}
  return /\s|[.!?…]/.test(text) ? errorText(text) : text;
}

export function approvalReason(text: string): string {
  if (['approved via interaction', 'denied via interaction', 'Read-only workspace access.', 'Workspace changed while approval was pending. Review the refreshed diff.'].includes(text) || text.startsWith('approved for session: ')) return coreText(text);
  return text;
}

export function approvalText(text: string): string {
  return text.split('\n').map(line => /^(?:Tool '.+' from source '.+' requires approval\.|Operation: |Targets?: |Source: sub-agent \(mode=|Sub-agent task: |Exact external target: |Workspace root: )/.test(line) || line === 'Approval grants this tool call access to this file only.' ? coreText(line) : approvalReason(line)).join('\n');
}

export function interactionText(text: string): string {
  if (!isChinese()) return text;
  const stop = 'Stop this process and its descendants?\n\n';
  if (text.startsWith(stop)) {
    const subject = text.slice(stop.length);
    return `${t('Stop this process and its descendants?')}\n\n${subject === 'all unresolved processes owned by this session' ? t('all unresolved processes owned by this session') : subject}`;
  }
  return templateText(text, 'Hidden input · {0}') ?? templateText(text, 'Compact context · estimated {0} / {1} tokens') ?? coreText(text);
}
