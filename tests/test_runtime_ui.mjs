import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const actualFunction = app.match(/^async function managedAction\(action,args=\{\}\) \{.*\}$/m)?.[0];
assert.ok(actualFunction, 'test must exercise the production managedAction function');

function fixture(result) {
  const observed = {requests: [], refreshes: 0};
  const context = vm.createContext({
    formProject: 'project-A', projectId: 'project-A', formRequestId: 'stable-request',
    session: {csrf: 'isolated-test'}, crypto: {randomUUID: () => 'unused'},
    request: async (url, options) => {
      observed.requests.push({url, body: JSON.parse(options.body)});
      return {result};
    },
    loadManagedRuntime: async () => {observed.refreshes++;},
  });
  vm.runInContext(actualFunction, context);
  return {call: context.managedAction, observed};
}

test('definite rejection is shown as an error after runtime refresh', async () => {
  const {call, observed} = fixture({rejected: true, error: 'no active turn to interrupt'});
  await assert.rejects(call('interrupt', {thread_id: 'thread-A', turn_id: 'turn-A'}),
                       /no active turn to interrupt/);
  assert.equal(observed.refreshes, 1);
  assert.equal(observed.requests.length, 1);
  assert.equal(observed.requests[0].body.request_id, 'stable-request');
  assert.equal(observed.requests[0].body.project_id, 'project-A');
});

test('uncertain dispatch remains uncertain without automatic retry', async () => {
  const result = {uncertain: true, error: 'connection closed'};
  const {call, observed} = fixture(result);
  assert.equal(await call('interrupt', {thread_id: 'thread-A', turn_id: 'turn-A'}), result);
  assert.equal(observed.requests.length, 1);
  assert.equal(observed.refreshes, 1);
});

test('accepted turn preserves the returned identity', async () => {
  const result = {thread_id: 'thread-A', turn_id: 'turn-A', status: 'inProgress'};
  const {call, observed} = fixture(result);
  assert.equal(await call('continue', {text: 'isolated test'}), result);
  assert.equal(observed.requests.length, 1);
});

const actualForm = app.match(/^function managedForm\(action,element\) \{.*\}$/m)?.[0];
assert.ok(actualForm);
test('approval form offers only actual single decisions and never sends on opening', () => {
  const captured = [];
  let sent = 0;
  const context = vm.createContext({
    managedRuntime: {threads: [{thread_id:'T',task_id:'K',turn_id:'U'}],
      pending_approvals:[{request_id:'7',thread_id:'T',turn_id:'U',method:'approval',
        params:{command:'/usr/bin/true',availableDecisions:['accept',{acceptWithExecpolicyAmendment:{}},'cancel']}}]},
    field: (...args) => {captured.push(args);return '';},
    openForm: (title, html, callback) => ({title, html, callback}),
    esc: x => String(x),
    managedAction: () => {sent++;},
  });
  vm.runInContext(actualForm,context);
  context.managedForm('approval',{dataset:{threadId:'T',approvalId:'7'}});
  const f = captured.find(x=>x[0]==='decision');
  assert.equal(f[3],'cancel');
  assert.deepEqual(JSON.parse(JSON.stringify(f[4].map(x=>x[0]))),['accept','cancel']);
  assert.equal(sent,0);
});

const actualEdgePaths=app.match(/^function codeEdgePaths\(edges,pos\)\{.*\}$/m)?.[0];
const actualBounds = app.match(/^function boundedCodeNeighborhood\(d,id\) \{.*\}$/m)?.[0];
const actualNeighborhood = app.match(/^async function showCodeNeighborhood\(id\)\{.*\}$/m)?.[0];
assert.ok(actualBounds && actualNeighborhood);
test('code canvas explicitly distinguishes hidden nodes and edges from query truncation', async () => {
  const data = {project_id:'project-A',version:'fixture-index',total_edges:25,omitted_edges:0,nodes: [{id:'center',name:'center',path:'file.js',line:1},
    ...Array.from({length:25},(_,i)=>({id:'n'+i,name:'neighbor'+i,path:'file.js',line:i+2}))],
    edges: Array.from({length:25},(_,i)=>({source:'center',target:'n'+i,kind:'calls',method:'fixture'})),
    scope:'static fixture',truncated:false};
  const box = {innerHTML:''};
  const context = vm.createContext({epoch:1,projectId:'project-A',URLSearchParams,esc:x=>String(x),
    scoped:x=>x,$:()=>box,request:async()=>data});
  vm.runInContext(actualEdgePaths+'\n'+actualBounds+'\n'+actualNeighborhood,context);
  await context.showCodeNeighborhood('center');
  assert.match(box.innerHTML,/画布显示 17 个节点、16 条关系/);
  assert.match(box.innerHTML,/另 9 个节点／9 条关系未显示（画布截断）/);
  assert.doesNotMatch(box.innerHTML,/查询已截断/);
  assert.match(box.innerHTML,/marker-end="url\(#code-arrow\)"/);
  assert.equal((box.innerHTML.match(/data-code-node=/g)||[]).length,17);
});

