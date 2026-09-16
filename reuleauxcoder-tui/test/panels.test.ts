import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import {setImmediate as nextTurn} from 'node:timers/promises';
import {RuntimeClient} from '../src/protocol/client.js';
import {RpcPeer} from '../src/protocol/peer.js';
import {TuiController} from '../src/state/controller.js';
import type {View} from '../src/protocol/wire.js';
import type {Action} from '../src/protocol/wire.js';
import {panelRows} from '../src/ui/panels.js';

test('compact opens choices and sends a typed strategy without a text form', async t => {
  const peer = new RpcPeer(new PassThrough(), new PassThrough());
  const client = new RuntimeClient(peer);
  const controller = new TuiController(client);
  t.after(() => {controller.dispose(); peer.close();});
  const calls: any[] = [];
  const action: Action = {action_id: 'system.compact', feature_id: 'system', description: 'Compact context', preview: true,
    parameters: [{name: 'force_strategy', kind: 'text', required: false, nullable: true, default: null}], triggers: [{kind: 'slash', value: '/compact'}]};
  client.panel = async () => ({refresh: 'update', definition: {
    view_type: 'compact', title: 'Compact · estimated 1,200 / 8,000 tokens',
    items: ['summarize', 'snip', 'collapse'].map(strategy => ({label: strategy, description: '', current: false, action: {action_id: action.action_id, command: {force_strategy: strategy}}})),
    children: [], filterable: false, keep_open_on_submit: false, return_to_parent_on_submit: false, show_auxiliary_actions: false,
  }});
  client.submitAction = async (id, command) => {
    calls.push({id, command});
    if (!command?.force_strategy) controller.session.emit('view', {action: 'open', title: 'Compact', focus: true, view_model: {}, reuse_key: 'compact'}, {});
    return {} as any;
  };
  await controller.openMenu({name: '/compact', title: 'Compact', actions: [action]});
  await nextTurn();
  assert.equal(controller.screen?.kind, 'list');
  if (controller.screen?.kind !== 'list') throw new Error('missing compact picker');
  assert.deepEqual(controller.screen.items.map(item => item.label), ['summarize', 'snip', 'collapse']);
  assert(!panelRows(controller, 80, 12)!.rows.join('\n').includes('Filter options'));
  await controller.key('x', {});
  assert.equal(controller.screen.filter.text, '');
  await controller.key('', {downArrow: true});
  await controller.key('', {return: true});
  assert.deepEqual(calls.at(-1), {id: 'system.compact', command: {force_strategy: 'snip'}});
  assert.equal(controller.screen, undefined);
});

for (const dismiss of [false, true]) {
  test(dismiss ? 'queued panel responses cannot reopen a dismissed view' : 'slow panel refresh cannot overwrite a newer completed state', async t => {
    const peer = new RpcPeer(new PassThrough(), new PassThrough());
    const client = new RuntimeClient(peer);
    const controller = new TuiController(client);
    t.after(() => {controller.dispose(); peer.close();});
    let release!: () => void;
    const slow = new Promise<void>(resolve => {release = resolve;});
    client.panel = async label => {
      if (label === 'Active') await slow;
      return {refresh: 'update', definition: {
        view_type: 'goal', title: 'Goal', items: [{label, description: '', current: false, action: null}],
        children: [], filterable: false, keep_open_on_submit: true, return_to_parent_on_submit: false,
      }};
    };
    const show = (label: string, action = 'refresh') => {
      const view: View = {action, title: 'Goal', view_model: {}, focus: true, reuse_key: 'goal'};
      controller.session.emit('view', view, label);
    };
    show('Ready', 'open');
    await nextTurn();
    show('Active');
    show('Complete');
    await nextTurn();
    if (dismiss) await controller.key('', {escape: true});
    release();
    await nextTurn();
    if (dismiss) assert.equal(controller.screen, undefined);
    else {
      assert.equal(controller.screen?.kind, 'list');
      if (controller.screen?.kind === 'list') assert.equal(controller.screen.items[0].label, 'Complete');
    }
  });
}
