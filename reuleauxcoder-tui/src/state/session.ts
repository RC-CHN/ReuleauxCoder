import {EventEmitter} from 'node:events';
import type {GitWorkspace} from '../protocol/wire.js';
import {emptyState, typeOf, type Json, type RecordData, type RuntimeState, type UIEvent, type View} from '../protocol/wire.js';
import {diff, fields} from '../ui/format.js';
import {updateProcess, type ProcessView} from './processes.js';

export interface Cell {id: string; kind: 'user' | 'assistant' | 'reasoning' | 'tool' | 'notice'; title: string; body: string; details: string; revision: number; appendRevision?: number; streaming: boolean; tone?: string; tool?: {name: string; arguments: RecordData; outcome?: RecordData}}
export class SessionStore extends EventEmitter {
  state: RuntimeState = emptyState;
  cells: Cell[] = [];
  plan: RecordData = {items: []};
  progress: RecordData = {};
  jobs = new Map<string, RecordData>();
  processes = new Map<string, ProcessView>();
  diagnostics = new Map<string, RecordData>();
  operations = new Map<string, RecordData>();
  startup: UIEvent[] = [];
  fatal: string | null = null;
  connected = false;
  contentRevision = 0;
  sidebarRevision = 0;
  private dirtyIndex = 0;
  private cellIndices = new WeakMap<Cell, number>();
  private streaming = new Set<Cell>();
  git: GitWorkspace | null = null;
  private next = 0;
  private generation = 0;
  private assistant: Cell | undefined;
  private reasoning: Cell | undefined;
  private tools = new Map<string, Cell>();
  private reviewedDiffs = new Set<string>();

