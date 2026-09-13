import assert from 'node:assert/strict';
import test from 'node:test';
import {decode, record} from '../src/protocol/wire.js';
import {SessionStore} from '../src/state/session.js';
import {safe} from '../src/ui/format.js';
import {TranscriptLayout} from '../src/ui/transcript.js';

test('LSP diagnostics show counts by default and retain full records behind F4', () => {
  const session = new SessionStore();
  const layout = new TranscriptLayout();
  const diagnostics = Array.from({length: 100}, (_, index) => record('RuntimeDiagnostic', {
    line: index + 1, character: 7, severity: index ? 'warning' : 'error',
    message: `Diagnostic message ${index + 1}`, code: `LSP${index + 1}`,
  }));
  session.runtime({payload: decode(record('DiagnosticsPublished', {
    file_path: 'src/main.ts', batch_id: 'batch-1', document_version: 2,
    diagnostic_generation: 1, diagnostics,
  }))});
  const text = (expanded: boolean) => safe(layout.render(session.cells, 120, 2000, 0, expanded, session.takeDirtyIndex()).rows.join('\n'));

  const compact = text(false);
  assert.match(compact, /LSP · src\/main\.ts/);
  assert.match(compact, /1 error · 99 warnings/);
  assert(!compact.includes('Diagnostic message'));
  assert(compact.split('\n').length <= 4, 'large diagnostic batches stay compact');
  assert.equal(session.cells[0].tone, 'error');

  const full = text(true);
  for (const detail of ['Line: 100', 'Character: 7', 'Severity: warning', 'Diagnostic message 100', 'Code: LSP100']) assert(full.includes(detail), detail);
  assert.equal(session.diagnostics.get('src/main.ts')!.diagnostics.length, 100);
  assert.equal(text(false), compact, 'F4 restores the compact view');
});
