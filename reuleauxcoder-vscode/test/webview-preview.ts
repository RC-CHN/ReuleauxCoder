import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {webviewHtml} from '../src/webview/html.js';
import type {HostSnapshot} from '../src/shared.js';
import {catalog, overview, panels} from './webview-fixture.js';

/** Standalone, offline interaction preview using the production webview bundle. */
export async function writePreview(language: string): Promise<string> {
  const zh = language === 'zh'; const data = structuredClone(overview);
  if (zh) {
    data.goal!.objective = '让工作区里的协作更自然';
    data.plan = [{step: '梳理输入与命令流程', status: 'completed'}, {step: '完善会话与审批界面', status: 'in_progress'}, {step: '验证 Windows 与 Remote', status: 'pending'}];
    data.progress = '输入与菜单已接通，正在审阅修改。'; data.jobs[0].task = '检查交互生命周期'; data.jobs[0].detail = '验证取消与过期请求';
    data.processes[0].output = '✓ steering 保留下一条草稿\n✓ Markdown 安全渲染';
  }
  const state: HostSnapshot = {hostId: 'preview', revision: 1, draftRevision: 0, phase: 'ready', generation: 0, environment: zh ? 'SSH · 交互预览' : 'SSH · Interactive preview', workspace: '/workspace/ReuleauxCoder', model: 'Reasoning model', mode: 'code', running: true, catalog, overview: data, draftText: '', draftItems: [], cells: [
    {id: 'user', role: 'user', text: zh ? '把命令放在输入框附近，审批在编辑器中完成。也记得保留任务进展。' : 'Keep commands beside the composer and reviews in the editor. Preserve task progress at a glance.'},
    {id: 'tool', role: 'tool', title: 'read_file', text: 'src/webview/main.ts\nsrc/webview/style.css', status: 'succeeded'},
    {id: 'assistant', role: 'assistant', text: zh ? '输入流程已经接通。这次修改包括：\n\n- **功能菜单**：点击即可选择，支持搜索与快捷键。\n- **就地审批**：打开文件审阅 diff，再选择是否批准。\n- **工作概况**：计划、子任务和进程进展随时可见。\n\n```ts\nawait commands.open("goal.show");\n```\n\n下面是待审阅的提案。' : 'The input flow is connected. This change includes:\n\n- **Command menu**: clickable choices, search and shortcuts.\n- **Contextual reviews**: open the file diff, then decide.\n- **Work overview**: plans, agents and process progress stay visible.\n\n```ts\nawait commands.open("goal.show");\n```\n\nThe proposal is ready below.'},
  ], reviews: [{id: 'review', title: 'edit_file', summary: zh ? '重组输入区，保持消息即时回显，并接入会话内命令面板。' : 'Reshape the composer, preserve immediate message echo, and connect conversation command panels.', documents: [{id: 'main', path: 'src/webview/main.ts'}, {id: 'style', path: 'src/webview/style.css'}], grants: [{id: 'files', label: zh ? '当前会话 · 所列文件' : 'This session · Listed files', description: zh ? '在当前会话中允许修改这些文件' : 'Allow changes to these files for this session', broad: false}]}]};
  const encoded = JSON.stringify({state, panels}).replaceAll('<', '\\u003c');
  const bootstrap = `const demo = ${encoded}; let serial = 0; let saved; let stack=[]; const publish = () => {demo.state.revision++; window.postMessage({kind:'snapshot', snapshot:structuredClone(demo.state)}, '*');};
window.acquireVsCodeApi = () => ({getState:()=>saved, setState:value=>{saved=value;}, postMessage: message => setTimeout(() => {
  const data=message.data||{}; let result=null; const state=demo.state;
  if(message.action==='ready') result=structuredClone(state);
  else if(message.action==='draft') state.draftText=data.text;
  else if(message.action==='send') {state.cells.push({id:data.id,role:'user',text:data.text,status:'applied'}); publish();}
  else if(message.action==='command.open'||message.action==='models'||message.action==='sessions') {
    const id=data.actionId || (message.action==='models'?'model.show':'goal.show');
    const action=state.catalog.find(item=>item.action_id===id);
    if(action) {stack=[];state.commandSurface={id:++serial,feature:action.feature_id,busy:false,canBack:false,...(action.parameters.length?{action}:{panel:structuredClone(demo.panels[id]||demo.panels['goal.show'])})}; publish();}
  } else if(message.action==='command.select') {
    const surface=state.commandSurface; const item=surface.panel.items[data.index]; const child=surface.panel.children.find(entry=>entry[0]===item.label)?.[1];
    if(child) {stack.push(surface);state.commandSurface={...surface,id:++serial,canBack:true,panel:child};}
    else if(item.action?.action_id.startsWith('skills.')||item.action?.action_id.startsWith('mcp.')) {item.current=!item.current;surface.id=++serial;}
    else if(item.action?.action_id==='goal.create') state.commandSurface={id:++serial,feature:'goal',busy:false,canBack:false,action:state.catalog.find(item=>item.action_id==='goal.create')};
    else state.commandSurface=undefined; publish();
  } else if(message.action==='command.policy') {
    let panel=state.commandSurface.panel;
    for(const index of data.path.slice(0,-1)) panel=panel.children.find(entry=>entry[0]===panel.items[index].label)[1];
    panel.items.forEach((item,index)=>item.current=index===data.path.at(-1)&&!!item.action?.command.action);
    const root=state.commandSurface.panel, scope=root.children.find(entry=>entry[0]===root.items[data.path[0]].label)[1];
    const selected=panel.items.find(item=>item.current)?.action.command.action;
    scope.items[data.path[1]].description=selected?'currently '+selected:'no override';
    root.items[data.path[0]].description=scope.children.map((entry,index)=>(index?'workspace: ':'session: ')+(entry[1].items.find(item=>item.current)?.action.command.action||'no override')).join(' · ');
    state.commandSurface.id=++serial;demo.panels['approval.show']=structuredClone(state.commandSurface.panel);publish();
  } else if(message.action==='command.back') {state.commandSurface={...stack.pop(),id:++serial,canBack:stack.length>0};publish();}
  else if(message.action==='command.close'||message.action==='command.submit') {state.commandSurface=undefined;publish();}
  else if(message.action==='approve'||message.action==='reject') {state.reviews=state.reviews.filter(item=>item.id!==data.id);publish();}
  else if(message.action==='review'||message.action==='git') {state.notice=${JSON.stringify(zh ? '这是交互预览；安装到 VS Code 后会在原生编辑区打开。' : 'Interactive preview: the installed extension opens this in the native VS Code editor.')};publish();}
  else if(message.action==='stop') {state.running=false;publish();}
  window.postMessage({kind:'response',id:message.id,result},'*');
},60)});`;
  const css = await readFile(resolve('dist/webview.css'), 'utf8'); const script = await readFile(resolve('dist/webview.js'), 'utf8');
  const html = webviewHtml({language: zh ? 'zh-CN' : 'en', nonce: 'preview', script: 'webview.js', style: 'webview.css', cspSource: "'self'"})
    .replace("style-src 'self';", "style-src 'nonce-preview';")
    .replace('<link rel="stylesheet" href="webview.css">', () => `<style nonce="preview">${css}</style>`)
    .replace('<script nonce="preview" src="webview.js"></script>', () => `<script nonce="preview">${bootstrap.replaceAll('</script', '<\\/script')}</script><script nonce="preview">${script.replaceAll('</script', '<\\/script')}</script>`);
  const path = resolve(`../artifacts/vscode-concept/preview-${language}.html`); await mkdir(resolve('../artifacts/vscode-concept'), {recursive: true}); await writeFile(path, html); return path;
}
