/** Feedback never delays an action, changes focus, or moves confirmation targets. */
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
const id = 'reuleaux-feedback';
export function reveal(node: HTMLElement): void {
  if (reduced.matches) return;
  for (const animation of node.getAnimations()) if (animation.id === id) animation.cancel();
  node.animate([{opacity: 0.65}, {opacity: 1}], {duration: 160, easing: 'ease-out'}).id = id;
}
reduced.addEventListener('change', () => {
  if (reduced.matches) for (const animation of document.getAnimations()) if (animation.id === id) animation.cancel();
});
