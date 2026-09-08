/* Render remote result text as text nodes. No result field can inject HTML. */
const labels = {success:'通过',passed:'通过',failure:'失败',failed:'失败',error:'执行错误',cancelled:'已取消',skipped:'未执行',not_applicable:'不适用',healthy:'正常',degraded:'异常',offline:'心跳过期',unknown:'状态未知',waiting:'等待',ready:'就绪'};
const names = {environment:'环境与依赖',frontend_build:'Frontend build',wheel_install:'Wheel 安装 / import',frontend_smoke:'Frontend smoke',backend_rebuild:'Backend rebuild',backend_smoke:'Backend smoke / JIT',flaggems:'FlagGems',compile_time:'Compile-time performance',pass_profile:'Pass profiling',ir_serialization:'IR serialization',architecture_review:'架构契约审查',control_plane:'CI 控制面检查',custom_test:'定向测试'};
const model = {data:null, selected:null};
const $ = id => document.getElementById(id);
const arr = value => Array.isArray(value) ? value : [];
const txt = value => typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value);
function el(tag, className, text) { const node=document.createElement(tag); if(className)node.className=className; if(text!=null)node.textContent=txt(text); return node; }
function badge(status) { const tone=['success','passed','healthy','ready'].includes(status)?'good':['failure','failed','error','offline'].includes(status)?'bad':['degraded','waiting'].includes(status)?'warn':status==='cancelled'?'info':''; return el('span','ci-badge '+tone,labels[status]||status||'未知'); }
function date(value) { const d=new Date(typeof value==='number'?value*1000:value); return value!=null&&!Number.isNaN(d.valueOf())?d.toLocaleString('zh-CN',{hour12:false}):'尚无时间记录'; }
function link(label, url) { try { const target=new URL(url); if(target.protocol!=='https:')return null; const a=el('a','',label); a.href=target.href; a.target='_blank'; a.rel='noopener noreferrer'; return a; } catch { return null; } }
function empty(container, message) { container.append(el('div','ci-empty',message)); }
function section(parent,title) { const s=el('section'); s.append(el('h3','',title)); parent.append(s); return s; }
function key(run) { return run.task_id+'/'+run.run_id; }
function title(run) { return run.pr_number?'PR #'+run.pr_number+' · '+run.target_branch:run.target_branch+' · '+(run.event_kind==='push'?'分支提交':'手动任务'); }

function renderWorkers() {
  const root=$('workers'); root.replaceChildren();
  if(!arr(model.data.workers).length) { empty(root,'尚未收到 worker 健康快照。请检查健康服务与结果发布通道。'); return; }
  for(const worker of model.data.workers) {
    const item=el('article','ci-worker'); const head=el('div','ci-worker-head');
    const heartbeat=typeof worker.heartbeat_at==='number'?worker.heartbeat_at*1000:Date.parse(worker.heartbeat_at);
    const state=!Number.isFinite(heartbeat)?'unknown':Date.now()-heartbeat>900000?'offline':worker.state;
    head.append(el('h3','',worker.worker_id),badge(state)); item.append(head);
    item.append(el('p','ci-muted','最近心跳：'+date(worker.heartbeat_at)));
    const profiles=arr(worker.workers);
    if(!profiles.length)item.append(el('p','ci-muted','没有版本容器状态。'));
    for(const profile of profiles) {
      item.append(el('p','ci-muted',(profile.profile_id||profile.container||'版本环境')+' · '+(profile.draining?'维护排空':profile.lease?'执行任务 '+profile.lease.task_id:profile.running?'常驻 / 空闲':'容器未运行')));
    }
    if(arr(worker.issues).length) { const list=el('ul'); for(const issue of worker.issues)list.append(el('li','',issue.message||issue.code)); item.append(list); }
    if(worker.notification)item.append(el('p','ci-muted','邮件通知：'+({sent:'已发送',pending:'待发送 / 重试',unchanged:'无新增异常',dry_run:'仅验证，未发送',healthy:'无异常'}[worker.notification.status]||worker.notification.status)));
    root.append(item);
  }
}

function filteredRuns() {
  const query=$('taskSearch').value.trim().toLowerCase(); const filter=$('resultFilter').value;
  return arr(model.data.runs).filter(run=>(filter==='all'||(filter==='failure'?['failure','error'].includes(run.conclusion):run.conclusion===filter))&&[run.task_id,run.pr_number,run.target_branch,run.tested_sha,run.run_id].join(' ').toLowerCase().includes(query));
}

