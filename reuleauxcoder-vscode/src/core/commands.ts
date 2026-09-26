import {typeOf, type Action, type Json, type Panel, type RuntimeClient} from '@reuleauxcoder/client';
import type {CommandSurface} from '../shared.js';
import {t} from '../i18n.js';

/** UI navigation belongs to the host; executable rows always come from the core. */
export class ConversationCommands {
  private client?: RuntimeClient;
  private detach?: () => void;
  private epoch = 0;
  private serial = 0;
  private accepting = false;
  private stack: {panel: Panel; path: string[]}[] = [];
  surface?: CommandSurface;
  constructor(private changed: () => void, private fail: (error: unknown) => void) {}
  bind(client: RuntimeClient): void {
    this.dispose(); this.client = client;
    let generation = client.state.session_generation;
    const event = (event: any, wire: any, eventGeneration: number) => {
      if (eventGeneration !== client.state.session_generation || typeOf(event.payload) !== 'ViewEventPayload' || !this.accepting || this.surface?.action) return;
      const epoch = this.epoch;
      void client.panel(wire.fields.payload).then(presentation => {
        if (epoch !== this.epoch || !this.accepting || !presentation?.definition) return;
        const panel = presentation.definition;
        if (event.payload.action === 'refresh' || !event.payload.focus) {
          if (presentation.refresh !== 'update') return;
          const start = this.stack.findIndex(entry => entry.panel.view_type === panel.view_type);
          if (start < 0) return;
          this.stack[start].panel = panel;
          for (let index = start + 1; index < this.stack.length; index++) {
            const id = this.stack[index].path.at(-1);
            const child = this.stack[index - 1].panel.children.find(([key]) => key === id)?.[1];
            if (!child) {this.stack.length = index; break;}
            this.stack[index].panel = child;
          }
        } else this.stack = [{panel, path: []}];
        this.showPanel();
      }).catch(this.fail);
    };
    const state = () => {if (generation !== client.state.session_generation) {generation = client.state.session_generation; this.close();}};
    const failure = () => this.close();
    client.on('event', event); client.on('state', state); client.on('failure', failure);
    this.detach = () => {client.off('event', event); client.off('state', state); client.off('failure', failure);};
  }
  async open(actionId: string): Promise<void> {
    if (this.surface?.busy) throw new Error(t('This panel changed. Choose the action again.'));
    const action = this.client?.catalog.find(action => action.action_id === actionId);
    if (!action) throw new Error(t('This action is no longer available.'));
    this.epoch++; this.accepting = true; this.stack = [];
    this.surface = {id: ++this.serial, feature: action.feature_id, busy: false, canBack: false};
    if (action.parameters.length && (!action.preview || action.parameters.some(parameter => parameter.required))) {
      this.surface.action = action; this.changed(); return;
    }
    await this.run(action.action_id, Object.fromEntries(action.parameters.map(parameter => [parameter.name, parameter.default])));
  }
  async submit(id: number, input: unknown): Promise<void> {
    const surface = this.current(id); const action = surface.action;
    if (!action || !input || typeof input !== 'object' || Array.isArray(input)) throw new Error(t('Invalid command form.'));
    const fields = input as Record<string, unknown>; const values: Record<string, Json> = {};
    for (const parameter of action.parameters) {
      const value = fields[parameter.name];
      if (value === undefined || value === null || value === '') {
        if (parameter.required && !parameter.nullable) throw new Error(t('Required'));
        values[parameter.name] = parameter.default; continue;
      }
      if (parameter.kind === 'integer' ? typeof value !== 'number' || !Number.isSafeInteger(value) : parameter.kind === 'boolean' ? typeof value !== 'boolean' : typeof value !== 'string' || value.length > 1024 * 1024) throw new Error(t('Check the field values.'));
      values[parameter.name] = value as Json;
    }
    // Keep the form on validation/transport failure. A successful submit may open a panel.
    this.surface = {...surface, action: undefined};
    const epoch = this.epoch;
    try {await this.run(action.action_id, values);}
    catch (error) {if (epoch === this.epoch) {this.surface = {...surface, id: ++this.serial, busy: false}; this.changed();} throw error;}
  }
  async select(id: number, index: number): Promise<void> {
    const surface = this.current(id); const panel = surface.panel;
    if (!Number.isInteger(index) || !panel?.items[index]) throw new Error(t('Invalid panel selection.'));
    const item = panel.items[index]; const key = item.id ?? item.label;
    const child = panel.children.find(([id]) => id === key)?.[1];
    if (child) {
      this.stack.push({panel: child, path: [...this.stack.at(-1)!.path, key]}); this.showPanel();
      if (child.on_open) await this.run(child.on_open.action_id, child.on_open.command);
    } else if (item.action) {
      if (!panel.keep_open_on_submit) {
        if (panel.return_to_parent_on_submit) this.back(id);
        else this.close();
      }
      await this.run(item.action.action_id, item.action.command);
    }
  }
  async policy(id: number, path: unknown): Promise<void> {
    const surface = this.current(id);
    if (surface.feature !== 'approval' || surface.panel?.view_type !== 'approval_rules' || !Array.isArray(path) || path.length !== 3 || !path.every(Number.isInteger)) throw new Error(t('This action is no longer available.'));
    let panel = surface.panel;
    for (const index of path.slice(0, -1)) {
      const item = panel.items[index];
      const child = item && panel.children.find(([key]) => key === (item.id ?? item.label))?.[1];
      if (!child || child.on_open) throw new Error(t('This panel changed. Choose the action again.'));
      panel = child;
    }
    const action = panel.items[path.at(-1)!]?.action;
    if (panel.view_type !== 'approval_actions' || !action?.action_id.startsWith('approval.')) throw new Error(t('This action is no longer available.'));
    const epoch = this.epoch;
    await this.run(action.action_id, action.command);
    // Refresh facts without reopening a dismissed panel or a different session.
    if (epoch === this.epoch && this.accepting) await this.open('approval.show');
  }
  back(id: number): void {this.current(id); this.epoch++; if (this.stack.length > 1) {this.stack.pop(); this.showPanel();} else this.close();}
  skill(id: number, name: unknown) {
    const surface = this.current(id);
    const item = surface.panel?.view_type === 'skills' && typeof name === 'string'
      ? surface.panel.items.find(item => item.id === name && item.details?.location) : undefined;
    if (!item) throw new Error(t('This action is no longer available.'));
    return item;
  }
  async reloadSkills(id: number): Promise<void> {
    if (this.current(id).panel?.view_type !== 'skills') throw new Error(t('This action is no longer available.'));
    await this.run('skills.reload', {});
  }
  dismiss(id: number): void {
    if (this.surface && this.surface.id !== id) throw new Error(t('This panel changed. Choose the action again.'));
    this.close();
  }
  close(): void {this.epoch++; this.accepting = false; this.stack = []; this.surface = undefined; this.changed();}
  private current(id: number): CommandSurface {
    if (!this.surface || this.surface.id !== id || this.surface.busy) throw new Error(t('This panel changed. Choose the action again.'));
    return this.surface;
  }
  private showPanel(): void {
    this.surface = {id: ++this.serial, feature: this.surface?.feature ?? '', busy: this.surface?.busy ?? false, panel: this.stack.at(-1)!.panel, canBack: this.stack.length > 1};
    this.changed();
  }
  private async run(action: string, values: Record<string, Json>): Promise<void> {
    const epoch = this.epoch;
    if (this.surface) {this.surface = {...this.surface, id: ++this.serial, busy: true};} this.changed();
    try {await this.client!.submitAction(action, values);}
    finally {if (epoch === this.epoch && this.surface) {this.surface.busy = false; this.changed();}}
  }
  dispose(): void {this.detach?.(); this.detach = undefined; this.close(); this.client = undefined;}
}
