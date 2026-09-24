import assert from 'node:assert/strict';
import test from 'node:test';
import {MessagePeer, RuntimeClient, decode, emptyState, record} from '@reuleauxcoder/client';
import {TuiController} from '../src/state/controller.js';
import {editor} from '../src/state/editor.js';
import {InputHistory} from '../src/state/history.js';

const tick = () => new Promise<void>(resolve => setImmediate(resolve));
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
    assert.equal(f.c.session.cells[0].body, 'first');
    assert.match(f.c.session.cells[0].title, /sending/);
    await f.c.key('', {return: true});
    assert.equal(f.requests.length, 1);
    await f.c.key('next');
    f.apply(0); // Application can arrive before admission acknowledgement.
    f.reply(0);
    await first;
    assert.equal(f.c.composer.text, 'next');
    assert.equal(f.c.session.cells.length, 1);
    assert.equal(f.c.session.cells[0].title, 'You');
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
    assert.match(f.c.session.cells[0].title, /unconfirmed/);
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