const actualSourceContext = app.match(/^function renderSourceContext\(d\)\{.*\}$/m)?.[0];
const actualEscape = app.match(/^const esc = .*$/m)?.[0];
assert.ok(actualSourceContext && actualEscape);
test('source context shows exact budget, omission and escaped verified text', () => {
  const box={innerHTML:''};const context=vm.createContext({$:()=>box});
  vm.runInContext(actualEscape+'\n'+actualSourceContext,context);
  context.renderSourceContext({scope:'verified original',used_bytes:700,budget_bytes:8192,
    version:'snapshot-sha',observed_at:'2026-09-30T08:00:00Z',truncated:true,
    items:[{name:'target',path:'a.py',source_start_line:2,source_end_line:3,
      sha256:'original-sha',source_truncated:true,text:'<script>not executed</script>'}],
    skipped:[{path:'changed.py',reason:'source_sha_mismatch'}]});
  assert.match(box.innerHTML,/700\/8192 UTF-8 字节（不是模型 token）/);
  assert.match(box.innerHTML,/2-3/);
  assert.match(box.innerHTML,/正文截断/);
  assert.match(box.innerHTML,/source_sha_mismatch/);
  assert.match(box.innerHTML,/&lt;script&gt;not executed&lt;\/script&gt;/);
  assert.doesNotMatch(box.innerHTML,/<script>/);
  assert.match(box.innerHTML,/snapshot-sha/);
});

const actualSourcePreview = app.match(/^function renderSourcePreview\(d\)\{.*\}$/m)?.[0];
assert.ok(actualSourcePreview);
test('source preview clearly warns about changed bytes using text-only fields', () => {
  const boxes=new Map();const context=vm.createContext({$:id=>{
    if(!boxes.has(id))boxes.set(id,{textContent:'',innerHTML:'untouched'});
    return boxes.get(id);
  }});
  vm.runInContext(actualSourcePreview,context);
  context.renderSourcePreview({path:'/source',locator:'line 1',freshness:'changed',
    hash_scope:'whole_file',registered_sha256:'old-sha',current_sha256:'new-sha',
    observed_at:'now',text:'<script>must stay literal</script>'});
  const status=boxes.get('source-freshness').textContent;
  assert.match(status,/已变化/);assert.match(status,/old-sha/);assert.match(status,/new-sha/);
  assert.match(status,/整份原文件/);assert.match(status,/不自动更新/);
  assert.equal(boxes.get('source-text').textContent,'<script>must stay literal</script>');
  assert.equal(boxes.get('source-text').innerHTML,'untouched');
});
test('source preview keeps missing registration unknown and identifies chat-item hash', () => {
  const boxes={};const context=vm.createContext({$:id=>boxes[id]??=( {textContent:''})});
  vm.runInContext(actualSourcePreview,context);
  context.renderSourcePreview({path:'/source',freshness:'unregistered',text:'text'});
  assert.match(boxes['source-freshness'].textContent,/无法判断版本一致性/);
  context.renderSourcePreview({path:'codex://threads/T',freshness:'match',hash_scope:'chat_item',text:'text'});
  assert.match(boxes['source-freshness'].textContent,/准确聊天片段/);
});

