import {typeOf, type RecordData, type RuntimeClient} from '@reuleauxcoder/client';
import type {WorkOverview} from '../shared.js';

/** A bounded projection of runtime facts, independent of transcript and command navigation. */
export class WorkOverviewStore {
  private client?: RuntimeClient;
  private off: (() => void)[] = [];
  private epoch = 0;
  private generation = 0;
  private timer?: ReturnType<typeof setTimeout>;
  private activeTools = new Map<string, string>();
  private data: WorkOverview = this.empty();
  constructor(private changed: () => void) {}
  private empty(): WorkOverview {return {contextTokens: 0, contextLimit: 0, approvalPolicy: '', mcpTools: 0, queued: 0, plan: [], progress: '', activity: '', jobs: [], processes: [], diagnostics: [], warnings: []};}
  snapshot(): WorkOverview {
    const state = this.client?.state;
    return {...this.data, goal: state?.goal, contextTokens: state?.context_tokens ?? 0, contextLimit: state?.context_limit ?? 0, approvalPolicy: state?.approval_policy ?? '', mcpTools: state?.mcp_tools ?? 0, queued: (state?.queued_commands.length ?? 0) + (state?.queued_steering.length ?? 0)};
  }
  bind(client: RuntimeClient): void {
    this.dispose(); this.client = client; this.data = this.empty(); this.generation = client.state.session_generation;
    const listen = (event: string, fn: (...args: any[]) => void) => {client.on(event, fn); this.off.push(() => client.off(event, fn));};
    listen('initialized', info => {
      this.data.plan = info.plan?.items ?? []; this.data.progress = info.progress?.summary ?? '';
      this.data.warnings = (info.startup_events ?? []).filter((event: any) => ['warning', 'error'].includes(event.level)).map((event: any) => event.message).slice(-20);
      for (const event of info.runtime_events ?? []) this.runtime(event);
      if (info.workspace_git) void this.pollGit(client, this.epoch);
      this.changed();
    });
    listen('state', state => {if (state.session_generation !== this.generation) {this.generation = state.session_generation; this.data = {...this.empty(), git: this.data.git}; this.activeTools.clear();} this.data.goalObservedAt = Date.now(); if (!state.running) {this.data.activity = ''; this.activeTools.clear();}});
    listen('completed', result => {if (result.clear_transcript) this.data = {...this.empty(), git: this.data.git}; if (result.session_changed && result.plan) {this.data.plan = result.plan.items ?? []; this.data.progress = result.progress?.summary ?? '';} this.changed();});
    listen('event', (event, _wire, generation) => {if (generation !== undefined && generation < this.generation) return; if (generation > this.generation) {this.generation = generation; this.data = {...this.empty(), git: this.data.git}; this.activeTools.clear();} if (typeOf(event.payload) === 'RuntimeEventPayload') {this.runtime(event.payload.event); this.changed();}});
    listen('failure', () => {this.epoch++; clearTimeout(this.timer);});
  }
  private async pollGit(client: RuntimeClient, epoch: number): Promise<void> {
    try {const git = await client.git(); if (epoch === this.epoch) {this.data.git = git; this.changed();}}
    catch (error) {if (epoch === this.epoch && !client.peer.closed) {this.data.git = {available: false, reason: String(error), branch: '', head: '', upstream: null, ahead: null, behind: null, files: [], additions: null, deletions: null, truncated: false}; this.changed();}}
    if (epoch === this.epoch && !client.peer.closed) {this.timer = setTimeout(() => void this.pollGit(client, epoch), 5000); this.timer.unref();}
  }
  private runtime(event: RecordData): void {
    const payload = event.payload; const kind = typeOf(payload);
    if (event.agent_id && event.agent_id !== this.client?.state.agent_id && !['SubagentJobChanged', 'DiagnosticsPublished', 'DiagnosticsCleared'].includes(kind ?? '')) return;
    switch (kind) {
      case 'PlanUpdated': this.data.plan = payload.items ?? []; break;
      case 'ProgressReported': this.data.progress = payload.summary ?? ''; break;
      case 'ToolCallStarted': this.activeTools.set(payload.tool_call_id, payload.tool_name); this.data.activity = payload.tool_name; break;
      case 'StreamChunk': this.data.activity = payload.reasoning ? 'Reasoning' : 'Writing'; break;
      case 'ReasoningDelta': this.data.activity = 'Reasoning'; break;
      case 'AssistantContentDelta': this.data.activity = 'Writing'; break;
      case 'ToolCallFinished': this.activeTools.delete(payload.tool_call_id); this.data.activity = [...this.activeTools.values()].at(-1) ?? ''; break;
      case 'TurnStarted': case 'ChatStarted': case 'TurnFinished': case 'ChatCompleted': this.activeTools.clear(); this.data.activity = ''; break;
      case 'SubagentJobChanged': this.data.jobs = [...this.data.jobs.filter(job => job.id !== payload.job_id), {id: payload.job_id, task: payload.task, status: payload.status, detail: payload.blocker || payload.error || payload.current_tool || payload.activity || ''}].slice(-100); break;
      case 'ProcessSessionChanged': {
        const previous = this.data.processes.find(process => process.id === payload.process_session_id);
        this.data.processes = [...this.data.processes.filter(process => process !== previous), {id: payload.process_session_id, command: payload.command ?? previous?.command ?? '', state: payload.state, elapsed: payload.elapsed_seconds ?? previous?.elapsed ?? 0, observedAt: Date.now(), output: ((previous?.output ?? '') + (payload.stdout ?? '') + (payload.stderr ?? '')).slice(-2048)}].slice(-100); break;
      }
      case 'DiagnosticsPublished': this.data.diagnostics = [...this.data.diagnostics.filter(item => item.path !== payload.file_path), {path: payload.file_path, errors: payload.diagnostics.filter((item: any) => item.severity === 'error').length, warnings: payload.diagnostics.filter((item: any) => item.severity === 'warning').length}].slice(-100); break;
      case 'DiagnosticsCleared': this.data.diagnostics = this.data.diagnostics.filter(item => item.path !== payload.file_path); break;
    }
  }
  dispose(): void {this.epoch++; clearTimeout(this.timer); for (const off of this.off.splice(0)) off(); this.activeTools.clear(); this.client = undefined;}
}
