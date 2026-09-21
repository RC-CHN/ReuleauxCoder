import assert from 'node:assert/strict';
import test from 'node:test';
import {edit, editor, layoutEditor} from '../src/state/editor.js';
import {inputLayout} from '../src/ui/viewport.js';

test('vertical movement retains the desired column through short lines', () => {
  let draft = {...editor('abcdefgh\nx\nABCDEFGH'), cursor: 7};
  draft = edit(draft, '', {downArrow: true}, 20);
  assert.equal(draft.cursor, 10);
  draft = edit(draft, '', {downArrow: true}, 20);
  assert.equal(draft.cursor, 18);
  draft = edit(draft, '', {upArrow: true}, 20);
  draft = edit(draft, '', {upArrow: true}, 20);
  assert.equal(draft.cursor, 7);
  assert.equal(edit(draft, '', {upArrow: true}, 20), draft);
  assert.equal(edit({...draft, cursor: 0}, '', {home: true}).cursor, 0);
  assert.equal(edit({...draft, cursor: 13}, '', {home: true}).cursor, 11);
  assert.equal(edit({...draft, cursor: 13}, '', {end: true}).cursor, 19);
});

test('vertical movement uses displayed word wrapping and whole Unicode graphemes', () => {
  const text = 'aa hello\n世界👩🏽‍💻';
  let draft = {...editor(text), cursor: 4};
  assert.deepEqual(inputLayout(draft, 7, 4).cursor, {x: 1, y: 1});
  draft = edit(draft, '', {downArrow: true}, 7);
  assert.deepEqual(inputLayout(draft, 7, 4).cursor, {x: 0, y: 2});
  draft = edit(draft, '', {upArrow: true}, 7);
  assert.equal(draft.cursor, 4);
  const layout = layoutEditor(text, 7);
  assert.equal(layoutEditor(text, 7), layout, 'cursor-only moves reuse text geometry');
  assert.notEqual(layoutEditor(text, 5), layout, 'resize computes new rows');
  assert.equal(edit(editor('世界👩🏽‍💻'), '', {leftArrow: true}).cursor, 2);
});