function productionFunction(name, next) {
  const start=app.indexOf('function '+name+'(');
  const end=app.indexOf('function '+next+'(',start+1);
  assert.ok(start>=0&&end>start);
  return app.slice(start,end).replace(/async $/,'');
}
function agentFixture(overrides={}) {
  const box={innerHTML:''};
  const context=vm.createContext({projectId:'A',state:{nodes:[],truncated:false},
    managedRuntime:null,managedRuntimeRead:{state:'unread',at:null,error:''},
    frontier:new Map(),statuses:{completed:'已完成',pending_validation:'待验证'},buckets:{},observedThreads:new Map(),observedThreadsOffset:0,observedThreadsCoverage:{total:0,start:0,shown:0,truncated:false},$:()=>box,...overrides});
  const source=app.match(/^function runtimeStatusLabel\(.*$/m)[0]+'\n'+actualEscape+'\n'+
    app.match(/^function observedThreadStatus\(.*$/m)[0]+'\n'+
    app.match(/^function observationTime\(.*$/m)[0]+'\n'+
    app.match(/^function observationTimeLabel\(.*$/m)[0]+'\n'+
    app.match(/^function linkedObservationText\(.*$/m)[0]+'\n'+
    productionFunction('agentWorkReferences','renderAgentReferences')+
    productionFunction('renderAgentReferences','agentObservationRows')+
    productionFunction('agentObservationRows','renderAgentStatus')+
    productionFunction('renderAgentStatus','loadManagedRuntime');
  vm.runInContext(source,context);
  return {context,box};
}
const historyThread={id:'N',kind:'Thread',title:'历史聊天',status:'active',
  updated_at:'2026-09-30T10:00:00Z',data:{thread_id:'T',status:'active'}};
test('record update never becomes observation time or proof of current activity',()=>{
  const {context,box}=agentFixture({state:{nodes:[historyThread]}});
  const row=context.agentObservationRows()[0];
  assert.equal(row.observedAt,null);assert.match(row.status,/当前状态未知/);
  assert.equal(row.recordedAt,historyThread.updated_at.replace('Z','.000Z'));
  context.renderAgentStatus();assert.match(box.innerHTML,/观察于 未知/);
  assert.match(box.innerHTML,/记录更新于/);
});
test('fresh readonly metadata is used without claiming desktop shared control',()=>{
  const {context,box}=agentFixture({state:{nodes:[historyThread]},observedThreads:new Map([
    ['T',{title:'最新标题',status:'notLoaded',observedAt:'2026-09-30T11:00:00Z'}]])});
  const row=context.agentObservationRows()[0];
  assert.equal(row.source,'Codex 独立只读连接');assert.equal(row.name,'最新标题');
  assert.equal(row.observedAt,'2026-09-30T11:00:00.000Z');
  assert.match(row.status,/无法确认桌面聊天状态/);
  context.renderAgentStatus();assert.match(box.innerHTML,/该时刻快照/);
});
test('failed readonly refresh keeps old time and labels cached activity unknown',()=>{
  const {context,box}=agentFixture({state:{nodes:[historyThread]},observedThreads:new Map([
    ['T',{status:'active',observedAt:'2026-09-30T09:00:00Z',
      checkedAt:'2026-09-30T12:00:00Z',readError:'<failure>'}]])});
  const row=context.agentObservationRows()[0];
  assert.match(row.status,/本次读取失败.*保留旧快照，当前状态未知/);
  assert.equal(row.observedAt,'2026-09-30T09:00:00.000Z');
  context.renderAgentStatus();assert.match(box.innerHTML,/&lt;failure&gt;/);
  assert.doesNotMatch(box.innerHTML,/<failure>/);
});
test('managed journal remains scoped, deduplicated and unknown when disconnected or unreadable',()=>{
  const runtime={threads:[{project_id:'A',thread_id:'T',turn_id:'U',status:'unknown',
    runtime_status:'disconnected',at:'2026-09-30T11:00:00Z'},
    {project_id:'B',thread_id:'foreign',status:'inProgress'}]};
  const {context,box}=agentFixture({state:{nodes:[historyThread]},managedRuntime:runtime,
    managedRuntimeRead:{state:'ok',at:'2026-09-30T12:00:00Z'}});
  let rows=context.agentObservationRows();assert.equal(rows.length,1);
  assert.equal(rows[0].target,'T / U');assert.match(rows[0].status,/连接断开，轮次待核对/);
  context.managedRuntimeRead={state:'error',at:'2026-09-30T13:00:00Z',error:'offline'};
  rows=context.agentObservationRows();assert.match(rows[0].status,/当前状态未知/);
  assert.equal(rows[0].observedAt,'2026-09-30T11:00:00.000Z');
  context.renderAgentStatus();assert.match(box.innerHTML,/受管日志读取失败/);
  assert.doesNotMatch(box.innerHTML,/foreign/);
});
test('invalid times stay unknown and both query and display truncation are explicit',()=>{
  const {context,box}=agentFixture({state:{truncated:true,nodes:Array.from({length:51},(_,i)=>({
    id:'agent'+i,kind:'Agent',title:i===0?'<script>bad</script>':'agent'+i,status:'active',
    updated_at:'invalid',data:{observed_at:'invalid'}}))},managedRuntime:{threads:[],truncated:true}});
  context.renderAgentStatus();assert.match(box.innerHTML,/当前载入 51 条，显示 50 条，已截断至 50 条/);
  assert.match(box.innerHTML,/项目图载入已截断/);assert.match(box.innerHTML,/受管日志查询已截断/);
  assert.equal((box.innerHTML.match(/<tr><td>/g)||[]).length,50);
  assert.match(box.innerHTML,/&lt;script&gt;bad&lt;\/script&gt;/);assert.doesNotMatch(box.innerHTML,/<script>/);
});
test('actual managed refresh failure changes the panel rather than leaving a fresh-looking cache',async()=>{
  const {context,box}=agentFixture({epoch:1,scoped:x=>x,
    managedRuntime:{threads:[{project_id:'A',thread_id:'T',status:'inProgress',at:'2026-09-30T11:00:00Z'}]},
    request:async()=>{throw new Error('disconnected');}});
  vm.runInContext('async '+productionFunction('loadManagedRuntime','managedAction'),context);
  await context.loadManagedRuntime();assert.equal(context.managedRuntimeRead.state,'error');
  assert.match(context.agentObservationRows()[0].status,/旧日志快照，当前状态未知/);
  assert.match(box.innerHTML,/受管日志读取失败/);
});
test('late managed response cannot overwrite a newly selected project observation',async()=>{
  let resolve;const {context}=agentFixture({epoch:1,scoped:x=>x,
    request:()=>new Promise(r=>{resolve=r;})});
  vm.runInContext('async '+productionFunction('loadManagedRuntime','managedAction'),context);
  const pending=context.loadManagedRuntime();context.epoch=2;context.projectId='B';
  context.managedRuntimeRead={state:'unread',at:null,error:''};
  resolve({threads:[{project_id:'A',thread_id:'old'}]});await pending;
  assert.equal(context.managedRuntime,null);assert.equal(context.managedRuntimeRead.state,'unread');
});
test('actual readonly refresh preserves failed snapshots and never advances their observation time',async()=>{
  const original={title:'old',status:'active',observedAt:10};
  const {context}=agentFixture({epoch:1,state:{nodes:[historyThread]},view:'overview',
    observingThreads:false,observedThreadsAt:0,observedThreads:new Map([['T',original]]),
    scoped:x=>x,URLSearchParams,nodes:new Map(),selected:null,
    request:async()=>{throw new Error('preview unavailable');}});
  vm.runInContext('async '+productionFunction('observeLinkedThreads','loadDiscoveredThreads'),context);
  await context.observeLinkedThreads();const snapshot=context.observedThreads.get('T');
  assert.equal(snapshot.observedAt,10);assert.equal(snapshot.status,'active');
  assert.equal(snapshot.readError,'preview unavailable');assert.ok(snapshot.checkedAt>10);
  assert.equal(context.observingThreads,false);
});

const actualInventory = app.match(/^async function showCodeInventory\(\)\{.*\}$/m)?.[0];
assert.ok(actualInventory, 'test exercises actual coverage-to-file-graph entry');
function inventoryFixture(request) {
  const box={innerHTML:'previous',textContent:''};
  const seen=[];
  const context=vm.createContext({epoch:1, URLSearchParams, $:()=>box,
    scoped:url=>url+'&project=A',request:async url=>{seen.push(url);return request();}});
  vm.runInContext(actualEscape+'\n'+actualInventory,context);
  return {context,box,seen};
}
const coverageData={scope:'indexed snapshot',version:'cache-sha',truncated:true,
 items:[{path:'/src/service.py',language:'python',status:'parsed',errors:[],sha256:'1234567890123456'},
 {path:'/src/<bad>".ps1',language:'powershell',status:'parse_error',errors:[{kind:'<error>'}],sha256:'abcdefabcdef00'}]};
test('coverage exposes native file buttons, escaped exact paths and unresolved scope',async()=>{
 const {context,box,seen}=inventoryFixture(async()=>coverageData);
 await context.showCodeInventory();
 assert.match(box.innerHTML,/button type="button"[^>]*data-code-node="\/src\/service.py"/);
 assert.match(box.innerHTML,/data-code-node="\/src\/&lt;bad&gt;&quot;.ps1"/);
 assert.doesNotMatch(box.innerHTML,/<bad>|<error>/);
 assert.match(box.innerHTML,/parse_error/);assert.match(box.innerHTML,/cache-sha/);
 assert.match(box.innerHTML,/更多文件须用 CLI 分页/);
 assert.match(box.innerHTML,/不代表没有依赖/);
 assert.equal(seen.length,1);
 const q=new URL(seen[0],'http://fixture').searchParams;
 assert.equal(q.get('project'),'A');assert.equal(q.get('limit'),'100');assert.equal(q.get('op'),'inventory');
});
test('coverage ignores old response after switching project',async()=>{
 let deliver;const waiting=new Promise(resolve=>deliver=resolve);
 const {context,box}=inventoryFixture(()=>waiting);
 const pending=context.showCodeInventory();context.epoch=2;deliver(coverageData);await pending;
 assert.equal(box.innerHTML,'previous');
});
test('coverage ignores old error and displays current failure as text without retry',async()=>{
 let reject;const waiting=new Promise((_,r)=>reject=r);
 const old=inventoryFixture(()=>waiting);const pending=old.context.showCodeInventory();
 old.context.epoch=2;reject(new Error('old failure'));await pending;assert.equal(old.box.textContent,'');
 const current=inventoryFixture(async()=>{throw new Error('<failure>');});await current.context.showCodeInventory();
 assert.equal(current.box.textContent,'<failure>');assert.equal(current.seen.length,1);
});

const actualImpact = app.match(/^async function showCodeImpact\(id\)\{.*\}$/m)?.[0];
function impactFixture(reply) {
  assert.ok(actualImpact,'readable impact must use the production function');
  const box={html:'',insertAdjacentHTML:(_position,html)=>{box.html+=html;}};
  const notifications=[];let resolve,reject;
  const context=vm.createContext({epoch:1,projectId:'project-A',URLSearchParams,
    scoped:u=>u,$:()=>box,notify:m=>notifications.push(m),
    request:()=>new Promise((yes,no)=>{resolve=yes;reject=no;})});
  vm.runInContext(actualEscape+'\n'+actualImpact,context);
  const pending=context.showCodeImpact('target');
  return {context,box,notifications,pending,resolve:()=>resolve(reply),reject};
}
const impactReply={project_id:'project-A',version:'index-version',scope:'static only',items:['caller'],
  nodes:[{id:'caller',name:'readable <caller>',path:'/root/<file>.js',line:17,kind:'function',
    via:{kind:'calls',method:'named_relative_export_candidate',path:'/root/<file>.js',line:19}}],
  truncated:true,bounds_hit:['depth_limit']};
test('impact displays readable escaped locations, actual relation method and scope',async()=>{
  const f=impactFixture(impactReply);f.resolve();await f.pending;
  assert.match(f.box.html,/readable &lt;caller&gt;/);assert.match(f.box.html,/&lt;file&gt;\.js:17/);
  assert.match(f.box.html,/named_relative_export_candidate/);assert.match(f.box.html,/\.js:19/);
  assert.match(f.box.html,/depth_limit/);assert.match(f.box.html,/index-version/);
  assert.match(f.box.html,/data-code-node="caller"/);assert.equal(f.notifications.length,0);
});
test('impact drops late response after project switch',async()=>{
  const f=impactFixture(impactReply);f.context.projectId='project-B';f.context.epoch=2;
  f.resolve();await f.pending;assert.equal(f.box.html,'');assert.equal(f.notifications.length,0);
});
test('impact refuses mismatching project response',async()=>{
  const f=impactFixture({...impactReply,project_id:'project-B'});f.resolve();await f.pending;
  assert.equal(f.box.html,'');assert.match(f.notifications[0],/项目身份不一致/);
});
test('impact ignores stale errors and a replaced detail panel',async()=>{
  const f=impactFixture(impactReply);f.context.epoch=2;f.reject(new Error('old project error'));
  await f.pending;assert.equal(f.notifications.length,0);
  const g=impactFixture(impactReply);g.context.$=()=>({});g.resolve();await g.pending;
  assert.equal(g.box.html,'');
});

const actualInputRender = app.match(/^function renderPendingInputs\(d\)\{.*\}$/m)?.[0];
const actualInputForm = app.match(/^function managedInputForm\(t,element\)\{.*\}$/m)?.[0];
assert.ok(actualInputRender && actualInputForm);
const inputQuestion = {request_id:'Q',thread_id:'T',turn_id:'U',params:{isBlocking:true,
  questions:[{id:'display',header:'展示',question:'<script>Choose A or B</script>',options:[
    {label:'A',description:'show A'},{label:'B',description:'show B'}]}]}};
test('pending input is escaped, accurately identified and secret answers require host',()=>{
 const context=vm.createContext({});vm.runInContext(actualEscape+'\n'+actualInputRender,context);
 const html=context.renderPendingInputs({pending_inputs:[inputQuestion]});
 assert.match(html,/等待用户输入/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
 assert.match(html,/data-input-id="Q"/);assert.match(html,/T · U · 请求 Q/);
 const hidden={...inputQuestion,params:{...inputQuestion.params,questions:[{...inputQuestion.params.questions[0],isSecret:true}]}};
 const secret=context.renderPendingInputs({pending_inputs:[hidden]});assert.match(secret,/宿主处理/);assert.doesNotMatch(secret,/data-managed="input"/);
});
test('input form never defaults an answer and dispatches only exact user questions',async()=>{
 const fields=[],sent=[];let captured;
 const context=vm.createContext({managedRuntime:{pending_inputs:[inputQuestion]},esc:x=>String(x),
  field:(...args)=>{fields.push(args);return '';},openForm:(title,html,cb)=>{captured={title,html,cb};},
  managedAction:async(action,args)=>{sent.push({action,args});return {answered:true};}});
 vm.runInContext(actualInputForm,context);const t={thread_id:'T',turn_id:'U'};
 assert.throws(()=>context.managedInputForm(t,{dataset:{inputId:'other'}}),/问题已变化/);
 context.managedInputForm(t,{dataset:{inputId:'Q'}});assert.equal(sent.length,0);assert.equal(fields[0][3],'');
 await assert.rejects(captured.cb({answer_0:''}),/明确回答/);assert.equal(sent.length,0);
 await captured.cb({answer_0:'B'});assert.equal(sent.length,1);
 assert.deepEqual(JSON.parse(JSON.stringify(sent[0])),{action:'input',args:{thread_id:'T',turn_id:'U',input_request_id:'Q',answers:{display:{answers:['B']}}}});
});

const actualOutputRender = app.match(/^function renderManagedOutput\(t\)\{.*\}$/m)?.[0];
assert.ok(actualOutputRender, 'live assistant output must use actual production render');
test('live output escapes content and distinguishes item end from task end', () => {
 const context=vm.createContext({esc:x=>String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')});
 vm.runInContext(actualOutputRender,context);
 const html=context.renderManagedOutput({status:'inProgress',output:{text:'<script>bad</script>',turn_id:'turn-A',item_id:'item-A',observed_at:'time-A',limit_chars:1024,truncated:true,complete:true}});
 assert.ok(html.includes('&lt;script&gt;bad&lt;/script&gt;'));assert.ok(!html.includes('<script>'));assert.ok(html.includes('turn-A')&&html.includes('item-A'));assert.ok(html.includes('尾部摘录，已截断')&&html.includes('任务状态分别核对'));assert.ok(html.includes('<details open>'));
 assert.equal(context.renderManagedOutput({status:'completed'}),'');
});

const actualContextRender=app.match(/^function renderExecutionContext\(c\)\{.*\}$/m)?.[0];
test('task context preview renders actual version gates and escaped source facts',()=>{assert.ok(actualContextRender);const context=vm.createContext({esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')});vm.runInContext(actualContextRender,context);const html=context.renderExecutionContext({project_id:'A',root:'/tmp/isolated',task:{id:'T',version:7,spec:'<script>literal</script>'},bytes:123,limit_bytes:16384,boundary:'approval is explicit',dependencies:[{id:'DEP',status:'completed'}],human_gates:[{id:'STOP',status:'pending'}],source_refs:[{source:{uri:'/tmp/original.md'}}],omitted_optional_refs:2,fingerprint:'current-hash'});for(const text of ['7','DEP','STOP','pending','/tmp/original.md','current-hash','省略 2','&lt;script&gt;'])assert.ok(html.includes(text));assert.ok(!html.includes('<script>'));});
test('execution preview is read only and guards late project responses',async()=>{const actual=app.match(/^async function managedExecutionForm\(action,t,base\)\{.*\}$/m)?.[0];assert.ok(actual);let calls=[],resolve;const context=vm.createContext({epoch:1,projectId:'A',scoped:x=>x+'?project=A',encodeURIComponent,request:async url=>{calls.push(url);return await new Promise(r=>resolve=r);},openForm:()=>{throw Error('foreign response must not open form');}});vm.runInContext(actual,context);const promise=context.managedExecutionForm('continue',{task_id:'T'},{});context.epoch=2;context.projectId='B';resolve({eligible:true});await assert.rejects(promise,/项目已切换/);assert.equal(calls.length,1);assert.ok(calls[0].includes('/api/runtime/context'));});

const summaryFunction=app.match(/^function codeStatusSummary\(status\)\{.*\}$/m)?.[0];
test('actual code status summary distinguishes old-cache pending and frozen applied with project/version/time',()=>{assert(summaryFunction);const context=vm.createContext({projectId:'P'});vm.runInContext(summaryFunction,context);const value={project_id:'P',available:true,source_root:'/source',version:'sha-current',built_at:'built',parser:'pinned',relation_count:4,files:[],freshness:'source snapshot',refresh:{state:'RESULT_UNKNOWN',request_id:'R',observed_at:'observed'}};assert.match(context.codeStatusSummary(value),/保留旧索引/);assert.match(context.codeStatusSummary(value),/sha-current/);assert.match(context.codeStatusSummary(value),/编号 R/);assert.match(context.codeStatusSummary(value),/观察 observed/);value.refresh.state='APPLIED';assert.match(context.codeStatusSummary(value),/已采用冻结快照/);delete value.refresh;assert.match(context.codeStatusSummary(value),/当前源版本须核对/);assert.throws(()=>context.codeStatusSummary({...value,project_id:'foreign'}),/身份不一致/);});
test('production code loader displays summary as text and retains stale project guard',()=>{const f=app.match(/^async function loadCodeIndex\(more=false\)\{.*\}$/m)?.[0];assert(f);assert(f.includes("if(ticket!==epoch||!$('code-status'))return"));assert(f.includes("$('code-status').textContent=codeStatusSummary(status)"));assert(!f.includes("$('code-status').innerHTML"));});

const actualPathNeighborhood=app.match(/^async function showCodeNeighborhood\(id\)\{.*\}$/m)?.[0];
const actualBoundedNeighborhood=app.match(/^function boundedCodeNeighborhood\(d,id\) \{.*\}$/m)?.[0];
function neighborhoodFixture(reply){
 const box={innerHTML:'prior',textContent:''};let deliver;
 const context=vm.createContext({epoch:1,projectId:'project-A',URLSearchParams,scoped:u=>u,$:()=>box,request:()=>new Promise(r=>deliver=r)});
 vm.runInContext(actualEscape+'\n'+actualEdgePaths+'\n'+actualBoundedNeighborhood+'\n'+actualPathNeighborhood,context);
 const pending=context.showCodeNeighborhood('target');return{box,context,pending,resolve:()=>deliver(reply)};
}
const neighborhoodReply={project_id:'project-A',version:'index-version',scope:'static only',nodes:[{id:'target',name:'target',path:'/root/file',line:1},{id:'caller',name:'caller',path:'/root/file',line:2}],edges:[{source:'caller',target:'target',kind:'calls',method:'static'}],total_edges:9,omitted_edges:8,truncated:true};
test('neighborhood shows exact project, version and query omissions',async()=>{const f=neighborhoodFixture(neighborhoodReply);f.resolve();await f.pending;assert.match(f.box.innerHTML,/project-A/);assert.match(f.box.innerHTML,/index-version/);assert.match(f.box.innerHTML,/查询未返回 8 条/);});
test('neighborhood rejects foreign response and drops old error after switching',async()=>{const bad=neighborhoodFixture({...neighborhoodReply,project_id:'project-B'});bad.resolve();await bad.pending;assert.equal(bad.box.innerHTML,'prior');assert.match(bad.box.textContent,/身份不一致/);const old=neighborhoodFixture({...neighborhoodReply,project_id:'project-B'});old.context.projectId='project-B';old.context.epoch=2;old.resolve();await old.pending;assert.equal(old.box.innerHTML,'prior');assert.equal(old.box.textContent,'');});
test('impact renders only supplied visited paths with escaping',async()=>{const f=impactFixture({...impactReply,nodes:[{...impactReply.nodes[0],witness_path:[{source:'<caller>',target:'target',method:'static <method>',path:'/root/<file>',line:19}],path_reaches_start:true}]});f.resolve();await f.pending;assert.match(f.box.html,/已访问路径：&lt;caller&gt;（定位未知） → target（定位未知）/);assert.match(f.box.html,/static &lt;method&gt;/);assert.doesNotMatch(f.box.html,/<caller>/);});

test('impact path uses readable exact intermediate locations even outside returned candidate list',async()=>{const f=impactFixture({...impactReply,path_nodes:[{id:'caller',name:'caller',path:'/root/file.js',line:17},{id:'middle',name:'middle <name>',path:'/root/other.js',line:21},{id:'target',name:'target',path:'/root/end.js',line:31}],nodes:[{...impactReply.nodes[0],witness_path:[{source:'caller',target:'middle',method:'static',path:'/root/file.js',line:19},{source:'middle',target:'target',method:'static',path:'/root/other.js',line:23}],path_reaches_start:true}]});f.resolve();await f.pending;assert.match(f.box.html,/caller（\/root\/file.js:17） → middle &lt;name&gt;（\/root\/other.js:21）/);assert.match(f.box.html,/target（\/root\/end.js:31）/);});

test('code edge arrows stop at card boundaries and parallel call sites retain distinct curves',()=>{const ctx=vm.createContext({});vm.runInContext(actualEdgePaths,ctx);const pos=new Map([['A',{x:0,y:0}],['B',{x:300,y:0}]]);const e=ctx.codeEdgePaths([{source:'A',target:'B',line:1},{source:'A',target:'B',line:2}],pos);assert.notEqual(e[0].d,e[1].d);assert.match(e[0].d,/^M 180 22.5 Q /);assert.match(e[0].d,/ 300 22.5$/);assert.deepEqual(e.map(x=>x.line),[1,2]);});
test('recursive code relation draws an explicit loop and missing canvas endpoint refuses',()=>{const ctx=vm.createContext({});vm.runInContext(actualEdgePaths,ctx);const pos=new Map([['A',{x:20,y:35}]]);const e=ctx.codeEdgePaths([{source:'A',target:'A'}],pos);assert.match(e[0].d,/^M 200 47 C /);assert.throws(()=>ctx.codeEdgePaths([{source:'A',target:'B'}],pos),/端点未显示/);});


test('linked metadata observes five unique identities per batch and rotates without loading turns',async()=>{
 const calls=[],linked=Array.from({length:7},(_,i)=>({id:'N'+i,kind:'Thread',title:'same name',version:1,data:{thread_id:'T'+i}}));
 const {context,box}=agentFixture({epoch:1,state:{nodes:[...linked,linked[0]]},view:'overview',observingThreads:false,observedThreadsAt:0,scoped:x=>x,URLSearchParams,nodes:new Map(),selected:null,request:async url=>{calls.push(url);const q=new URL(url,'http://local').searchParams;return{thread:{id:'T'+q.get('id').slice(1),title:'same name',status:'notLoaded'},observed_at:'2026-10-07T01:00:00Z'}}});
 vm.runInContext('async '+productionFunction('observeLinkedThreads','loadDiscoveredThreads'),context);
 await context.observeLinkedThreads();assert.equal(calls.length,5);assert(calls.every(x=>x.includes('/api/thread-observation?')&&!x.includes('thread-preview')));assert.equal(context.observedThreads.size,5);assert.equal(context.observedThreadsCoverage.total,7);assert.equal(context.observedThreadsOffset,5);
 context.renderAgentStatus();assert(box.innerHTML.includes('本批 1–5/7'));assert.match(box.innerHTML,/其他项保留旧观察或未知/);
 context.observedThreadsAt=0;await context.observeLinkedThreads();assert.equal(calls.length,7);assert.equal(context.observedThreads.size,7);assert.equal(context.observedThreadsOffset,0);assert.equal(context.observedThreads.get('T0').observedAt,'2026-10-07T01:00:00Z');
});
test('late linked metadata does not enter a changed project even with the same title',async()=>{
 let resolve;const {context}=agentFixture({epoch:1,state:{nodes:[{...historyThread,version:1}]},view:'overview',observingThreads:false,observedThreadsAt:0,scoped:x=>x,URLSearchParams,nodes:new Map(),selected:null,request:()=>new Promise(r=>resolve=r)});
 vm.runInContext('async '+productionFunction('observeLinkedThreads','loadDiscoveredThreads'),context);const pending=context.observeLinkedThreads();context.epoch=2;context.projectId='B';resolve({thread:{id:'T',title:'same name',status:'active'},observed_at:'2026-10-07T01:00:00Z'});await pending;assert.equal(context.observedThreads.size,0);assert.equal(context.observedThreadsOffset,0);assert.equal(context.observedThreadsCoverage.total,0);
});

test('agent references follow exact execution identity and explicit output edges, not titles or related links',()=>{
 const at='2026-10-07T02:00:00Z',task={id:'K',kind:'Task',title:'same',status:'pending_validation',updated_at:at,data:{}},foreign={...task,id:'X'},
  source={id:'F',kind:'Artifact',title:'<artifact>',updated_at:at,data:{source:{uri:'/source/a.md',locator:'line 7'}}},
  unrelated={...source,id:'Z',title:'must not leak'},threads=[historyThread,{...historyThread,id:'N2',data:{thread_id:'other'}}];
 const {context,box}=agentFixture({state:{nodes:[...threads,task,foreign,source,unrelated],edges:[
  {source:'K',target:'N',relation:'executed_by'},{source:'X',target:'N2',relation:'executed_by'},
  {source:'K',target:'F',relation:'produces'},{source:'K',target:'Z',relation:'related_to'}]}});
 const row=context.agentObservationRows().find(r=>r.target==='T');
 assert.deepEqual(JSON.parse(JSON.stringify(row.references.tasks.map(n=>n.id))),['K']);
 assert.deepEqual(JSON.parse(JSON.stringify(row.references.outputs.map(n=>n.id))),['F']);
 assert.match(context.renderAgentReferences(row.references),/data-node="K"/);
 assert.match(context.renderAgentReferences(row.references),/data-node="F"/);
 assert.match(context.renderAgentReferences(row.references),/line 7/);
 assert.doesNotMatch(context.renderAgentReferences(row.references),/must not leak|data-node="X"/);
 context.renderAgentStatus();assert.match(box.innerHTML,/&lt;artifact&gt;/);assert.doesNotMatch(box.innerHTML,/<artifact>/);
});
test('managed output links keep missing tasks unknown, cap lists, and exclude another project',()=>{
 const at='2026-10-07T03:00:00Z',tasks=Array.from({length:4},(_,i)=>({id:'K'+i,kind:'PlanItem',title:'task'+i,status:'completed',updated_at:at,data:{}})),
 outputs=Array.from({length:5},(_,i)=>({id:'F'+i,kind:'File',title:'file'+i,updated_at:at,data:{}}));
 const {context,box}=agentFixture({state:{nodes:[historyThread,...tasks,...outputs],edges:[
  ...tasks.map(n=>({source:n.id,target:'N',relation:'executed_by'})),...outputs.map(n=>({source:'K0',target:n.id,relation:'writes'})),
  {source:'K0',target:'outside-file',relation:'produces'}]},
 managedRuntime:{threads:[{project_id:'A',thread_id:'T',task_id:'missing',at},{project_id:'B',thread_id:'foreign',task_id:'K0',at}]}});
 const row=context.agentObservationRows()[0];assert.equal(row.references.taskTotal,4);assert.equal(row.references.tasks.length,3);
 assert.equal(row.references.outputTotal,5);assert.equal(row.references.outputs.length,3);assert.equal(row.references.unavailable,1);
 assert.equal(row.references.outputsUnavailable,1);context.renderAgentStatus();
 assert.match(box.innerHTML,/另 1 项任务未显示/);assert.match(box.innerHTML,/另 2 项产物未显示/);
 assert.match(box.innerHTML,/任务定位未载入或类型不符/);assert.match(box.innerHTML,/未登记原件定位/);
 assert.doesNotMatch(box.innerHTML,/foreign/);assert.match(box.innerHTML,/关联产物（未自动验收）/);
});


test('code status distinguishes per-language static capabilities and legacy unknown scope',()=>{
 const source=app;
 const start=source.indexOf('function codeStatusSummary('),end=source.indexOf('async function loadCodeIndex(',start);
 const context={projectId:'project'};vm.createContext(context);vm.runInContext(source.slice(start,end),context);
 const status={project_id:'project',available:true,files:[{language:'css',status:'parsed',count:1}],semantic_capabilities:{css:{symbols:'selector rule sets',imports:'quoted relative',calls:'none',unknown:'cascade'}},semantic_scope:'parsed不等于关系完整'};
 const text=context.codeStatusSummary(status);assert.match(text,/css parsed 1/);assert.match(text,/selector rule sets/);assert.match(text,/未知 cascade/);assert.match(text,/parsed不等于关系完整/);
 assert.match(context.codeStatusSummary({...status,semantic_capabilities:{}}),/旧缓存未声明，保持未知/);
 assert.throws(()=>context.codeStatusSummary({...status,project_id:'foreign'}),/身份不一致/);
});
