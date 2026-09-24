import MarkdownIt from 'markdown-it';
import {t} from '../i18n.js';
import {icon} from './icons.js';

const markdown = new MarkdownIt({html: false, linkify: true, breaks: false});
// Remote images must not make network requests just because a reply is displayed.
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(`[${tokens[index].content || 'image'}]`);
export function renderMarkdown(target: HTMLElement, text: string, openLink: (url: string) => void): void {
  target.classList.add('markdown'); target.innerHTML = markdown.render(text);
  for (const link of target.querySelectorAll('a')) {
    const href = link.getAttribute('href') ?? '';
    if (!/^https?:\/\//i.test(href)) {link.removeAttribute('href'); continue;}
    link.title = href; link.addEventListener('click', event => {event.preventDefault(); openLink(href);});
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
