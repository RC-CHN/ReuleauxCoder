import type {HostSnapshot, WebRequest} from '../shared.js';
import {t, errorText, coreText} from '../i18n.js';
import {icon} from './icons.js';
import {interactionText, approvalText, approvalReason} from '../core-messages.js';

/** Stable cards preserve in-progress answers through streaming snapshots. */
export class AttentionCards {
  private nodes = new Map<string, {node: HTMLElement; signature: string}>();
  constructor(private root: HTMLElement, private request: (action: string, data?: WebRequest['data']) => Promise<any>) {}
  update(state: HostSnapshot): void {
    const live = new Set<string>();
    for (const review of state.reviews) {
      const key = `${state.hostId}:${state.generation}:${review.id}`; live.add(key);
      if (this.nodes.get(key)?.signature === JSON.stringify(review)) continue;
      const previous = this.nodes.get(key)?.node;
      const title = review.tool === 'shell' ? t('Run this command?') : review.documents.length ? t('Apply these changes?') : coreText(review.title);
      const card = this.card(t('Tool approval'), title, 'shield');
      if (review.tool) {
        const tool = document.createElement('div'); tool.className = 'review-tool';
        tool.textContent = `${review.tool}${review.source ? ' · ' + coreText(review.source) : ''}`; card.querySelector('.attention-heading > div')!.append(tool);
      }
      if (review.context) {const context = document.createElement('p'); context.className = 'attention-description'; context.textContent = `${t('Agents')} · ${review.context}`; card.append(context);}
      const description = document.createElement('p'); description.className = 'attention-description'; description.textContent = approvalText(review.summary); card.append(description);
      if (review.reason && !review.summary.includes(review.reason)) {
        const reason = document.createElement('p'); reason.className = 'review-reason'; reason.textContent = `${t('Reason')} · ${approvalReason(review.reason)}`; card.append(reason);
      }
      if (review.cwd !== undefined) {
        const location = document.createElement('div'); location.className = 'review-location'; location.append(icon('terminal'));
        location.append(document.createTextNode(`${state.environment} · ${review.cwd ?? t('Current core directory')}`)); card.append(location);
      }
      for (const section of review.preview ?? []) {
        const preview = document.createElement(section.secondary ? 'details' : 'section'); preview.className = 'review-preview';
        const title = document.createElement(section.secondary ? 'summary' : 'div'); title.textContent = coreText(section.title);
        const text = document.createElement('pre'); text.textContent = section.title === 'Outside workspace' ? approvalText(section.content) : section.content;
        preview.append(title, text);
        if (section.truncated) preview.append(this.button(t('Preview shortened · View full details'), () => this.act(card, 'review', {id: review.id})));
        card.append(preview);
      }
      const files = document.createElement('div'); files.className = 'review-files';
      for (const file of review.documents) {
        const button = this.button(file.path, () => this.act(card, 'review', {id: review.id, documentId: file.id}));
        const path = file.path.replaceAll('\\', '/'); const name = path.split('/').at(-1)!;
        const label = button.querySelector('span')!; label.textContent = name;
        const directory = document.createElement('small'); directory.textContent = path.slice(0, -name.length); label.append(directory);
        const action = document.createElement('span'); action.className = 'file-action'; action.textContent = t('View diff');
        button.prepend(icon('commands')); button.append(action, icon('chevron')); button.className = 'review-file'; button.title = `${t('Review in editor')} · ${file.path}`; files.append(button);
      }
      card.append(files);
      if (review.dirty) {const warning = document.createElement('p'); warning.className = 'review-warning'; warning.textContent = t('This proposal targets unsaved editor changes. Save them and ask the core for a new proposal.'); card.append(warning);}
      const foot = document.createElement('div'); foot.className = 'review-decision';
      const scope = document.createElement('small'); scope.textContent = t('Your approval applies only to this proposal.'); foot.append(scope);
      const buttons = document.createElement('div'); buttons.className = 'actions';
      let scopeId: string | undefined;
      const approve = this.button(review.dirty ? t('Save and request new proposal') : t('Allow once'), () => this.act(card, review.dirty ? 'saveReview' : 'approve', {id: review.id, scopeId}));
      approve.className = 'primary'; approve.prepend(icon(review.dirty ? 'history' : 'check'));
      if (review.grants?.length && !review.dirty) {
        const alternatives = document.createElement('details'); alternatives.className = 'review-alternatives';
        alternatives.open = previous?.querySelector<HTMLDetailsElement>('.review-alternatives')?.open ?? false;
        const heading = document.createElement('summary'); heading.textContent = t('Allow similar calls for this session…'); alternatives.append(heading);
        const fieldset = document.createElement('fieldset'); const legend = document.createElement('legend'); legend.textContent = t('Approval scope'); fieldset.append(legend);
        for (const option of [{id: '', label: t('Allow once'), description: t('Your approval applies only to this proposal.'), broad: false}, ...review.grants]) {
          const label = document.createElement('label'); const input = document.createElement('input'); input.type = 'radio'; input.name = `scope-${review.id}`; input.value = option.id; input.checked = !option.id;
          label.classList.toggle('broad-scope', option.broad);
          const text = document.createElement('span'); text.textContent = coreText(option.label); const description = document.createElement('small'); description.textContent = `${option.broad ? t('Broad permission') + ' · ' : ''}${option.description}`; text.append(description); label.append(input, text); fieldset.append(label);
          input.addEventListener('change', () => {if (!input.checked) return; scopeId = option.id || undefined; scope.textContent = scopeId ? `${t('This session')} · ${coreText(option.label)} · ${option.description}` : t('Your approval applies only to this proposal.'); approve.querySelector('span')!.textContent = scopeId ? t('Allow for this session') : t('Allow once');});
        }
        alternatives.append(fieldset); card.append(alternatives);
      }
      const form = document.createElement('form'); form.className = 'review-feedback'; form.hidden = true;
      const input = document.createElement('textarea'); input.placeholder = t('Explain what should change…'); input.required = true; input.maxLength = 8192;
      const label = document.createElement('label'); label.textContent = t('What should the agent do differently?'); label.append(input);
      const reject = this.button(t('Send feedback and deny'), () => {}); reject.type = 'submit'; form.append(label, reject);
      form.addEventListener('submit', event => {event.preventDefault(); this.act(card, 'reject', {id: review.id, feedback: input.value});});
      const feedback = this.button(t('Give feedback'), () => {form.hidden = !form.hidden; feedback.setAttribute('aria-expanded', String(!form.hidden)); if (!form.hidden) {input.focus(); form.scrollIntoView({block: 'nearest'});}});
      feedback.className = 'review-feedback-toggle'; feedback.setAttribute('aria-expanded', 'false');
      input.value = previous?.querySelector<HTMLTextAreaElement>('.review-feedback textarea')?.value ?? '';
      if (previous?.querySelector<HTMLElement>('.review-feedback')?.hidden === false) {form.hidden = false; feedback.setAttribute('aria-expanded', 'true');}
      const deny = this.button(t('Deny'), () => this.act(card, 'reject', {id: review.id})); deny.className = 'review-deny';
      buttons.append(feedback, deny, approve); foot.append(buttons); card.append(foot, form);
      if (!review.documents.length && !review.preview?.length) card.append(this.button(t('View details'), () => this.act(card, 'review', {id: review.id})));
      this.put(key, card, review);
    }
    for (const item of state.interactions ?? []) {
      const key = `${state.hostId}:${state.generation}:${item.id}`; live.add(key);
      if (this.nodes.get(key)?.signature === JSON.stringify(item)) continue;
      const card = this.card(t('Your input is needed'), interactionText(item.title), 'goal');
      const message = document.createElement('p'); message.className = 'attention-description'; message.textContent = interactionText(item.message); card.append(message);
      const actions = document.createElement('div'); actions.className = 'actions';
      if (item.kind === 'input_text') {
        const form = document.createElement('form'); form.className = 'answer-form';
        const input = document.createElement('input'); input.type = item.secret ? 'password' : 'text'; input.required = !item.allowEmpty; input.value = item.secret ? '' : item.initial ?? ''; input.autocomplete = 'off'; input.spellcheck = !item.secret; input.placeholder = interactionText(item.placeholder ?? ''); input.setAttribute('aria-label', interactionText(item.message || item.title));
        const submit = this.button(t('Continue'), () => {}); submit.type = 'submit'; submit.className = 'primary';
        form.append(input, submit); form.addEventListener('submit', event => {event.preventDefault(); const value = input.value; if (item.secret) input.value = ''; this.act(card, 'answer', {id: item.id, value});}); card.append(form);
      } else if (item.kind === 'choose_one') {
        const options = document.createElement('div'); options.className = 'answer-options';
        for (const option of item.items ?? []) {const button = this.button(coreText(option.label), () => this.act(card, 'answer', {id: item.id, selected: option.id})); const description = document.createElement('small'); description.textContent = coreText(option.description); button.append(description); options.append(button);} card.append(options);
      } else if (item.kind === 'confirm') {
        const confirm = this.button(t('Confirm'), () => this.act(card, 'answer', {id: item.id, confirmed: true})); confirm.className = 'primary'; actions.append(confirm);
      }
      if (item.allowCancel) actions.append(this.button(t('Cancel'), () => this.act(card, 'answer', {id: item.id, cancel: true})));
      card.append(actions); this.put(key, card, item);
    }
    for (const [key, entry] of this.nodes) if (!live.has(key)) {entry.node.querySelectorAll('input').forEach(input => input.value = ''); entry.node.remove(); this.nodes.delete(key);}
    const jump = document.getElementById('attention-jump')!; jump.hidden = live.size === 0;
    jump.querySelector('span')!.textContent = String(live.size);
    jump.title = t('Waiting for you · {0}', live.size); jump.setAttribute('aria-label', jump.title);
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
