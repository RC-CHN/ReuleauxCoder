import {t} from '../i18n.js';
import * as vscode from 'vscode';
import {basename, normalize} from 'node:path';
import {record, type PendingInteraction, type RuntimeClient} from '@reuleauxcoder/client';
import type {ReviewSummary} from '../shared.js';

function pathKey(path: string): string {const value = normalize(path); return process.platform === 'win32' ? value.toLowerCase() : value;}
interface ReviewNode {id: string; label: string; path?: string; documentId?: string}

export class NativeReviews implements vscode.TextDocumentContentProvider, vscode.TreeDataProvider<ReviewNode>, vscode.Disposable {
  private client?: RuntimeClient;
  private detach?: () => void;
  private changed = new vscode.EventEmitter<ReviewNode | undefined>();
  readonly onDidChangeTreeData = this.changed.event;
  private cache = new Map<string, string>();
  private opened = new Set<string>();
  private presenting = new Set<string>();
  private tokens = new Map<string, vscode.CancellationTokenSource>();
  private epoch = 0;
  constructor(private readonly onChange: () => void, private readonly fail: (error: unknown) => void) {}
  bind(client: RuntimeClient): void {
    for (const token of this.tokens.values()) {token.cancel(); token.dispose();} this.tokens.clear();
    this.detach?.(); this.client = client; this.epoch++; this.cache.clear(); this.opened.clear();
    const listener = () => {
      for (const [id, token] of this.tokens) if (!this.find(id)) {token.cancel(); token.dispose(); this.tokens.delete(id);}
      for (const id of this.opened) if (!this.find(id)) this.opened.delete(id);
      this.changed.fire(undefined); this.onChange(); void this.present().catch(this.fail);
    };
    client.on('interactions', listener); this.detach = () => client.off('interactions', listener);
  }
  summaries(): ReviewSummary[] {return (this.client?.interactions ?? []).filter(item => item.kind === 'review').map(item => ({id: item.request.request_id, title: item.request.title, summary: item.request.summary, documents: (item.request.documents ?? []).map((doc: any) => ({id: doc.id, path: doc.path}))}));}
  getChildren(node?: ReviewNode): ReviewNode[] {
    if (!node) return this.summaries().map(item => ({id: item.id, label: item.title}));
    return this.summaries().find(item => item.id === node.id)?.documents.map(doc => ({id: node.id, label: basename(doc.path), path: doc.path, documentId: doc.id})) ?? [];
  }
  getTreeItem(node: ReviewNode): vscode.TreeItem {
    const item = new vscode.TreeItem(node.label, node.documentId ? vscode.TreeItemCollapsibleState.None : vscode.TreeItemCollapsibleState.Expanded);
    item.description = node.path; item.iconPath = new vscode.ThemeIcon(node.documentId ? 'diff' : 'shield');
    item.command = {command: 'reuleaux.review', title: t('Review'), arguments: [node.id, node.documentId]}; return item;
  }
  private find(id: string): PendingInteraction | undefined {return this.client?.interactions.find(item => item.request.request_id === id);}
  private token(id: string): vscode.CancellationToken {
    let source = this.tokens.get(id); if (!source) {source = new vscode.CancellationTokenSource(); this.tokens.set(id, source);}
    return source.token;
  }
  private uri(requestId: string, documentId: string, side: string, path: string): vscode.Uri {
    return vscode.Uri.from({scheme: 'reuleaux-review', path: `/${requestId}/${documentId}/${basename(path)}`, query: new URLSearchParams({side, epoch: String(this.epoch)}).toString()});
  }
  async provideTextDocumentContent(uri: vscode.Uri): Promise<string> {
    const key = uri.toString(); if (this.cache.has(key)) return this.cache.get(key)!;
    const [, requestId, documentId] = uri.path.split('/'); const params = new URLSearchParams(uri.query); const side = params.get('side');
    if (params.get('epoch') !== String(this.epoch) || !this.find(requestId) || !['before', 'after'].includes(side ?? '')) throw new Error(t('This review has expired. Open the current proposal.'));
    const client = this.client!; const epoch = this.epoch;
    const content = await client.reviewDocument(requestId, documentId, side as 'before' | 'after');
    if (epoch !== this.epoch || !this.find(requestId)) throw new Error(t('This review has expired.'));
    while (this.cache.size >= 16) this.cache.delete(this.cache.keys().next().value!);
    this.cache.set(key, content); return content;
  }
  async open(id?: string, documentId?: string): Promise<void> {
    const request = id ? this.find(id) : this.client?.interactions.find(item => item.kind === 'review');
    if (!request) throw new Error(t('No active review.'));
    const documents = request.request.documents ?? [];
    const document = documents.find((item: any) => item.id === documentId) ?? documents[0];
    if (document) {
      const left = this.uri(request.request.request_id, document.id, 'before', document.path);
      const right = this.uri(request.request.request_id, document.id, 'after', document.path);
      try {
        await Promise.all([this.provideTextDocumentContent(left), this.provideTextDocumentContent(right)]);
        await vscode.commands.executeCommand('vscode.diff', left, right, t('Proposal: {0}', basename(document.path)), {preview: false, preserveFocus: true, viewColumn: vscode.ViewColumn.One});
        return;
      } catch (error) {
        if (!(error instanceof Error) || !error.message.includes('4 Mi-character')) throw error;
        void vscode.window.showInformationMessage(t('The proposal is too large for the native diff. Showing the text preview.'));
      }
    }
    {
      const text = [request.request.summary, ...(request.request.sections ?? []).map((section: any) => `${section.title}\n${typeof section.content === 'string' ? section.content : JSON.stringify(section.content, null, 2)}`)].join('\n\n');
      await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({content: text, language: 'diff'}), {preview: false});
      const choice = await vscode.window.showQuickPick([t('Approve once'), t('Deny')], {title: request.request.title}, this.token(request.request.request_id));
      if (choice) await this.decide(choice === t('Approve once'), request.request.request_id);
    }
  }
  private currentId(argument?: string | vscode.Uri): string | undefined {
    if (typeof argument === 'string') return argument;
    const tab = vscode.window.tabGroups.activeTabGroup.activeTab?.input;
    const uri = argument ?? (tab instanceof vscode.TabInputTextDiff ? tab.modified : vscode.window.activeTextEditor?.document.uri);
    return uri?.scheme === 'reuleaux-review' && new URLSearchParams(uri.query).get('epoch') === String(this.epoch) ? uri.path.split('/')[1] : undefined;
  }
  async decide(approved: boolean, argument?: string | vscode.Uri): Promise<void> {
    let id = this.currentId(argument);
    if (!id) {
      const selected = await vscode.window.showQuickPick(this.summaries().map(item => ({label: item.title, id: item.id})), {placeHolder: t('Choose the proposal to review')});
      id = selected?.id;
    }
    const pending = id ? this.find(id) : undefined; if (!pending) return;
    if (approved) {
      const paths = new Set((pending.request.documents ?? []).map((document: any) => pathKey(document.path)));
      const dirty = vscode.workspace.textDocuments.filter(document => document.isDirty && paths.has(pathKey(document.uri.fsPath)));
      if (dirty.length) {
        const action = await vscode.window.showQuickPick([t('Save and request new proposal'), t('Deny this proposal')], {title: t('This proposal targets unsaved editor changes. Save them and ask the core for a new proposal.')}, this.token(id!));
        if (!action) return;
        if (action === t('Save and request new proposal')) {
          for (const document of dirty) if (!await document.save()) throw new Error(t('The document could not be saved. Approval remains pending.'));
        }
        if (this.find(id!) !== pending) return;
        this.client!.answer(id!, record('ReviewResponse', {approved: false, cancelled: false, action: 'deny', reason: 'The editor contained unsaved changes. Re-read the current file and propose the edit again against its saved content.'}));
        return;
      }
    }
    if (this.find(id!) !== pending) return;
    this.client!.answer(id!, record('ReviewResponse', {approved, cancelled: false, action: approved ? 'allow_once' : 'deny', reason: approved ? 'Approved in the native editor.' : 'Denied in the native editor.'}));
  }
  private async present(): Promise<void> {
    const item = this.client?.interactions[0]; if (!item || this.presenting.has(item.request.request_id)) return;
    const id = item.request.request_id; const epoch = this.epoch; this.presenting.add(id);
    try {
      if (item.kind === 'review') {
        if (!this.opened.has(id)) {
          this.opened.add(id);
          if (vscode.workspace.getConfiguration('reuleaux').get('openDiffAutomatically', true)) await this.open(id);
        }
        return;
      }
      let response;
      if (item.kind === 'confirm') {const result = await vscode.window.showQuickPick([t('Confirm'), t('Cancel')], {title: item.request.title, placeHolder: item.request.message}, this.token(id)); response = record('ConfirmResponse', {confirmed: result === t('Confirm'), cancelled: result === undefined});}
      else if (item.kind === 'choose_one') {const result = await vscode.window.showQuickPick((item.request.items ?? []).map((choice: any) => ({label: choice.label, description: choice.description, id: choice.id})), {title: item.request.title}, this.token(id)); response = record('ChooseOneResponse', {selected_id: (result as any)?.id ?? null, cancelled: !result});}
      else if (item.kind === 'input_text') {const result = await vscode.window.showInputBox({title: item.request.title, prompt: item.request.prompt, value: item.request.initial_value, password: item.request.secret, ignoreFocusOut: true}, this.token(id)); response = record('InputTextResponse', {value: result ?? null, cancelled: result === undefined});}
      if (response && epoch === this.epoch && this.find(id) === item) this.client!.answer(id, response);
    } finally {this.presenting.delete(id);}
  }
  dispose(): void {this.detach?.(); this.epoch++; this.cache.clear(); for (const token of this.tokens.values()) {token.cancel(); token.dispose();} this.tokens.clear(); this.changed.dispose();}
}
