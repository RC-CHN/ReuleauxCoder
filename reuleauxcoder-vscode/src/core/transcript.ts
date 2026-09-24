import {EventEmitter} from 'node:events';
import {typeOf, type RuntimeClient, type RecordData, type SubmissionSink} from '@reuleauxcoder/client';
import type {ChatCell} from '../shared.js';

/** Host-owned, serializable presentation. Browser views receive snapshots, never RPC ownership. */
export class Transcript extends EventEmitter implements SubmissionSink {
  cells: ChatCell[] = [];
  private next = 0;
  private assistant?: ChatCell;
  private reasoning?: ChatCell;
  private tools = new Map<string, ChatCell>();
  private sent = new Map<string, ChatCell>();
  private generation = 0;
  private subscriptions: (() => void)[] = [];
  bind(client: RuntimeClient): void {
    this.dispose(); this.clear();
    const listen = (name: string, callback: (...args: any[]) => void) => {client.on(name, callback); this.subscriptions.push(() => client.off(name, callback));};
    listen('initialized', info => {
      for (const message of info.recent_conversation ?? []) this.add(message.role === 'user' ? 'user' : 'assistant', message.content);
      this.emit('change');
    });
    listen('state', state => {if (state.session_generation !== this.generation) {this.generation = state.session_generation; this.clear();} if (!state.running) this.assistant = this.reasoning = undefined; this.emit('change');});
    listen('event', (event, _wire, generation) => {
      if (generation !== undefined && generation < this.generation) return;
      if (generation !== undefined && generation > this.generation) {this.generation = generation; this.clear();}
      if (typeOf(event.payload) === 'RuntimeEventPayload') this.runtime(event.payload.event, client);
      else if (typeOf(event.payload) === 'ViewEventPayload' && typeOf(event.payload.view_model) === 'SessionResumeViewModel') {
        this.clear(); for (const entry of event.payload.view_model.entries) this.add(entry.role === 'user' ? 'user' : 'assistant', entry.content);
      } else if (event.message && !['ViewEventPayload', 'InteractionPromptPayload'].includes(typeOf(event.payload) ?? '')) this.add('notice', event.message);
      this.emit('change');
    });
    listen('completed', result => {if (result.clear_transcript) this.clear(); this.assistant = this.reasoning = undefined; this.emit('change');});
    listen('operationFailure', message => this.notice(message));
  }
  submission(id: string, text: string, status: string, detail = ''): void {
    const cell = this.sent.get(id) ?? this.add('user', text, id);
    this.sent.set(id, cell);
    if (cell.status !== 'applied' || status === 'applied') {cell.status = status; cell.detail = detail;}
    this.emit('change');
  }
  notice(text: string): void {this.add('notice', text); this.emit('change');}
  private add(role: ChatCell['role'], text: string, id = `cell-${++this.next}`): ChatCell {
    const cell = {id, role, text: String(text).slice(-262144)};
    this.cells.push(cell);
    if (this.cells.length > 2000) this.cells.splice(0, this.cells.length - 2000);
    return cell;
  }
  private runtime(event: RecordData, client: RuntimeClient): void {
    const payload = event.payload;
    if (event.agent_id && event.agent_id !== client.state.agent_id) return;
    switch (typeOf(payload)) {
      case 'TurnStarted': case 'UserSteeringApplied': {
        const value = payload.user_input;
        const text = typeof value === 'string' ? value : value?.text ?? '';
        const id = payload.submission_id ?? value?.submission_id;
        if (id) {this.submission(id, text, 'applied'); this.emit('applied', id);} else this.add('user', text);
        this.assistant = this.reasoning = undefined; break;
      }
      case 'StreamChunk': if (payload.reasoning) {this.reasoning ??= this.add('reasoning', ''); this.reasoning.text = (this.reasoning.text + payload.text).slice(-262144); break;}
      // Legacy StreamChunk and typed content deltas share the same visible stream.
      case 'AssistantContentDelta': this.assistant ??= this.add('assistant', ''); this.assistant.text = (this.assistant.text + payload.text).slice(-262144); break;
      case 'ReasoningDelta': this.reasoning ??= this.add('reasoning', ''); this.reasoning.text = (this.reasoning.text + payload.text).slice(-262144); break;
      case 'TurnFinished': case 'ChatCompleted': if (!this.assistant && payload.render_response && payload.response) this.add('assistant', payload.response); this.assistant = this.reasoning = undefined; break;
      case 'AssistantStreamInterrupted': this.assistant = this.reasoning = undefined; break;
      case 'ToolCallStarted': {this.assistant = this.reasoning = undefined; const cell = this.add('tool', JSON.stringify(payload.arguments ?? {}, null, 2)); cell.title = payload.tool_name; cell.status = 'running'; this.tools.set(payload.tool_call_id, cell); break;}
      case 'ToolOutputDelta': {const cell = this.tools.get(payload.tool_call_id); if (cell) cell.text = (cell.text + payload.text).slice(-65536); break;}
      case 'ToolCallFinished': {const cell = this.tools.get(payload.tool_call_id); if (cell) {cell.status = payload.outcome?.status ?? 'complete'; cell.text = String(payload.outcome?.content ?? payload.outcome?.summary ?? cell.text).slice(-65536);} this.tools.delete(payload.tool_call_id); break;}
    }
  }
  clear(): void {this.cells = []; this.tools.clear(); this.sent.clear(); this.assistant = this.reasoning = undefined; this.emit('reset');}
  dispose(): void {for (const dispose of this.subscriptions.splice(0)) dispose();}
}
