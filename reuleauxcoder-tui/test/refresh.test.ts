import assert from 'node:assert/strict';
import test, {type TestContext} from 'node:test';
import {setImmediate} from 'node:timers/promises';
import {MessagePeer, RuntimeClient, emptyState} from '@reuleauxcoder/client';
import {TuiController} from '../src/state/controller.js';

function polling(t: TestContext) {
  t.mock.timers.enable({apis: ['setTimeout', 'setInterval', 'Date']});
  t.mock.method(performance, 'now', () => Date.now());
  const requests: {method: string; id: string}[] = [];
  const peer = new MessagePeer(message => requests.push(message as typeof requests[number]));
  const client = new RuntimeClient(peer);
  client.info = {conditional_snapshots: true};
  const controller = new TuiController(client);
  controller.session.connected = true;
  controller.startRefresh();
  t.after(() => {controller.dispose(); client.close();});
  const advance = async (ms: number) => {t.mock.timers.tick(ms); await setImmediate();};
  return {peer, controller, requests, advance};
}

test('snapshot timeouts retry, preserve pushed state, and recover on a later snapshot', async t => {
  const {peer, controller, requests, advance} = polling(t);
  await advance(500);
  await advance(5000);
  assert.equal(controller.session.fatal, null);
  assert(controller.session.connected);
  peer.receive({jsonrpc: '2.0', method: 'runtime.state', params: {state: {...emptyState, revision: 1}}});
  assert.equal(controller.session.state.revision, 1);
  await advance(500);
  await advance(5000);
  assert.equal(controller.session.cells.length, 1, 'repeated timeouts produce one warning');
  await advance(500);
  assert.deepEqual(requests.map(request => request.method), Array(3).fill('runtime.snapshot'));
  peer.receive({jsonrpc: '2.0', id: requests[0].id, result: {...emptyState, revision: 80}});
  peer.receive({jsonrpc: '2.0', id: requests[2].id, result: {...emptyState, revision: 2}});
  await setImmediate();
  assert.equal(controller.session.state.revision, 2);
  await advance(5000);
  peer.close(new Error('Backend disconnected'));
  await setImmediate();
  assert.equal(controller.session.fatal, 'Backend disconnected');
  assert.equal(controller.session.connected, false);
  assert.equal(controller.session.cells.length, 2, 'the disconnect is reported once');
});

test('a pending snapshot timing out during shutdown still permits a clean saved exit', async t => {
  const {peer, controller, requests, advance} = polling(t);
  await advance(500);
  let saved: string | undefined;
  controller.once('exit', id => {saved = id;});
  const closing = controller.finish();
  await advance(5000);
  assert.deepEqual(requests.map(request => request.method), ['runtime.snapshot', 'runtime.shutdown']);
  peer.receive({jsonrpc: '2.0', id: requests[1].id, result: 'saved-session'});
  await closing;
  assert.equal(saved, 'saved-session');
  assert.equal(controller.session.fatal, null);
  assert.equal(controller.session.cells.length, 0);
});
