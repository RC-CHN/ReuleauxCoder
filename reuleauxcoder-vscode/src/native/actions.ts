import {t} from '../i18n.js';
import * as vscode from 'vscode';
import {typeOf, type Action, type Panel, type RuntimeClient, type Json} from '@reuleauxcoder/client';

/** Backend catalogs and panel projections remain authoritative; native UI supplies inputs. */
export class NativeActions implements vscode.Disposable {
  private off?: () => void;
  private epoch = 0;
  private tokens = new Set<vscode.CancellationTokenSource>();
  constructor(private fail: (error: unknown) => void) {}
  bind(client: RuntimeClient): void {
    this.reset(); let generation = client.state.session_generation;
    const handler = (event: any, wire: any) => {
      if (typeOf(event.payload) !== 'ViewEventPayload' || !event.payload.focus) return;
      const epoch = this.epoch;
      void client.panel(wire.fields.payload).then(result => {if (epoch === this.epoch && result?.definition) return this.panel(client, result.definition, epoch);}).catch(this.fail);
    };
    const state = () => {if (client.state.session_generation !== generation) {generation = client.state.session_generation; this.cancelDialogs();}};
    const failure = () => this.cancelDialogs();
    client.on('event', handler); client.on('state', state); client.on('failure', failure);
    this.off = () => {client.off('event', handler); client.off('state', state); client.off('failure', failure);};
  }
  async choose(client: RuntimeClient): Promise<void> {
    const epoch = this.epoch; const token = new vscode.CancellationTokenSource(); this.tokens.add(token);
    try {
      const choice = await vscode.window.showQuickPick(client.catalog.map(action => ({label: action.action_id, description: action.description, action})), {title: t('Reuleaux actions'), matchOnDescription: true}, token.token);
      if (choice && epoch === this.epoch) await this.execute(client, choice.action);
    } finally {this.tokens.delete(token); token.dispose();}
  }
  async execute(client: RuntimeClient, action: Action): Promise<void> {
    const epoch = this.epoch; const token = new vscode.CancellationTokenSource(); this.tokens.add(token);
    try {
    const values: Record<string, Json> = {};
    for (const parameter of action.parameters) {
      const value = await vscode.window.showInputBox({title: action.action_id, prompt: `${parameter.name}${parameter.required ? ` (${t('Required')})` : ''}`, value: parameter.default == null ? '' : String(parameter.default), validateInput: value => parameter.required && !value ? t('Required') : parameter.kind === 'integer' && value && !/^-?\d+$/.test(value) ? t('Enter an integer') : parameter.kind === 'boolean' && value && !['true', 'false'].includes(value) ? t('Enter true or false') : undefined}, token.token);
      if (value === undefined || epoch !== this.epoch) return;
      if (!value && !parameter.required) {if (parameter.default != null) values[parameter.name] = parameter.default; continue;}
      values[parameter.name] = parameter.kind === 'integer' ? Number(value) : parameter.kind === 'boolean' ? value === 'true' : value;
    }
    await client.submitAction(action.action_id, values);
    } finally {this.tokens.delete(token); token.dispose();}
  }
  private async panel(client: RuntimeClient, panel: Panel, epoch: number): Promise<void> {
    const token = new vscode.CancellationTokenSource(); this.tokens.add(token);
    try {
      const items = panel.items.map(item => ({label: item.label, description: item.description, action: item.action, child: panel.children.find(([id]) => id === (item.id ?? item.label))?.[1]}));
      if (!items.length) {await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({content: [panel.title, panel.body, panel.output].filter(Boolean).join('\n\n')}), {preview: true}); return;}
      const selected = await vscode.window.showQuickPick(items, {title: panel.title, placeHolder: panel.body?.slice(0, 150), matchOnDescription: true}, token.token);
      if (epoch !== this.epoch || !selected) return;
      if (selected.child) {
        if (selected.child.on_open) await client.submitAction(selected.child.on_open.action_id, selected.child.on_open.command);
        else await this.panel(client, selected.child, epoch);
      }
      else if (selected.action) await client.submitAction(selected.action.action_id, selected.action.command);
    } finally {this.tokens.delete(token); token.dispose();}
  }
  private cancelDialogs(): void {this.epoch++; for (const token of this.tokens) {token.cancel(); token.dispose();} this.tokens.clear();}
  private reset(): void {this.off?.(); this.cancelDialogs();}
  dispose(): void {this.reset();}
}
