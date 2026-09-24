import {cancellation, record, type RuntimeClient} from '@reuleauxcoder/client';
import type {InlineInteraction} from '../shared.js';
import {t} from '../i18n.js';

export function inlineInteractions(client?: RuntimeClient): InlineInteraction[] {
  return (client?.interactions ?? []).filter(item => item.kind !== 'review').map(({kind, request}) => ({
    id: request.request_id, kind, title: request.title, message: request.prompt ?? request.message ?? '',
    secret: request.secret === true, initial: request.secret ? '' : request.initial_value,
    placeholder: request.placeholder, allowEmpty: request.allow_empty === true, allowCancel: request.allow_cancel !== false,
    items: request.items?.map((item: any) => ({id: item.id, label: item.label, description: item.description})),
  }));
}

/** Accept only responses applicable to this still-pending backend request. */
export function answerInteraction(client: RuntimeClient, data: Record<string, unknown>): void {
  const pending = client.interactions.find(item => item.request.request_id === data.id);
  if (!pending || pending.kind === 'review') throw new Error(t('This request has expired.'));
  const request = pending.request;
  if (data.cancel === true) {
    if (request.allow_cancel === false) throw new Error(t('Choose an option to continue.'));
    client.answer(request.request_id, cancellation(pending.kind)); return;
  }
  switch (pending.kind) {
    case 'confirm':
      if (typeof data.confirmed !== 'boolean') throw new Error(t('Invalid confirmation.'));
      client.answer(request.request_id, record('ConfirmResponse', {confirmed: data.confirmed, cancelled: false})); return;
    case 'choose_one':
      if (typeof data.selected !== 'string' || !request.items.some((item: any) => item.id === data.selected)) throw new Error(t('Choose an option to continue.'));
      client.answer(request.request_id, record('ChooseOneResponse', {selected_id: data.selected, cancelled: false})); return;
    case 'input_text':
      if (typeof data.value !== 'string' || data.value.length > 1024 * 1024 || (!request.allow_empty && !data.value.trim())) throw new Error(t('Required'));
      client.answer(request.request_id, record('InputTextResponse', {value: data.value, cancelled: false})); return;
    default: throw new Error(t('Unsupported interaction.'));
  }
}
