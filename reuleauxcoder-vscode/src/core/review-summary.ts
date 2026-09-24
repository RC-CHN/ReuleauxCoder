import type {PendingInteraction} from '@reuleauxcoder/client';
import type {ReviewSummary} from '../shared.js';

/** Bounded presentation facts; approval identities and decisions remain core-owned. */
export function reviewSummary(request: PendingInteraction['request'], dirty: boolean): ReviewSummary {
  const context = request.context;
  const sections = request.sections ?? [];
  const args = sections.find((section: any) => section.id === 'args' && section.content && typeof section.content === 'object')?.content;
  const command = context?.tool_name === 'shell' && typeof args?.command === 'string' ? args.command : undefined;
  const preview = sections.filter((section: any) => section.kind !== 'diff').map((section: any) => {
    const content = typeof section.content === 'string' ? section.content : JSON.stringify(section.content, null, 2) ?? '';
    return {title: section.title, content: content.slice(0, 6000), truncated: content.length > 6000, secondary: command !== undefined && section.id === 'args'};
  });
  if (command !== undefined) preview.unshift({title: 'Command', content: command.slice(0, 6000), truncated: command.length > 6000, secondary: false});
  return {
    id: request.request_id, title: request.title, summary: request.summary, dirty,
    grants: request.grant_options, context: context?.subagent_task ?? '',
    tool: context?.tool_name, source: context?.tool_source, reason: context?.reason,
    cwd: command !== undefined ? typeof args.cwd === 'string' ? args.cwd : null : undefined,
    preview,
    documents: (request.documents ?? []).map((doc: any) => ({id: doc.id, path: doc.path})),
  };
}
