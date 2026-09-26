/** Python's fixed data-contract codec. Symbols cannot collide with user JSON keys. */
export type Json = null | boolean | number | string | Json[] | {[key: string]: Json};
export const recordType = Symbol('recordType');
export type RecordData = {[key: string]: any; [recordType]?: string};

export function decode(value: Json): any {
  if (Array.isArray(value)) return value.map(decode);
  if (value === null || typeof value !== 'object') return value;
  const tag = value.$type;
  if (tag === 'tuple' || tag === 'frozenset') return (value.items as Json[]).map(decode);
  if (tag === 'dict') return Object.fromEntries(Object.entries(value.items as {[key: string]: Json}).map(([key, item]) => [key, decode(item)]));
  if (typeof tag === 'string') {
    if ('value' in value) return value.value;
    const fields = decode(value.fields);
    Object.defineProperty(fields, recordType, {value: tag});
    return fields;
  }
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, decode(item)]));
}

export function typeOf(value: unknown): string | undefined {
  return value !== null && typeof value === 'object' ? (value as RecordData)[recordType] : undefined;
}
export const record = (name: string, fields: {[key: string]: Json}): Json => ({$type: name, fields});
export const enumValue = (name: string, value: string): Json => ({$type: name, value});
export const tuple = (items: Json[], type = 'tuple'): Json => ({$type: type, items});
export const actionRequest = (id: string, command: {[key: string]: Json} = {}): Json => record('ActionRequest', {action_id: id, command});

export interface Parameter {name: string; kind: 'text' | 'integer' | 'boolean'; required: boolean; nullable: boolean; default: string | number | boolean | null}
export interface Action {action_id: string; feature_id: string; description: string; preview: boolean; parameters: Parameter[]; triggers: {kind: string; value: string}[]}
export interface RuntimeState {
  support_modal?: string[];
  goal?: Goal | null;
  revision: number; session_id: string | null; agent_id: string | null; session_generation: number;
  running: boolean; stopping: boolean; interrupt_pending: boolean;
  queued_commands: string[]; queued_steering: string[]; model: string;
  context_tokens: number; context_limit: number; mcp_enabled: number; mcp_tools: number;
  mcp_state: string; workspace: string; exit_saved_session_id: string | null;
  approval_waiting: number;
  mode?: string | null; approval_policy?: string;
}
export interface ImageReference {
  attachment_id: string; variant_id: string; mime_type: string;
  width: number; height: number; size_bytes: number;
  original_width: number; original_height: number; name: string; turn_id: string | null;
}
export interface AttachmentReference {
  attachment_id: string; name: string; mime_type: string; size_bytes: number;
  /** POSIX-style path relative to the backend workspace. */
  path: string;
}
export interface Goal {
  id: string; objective: string;
  status: 'active' | 'paused' | 'blocked' | 'usage_limited' | 'budget_limited' | 'complete';
  token_budget: number | null; tokens_used: number; estimated_requests: number;
  time_used_seconds: number; created_at: number; updated_at: number;
}
export interface GitFile {path: string; index: string; worktree: string; conflict: boolean}
export interface GitWorkspace {
  available: boolean; branch: string; head: string; upstream: string | null;
  ahead: number | null; behind: number | null; files: GitFile[];
  additions: number | null; deletions: number | null; truncated: boolean; reason: string | null;
}
export interface PanelItemDetails {source: string; location: string; description: string; titles: [string, string][]; summaries: [string, string][]; icon: string; category: string}
export interface PanelItem {label: string; description: string; current: boolean; id?: string | null; details?: PanelItemDetails | null; action: {action_id: string; command: {[key: string]: Json}} | null}
export interface Panel {view_type: string; title: string; items: PanelItem[]; children: [string, Panel][]; filterable: boolean; keep_open_on_submit: boolean; return_to_parent_on_submit: boolean; show_auxiliary_actions?: boolean; body?: string; output?: string | null; on_open?: PanelItem['action']}
export interface View {action: string; title: string; view_model: RecordData; focus: boolean; reuse_key: string | null}
export interface UIEvent {message: string; level: string; kind: string; payload: RecordData | null; data: RecordData; timestamp: number}
export interface Interaction extends RecordData {request_id: string; title: string}
export interface PendingInteraction {kind: string; request: Interaction; expiresAt: number | null; resolve: (value: Json) => void; timer?: ReturnType<typeof setTimeout>}

export const emptyState: RuntimeState = {revision: 0, session_id: null, agent_id: null, session_generation: 0, running: false, stopping: false, interrupt_pending: false, queued_commands: [], queued_steering: [], model: '', context_tokens: 0, context_limit: 0, mcp_enabled: 0, mcp_tools: 0, mcp_state: 'ready', workspace: '', exit_saved_session_id: null, approval_waiting: 0};

export function cancellation(kind: string): Json {
  switch (kind) {
    case 'confirm': return record('ConfirmResponse', {confirmed: false, cancelled: true});
    case 'choose_one': return record('ChooseOneResponse', {selected_id: null, cancelled: true});
    case 'input_text': return record('InputTextResponse', {value: null, cancelled: true});
    case 'review': return record('ReviewResponse', {approved: false, cancelled: true, action: 'deny', reason: 'Interaction cancelled'});
    default: throw new Error(`Unknown interaction kind: ${kind}`);
  }
}
