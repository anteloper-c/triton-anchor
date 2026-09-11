/* Shared v4 projection for A's task, operator and performance views. */
(function (global) {
  const array = value => Array.isArray(value) ? value : [];
  const status = value => ({pass:'passed',fail:'failed',infra_error:'error',pending:'waiting',blocked_dependency:'skipped',not_selected:'skipped'}[value] || value || 'unknown');
  const safeUrl = value => { try { const url = new URL(value); return url.protocol === 'https:' ? url.href : ''; } catch { return ''; } };
  const subject = task => task.repository + '/' + (task.pr_number ? 'pr/' + task.pr_number : 'branch/' + task.target_branch);
  const timestamp = value => typeof value === 'number' ? value * 1000 : Date.parse(value) || 0;
  const duration = value => Math.max(0, (timestamp(value.finished_at) - timestamp(value.started_at)) / 1000);
  function normalize(feed) {
    if (feed.schema !== 'triton-anchor-dashboard/v4' || !Array.isArray(feed.tasks)) throw new Error('结果数据格式不兼容');
    const ordered = [...feed.tasks].sort((a,b) => timestamp(b.task?.captured_at) - timestamp(a.task?.captured_at));
    const latest = new Map();
    for (const item of ordered) if (!latest.has(subject(item.task || {}))) latest.set(subject(item.task || {}), item.task?.task_id);
    const runs = ordered.map(item => {
      const task = item.task || {}, result = item.result || {};
      const delivered = new Map(array(item.delivery?.artifacts).map(entry => [entry.artifact_id, entry]));
      const artifacts = array(result.artifacts).map(entry => {
        const delivery = delivered.get(entry.artifact_id) || {};
        return {...entry, status: delivery.status || 'pending', url: delivery.status === 'ready' ? safeUrl(delivery.url) : ''};
      });
      const artifactLink = (executionId, pattern) => artifacts.find(a => a.execution_id === executionId && pattern.test(a.source_path || a.path || '') && a.url)?.url || '';
      const required = new Set(array(result.required_checks));
      const checks = array(result.checks).filter(c => !c.variant || c.variant === 'candidate').map(check => ({...check,
        id: check.tool_id, status: status(check.status), required: required.has(check.tool_id),
        reason: check.reason || (check.status === 'pass' ? 'all commands completed successfully' : '')}));
      const local = result.status || item.status || 'pending';
      const conclusion = local === 'pass' ? (item.delivery_status === 'ready' ? 'success' : 'waiting') :
                         local === 'fail' ? 'failure' : local === 'infra_error' ? 'error' : status(local);
      const reviews = result.reviews || {};
      const evidence = checks.filter(c => c.execution_id).map(check => ({...check,
        tool: check.tool_id, returncode: check.exit_code, elapsed_seconds: duration(check),
        log_url: artifactLink(check.execution_id, /(?:command|execution|\.log)/)}));
      const performance = array(result.performance).map(entry => ({...entry, tool: entry.tool_id,
        reason: entry.details?.performance?.reason,
        summary: entry.details?.performance?.status === 'not_comparable' ? '已测候选版本，缺少同条件可比基线。' :
                 entry.details?.performance?.status === 'warning' ? '检测到性能变化；有效性能回退仅报告。' : '测量和比较结果已记录。'}));
      const blockers = array(result.blockers).map(entry => typeof entry === 'string' ? entry : entry.reason || entry.finding?.summary || entry.tool_id || entry.kind);
      return {...task, task_id: task.task_id || result.task?.task_id || '', run_id: result.run_id || 'pending',
        completed_at: result.completed_at || Math.max(0,...checks.map(c => timestamp(c.finished_at))) / 1000 || task.captured_at,
        is_current: latest.get(subject(task)) === task.task_id, conclusion, local_conclusion: status(local),
        delivery_status: item.delivery_status || 'pending', artifacts, checks, evidence, performance,
        policy: {...(result.policy || {}), docs_only: result.policy?.impact?.level === 'non_executable',
                 manual_full: task.full, changed_paths: array(result.changes).map(c => c.path)},
        blocking_reasons: [...blockers, ...array(result.unfinished).map(name => '未完成：' + name)],
        ai_review: {summary: result.summary || reviews.pr_info?.summary, architecture: {...(reviews.architecture || {}), status: status(reviews.architecture?.status)}, findings: array(result.findings)},
        environment: result.environment || {}, raw: result, receiver_message: item.receiver_message || '',
        result_url: safeUrl(item.result_url), artifacts_url: '', worker_revision_sha: task.worker_revision_sha};
    });
    return {schema:feed.schema, data_mode:feed.data_mode || 'live', generated_at:feed.generated_at,
      runs, warnings:ordered.filter(item => item.receiver_error).map(item => ({path:item.task?.task_id, reason:item.receiver_message || item.receiver_error}))};
  }
  function business(data) {
    const runs = data.runs;
    const full = runs.find(run => run.checks.some(check => check.details?.flaggems?.mode === 'full'));
    const fg = full?.checks.find(check => check.details?.flaggems?.mode === 'full')?.details.flaggems;
    const operators = array(fg?.results).map((row,index) => ({index:row.index || index+1, name:row.op,
      status:({'通过':'passed','成功':'passed','失败':'failed','超时':'timeout'}[row.test_status] || (row.exit_code === 0 && row.passed > 0 ? 'passed' : 'failed')),
      failure_stage:(row.first_failed_stage === '全部通过' ? '' : row.first_failed_stage) || row.timeout_reason || '', duration_ms:row.duration_seconds * 1000,
      log_url:full?.artifacts.find(a => (a.source_path || '').endsWith(row.log_file) && a.url)?.url || ''}));
    const latestBackends = new Map();
    for (const run of runs) {
      const profile = run.environment.profile || run.environment.generation || '未记录环境';
      if (run.checks.some(c => c.id.startsWith('backend_') && c.status !== 'not_applicable') && !latestBackends.has(profile)) latestBackends.set(profile,run);
    }
    const backends = [...latestBackends].map(([profile,run]) => ({id:profile,name:profile,profile,
      state:run.conclusion === 'success' ? 'passed' : status(run.conclusion),sha:run.tested_sha,tested_at:run.completed_at,
      tests:{delivery:status(run.delivery_status === 'ready' ? 'pass' : run.delivery_status),
        ...Object.fromEntries(['compile_time','pass_profile','ir_serialization'].map(id => [id,run.checks.find(c => c.id === id)?.status || 'unknown']))},
      result_url:run.result_url}));
    const measured = runs.find(run => run.checks.some(c => c.details?.measurements));
    const compile = measured?.checks.find(c => c.id === 'compile_time')?.details || {};
    const passes = measured?.checks.find(c => c.id === 'pass_profile')?.details || {};
    const ir = measured?.checks.find(c => c.id === 'ir_serialization')?.details || {};
    const compileRows = Object.entries(compile.measurements?.summary || {}).map(([name,value]) => {
      const compare = array(compile.performance?.kernels).find(row => row.kernel === name || row.name === name);
      return {name,candidate_ms:value.compile_est?.median_ms,delta_percent:compare?.change_ratio == null ? null : compare.change_ratio * 100,status:compare?.status || 'passed'};
    }).filter(row => Number.isFinite(row.candidate_ms));
    const passRows = Object.entries(passes.measurements?.summary || {}).flatMap(([kernel,value]) =>
      Object.entries(value.passes || {}).map(([name,timing]) => ({name:kernel + ' · ' + name,median_ms:timing.wall_ms?.median_ms})))
      .filter(row => Number.isFinite(row.median_ms)).sort((a,b) => b.median_ms-a.median_ms).slice(0,20);
    const irRows = Object.entries(ir.measurements?.summary || {}).flatMap(([kernel,value]) =>
      Object.entries(value.metrics || {}).map(([name,timing]) => ({name:kernel + ' · ' + name,median_ms:timing.median_ms})))
      .filter(row => Number.isFinite(row.median_ms));
    return {manifest:{generated_at:data.generated_at,mode:data.data_mode === 'fixture' ? 'mock' : 'live',downloads:{}},
      fullTest:{run:{backend:full?.environment.profile || '尚无全量算子结果',sha:full?.tested_sha || ''},operators},
      backends:{backends},performance:{backend:measured?.environment.profile || '尚无有效测量',compile_time:{kernels:compileRows},pass_profile:{hotspots:passRows},ir_serialization:{metrics:irRows}}};
  }
  global.LocalCIData = {normalize,business,status,safeUrl};
  if (typeof module !== 'undefined' && module.exports) module.exports = global.LocalCIData;
})(typeof window === 'undefined' ? globalThis : window);