  add(kind: Cell['kind'], title: string, body: string, details = '', streaming = false): Cell {
    const cell: Cell = {id: String(++this.next), kind, title, body, details, revision: 0, streaming};
    this.cells.push(cell);
    this.cellIndices.set(cell, this.cells.length - 1);
    if (streaming) this.streaming.add(cell);
    this.dirtyIndex = Math.min(this.dirtyIndex, this.cells.length - 1);
    this.contentRevision++;
    return cell;
  }
  notice(message: string, tone = 'info') {const cell = this.add('notice', tone === 'error' ? 'Error' : 'Notice', message); cell.tone = tone; this.emit('change');}
  clear() {this.cells = []; this.streaming.clear(); this.dirtyIndex = 0; this.contentRevision++; this.sidebarRevision++; this.tools.clear(); this.jobs.clear(); this.processes.clear(); this.diagnostics.clear(); this.operations.clear(); this.reviewedDiffs.clear(); this.assistant = this.reasoning = undefined; this.plan = {items: []}; this.progress = {};}
  get activeCell(): Cell | undefined {
    let latest: Cell | undefined, tool: Cell | undefined;
    for (const cell of this.streaming) {latest = cell; if (cell.kind === 'tool') tool = cell;}
    return tool ?? latest;
  }
  takeDirtyIndex() {const index = this.dirtyIndex; this.dirtyIndex = Infinity; return index;}
  update(state: RuntimeState) {
    if (state.session_generation > this.generation) {this.clear(); this.generation = state.session_generation;}
    if (!state.running) for (const cell of this.streaming) this.finishCell(cell);
    this.state = state; this.emit('change');
  }
  initialize(info: any) {
    this.update(info.state);
    this.startup = info.startup_events;
    for (const message of info.recent_conversation) this.add(message.role === 'user' ? 'user' : 'assistant', message.role === 'user' ? 'You' : 'Reuleaux', message.content);
    this.plan = info.plan ?? {items: []}; this.progress = info.progress ?? {};
    for (const event of info.runtime_events) this.runtime(event);
    for (const event of this.startup) if (event.level === 'error' || event.level === 'warning') this.notice(event.message, event.level);
    this.connected = true; this.emit('change');
  }
  completed(result: any) {
    if (result.clear_transcript) this.clear();
    if (result.session_changed && result.plan) {this.plan = result.plan; this.progress = result.progress;}
    this.emit('change');
  }
  reviewed(request: RecordData, response: RecordData) {
    if (response.approved) for (const section of request.sections ?? []) if (section.kind === 'diff' && typeof section.content === 'string') this.reviewedDiffs.add(section.content);
  }
  event(event: UIEvent, wire?: Json, generation?: number) {
    if (generation !== undefined) {
      if (generation < this.generation) return;
      if (generation > this.generation) {this.clear(); this.generation = generation;}
    }
    const payload = event.payload;
    switch (typeOf(payload)) {
      case 'RuntimeEventPayload': this.runtime(payload!.event, payload!.generation_owner_agent_id); break;
      case 'ViewEventPayload': {
        const view = payload as unknown as View;
        if (typeOf(view.view_model) === 'SessionResumeViewModel') {
          this.clear();
          for (const entry of view.view_model.entries) this.add(entry.role === 'user' ? 'user' : 'assistant', entry.role === 'user' ? 'You' : 'Reuleaux', entry.content);
        } else this.emit('view', view, (wire as any)?.fields.payload);
        break;
      }
      case 'ReasoningNoticePayload': this.add('reasoning', payload!.title, event.message).tone = 'inline'; break;
      case 'RemoteStreamPayload': this.add('tool', payload!.tool_name, payload!.chunk); break;
      case 'InteractionPromptPayload': break; // The correlated reverse request owns this UI.
      default: if (event.message) {const cell = this.add('notice', event.kind, event.message, Object.keys(event.data ?? {}).length ? fields(event.data) : ''); cell.tone = event.level;}
    }
    this.emit('change');
  }
  runtime(event: RecordData, owner?: string) {
    const root = !event.agent_id || event.agent_id === this.state.agent_id;
    const generation = event.session_generation;
    if (root && (!owner || owner === this.state.agent_id) && generation != null) {
      if (generation < this.generation) return;
      if (generation > this.generation) {this.clear(); this.generation = generation;}
    }
    const p = event.payload;
    const type = typeOf(p);
    if (!root && !['SubagentJobChanged', 'OperationPhaseChanged', 'DiagnosticsPublished', 'DiagnosticsCleared'].includes(type ?? '')) return;
    switch (type) {
      case 'TurnStarted': case 'ChatStarted':
        this.assistant = this.reasoning = undefined;
        if (p.user_input) this.add('user', 'You', p.user_input.replace(/^\[SESSION_RESUME\][^\n]*\n\n/, '')); break;
      case 'AssistantContentDelta': case 'StreamChunk':
        if (p.reasoning) {this.appendReasoning(p); break;}
        this.finishCell(this.reasoning); this.reasoning = undefined;
        this.assistant ??= this.add('assistant', 'Reuleaux', '', '', true);
        this.assistant.body += p.text; this.touch(this.assistant, true); break;
      case 'ReasoningDelta': this.appendReasoning(p); break;
      case 'TurnFinished': case 'ChatCompleted':
        if (!this.assistant && p.render_response && p.response) this.assistant = this.add('assistant', 'Reuleaux', p.response);
        for (const cell of this.streaming) this.finishCell(cell);
        this.assistant = this.reasoning = undefined; break;
      case 'AssistantStreamInterrupted':
        this.finishCell(this.assistant); this.finishCell(this.reasoning); this.reasoning = undefined;
        this.assistant = undefined; this.add('notice', 'Steering', 'Current response interrupted to apply your next instruction.'); break;
      case 'ToolCallStarted': {
        this.finishCell(this.assistant); this.finishCell(this.reasoning);
        this.assistant = this.reasoning = undefined;
        const cell = this.add('tool', p.tool_name, 'Running…', fields(p.arguments), true);
        cell.tool = {name: p.tool_name, arguments: p.arguments};
        this.tools.set(p.tool_call_id, cell); break;
      }
      case 'ToolOutputDelta': {
        const cell = this.tools.get(p.tool_call_id);
        if (cell) {cell.body = (cell.body === 'Running…' ? '' : cell.body) + p.text; this.touch(cell);}
        else this.add('tool', p.tool_call_id, p.text);
        break;
      }
      case 'ToolCallFinished': {
        const cell = this.tools.get(p.tool_call_id) ?? this.add('tool', p.tool_name, '');
        const out = p.outcome;
        cell.tool = {name: p.tool_name, arguments: cell.tool?.arguments ?? {}, outcome: out};
        const body = [out.summary, out.content, out.stdout, out.stderr ? '[stderr]\n' + out.stderr : ''].filter(Boolean).join('\n');
        const detail = [cell.details, fields(out)].filter(Boolean).join('\n\n');
        cell.title = `${p.tool_name} · ${out.status}`;
        cell.body = body + (out.diff ? this.reviewedDiffs.has(out.diff.unified) ? '\nReviewed diff applied.' : '\n' + diff(out.diff.unified) : '');
        cell.details = detail; cell.streaming = false; this.touch(cell);
        this.streaming.delete(cell); this.tools.delete(p.tool_call_id);
        if (out.diff) this.reviewedDiffs.delete(out.diff.unified);
        cell.tone = out.status === 'succeeded' ? 'success' : 'warning';
        const snapshot = out.metadata?.process_snapshot;
        if (p.tool_name === 'shell' && snapshot && snapshot.state !== 'exited') {
          const process = this.processes.get(snapshot.session_id);
          if (process) {process.background = true; this.processCompleted(process);}
        }
        break;
      }
      case 'SubagentJobChanged': this.jobs.set(p.job_id, p); this.sidebarRevision++; break;
      case 'ProcessSessionChanged': {
        const process = updateProcess(this.processes.get(p.process_session_id), p);
        this.processes.set(p.process_session_id, process);
        this.sidebarRevision++;
        this.processCompleted(process);
        break;
      }
      case 'DiagnosticsPublished': {
        this.diagnostics.set(p.file_path, p);
        const counts = new Map<string, number>();
        for (const diagnostic of p.diagnostics) counts.set(diagnostic.severity, (counts.get(diagnostic.severity) ?? 0) + 1);
        const summary = [...counts].map(([severity, count]) => `${count} ${severity}${count === 1 || severity === 'info' ? '' : 's'}`).join(' · ');
        const cell = this.add('notice', 'LSP · ' + p.file_path, summary || 'No diagnostics', fields(p.diagnostics));
        cell.tone = counts.has('error') ? 'error' : counts.has('warning') ? 'warning' : 'info';
        break;
      }
      case 'DiagnosticsCleared': this.diagnostics.delete(p.file_path); break;
      case 'PlanUpdated': this.plan = p; break;
      case 'ProgressReported': this.progress = p; break;
      case 'OperationPhaseChanged': this.operations.set(p.operation_id, p); break;
      case 'UserSteeringApplied': this.add('user', 'You · steering applied', p.user_input); break;
      case 'ApprovalRequested': break;
      case 'ApprovalResolved': this.add('notice', p.approved ? 'Approved' : 'Denied', [p.reason, p.grant_label, p.mode, p.released_count ? `${p.released_count} queued requests released` : null, p.resolution_source].filter(Boolean).join(' · ')); break;
      default: this.add('notice', type ?? 'Runtime event', fields(p));
    }
    this.emit('change');
  }
  private appendReasoning(p: RecordData) {
    this.finishCell(this.assistant); this.assistant = undefined;
    this.reasoning ??= this.add('reasoning', 'Thinking', '', '', true);
    this.reasoning.body += p.text; this.touch(this.reasoning, this.reasoning.tone === (p.display_mode === 'inline' ? 'inline' : 'collapsed'));
    this.reasoning.tone = p.display_mode === 'inline' ? 'inline' : 'collapsed';
  }
  private processCompleted(process: ProcessView) {
    if (process.state !== 'exited' || !process.background || process.notified) return;
    process.notified = true;
    const success = process.exit_code === 0 && process.termination_reason === 'exit';
    const cell = this.add('notice', success ? 'Background process completed' : 'Background process stopped',
      `${process.command}\n${process.process_session_id} · ${process.termination_reason ?? 'exit'}${process.exit_code == null ? '' : ` · exit ${process.exit_code}`}`);
    cell.tone = success ? 'success' : 'warning';
  }
  private finishCell(cell: Cell | undefined) {
    if (cell?.streaming) {cell.streaming = false; this.streaming.delete(cell); this.touch(cell);}
  }
  private touch(cell: Cell, appended = false) {cell.revision++; if (appended) cell.appendRevision = (cell.appendRevision ?? 0) + 1; this.contentRevision++; this.dirtyIndex = Math.min(this.dirtyIndex, this.cellIndices.get(cell)!);}
}
