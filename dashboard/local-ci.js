/* Render remote result text as text nodes. No result field can inject HTML. */
const labels = {success:'通过',passed:'通过',failure:'失败',failed:'失败',error:'执行错误',cancelled:'已取消',skipped:'未执行',not_applicable:'不适用',healthy:'正常',degraded:'异常',offline:'离线',snapshot_stale:'快照已过期',unknown:'状态未知',waiting:'等待',ready:'已交付',pending:'待交付',expired:'已到期',not_comparable:'无可比基线'};
const names = {environment:'环境与依赖',frontend_build:'前端构建',frontend_install:'前端安装与导入',frontend_tests:'前端测试',wheel_install:'Wheel 安装与导入',frontend_smoke:'前端基本功能验证',backend_build:'后端构建',backend_install:'后端安装与发现',backend_tests:'后端测试',backend_rebuild:'后端重新构建',backend_smoke:'后端基本功能与 JIT 验证',flaggems:'FlagGems',compile_time:'编译时间性能',pass_profile:'编译阶段性能剖析',ir_serialization:'IR 序列化',pr_information:'PR 说明与改动核验',architecture_review:'架构与接口约束审查',control_plane:'CI 流程检查',custom_test:'定向测试'};
const friendlyReasons = {'cancelled':'任务已取消','missing required check':'必检尚未完成','PR intent and attributes were not reviewed':'PR 意图与属性尚未完成核验','all commands completed successfully':'命令执行成功','not selected for this change':'本次改动未触发该项检查','minimum frontend coverage':'编译器改动的最低检查范围','frontend code or test behavior changed':'前端代码或测试行为发生变化','architecture contract review is mandatory':'架构审查为必检项'};
const model = {data:null, selected:null};
const $ = id => document.getElementById(id);
const arr = value => Array.isArray(value) ? value : [];
const txt = value => typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value);
function el(tag, className, text) { const node=document.createElement(tag); if(className)node.className=className; if(text!=null)node.textContent=txt(text); return node; }
function badge(status) { const tone=['success','passed','healthy','ready'].includes(status)?'good':['failure','failed','error','offline'].includes(status)?'bad':['degraded','waiting','pending','expired','snapshot_stale'].includes(status)?'warn':status==='cancelled'?'info':''; return el('span','ci-badge '+tone,labels[status]||status||'未知'); }
function date(value) { const d=new Date(typeof value==='number'?value*1000:value); return value!=null&&!Number.isNaN(d.valueOf())?d.toLocaleString('zh-CN',{hour12:false}):'尚无时间记录'; }
function link(label, url) { try { const target=new URL(url); if(target.protocol!=='https:')return null; const a=el('a','',label); a.href=target.href; a.target='_blank'; a.rel='noopener noreferrer'; return a; } catch { return null; } }
function empty(container, message) { container.append(el('div','ci-empty',message)); }
function section(parent,title) { const s=el('section'); s.append(el('h3','',title)); parent.append(s); return s; }
function key(run) { return run.task_id+'/'+run.run_id; }
function title(run) { return run.pr_number?'PR #'+run.pr_number+' · '+run.target_branch:run.target_branch+' · '+(run.event_kind==='push'?'分支提交':'手动任务'); }
function reason(value) { return friendlyReasons[value]||txt(value)||'没有记录原因'; }
function publicText(value) { let text=txt(value);for(const [id,label] of Object.entries(names))text=text.replaceAll(id+':',label+'：');for(const [message,label] of Object.entries(friendlyReasons))text=text.replaceAll(message,label);return text.replaceAll('Codex cancelled','AI 验证已取消').replaceAll('base..merge','基准提交与合并验证提交之间').replace(/\bhead\(([^)]+)\)/g,'PR 提交 $1').replace(/\bmerge\(([^)]+)\)/g,'合并验证提交 $1').replaceAll('changed_paths','影响文件列表').replaceAll('Frontend','前端').replaceAll('Backend','后端').replaceAll(' smoke','基本功能验证').replaceAll(' wheel',' wheel 包'); }