function renderList() {
  const root=$('taskList'); root.replaceChildren(); const runs=filteredRuns();
  $('taskCount').textContent=runs.length;
  if(!runs.some(run=>key(run)===model.selected))model.selected=runs.length?key(runs[0]):null;
  if(!runs.length)empty(root,arr(model.data.runs).length?'没有符合筛选条件的任务。':'尚未发布 Local CI 结果。');
  for(const run of runs) {
    const button=el('button','ci-task'); button.type='button'; button.setAttribute('aria-pressed',String(model.selected===key(run)));
    button.append(el('strong','',title(run)),badge(run.conclusion),el('small','',run.tested_sha.slice(0,12)+(run.is_latest?' · 此任务最新发布':'')),el('small','',date(run.completed_at)));
    button.addEventListener('click',()=>{model.selected=key(run);renderList();}); root.append(button);
  }
  renderDetail(runs.find(run=>key(run)===model.selected));
}

function evidenceList(parent, entries) {
  const list=el('ul','ci-evidence');
  for(const entry of arr(entries))list.append(el('li','',typeof entry==='string'?entry:[entry.path,entry.line?'L'+entry.line:'',entry.reason||entry.summary||''].filter(Boolean).join(' · ')));
  parent.append(list);
}

function renderDetail(run) {
  const root=$('taskDetail'); root.replaceChildren();
  if(!run) { empty(root,'选择一个任务以查看检查范围、AI 审查和执行证据。尚无数据时不会显示通过状态。'); return; }
  const head=el('div','ci-detail-head'); const heading=el('div'); heading.append(el('p','eyebrow','AI-DRIVEN SELF-TESTING & REVIEW'),el('h2','',title(run))); head.append(heading,badge(run.conclusion));root.append(head);
  const identity=el('div','ci-identity'); identity.append(el('code','',run.tested_sha),el('span','',date(run.completed_at))); root.append(identity);
  const links=el('div','ci-links'); for(const [label,url] of [['原始结果',run.result_url],['全部产物',run.artifacts_url]]) { const a=link(label,url); if(a)links.append(a); }root.append(links);
  const metrics=el('div','ci-metrics'); const checks=arr(run.checks); const values=[[checks.filter(c=>c.required).length,'最低必检项'],[checks.filter(c=>c.status==='passed').length,'已通过检查'],[checks.filter(c=>['skipped','not_applicable'].includes(c.status)).length,'未执行 / 不适用'],[arr(run.evidence).length,'命令执行记录']];
  for(const [value,label] of values){const box=el('div','ci-metric');box.append(el('strong','',value),el('span','',label));metrics.append(box);}root.append(metrics);
  if(arr(run.blocking_reasons).length){const box=el('div','ci-blockers');box.append(el('h3','','阻塞原因'));const list=el('ul');for(const reason of run.blocking_reasons)list.append(el('li','',reason));box.append(list);root.append(box);}
  if(run.source_unchanged===false)root.append(el('p','ci-notice','被测源码在执行中发生变化，当前结果不能作为对应提交的通过证据。'));
  const scope=section(root,'检查选择与执行结果');
  const policy=run.policy||{};scope.append(el('p','ci-muted',policy.docs_only?'文档变更：依规则免构建；架构审查仍需提供证据。':policy.manual_full?'维护者手动触发全量测试。':'按改动影响选择检查，并满足主机控制面规定的最低要求。'));
  const wrap=el('div','ci-table-shell'),table=el('table','ci-table'),thead=el('thead'),header=el('tr'); for(const s of ['检查','要求','结果','选择 / 未执行原因'])header.append(el('th','',s));thead.append(header);table.append(thead);const tbody=el('tbody');
  for(const check of checks){const tr=el('tr'); const name=el('td','',names[check.id]||check.id); name.append(el('small','',check.id));const requirement=el('td','',check.required?'必检':'按影响选择');const result=el('td');result.append(badge(check.status));const reason=el('td','',check.reason||'没有记录原因');if(policy.reasons?.[check.id])reason.append(el('small','','规则：'+policy.reasons[check.id]));tr.append(name,requirement,result,reason);tbody.append(tr);}table.append(tbody);wrap.append(table);scope.append(wrap);
  const review=run.ai_review||{};const ai=section(root,'AI 审查与定向验证');const summary=el('div','ci-review');summary.append(el('p','',review.summary||'未收到完整 AI 审查。'));ai.append(summary);
  const architecture=review.architecture||{};const arch=el('div','ci-review');arch.append(el('strong','','架构契约 '),badge(architecture.status||'unknown'),el('p','',architecture.summary||'没有架构审查证据。'));evidenceList(arch,architecture.evidence);ai.append(arch);
  for(const finding of arr(review.findings)){const card=el('div','ci-review');card.append(el('strong','',(finding.blocking?'阻塞 · ':'供维护者判断 · ')+(finding.severity||'风险')),el('p','',finding.summary||finding.title||'发现'));if(finding.qualification)card.append(el('p','ci-muted',finding.qualification));evidenceList(card,finding.code_evidence);if(arr(finding.reproduction_receipts).length)card.append(el('p','ci-muted','复现命令：'+finding.reproduction_receipts.join('、')));ai.append(card);}
  const performance=section(root,'性能变化');performance.append(el('p','ci-muted','性能回退或纯耗时变化仅报告；基准执行失败仍会阻塞。只有 Triton 3.0 环境具备后端、算子和性能能力。'));
  if(!arr(run.performance).length)performance.append(el('p','ci-muted','本次没有可展示的性能测量或基线对比。请结合上方检查状态判断是否适用。'));
  for(const item of arr(run.performance)){const card=el('div','ci-performance');card.append(el('strong','',names[item.tool]||item.tool||item.id||'性能记录'),el('pre','',JSON.stringify(item,null,2)));performance.append(card);}
  const receipts=section(root,'主机记录的执行证据');
  if(!arr(run.evidence).length)receipts.append(el('p','ci-muted','没有已发布的命令执行记录。'));
  for(const receipt of arr(run.evidence)){const card=el('details','ci-receipt');card.id='receipt-'+receipt.id;card.append(el('summary','',receipt.id+' · '+(names[receipt.tool]||receipt.tool)+' · exit '+receipt.returncode+' · '+receipt.elapsed_seconds+'s'+(receipt.termination?' · '+receipt.termination:'')),el('pre','',arr(receipt.argv).join(' ')),el('p','ci-muted','工作目录：'+receipt.cwd),el('p','ci-muted','日志 SHA-256：'+(receipt.log_sha256||'未提供')));const a=link('查看原始日志',receipt.log_url);if(a)card.append(a);receipts.append(card);}
  const context=el('details','ci-receipt');context.append(el('summary','','任务身份与影响文件'),el('pre','',JSON.stringify({task_id:run.task_id,run_id:run.run_id,task_ref:run.task_ref,base_sha:run.base_sha,head_sha:run.head_sha,worker_revision_sha:run.worker_revision_sha,changed_paths:policy.changed_paths},null,2)));root.append(context);
}

