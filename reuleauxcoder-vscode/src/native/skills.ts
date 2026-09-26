import * as vscode from 'vscode';
import {createHash} from 'node:crypto';
import type {PanelItem} from '@reuleauxcoder/client';
import {t} from '../i18n.js';

/** Only receives a skill resolved against the current core-owned panel. */
export class SkillDocuments implements vscode.TextDocumentContentProvider, vscode.Disposable {
  private documents = new Map<string, string>();
  private changed = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.changed.event;
  provideTextDocumentContent(uri: vscode.Uri): string {
    const content = this.documents.get(uri.toString());
    if (content === undefined) throw new Error(t('This skill preview expired. Open it again from Skills.'));
    return content;
  }
  async open(folder: vscode.WorkspaceFolder, item: PanelItem): Promise<void> {
    const details = item.details!;
    let uri = folder.uri.with({path: vscode.Uri.file(details.location).path, query: '', fragment: ''});
    if (details.source === 'builtin') {
      const content = new TextDecoder().decode(await vscode.workspace.fs.readFile(uri));
      const key = createHash('sha256').update(uri.toString()).digest('hex');
      uri = vscode.Uri.from({scheme: 'reuleaux-skill', path: `/${item.id}/SKILL.md`, query: key});
      this.documents.set(uri.toString(), content); this.changed.fire(uri);
    }
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri), {viewColumn: vscode.ViewColumn.One, preview: true});
  }
  dispose(): void {this.documents.clear(); this.changed.dispose();}
}
