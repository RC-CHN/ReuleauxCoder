import assert from 'node:assert/strict';
import test from 'node:test';
import {MessagePeer, RuntimeClient, InputQueueProjection, decode, emptyState, record} from '@reuleauxcoder/client';
import {TuiController} from '../src/state/controller.js';
import {editor} from '../src/state/editor.js';
import {InputHistory} from '../src/state/history.js';

const tick = () => new Promise<void>(resolve => setImmediate(resolve));

test('queue projection uses identities, retains external inputs and ignores stale applied snapshots', () => {
  const queue = new InputQueueProjection();
  const state = {...emptyState, queued_steering: ['same', 'same', 'same'], queued_inputs: [
    {text: 'same', submission_id: 'local', steering_id: 's1'},
    {text: 'same', submission_id: 'external', steering_id: 's2'},
    {text: 'same', steering_id: 's3'},
  ]};
  queue.track('local');
  assert.deepEqual(queue.untracked(state).map(item => item.steering_id), ['s2', 's3']);
  queue.applied('local', 's1'); queue.applied(undefined, 's3');
  assert.deepEqual(queue.remaining(state)?.map(item => item.steering_id), ['s2']);
  assert.deepEqual(queue.untracked({...emptyState, queued_steering: ['same']}), [], 'Older text-only snapshots cannot resurrect a local applied message');
  queue.clear();
  assert.equal(queue.untracked(state).length, 3);
  assert.deepEqual(queue.untracked({...emptyState, queued_steering: ['legacy']}), [{text: 'legacy'}]);
});
function fixture(running = false, history = new InputHistory()) {
  const requests: any[] = [];
  const peer = new MessagePeer(message => requests.push(message));
  const client = new RuntimeClient(peer);
  client.state = {...emptyState, session_id: 'session', agent_id: 'agent', running, revision: 1};
  const c = new TuiController(client, history);
  c.session.update(client.state);
  const reply = (index: number, status = running ? 'steering' : 'running') => {
    peer.receive({jsonrpc: '2.0', id: requests[index].id, result: {status, state: {...client.state, revision: 2}, submission_id: requests[index].params.submission_id}});
  };
  const apply = (index: number) => c.session.runtime({agent_id: 'agent', session_generation: 0, payload: decode(record(running ? 'UserSteeringApplied' : 'TurnStarted', {
    user_input: requests[index].params.value, submission_id: requests[index].params.submission_id,
  }))});
  return {c, client, peer, requests, reply, apply, close() {c.dispose(); client.close();}};
}

for (const running of [false, true]) test(`immediate ${running ? 'steering' : 'ordinary'} echo owns the sent draft until matching application`, async () => {
  const f = fixture(running);
  try {
    f.c.composer = editor('first');
    const first = f.c.key('', {return: true});
    await tick();
    assert.equal(f.c.composer.text, '');
    assert.equal(f.c.session.cells.length, 0);
    assert.equal([...f.c.session.pendingInputs.values()][0].body, 'first');
    assert.match([...f.c.session.pendingInputs.values()][0].title, /sending/);
    await f.c.key('', {return: true});
    assert.equal(f.requests.length, 1);
    await f.c.key('next');
    f.apply(0); // Application can arrive before admission acknowledgement.
    f.reply(0);
    await first;
    assert.equal(f.c.composer.text, 'next');
    assert.equal(f.c.session.cells.length, 1);
    assert.equal(f.c.session.cells[0].title, 'You');
    assert.equal(f.c.session.pendingInputs.size, 0);
  } finally {f.close();}
});