async function load() {
  const notice=$('dataNotice'); $('refresh').disabled=true;
  try {
    const requested=new URLSearchParams(location.search).get('data');
    const source=requested&&/^data\/[A-Za-z0-9_.-]+\.json$/.test(requested)?requested:'data/local-ci.json';
    const response=await fetch(source,{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);
    const data=await response.json();if(data.schema!=='triton-anchor-dashboard-local-ci/v1'||!Array.isArray(data.runs)||!Array.isArray(data.workers))throw new Error('结果数据格式不兼容');
    model.data=data;notice.hidden=data.data_mode!=='fixture';notice.textContent='本机界面样例 · 以下数据用于验证展示，不构成编译、硬件或部署验收证据。';
    $('updatedAt').textContent=data.generated_at?'结果同步：'+date(data.generated_at):'尚未同步真实结果';
    const warnings=$('syncWarnings');warnings.replaceChildren();warnings.hidden=!arr(data.warnings).length;if(!warnings.hidden){warnings.append(el('strong','','部分发布数据未通过校验'));const list=el('ul');for(const warning of data.warnings)list.append(el('li','',warning.path+'：'+warning.reason));warnings.append(list);}
    renderWorkers();renderList();
  } catch(error) { notice.hidden=false;notice.textContent='无法加载 Local CI 数据：'+error.message+'。页面不据此推断 CI 通过。';if(!model.data){empty($('workers'),'健康数据不可用。');empty($('taskDetail'),'结果数据不可用。');} }
  finally {$('refresh').disabled=false;}
}
$('refresh').addEventListener('click',load);$('taskSearch').addEventListener('input',()=>model.data&&renderList());$('resultFilter').addEventListener('change',()=>model.data&&renderList());
load();setInterval(load,60000);