function facts(parent,rows) {const list=el('dl','ci-facts');for(const [name,value] of rows){list.append(el('dt','',name),el('dd','',value??'未采集'));}parent.append(list);}

function filteredRuns() {
  const query=$('taskSearch').value.trim().toLowerCase(); const filter=$('resultFilter').value;
  const prQuery=query.match(/^#?([1-9][0-9]*)$/); const includeHistory=$('historyFilter').value==='all';
  return arr(model.data.runs).filter(run=>(includeHistory||run.is_current)&&(filter==='all'||(filter==='failure'?['failure','error'].includes(run.conclusion):run.conclusion===filter))&&(prQuery?String(run.pr_number)===prQuery[1]:[run.pr_number,run.target_branch,run.tested_sha].join(' ').toLowerCase().includes(query)));
}

function renderList() {
  const root=$('taskList'); root.replaceChildren(); const runs=filteredRuns();
  $('taskCount').textContent=runs.length;
  if(!runs.some(run=>key(run)===model.selected))model.selected=runs.length?key(runs[0]):null;
  if(!runs.length)empty(root,arr(model.data.runs).length?'没有符合筛选条件的任务。':'尚未发布 Local CI 结果。');
  for(const run of runs) {
    const button=el('button','ci-task'); button.type='button'; button.setAttribute('aria-pressed',String(model.selected===key(run)));
    button.append(el('strong','',title(run)),badge(run.conclusion),el('small','',run.tested_sha.slice(0,12)+(run.is_current?' · 当前结果':' · 历史记录')),el('small','',date(run.completed_at)));
    button.addEventListener('click',()=>{model.selected=key(run);renderList();}); root.append(button);
  }
  renderDetail(runs.find(run=>key(run)===model.selected));
}

function evidenceList(parent, entries) {
  const list=el('ul','ci-evidence');
  for(const entry of arr(entries))list.append(el('li','',typeof entry==='string'?entry:[entry.path,entry.line?'L'+entry.line:'',entry.reason||entry.summary||''].filter(Boolean).join(' · ')));
  parent.append(list);
}

function blockingSummary(run) {
  const checks=arr(run.checks).filter(check=>['error','skipped','not_applicable'].includes(check.status));
  const original=arr(run.blocking_reasons);
  const preparationFailed=original.some(value=>String(value).startsWith('worker preparation'));
  const reasons=[...new Set(original.filter(value=>!(preparationFailed&&value==='tested tracked source changed during execution')&&!checks.some(check=>String(value).startsWith(check.id+': '))).map(value=>String(value).startsWith('worker preparation')?'CI 运行环境准备未完成，需要维护者处理后重新执行；详细原因见完整报告。':value==='tested tracked source changed during execution'?'源码一致性校验未通过，需要重新验证。':value))];
  if(original.length&&!reasons.length)reasons.push('验证未完成，执行错误和未执行项目详见下方检查结果。');
  return reasons;
}

function renderDetail(run) {
  const root=$('taskDetail'); root.replaceChildren();
  if(!run) { empty(root,'选择一个任务以查看检查范围、审查结论和执行证据。尚无数据时不会显示通过状态。'); return; }
  const head=el('div','ci-detail-head'); const heading=el('div'); heading.append(el('p','eyebrow','验证与审查'),el('h2','',title(run))); head.append(heading,badge(run.conclusion));root.append(head);
  const identity=el('div','ci-identity'); identity.append(el('code','',run.tested_sha),el('span','',date(run.completed_at))); root.append(identity);
  facts(root,[['本地验证',labels[run.local_conclusion]||run.local_conclusion],['证据交付',labels[run.delivery_status]||run.delivery_status],['控制版本',run.worker_revision_sha],['环境',run.environment.profile||'未记录']]);
  if(run.receiver_message)root.append(el('p','ci-notice',run.receiver_message));
  const links=el('div','ci-links'); for(const [label,url] of [['查看完整结果',run.result_url],['查看执行产物',run.artifacts_url]]) { const a=link(label,url); if(a)links.append(a); }root.append(links);
  const metrics=el('div','ci-metrics'); const checks=arr(run.checks); const values=[[checks.filter(c=>c.required).length,'最低必检项'],[checks.filter(c=>c.status==='passed').length,'已通过检查'],[checks.filter(c=>['skipped','not_applicable'].includes(c.status)).length,'未执行 / 不适用'],[arr(run.evidence).length,'命令执行记录']];
  for(const [value,label] of values){const box=el('div','ci-metric');box.append(el('strong','',value),el('span','',label));metrics.append(box);}root.append(metrics);
  const blockers=blockingSummary(run);
  if(blockers.length){const box=el('div','ci-blockers');box.append(el('h3','','阻塞原因'));const list=el('ul');for(const reason of blockers)list.append(el('li','',publicText(reason)));box.append(list);root.append(box);}
  const scope=section(root,'检查选择与执行结果');
  const policy=run.policy||{};scope.append(el('p','ci-muted',policy.docs_only?'文档变更：依规则免构建；架构审查仍需提供证据。':policy.manual_full?'维护者手动触发全量测试。':'按改动影响选择检查，并满足主机控制面规定的最低要求。'));
  const wrap=el('div','ci-table-shell'),table=el('table','ci-table'),thead=el('thead'),header=el('tr'); for(const s of ['检查','要求','结果','选择 / 未执行原因'])header.append(el('th','',s));thead.append(header);table.append(thead);const tbody=el('tbody');
  for(const check of checks){const tr=el('tr'); const name=el('td','',names[check.id]||'补充检查');const requirement=el('td','',check.required?'必检':'按影响选择');const result=el('td');result.append(badge(check.status));const explanation=el('td','',publicText(reason(check.reason)));if(policy.reasons?.[check.id])explanation.append(el('small','','选择依据：'+publicText(reason(policy.reasons[check.id]))));tr.append(name,requirement,result,explanation);tbody.append(tr);}table.append(tbody);wrap.append(table);scope.append(wrap);
  const review=run.ai_review||{};const ai=section(root,'AI 辅助审查与定向验证');const summary=el('div','ci-review');summary.append(el('p','',publicText(review.summary)||'未收到完整审查结论。'));ai.append(summary);
  const architecture=review.architecture||{};const arch=el('div','ci-review');arch.append(el('strong','','架构契约 '),badge(architecture.status||'unknown'),el('p','',publicText(architecture.summary)||'没有架构审查证据。'));evidenceList(arch,architecture.evidence);ai.append(arch);
  for(const finding of arr(review.findings)){const card=el('div','ci-review');card.append(el('strong','',finding.blocking?'合入阻塞':'需要人工判断'),el('p','',publicText(finding.summary||finding.title)||'发现'));if(finding.qualification)card.append(el('p','ci-muted',publicText(finding.qualification)));evidenceList(card,finding.code_evidence);ai.append(card);}
  const performance=section(root,'性能变化');performance.append(el('p','ci-muted','性能回退或纯耗时变化仅报告；基准执行失败仍会阻塞。适用能力以该任务的环境声明为准。'));
  if(!arr(run.performance).length)performance.append(el('p','ci-muted','本次没有可展示的性能测量或基线对比。请结合上方检查状态判断是否适用。'));
  for(const item of arr(run.performance)){const card=el('div','ci-performance');card.append(el('strong','',names[item.tool]||'性能记录'),el('p','ci-muted',item.summary||item.reason||'已记录性能结果，详细数据见完整结果。'));performance.append(card);}
  const delivery=section(root,'证据与交付');
  if(!arr(run.artifacts).length)empty(delivery,'尚无已封存的执行产物。');
  const delivered=el('ul','ci-evidence');
  for(const artifact of arr(run.artifacts)){
    const row=el('li');const label=artifact.source_path||artifact.path||artifact.artifact_id;
    const target=link(label,artifact.url);row.append(target||el('span','',label),el('span','ci-muted',' · '+(artifact.required?'必要证据':'附加证据')+' · '),badge(artifact.status));
    if(artifact.omitted_bytes)row.append(el('span','ci-muted',' · 省略 '+artifact.omitted_bytes+' bytes'));
    delivered.append(row);
  }
  delivery.append(delivered);
  const receipts=section(root,'执行记录');
  if(!arr(run.evidence).length)receipts.append(el('p','ci-muted','没有已发布的命令执行记录。'));
  const receiptGroups=new Map();for(const receipt of arr(run.evidence)){const group=receiptGroups.get(receipt.tool)||[];group.push(receipt);receiptGroups.set(receipt.tool,group);}
  for(const [tool,group] of receiptGroups){const failed=group.some(item=>item.returncode!==0);const duration=group.reduce((sum,item)=>sum+(Number(item.elapsed_seconds)||0),0);const card=el('details','ci-receipt');const summary=el('summary','ci-receipt-row');summary.append(el('strong','',names[tool]||'补充检查'),badge(failed?'error':'passed'),el('span','ci-muted',group.length+' 条记录 · '+duration.toFixed(1)+' 秒'));card.append(summary);const logs=el('ul','ci-evidence');for(const [index,item] of group.entries()){const a=link('日志 '+(index+1),item.log_url);if(a){const li=el('li');li.append(a);logs.append(li);}}if(logs.children.length)card.append(logs);for(const item of group)card.append(el('p','ci-muted',(item.execution_id||'')+' · '+(item.execution_kind||'builtin')+' · exit '+item.returncode));receipts.append(card);}
  const context=el('details','ci-receipt');const commitBox=el('div','ci-evidence');commitBox.append(el('p','',run.pr_number?'PR 提交：'+run.head_sha:'被测提交：'+run.tested_sha));if(run.pr_number&&run.tested_sha!==run.head_sha)commitBox.append(el('p','',`与 ${run.target_branch} 合并后的验证提交：${run.tested_sha}`));if(arr(policy.changed_paths).length){commitBox.append(el('p','','影响文件：'));evidenceList(commitBox,policy.changed_paths);}context.append(el('summary','','被测提交与影响文件'),commitBox);root.append(context);
}

async function load() {
  const notice=$('dataNotice'); const refresh=$('refresh'); refresh.disabled=true;refresh.textContent='读取中…';
  try {
    const requested=new URLSearchParams(location.search).get('data');
    const source=requested&&/^data\/[A-Za-z0-9_.-]+\.json$/.test(requested)?requested:'data/v4-tasks.json';
    const response=await fetch(source+(source.includes('?')?'&':'?')+'refresh='+Date.now(),{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);
    const data=LocalCIData.normalize(await response.json());
    model.data=data;notice.hidden=data.data_mode!=='fixture';notice.textContent='本机界面样例 · 以下数据用于验证展示，不构成编译、硬件或部署验收证据。';
    $('updatedAt').textContent=data.generated_at?'数据生成：'+date(data.generated_at)+' · 本页读取：'+new Date().toLocaleTimeString('zh-CN',{hour12:false}):'尚未同步真实结果';
    const warnings=$('syncWarnings');warnings.replaceChildren();warnings.hidden=!arr(data.warnings).length;if(!warnings.hidden){warnings.append(el('strong','','部分发布数据未通过校验'));const list=el('ul');for(const warning of data.warnings)list.append(el('li','',warning.path+'：'+warning.reason));warnings.append(list);}
    renderList();
  } catch(error) { notice.hidden=false;notice.textContent='无法加载 Local CI 数据：'+error.message+'。页面不据此推断 CI 通过。';if(!model.data){empty($('taskDetail'),'结果数据不可用。');} }
  finally {refresh.disabled=false;refresh.textContent='查询最新快照';}
}
$('refresh').addEventListener('click',load);$('taskSearch').addEventListener('input',()=>model.data&&renderList());$('historyFilter').addEventListener('change',()=>model.data&&renderList());$('resultFilter').addEventListener('change',()=>model.data&&renderList());
const initialPr=new URLSearchParams(location.search).get('pr');if(/^[1-9][0-9]*$/.test(initialPr||''))$('taskSearch').value=initialPr;
load();setInterval(()=>{if(!document.hidden)load();},300000);