test('queued steering enters at the application boundary and splits streamed replies', async () => {
  const f = fixture(true);
  const runtime = (type: string, fields: any) => f.c.session.runtime({agent_id: 'agent', session_generation: 0, payload: decode(record(type, fields))});
  try {
    runtime('AssistantContentDelta', {text: 'Before'});
    const revision = f.c.session.contentRevision;
    const send = f.c.submissions.send('same instruction', 'same instruction');
    f.reply(0); await send;
    assert.equal(f.c.session.cells.length, 1);
    assert.equal(f.c.session.contentRevision, revision, 'Pending updates must not invalidate transcript layout');
    assert.equal(f.c.session.pendingInputs.size, 1);
    runtime('AssistantContentDelta', {text: ' the injection'});
    runtime('ReasoningDelta', {text: 'Old reasoning'});
    f.apply(0);
    runtime('AssistantContentDelta', {text: 'After'});
    f.apply(0); // A duplicate proof must not split the new reply.
    runtime('AssistantContentDelta', {text: ' the injection'});
    assert.deepEqual(f.c.session.cells.map(cell => [cell.kind, cell.body]), [
      ['assistant', 'Before the injection'], ['reasoning', 'Old reasoning'],
      ['user', 'same instruction'], ['assistant', 'After the injection'],
    ]);
    assert(f.c.session.cells.slice(0, 3).every(cell => !cell.streaming));
    const second = f.c.submissions.send('same instruction', 'same instruction');
    f.reply(1); await second;
    runtime('ToolCallStarted', {tool_call_id: 'tool', tool_name: 'read_file', arguments: {path: 'a.ts'}});
    runtime('ToolCallFinished', {tool_call_id: 'tool', tool_name: 'read_file', outcome: {status: 'succeeded', content: 'Done'}});
    f.apply(1);
    assert.deepEqual(f.c.session.cells.slice(-2).map(cell => cell.kind), ['tool', 'user']);
    assert.equal(f.c.session.cells.filter(cell => cell.kind === 'user').length, 2, 'Equal text with distinct IDs is not a duplicate');
    assert.equal(f.c.session.pendingInputs.size, 0);
  } finally {f.close();}
});

test('stopped guidance stays outside history, including a late queued receipt', async () => {
  const f = fixture(true);
  try {
    const send = f.c.submissions.send('never applied', 'never applied');
    f.peer.receive({jsonrpc: '2.0', method: 'runtime.state', params: {state: {...f.client.state, running: false, revision: 5}}});
    f.reply(0); await send;
    assert.equal(f.c.session.cells.length, 0);
    assert.equal([...f.c.session.pendingInputs.values()][0].tone, 'not-applied');
  } finally {f.close();}
});

test('slow local history does not delay clearing or visible feedback', async () => {
  const history = new InputHistory();
  let release!: () => void;
  history.add = () => new Promise<void>(resolve => {release = resolve;});
  const f = fixture(false, history);
  try {
    let changes = 0;
    f.c.on('change', () => changes++);
    f.c.composer = editor('save later');
    const send = f.c.key('', {return: true});
    await tick();
    assert.equal(f.c.composer.text, '');
    assert(changes > 0);
    f.reply(0);
    await send;
    release();
  } finally {f.close();}
});

test('unconfirmed delivery retries with the same ID and leaves the next draft alone', async () => {
  const f = fixture(true);
  try {
    f.c.composer = editor('first');
    const send = f.c.key('', {return: true});
    await tick();
    await f.c.key('next');
    f.peer.receive({jsonrpc: '2.0', id: f.requests[0].id, error: {code: -32603, message: 'snapshot failed'}});
    await send;
    assert.equal(f.c.session.cells.length, 0);
    assert.match([...f.c.session.pendingInputs.values()][0].title, /unconfirmed/);
    const retry = f.c.submissions.retry();
    await tick();
    assert.equal(f.requests[0].params.submission_id, f.requests[1].params.submission_id);
    f.reply(1); f.apply(1);
    await retry;
    assert.equal(f.c.composer.text, 'next');
    assert.equal(f.c.session.cells.length, 1);
  } finally {f.close();}
});

test('late acknowledgement after session reset cannot recreate the old user cell', async () => {
  const f = fixture();
  try {
    f.c.composer = editor('old session');
    const send = f.c.key('', {return: true});
    await tick();
    f.peer.receive({jsonrpc: '2.0', method: 'runtime.state', params: {state: {...f.client.state, session_generation: 1, revision: 5}}});
    await f.c.key('new session');
    f.reply(0);
    await send;
    assert.equal(f.c.composer.text, 'new session');
    assert.equal(f.c.session.cells.length, 0);
    assert.equal(f.c.session.pendingInputs.size, 0);
  } finally {f.close();}
});

test('application proof takes precedence over a later RPC failure', async () => {
  const f = fixture(true);
  try {
    f.c.composer = editor('already applied');
    const send = f.c.key('', {return: true});
    await tick();
    f.apply(0);
    f.peer.receive({jsonrpc: '2.0', id: f.requests[0].id, error: {code: -32603, message: 'late failure'}});
    await send;
    assert.equal(f.c.session.cells[0].title, 'You');
    assert.doesNotMatch(f.c.status, /late failure|unconfirmed/);
    await f.c.submissions.retry();
    assert.equal(f.requests.length, 1);
    assert.equal(f.c.status, 'No failed submission to retry.');
  } finally {f.close();}
});
