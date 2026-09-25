import * as vscode from 'vscode';
import type {ConfigDocument} from '@reuleauxcoder/client';
import type {ConfigurationEditor} from '../core/configuration-editor.js';
import {t} from '../i18n.js';
import {canonicalPathKey} from './paths.js';

/** Uses the workspace extension host's native editor, including Remote URIs. */
export class ConfigurationDocuments {
  constructor(private configuration: ConfigurationEditor, private folder: vscode.WorkspaceFolder) {}
  private uri(path: string): vscode.Uri {return this.folder.uri.with({path: vscode.Uri.file(path).path});}
  private entries() {
    return (this.configuration.state?.sources ?? []).flatMap(source => {
      const key = canonicalPathKey(source.path);
      return vscode.workspace.textDocuments.filter(doc => doc.uri.scheme === this.folder.uri.scheme
        && doc.uri.authority === this.folder.uri.authority && canonicalPathKey(doc.uri.fsPath) === key)
        .map(document => ({source, document}));
    });
  }
  sync(invalidate = false): void {
    const documents: ConfigDocument[] = this.entries().filter(entry => entry.document?.isDirty)
      .map(({source, document}) => ({scope: source.scope, content: document!.getText()}));
    this.configuration.setDocuments(documents, invalidate);
  }
  owns(document: vscode.TextDocument): boolean {
    return document.uri.scheme === this.folder.uri.scheme && document.uri.authority === this.folder.uri.authority
      && !!this.configuration.state?.sources.some(source => canonicalPathKey(source.path) === canonicalPathKey(document.uri.fsPath));
  }
  async open(scope: string): Promise<void> {
    const uri = this.uri(this.configuration.source(scope));
    // File creation is an explicit editor action, never an inspection/startup side effect.
    const edit = new vscode.WorkspaceEdit(); edit.createFile(uri, {ignoreIfExists: true});
    if (!await vscode.workspace.applyEdit(edit)) throw new Error(t('Could not open the configuration file.'));
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri), {preview: false});
    this.sync();
  }
  async check(profile?: string) {
    this.sync();
    return this.configuration.check(profile);
  }
  async save(): Promise<void> {
    const entries = this.entries().filter(entry => entry.document?.isDirty);
    const versions = entries.map(entry => entry.document!.version);
    const result = await this.check();
    if (!result?.valid) throw new Error(t('Fix configuration errors before saving here.'));
    for (let i = 0; i < entries.length; i++) {
      const document = entries[i].document!;
      if (document.version !== versions[i]) throw new Error(t('Configuration changed during the check. Check again.'));
      // VS Code handles external-file conflicts and preserves comments, order and EOL.
      if (!await document.save()) throw new Error(t('Configuration was not saved. Resolve the editor conflict and try again.'));
    }
    this.sync(true);
    await this.check();
  }
}
