import MarkdownIt from 'markdown-it';
import {t} from '../i18n.js';
import {icon} from './icons.js';
import {fileReference, fileReferences} from '../file-links.js';

const markdown = new MarkdownIt({html: false, linkify: true, breaks: false});
// .md is also a DNS suffix. Bare AGENT.md belongs to the workspace, not http://agent.md.
markdown.linkify.set({fuzzyLink: false});
// Remote images must not make network requests just because a reply is displayed.
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(`[${tokens[index].content || 'image'}]`);
export function renderMarkdown(target: HTMLElement, text: string, openLink: (url: string) => void, openFile: (path: string) => void): void {
  target.classList.add('markdown'); target.innerHTML = markdown.render(text);
  const fileLink = (link: HTMLAnchorElement, reference: string) => {
    link.href = '#'; link.dataset.file = reference; link.title = t('Open workspace file: {0}', reference); link.classList.add('file-reference');
    link.addEventListener('click', event => {event.preventDefault(); openFile(reference);});
    link.addEventListener('auxclick', event => {if (event.button === 1) {event.preventDefault(); openFile(reference);}});
  };
  for (const link of target.querySelectorAll('a')) {
    const href = link.getAttribute('href') ?? '';
    if (fileReference(href)) {fileLink(link, href); continue;}
    if (!/^https?:\/\//i.test(href)) {link.removeAttribute('href'); continue;}
    link.title = href; link.addEventListener('click', event => {event.preventDefault(); openLink(href);});
  }
  for (const code of target.querySelectorAll('code')) {
    if (code.closest('pre, a') || /\s/.test(code.textContent ?? '') || !fileReference(code.textContent ?? '')) continue;
    const link = document.createElement('a'); fileLink(link, code.textContent!); code.replaceWith(link); link.append(code);
  }
  const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  while (walker.nextNode()) if (!walker.currentNode.parentElement?.closest('a, pre, code')) nodes.push(walker.currentNode as Text);
  for (const node of nodes) {
    const value = node.textContent!; const fragment = document.createDocumentFragment(); let end = 0;
    for (const match of value.matchAll(fileReferences)) {
      if (match.index! > 0 && /[\w/@.:\\-]/.test(value[match.index! - 1]) || !fileReference(match[0])) continue;
      fragment.append(value.slice(end, match.index));
      const link = document.createElement('a'); link.textContent = match[0]; fileLink(link, match[0]); fragment.append(link); end = match.index! + match[0].length;
    }
    if (end) {fragment.append(value.slice(end)); node.replaceWith(fragment);}
  }
  for (const pre of target.querySelectorAll('pre')) {
    const code = pre.querySelector('code'); if (!code) continue;
    const wrapper = document.createElement('div'); wrapper.className = 'code-block'; pre.replaceWith(wrapper);
    const toolbar = document.createElement('div'); toolbar.className = 'code-toolbar';
    const language = document.createElement('span'); language.textContent = code.className.replace(/^language-/, '') || t('Code');
    const copy = document.createElement('button'); copy.type = 'button'; copy.title = t('Copy code'); copy.append(icon('copy'), document.createTextNode(t('Copy')));
    copy.addEventListener('click', () => {void navigator.clipboard.writeText(code.textContent ?? '').then(() => {copy.textContent = t('Copied');}, () => {copy.textContent = t('Copy failed');});});
    toolbar.append(language, copy); wrapper.append(toolbar, pre);
  }
  for (const table of target.querySelectorAll('table')) {const scroll = document.createElement('div'); scroll.className = 'table-scroll'; table.replaceWith(scroll); scroll.append(table);}
}
