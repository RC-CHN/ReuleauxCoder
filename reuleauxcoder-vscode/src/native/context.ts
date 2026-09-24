import {t} from '../i18n.js';
import * as vscode from 'vscode';
import {randomUUID} from 'node:crypto';
import type {DraftItem} from '../shared.js';

export async function editorContext(uri?: vscode.Uri, range?: vscode.Range, diagnostics?: readonly vscode.Diagnostic[]): Promise<DraftItem> {
  const editor = vscode.window.activeTextEditor;
  uri ??= editor?.document.uri;
  if (!uri || !['file', 'vscode-remote'].includes(uri.scheme)) throw new Error(t('Choose a file in the workspace.'));
  const document = await vscode.workspace.openTextDocument(uri);
  range ??= editor?.document.uri.toString() === uri.toString() && !editor.selection.isEmpty ? editor.selection : undefined;
  const name = vscode.workspace.asRelativePath(uri, true);
  let text = `Editor context: ${name}`;
  if (range) text += `\nLines ${range.start.line + 1}-${range.end.line + 1}:\n${document.getText(range).slice(0, 65536)}`;
  else if (document.isDirty) text += `\nUnsaved editor snapshot (first 65,536 characters):\n${document.getText().slice(0, 65536)}`;
  else text += '\nRead the saved file from the workspace as needed.';
  if (document.isDirty) text += '\nThis editor has unsaved changes; they have not been written to disk.';
  if (diagnostics?.length) text += '\nDiagnostics:\n' + diagnostics.slice(0, 20).map(item => `${item.range.start.line + 1}:${item.range.start.character + 1} ${item.message.slice(0, 2000)}`).join('\n');
  return {id: randomUUID(), kind: 'context', name: name + (range ? `:${range.start.line + 1}-${range.end.line + 1}` : ''), text};
}

export const diagnosticActions: vscode.CodeActionProvider = {
  provideCodeActions(document, range, context) {
    if (!context.diagnostics.length) return [];
    const action = new vscode.CodeAction(t('Explain or fix with Reuleaux'), vscode.CodeActionKind.QuickFix);
    action.diagnostics = [...context.diagnostics];
    action.command = {command: 'reuleaux.fixDiagnostic', title: action.title, arguments: [document.uri, range, context.diagnostics]};
    return [action];
  },
};
