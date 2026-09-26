/** Follow new output until the reader moves away; never pull a small upward scroll back. */
export class TranscriptScroll {
  private following = true;
  private top = 0;
  private touchY = 0;

  constructor(private readonly transcript: HTMLElement, private readonly latest: HTMLElement) {
    transcript.addEventListener('wheel', event => {if (event.deltaY < 0) this.following = false;}, {passive: true});
    transcript.addEventListener('touchstart', event => {this.touchY = event.touches[0]?.clientY ?? 0;}, {passive: true});
    transcript.addEventListener('touchmove', event => {
      const y = event.touches[0]?.clientY ?? this.touchY;
      if (y > this.touchY) this.following = false;
      this.touchY = y;
    }, {passive: true});
    transcript.addEventListener('keydown', event => {
      if ((event.target as Element).closest('input, textarea, select, [contenteditable="true"]')) return;
      if (['ArrowUp', 'PageUp', 'Home'].includes(event.key) || event.key === ' ' && event.shiftKey) this.following = false;
    });
    transcript.addEventListener('scroll', () => {
      const top = transcript.scrollTop;
      if (top < this.top - 1) this.following = false;
      else if (top > this.top && transcript.scrollHeight - top - transcript.clientHeight <= 2) {
        this.following = true; latest.hidden = true;
      }
      this.top = top;
    }, {passive: true});
    latest.addEventListener('click', () => this.resume());
  }

  reset(): void {this.following = true;}

  update(changed: boolean): void {
    if (this.following) this.resume();
    else if (changed) this.latest.hidden = false;
  }

  private resume(): void {
    this.following = true;
    this.transcript.scrollTop = this.transcript.scrollHeight;
    this.top = this.transcript.scrollTop;
    this.latest.hidden = true;
  }
}
