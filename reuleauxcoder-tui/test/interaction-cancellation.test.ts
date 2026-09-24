import assert from 'node:assert/strict';
import test from 'node:test';
import {MessagePeer, RuntimeClient, decode, record} from '@reuleauxcoder/client';

test('cancel in the request delivery tick settles without opening a modal', async () => {
  const replies: any[] = [];
  const peer = new MessagePeer(message => replies.push(message));
  const client = new RuntimeClient(peer);
  let shown = 0;
  client.on('interactions', () => {shown += client.interactions.length;});
  try {
    peer.receive({jsonrpc: '2.0', id: '1', method: 'interaction.request', params: {
      kind: 'confirm', request: record('ConfirmRequest', {request_id: 'early', title: 'Continue?'}),
    }});
    peer.receive({jsonrpc: '2.0', method: 'interaction.cancel', params: {request_id: 'early'}});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(shown, 0);
    assert.equal(client.interactions.length, 0);
    assert.equal(decode(replies[0].result).cancelled, true);
  } finally {client.close();}
});

test('closing before a deferred request handler runs cannot leave an interaction or timer', async () => {
  const peer = new MessagePeer(() => {});
  const client = new RuntimeClient(peer);
  peer.receive({jsonrpc: '2.0', id: '1', method: 'interaction.request', params: {
    kind: 'confirm', request: record('ConfirmRequest', {request_id: 'closing'}), timeout_seconds: 60,
  }});
  client.close();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(client.interactions.length, 0);
});
