import type {HostSnapshot, WebRequest} from '../shared.js';
import {t, toolLabel} from '../i18n.js';
import {reveal} from './motion.js';

export class ActivityView {
  private state?: HostSnapshot;
  private busy = false;
  private signature = '';
  private marker = document.createElement('span');
  private label = document.createElement('span');

  constructor(private root: HTMLElement, private steering: HTMLElement, private request: (action: string, data?: WebRequest['data']) => Promise<any>, private notice: (error: unknown) => void) {
    this.marker.className = 'activity-mark'; this.marker.setAttribute('aria-hidden', 'true');
    for (let i = 0; i < 3; i++) this.marker.append(document.createElement('i'));
    this.label.className = 'activity-label'; root.replaceChildren(this.marker, this.label);
    steering.querySelector('button')!.addEventListener('click', () => void this.promote());
  }

  update(state: HostSnapshot): void {
    if (this.state && (state.hostId !== this.state.hostId || state.generation !== this.state.generation)) this.busy = false;
    this.state = state;
    const status = state.steering, waiting = state.reviews.length + (state.interactions?.length ?? 0);
    const activity = state.overview?.activity;
    const kind = state.phase !== 'ready' ? state.phase : status?.stopping ? 'stopping' : waiting ? 'waiting' : status?.pending ? 'steering' : !state.running ? 'ready' : activity === 'Writing' ? 'responding' : activity && activity !== 'Reasoning' ? 'tool' : 'thinking';
    const label = {idle: t('Disconnected'), starting: t('Connecting…'), installing: t('Installing…'), stopping: t('Stopping…'), failed: t('Connection failed'), waiting: t('Waiting for you'), steering: t('Applying guidance…'), ready: t('Ready'), responding: t('Responding'), tool: t('Running'), thinking: t('Thinking')}[kind];
    const signature = `${kind}:${label}:${activity}`;
    if (signature !== this.signature) {
      this.signature = signature; this.root.dataset.state = kind; this.label.textContent = label;
      this.root.title = kind === 'tool' ? `${label} · ${toolLabel(activity!)}` : label;
      this.root.setAttribute('aria-label', this.root.title); reveal(this.root);
    }
    const visible = state.phase === 'ready' && state.running && !!status && (status.queued > 0 || status.pending);
    const wasHidden = this.steering.hidden; this.steering.hidden = !visible;
    if (!visible) return;
    this.steering.querySelector<HTMLElement>('[data-steering-message]')!.textContent = status!.stopping ? t('Stopping; queued guidance is retained in the conversation.') : status!.pending ? t('Interrupting the current step to apply your guidance…') : t('{0} queued · applies after the current step', status!.queued);
    const button = this.steering.querySelector('button')!;
    button.disabled = this.busy || status!.pending || status!.stopping || !status!.supported;
    button.textContent = status!.pending || this.busy ? t('Applying…') : t('Guide now');
    button.title = status!.supported ? t('Interrupt the current step and apply queued messages without stopping the task.') : t('Update the core to apply guidance immediately.');
    this.steering.setAttribute('aria-busy', String(this.busy || status!.pending));
    if (wasHidden) reveal(this.steering);
  }

  private async promote(): Promise<void> {
    const state = this.state;
    if (!state?.steering?.supported || state.steering.pending || state.steering.stopping || this.busy) return;
    this.busy = true; this.update(state);
    try {await this.request('steer');}
    catch (error) {this.notice(error);}
    finally {this.busy = false; if (this.state) this.update(this.state);}
  }
}
