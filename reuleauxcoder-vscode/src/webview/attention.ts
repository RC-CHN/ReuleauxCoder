import type {HostSnapshot, WebRequest} from '../shared.js';
import {t, errorText} from '../i18n.js';
import {icon} from './icons.js';

/** Stable cards preserve in-progress answers through streaming snapshots. */
export class AttentionCards {
  private nodes = new Map<string, {node: HTMLElement; signature: string}>();
  constructor(private root: HTMLElement, private request: (action: string, data?: WebRequest['data']) => Promise<any>) {}
  update(state: HostSnapshot): void {
    const live = new Set<string>();
    for (const review of state.reviews) {
      const key = `${state.hostId}:${state.generation}:${review.id}`; live.add(key);
      if (this.nodes.get(key)?.signature === JSON.stringify(review)) continue;
      const card = this.card(t('Review changes'), errorText(review.title), 'shield');
      if (review.context) {const context = document.createElement('p'); context.className = 'attention-description'; context.textContent = `${t('Agents')} · ${review.context}`; card.append(context);}
      const description = document.createElement('p'); description.className = 'attention-description'; description.textContent = review.summary; card.append(description);
      const files = document.createElement('div'); files.className = 'review-files';
      for (const document of review.documents) {
        const button = this.button(document.path, () => this.act(card, 'review', {id: review.id, documentId: document.id}));
        button.prepend(icon('expand')); button.append(icon('chevron')); button.className = 'review-file'; button.title = t('Review in editor'); files.append(button);
      }
      card.append(files);
      if (review.dirty) {const warning = document.createElement('p'); warning.className = 'review-warning'; warning.textContent = t('This proposal targets unsaved editor changes. Save them and ask the core for a new proposal.'); card.append(warning);}
      const foot = document.createElement('div'); foot.className = 'review-decision';
      const scope = document.createElement('small'); scope.textContent = t('Your approval applies only to this proposal.'); foot.append(scope);
      const buttons = document.createElement('div'); buttons.className = 'actions';
      if (!review.documents.length) buttons.append(this.button(t('View details'), () => this.act(card, 'review', {id: review.id})));
      buttons.append(this.button(t('Deny'), () => this.act(card, 'reject', {id: review.id})));
      let scopeId: string | undefined;
      const approve = this.button(review.dirty ? t('Save and request new proposal') : t('Approve once'), () => this.act(card, review.dirty ? 'saveReview' : 'approve', {id: review.id, scopeId}));
      approve.className = 'primary'; approve.prepend(icon(review.dirty ? 'history' : 'check')); buttons.append(approve); foot.append(buttons); card.append(foot); this.put(key, card, review);
      const alternatives = document.createElement('details'); alternatives.className = 'review-alternatives'; const heading = document.createElement('summary'); heading.textContent = t('Scope and feedback'); alternatives.append(heading);
      if (review.grants?.length && !review.dirty) {
        const fieldset = document.createElement('fieldset'); const legend = document.createElement('legend'); legend.textContent = t('Approval scope'); fieldset.append(legend);
        for (const option of [{id: '', label: t('Approve once'), description: t('Your approval applies only to this proposal.'), broad: false}, ...review.grants]) {
          const label = document.createElement('label'); const input = document.createElement('input'); input.type = 'radio'; input.name = `scope-${review.id}`; input.value = option.id; input.checked = !option.id;
          const text = document.createElement('span'); text.textContent = errorText(option.label); const description = document.createElement('small'); description.textContent = `${option.broad ? t('Broad permission') + ' · ' : ''}${errorText(option.description)}`; text.append(description); label.append(input, text); fieldset.append(label);
          input.addEventListener('change', () => {if (!input.checked) return; scopeId = option.id || undefined; scope.textContent = option.description; approve.querySelector('span')!.textContent = scopeId ? t('Approve selected scope') : t('Approve once');});
        }
        alternatives.append(fieldset);
      }
      const form = document.createElement('form'); const input = document.createElement('textarea'); input.placeholder = t('Explain what should change…'); input.setAttribute('aria-label', input.placeholder); input.required = true; input.maxLength = 8192;
      const reject = this.button(t('Send feedback and deny'), () => {}); reject.type = 'submit'; form.append(input, reject); form.addEventListener('submit', event => {event.preventDefault(); this.act(card, 'reject', {id: review.id, feedback: input.value});}); alternatives.append(form); card.append(alternatives);
    }
    for (const item of state.interactions ?? []) {
      const key = `${state.hostId}:${state.generation}:${item.id}`; live.add(key);
      if (this.nodes.get(key)?.signature === JSON.stringify(item)) continue;
      const card = this.card(t('Your input is needed'), item.title, 'goal');
      const message = document.createElement('p'); message.className = 'attention-description'; message.textContent = item.message; card.append(message);
      const actions = document.createElement('div'); actions.className = 'actions';
      if (item.kind === 'input_text') {
        const form = document.createElement('form'); form.className = 'answer-form';
        const input = document.createElement('input'); input.type = item.secret ? 'password' : 'text'; input.required = !item.allowEmpty; input.value = item.secret ? '' : item.initial ?? ''; input.autocomplete = 'off'; input.spellcheck = !item.secret; input.placeholder = item.placeholder ?? ''; input.setAttribute('aria-label', item.message || item.title);
        const submit = this.button(t('Continue'), () => {}); submit.type = 'submit'; submit.className = 'primary';
        form.append(input, submit); form.addEventListener('submit', event => {event.preventDefault(); const value = input.value; if (item.secret) input.value = ''; this.act(card, 'answer', {id: item.id, value});}); card.append(form);
      } else if (item.kind === 'choose_one') {
        const options = document.createElement('div'); options.className = 'answer-options';
        for (const option of item.items ?? []) {const button = this.button(option.label, () => this.act(card, 'answer', {id: item.id, selected: option.id})); const description = document.createElement('small'); description.textContent = option.description; button.append(description); options.append(button);} card.append(options);
      } else if (item.kind === 'confirm') {
        const confirm = this.button(t('Confirm'), () => this.act(card, 'answer', {id: item.id, confirmed: true})); confirm.className = 'primary'; actions.append(confirm);
      }
      if (item.allowCancel) actions.append(this.button(t('Cancel'), () => this.act(card, 'answer', {id: item.id, cancel: true})));
      card.append(actions); this.put(key, card, item);
    }
    for (const [key, entry] of this.nodes) if (!live.has(key)) {entry.node.querySelectorAll('input').forEach(input => input.value = ''); entry.node.remove(); this.nodes.delete(key);}
    const jump = document.getElementById('attention-jump')!; jump.hidden = live.size === 0;
    jump.querySelector('span')!.textContent = t('Waiting for you · {0}', live.size);
  }
  private card(eyebrow: string, title: string, name: 'shield' | 'goal'): HTMLElement {
    const card = document.createElement('article'); card.className = 'attention-card';
    const head = document.createElement('div'); head.className = 'attention-heading'; head.append(icon(name));
    const text = document.createElement('div'); const small = document.createElement('small'); small.textContent = eyebrow; const heading = document.createElement('h3'); heading.textContent = title; text.append(small, heading); head.append(text); card.append(head); return card;
  }
  private put(key: string, node: HTMLElement, value: unknown): void {
    const previous = this.nodes.get(key); if (previous) previous.node.replaceWith(node); else this.root.append(node);
    this.nodes.set(key, {node, signature: JSON.stringify(value)});
  }
  private button(text: string, action: () => void): HTMLButtonElement {
    const button = document.createElement('button'); button.type = 'button'; const label = document.createElement('span'); label.textContent = text; button.append(label); button.addEventListener('click', action); return button;
  }
  private act(card: HTMLElement, action: string, data: Record<string, unknown>): void {
    if (card.getAttribute('aria-busy') === 'true') return;
    card.setAttribute('aria-busy', 'true'); card.querySelectorAll<HTMLButtonElement>('button').forEach(button => button.disabled = true);
    card.querySelector('.form-error')?.remove();
    void this.request(action, data).catch(reason => {const error = document.createElement('p'); error.className = 'form-error'; error.setAttribute('role', 'alert'); error.textContent = errorText(reason); card.append(error);}).finally(() => {card.setAttribute('aria-busy', 'false'); card.querySelectorAll<HTMLButtonElement>('button').forEach(button => button.disabled = false);});
  }
}
