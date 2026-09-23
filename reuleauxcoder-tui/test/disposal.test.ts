import assert from 'node:assert/strict';
import test from 'node:test';
import {setTimeout as delay} from 'node:timers/promises';
import {MessagePeer} from '@reuleauxcoder/client';
import {RuntimeClient} from '@reuleauxcoder/client';
import {TuiController} from '../src/state/controller.js';

test('disposing and remounting controllers detaches only their own listeners', async t => {
  let writes = 0, hostEvents = 0;
  const client = new RuntimeClient(new MessagePeer(() => {writes++;}));
  t.after(() => client.close());
  client.on('command', () => hostEvents++);
  const retired = Array.from({length: 3}, () => {
    const controller = new TuiController(client);
    controller.changed(); // Disposal must also cancel an already queued update.
    controller.dispose(); controller.dispose();
    assert.equal(controller.session.listenerCount('change'), 0);
    assert.equal(controller.session.listenerCount('view'), 0);
    return controller;
  });
  const current = new TuiController(client);
  t.after(() => current.dispose());
  const revisions = retired.map(controller => controller.snapshot());
  client.emit('command', 'Only the mounted view should receive this');
  client.emit('state', {...client.state, revision: 1, model: 'changed'});
  client.emit('shutdownProgress', 'Saving…');
  client.emit('operationFailure', 'Synthetic failure');
  await delay(30);
  for (const [index, controller] of retired.entries()) {
    assert.equal(controller.session.cells.length, 0);
    assert.equal(controller.session.state.revision, 0);
    assert.equal(controller.status, '');
    assert.equal(controller.shutdownProgress, 'Stopping active tasks…');
    assert.equal(controller.snapshot(), revisions[index]);
  }
  assert.equal(current.session.cells.length, 2);
  assert.equal(current.session.state.model, 'changed');
  assert.equal(current.shutdownProgress, 'Saving…');
  assert.equal(hostEvents, 1);
  assert.equal(writes, 0, 'view disposal must not shut down the host runtime');
  assert.equal(client.peer.closed, false);
});

test('disposal ignores pending panel replies and stops history pagination', async t => {
  const client = new RuntimeClient(new MessagePeer(() => {}));
  const controller = new TuiController(client);
  t.after(() => {controller.dispose(); client.close();});
  let panelReply!: (value: null) => void;
  client.panel = () => new Promise(resolve => {panelReply = resolve;});
  controller.session.emit('view', {action: 'open', focus: true, title: 'Late view', view_model: {}}, {});
  await Promise.resolve(); // Let the queued panel request start.
  let historyReply!: (value: any) => void;
  let historyRequests = 0;
  client.history = () => {historyRequests++; return new Promise(resolve => {historyReply = resolve;});};
  controller.showHistory();
  assert(controller.screen?.kind === 'history');
  const browser = controller.screen.browser;
  assert(browser.loading);
  controller.dispose();
  panelReply(null);
  historyReply({session_generation: client.state.session_generation, page: {records: [], indexed_bytes: 1, source_bytes: 2}});
  await delay(30);
  assert.equal(controller.screens.length, 1);
  assert.equal(controller.screen?.kind, 'history');
  assert.equal(browser.loading, false);
  assert.equal(browser.page, undefined);
  assert.equal(historyRequests, 1, 'an unmounted browser must not request another index page');
  assert.equal(controller.session.cells.length, 0);
});
