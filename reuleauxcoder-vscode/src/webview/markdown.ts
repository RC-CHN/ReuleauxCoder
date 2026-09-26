import MarkdownIt from 'markdown-it';
import {t} from '../i18n.js';
import {icon} from './icons.js';
import {fileReference, fileReferences} from '../file-links.js';

const markdown = new MarkdownIt({html: false, linkify: true, breaks: false});
// .md is also a DNS suffix. Bare AGENT.md belongs to the workspace, not http://agent.md.
markdown.linkify.set({fuzzyLink: false});
// Remote images must not make network requests just because a reply is displayed.
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(`[${tokens[index].content || 'image'}]`);
interface Code {type: string; info: string; content: string}
interface Block {html: string; nodes: Node[]; code?: Code}
const rendered = new WeakMap<HTMLElement, Block[]>();

export function renderMarkdown(target: HTMLElement, text: string, openLink: (url: string) => void, openFile: (path: string) => void): void {
  target.classList.add('markdown');
  // Parse the whole document so reference definitions and unfinished lists/fences keep
  // Markdown semantics. Only replace changed top-level blocks in the live DOM.
  const env = {}, tokens = markdown.parse(text, env), html: string[] = [], codes: (Code | undefined)[] = [];
  for (let start = 0; start < tokens.length;) {
    let end = start + 1, nesting = tokens[start].nesting;
    while (nesting > 0 && end < tokens.length) nesting += tokens[end++].nesting;
    html.push(markdown.renderer.render(tokens.slice(start, end), markdown.options, env));
    const token = tokens[start];
    codes.push(end === start + 1 && ['fence', 'code_block'].includes(token.type) ? {type: token.type, info: token.info, content: token.content} : undefined);
    start = end;
  }
  const previous = rendered.get(target) ?? [];
  let prefix = 0, suffix = 0;
  while (prefix < previous.length && prefix < html.length && previous[prefix].html === html[prefix]) prefix++;
  while (suffix < previous.length - prefix && suffix < html.length - prefix && previous[previous.length - 1 - suffix].html === html[html.length - 1 - suffix]) suffix++;
  // A growing code block keeps its toolbar, scroll position and existing text
  // nodes. Parsing still decides its boundaries and language on every update.
  while (prefix < previous.length - suffix && prefix < html.length - suffix) {
    const block = previous[prefix], code = codes[prefix];
    if (!code || !block.code || code.type !== block.code.type || code.info !== block.code.info) break;
    const element = (block.nodes.find(node => node instanceof HTMLElement) as HTMLElement | undefined)?.querySelector('pre > code');
    if (!element) break;
    if (element.firstChild instanceof Text && element.childNodes.length === 1) {
      const before = block.code.content, after = code.content;
      let start = 0, end = 0;
      while (start < before.length && start < after.length && before[start] === after[start]) start++;
      while (end < before.length - start && end < after.length - start && before[before.length - end - 1] === after[after.length - end - 1]) end++;
      element.firstChild.replaceData(start, before.length - start - end, after.slice(start, after.length - end));
    } else element.textContent = code.content;
    block.html = html[prefix]; block.code = code; prefix++;
  }
  const anchor = previous[previous.length - suffix]?.nodes[0] ?? null;
  const next = previous.slice(0, prefix);
  for (const block of previous.slice(prefix, previous.length - suffix)) for (const node of block.nodes) node.parentNode?.removeChild(node);
  const fragment = document.createDocumentFragment();
  for (let index = prefix; index < html.length - suffix; index++) {
    const value = html[index];
    const container = document.createElement('div'); container.innerHTML = value;
    decorateMarkdown(container, openLink, openFile);
    const nodes = [...container.childNodes]; fragment.append(...nodes); next.push({html: value, nodes, code: codes[index]});
  }
  target.insertBefore(fragment, anchor);
  rendered.set(target, next.concat(suffix ? previous.slice(-suffix) : []));
}

function decorateMarkdown(target: HTMLElement, openLink: (url: string) => void, openFile: (path: string) => void): void {
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
