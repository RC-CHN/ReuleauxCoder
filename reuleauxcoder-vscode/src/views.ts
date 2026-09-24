import * as vscode from 'vscode';
import {randomBytes, randomUUID} from 'node:crypto';
import type {WorkspaceSession} from './core/session.js';
import type {NativeReviews} from './native/reviews.js';
import type {WebRequest} from './shared.js';
import {t, errorText} from './i18n.js';
import {webviewHtml} from './webview/html.js';
import {answerInteraction} from './core/interactions.js';

export class ConversationViews implements vscode.WebviewViewProvider, vscode.Disposable {
  private views = new Map<vscode.Webview, {owner: string; disposables: vscode.Disposable[]; session?: WorkspaceSession}>();
  private editor?: vscode.WebviewPanel;
  private timer?: ReturnType<typeof setTimeout>;
  private sequence = 0;
  constructor(private extension: vscode.Uri, private session: () => Promise<WorkspaceSession>, private reviews: NativeReviews, private command: (action: string, data?: Record<string, any>) => Promise<unknown>) {}
  resolveWebviewView(view: vscode.WebviewView): void {this.attach(view.webview, view);}
  showCommands(): void {for (const webview of this.views.keys()) void webview.postMessage({kind: 'commands'});}
  openEditor(): void {
    if (this.editor) {this.editor.reveal(vscode.ViewColumn.One); return;}
    const panel = vscode.window.createWebviewPanel('reuleaux.conversation', 'Reuleaux', vscode.ViewColumn.One, {enableScripts: true, retainContextWhenHidden: false});
    this.editor = panel; panel.iconPath = vscode.Uri.joinPath(this.extension, 'media', 'reuleaux.svg');
    panel.onDidDispose(() => {if (this.editor === panel) this.editor = undefined;});
    this.attach(panel.webview, panel);
  }
  private attach(webview: vscode.Webview, view: {onDidDispose: vscode.Event<void>}): void {
    const owner = randomUUID(); const disposables: vscode.Disposable[] = [];
    this.views.set(webview, {owner, disposables});
    webview.options = {enableScripts: true, localResourceRoots: [vscode.Uri.joinPath(this.extension, 'dist')]};
    webview.html = this.html(webview);
    disposables.push(webview.onDidReceiveMessage((message: unknown) => {
      if (!message || typeof message !== 'object') return;
      const request = message as WebRequest;
      if (typeof request.id !== 'string' || request.id.length > 128 || typeof request.action !== 'string') return;
      void this.receive(owner, request).then(result => {
        if (this.views.has(webview)) void webview.postMessage({kind: 'response', id: request.id, result: result ?? null});
      }, error => {if (this.views.has(webview)) void webview.postMessage({kind: 'response', id: request.id, error: errorText(error)});});
    }));
    disposables.push(view.onDidDispose(() => {
      const ownerSession = this.views.get(webview)?.session;
      this.views.delete(webview); for (const disposable of disposables) disposable.dispose();
      void ownerSession?.uploads?.cancel(owner).catch(() => {});
    }));
  }
  private async receive(owner: string, request: WebRequest): Promise<unknown> {
    const session = await this.session(); const data = request.data ?? {};
    const entry = [...this.views.values()].find(entry => entry.owner === owner);
    if (!entry) throw new Error(t('View closed.'));
    entry.session = session;
    if (['send', 'retry', 'upload.begin', 'newSession', 'sessions', 'models', 'approve', 'reject', 'saveReview', 'answer'].includes(request.action) || request.action.startsWith('command.')) {
      if (data.hostId !== session.snapshot().hostId || data.generation !== session.snapshot().generation) throw new Error(t('Session changed. Review your draft and send it again.'));
    }
    switch (request.action) {
      case 'ready': {
        const snapshot = {...session.snapshot(this.reviews.summaries()), revision: ++this.sequence};
        if (session.phase === 'idle' && vscode.workspace.getConfiguration('reuleaux').get('autoStart', true)) void this.command('start').catch(() => {});
        return snapshot;
      }
      case 'draft': if (typeof data.text === 'string' && data.text.length <= 1024 * 1024) session.draftText = data.text; return;
      case 'send': session.submit(data.id, data.text, data.items, data.generation); return;
      case 'retry': await session.submissions?.retry(data.id); return;
      case 'remove': session.remove(data.id); return;
      case 'command.open': session.requireClient(); return session.commands.open(data.actionId);
      case 'command.submit': session.requireClient(); return session.commands.submit(data.surfaceId, data.values);
      case 'command.select': session.requireClient(); return session.commands.select(data.surfaceId, data.index);
      case 'command.policy': session.requireClient(); return session.commands.policy(data.surfaceId, data.path);
      case 'command.back': return session.commands.back(data.surfaceId);
      case 'command.close': session.commands.dismiss(data.surfaceId); return;
      case 'answer': return answerInteraction(session.requireClient(), data);
      case 'upload.begin': return await session.uploads?.begin(owner, data.name, data.size, data.image === true) ?? Promise.reject(new Error(t('Start the core before uploading.')));
      case 'upload.append': return await session.uploads?.append(owner, data.id, data.offset, data.data);
      case 'upload.complete': {
        const item = await session.uploads?.complete(owner, data.id);
        if (!item) throw new Error(t('Upload expired.')); session.add(item); return item.id;
      }
      case 'upload.cancel': await session.uploads?.cancel(owner, data.id); return;
      case 'start': case 'install': case 'selectCore': case 'logs': case 'stop': case 'openEditor': case 'newSession': case 'sessions': case 'actions': case 'models': case 'review': case 'approve': case 'reject': case 'saveReview': case 'addUri': case 'openLink': case 'openFile': case 'git': return this.command(request.action, data);
      default: throw new Error(t('Unknown view action.'));
    }
  }
  changed(): void {
    if (!this.timer) this.timer = setTimeout(() => {this.timer = undefined; void this.broadcast();}, 32);
  }
  private async broadcast(): Promise<void> {
    if (!this.views.size) return;
    try {const session = await this.session(); const snapshot = {...session.snapshot(this.reviews.summaries()), revision: ++this.sequence}; for (const webview of this.views.keys()) void webview.postMessage({kind: 'snapshot', snapshot});}
    catch (error) {for (const webview of this.views.keys()) void webview.postMessage({kind: 'error', error: errorText(error)});}
  }
  private html(webview: vscode.Webview): string {
    const nonce = randomBytes(18).toString('base64');
    const script = webview.asWebviewUri(vscode.Uri.joinPath(this.extension, 'dist', 'webview.js'));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(this.extension, 'dist', 'webview.css'));
    return webviewHtml({script: script.toString(), style: style.toString(), nonce, cspSource: webview.cspSource, language: vscode.env.language});
  }
  dispose(): void {clearTimeout(this.timer); for (const {disposables} of this.views.values()) for (const disposable of disposables) disposable.dispose(); this.views.clear(); this.editor?.dispose();}
}
