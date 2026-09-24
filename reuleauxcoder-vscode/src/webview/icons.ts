const paths = {
  brand: 'M12 2A20 20 0 0 1 22 19.32A20 20 0 0 1 2 19.32A20 20 0 0 1 12 2Z M8 10l3 3-3 3m5 0h3',
  goal: 'M20 10a8 8 0 1 1-6-6 M12 8a4 4 0 1 0 4 4 M12 12l9-9m-5 0h5v5',
  model: 'm12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5',
  mode: 'M5 3v18M19 3v18M3 8h4M17 16h4M12 3v18M10 12h4',
  skills: 'm12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z',
  tools: 'M8 3v5m8-5v5M5 8h14v3a7 7 0 0 1-14 0V8Zm7 10v3',
  history: 'M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v5l3 2',
  terminal: 'm5 6 6 6-6 6m8 0h6',
  agents: 'M8 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm8 1a3 3 0 1 0 0-6M2 20v-2a6 6 0 0 1 12 0v2m3-7a5 5 0 0 1 5 5v2',
  shield: 'm12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Zm-4 9 3 3 5-6',
  settings: 'M4 6h16M4 12h16M4 18h16M8 4v4m8 2v4m-6 2v4',
  plus: 'M12 5v14M5 12h14', close: 'm6 6 12 12M6 18 18 6',
  expand: 'M14 4h6v6m0-6-9 9M10 4H4v16h16v-6',
  arrow: 'm6 12 6-6 6 6m-6-6v14', chevron: 'm9 5 7 7-7 7', back: 'm15 5-7 7 7 7',
  check: 'm5 12 4 4L19 6', search: 'M10 17a7 7 0 1 0 0-14 7 7 0 0 0 0 14Zm5-2 6 6',
  attach: 'm8 12 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9m-2 10 6-6',
  stop: 'M6 6h12v12H6Z', copy: 'M8 8h12v13H8ZM16 8V3H3v13h5',
  commands: 'm8 5-5 7 5 7m8-14 5 7-5 7m-3-16-2 18',
} as const;
export type IconName = keyof typeof paths;
export function icon(name: IconName): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('class', 'icon'); svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', paths[name]); svg.append(path); return svg;
}
export function decorateIcons(root: ParentNode = document): void {
  root.querySelectorAll<HTMLElement>('[data-icon]').forEach(node => node.prepend(icon(node.dataset.icon as IconName)));
}
