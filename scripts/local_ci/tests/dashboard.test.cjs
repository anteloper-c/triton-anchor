const test = require('node:test');
const assert = require('node:assert/strict');
const { normalize, business } = require('../../../dashboard/data.js');
const task = (id, date) => ({task_id:id, repository:'example/repo',pr_number:7,target_branch:'main',tested_sha:id.repeat(40),captured_at:date});

test('old pass cannot override a newer pending or cancelled task', () => {
  const data = normalize({schema:'triton-anchor-dashboard',tasks:[
    {task:task('a','2026-09-09'),status:'cancelled',result:{status:'pass',run_id:'old'}},
    {task:task('b','2026-09-10'),status:'pending'}]});
  assert.equal(data.runs[0].is_current,true);
  assert.equal(data.runs[0].conclusion,'waiting');
  assert.equal(data.runs[1].is_current,false);
  assert.equal(data.runs[1].conclusion,'cancelled');
});

test('omitted files and unsafe URLs are not clickable', () => {
  const artifacts = [{path:'missing',omitted:'Too large'},{path:'safe'},{path:'unsafe'}];
  const data = normalize({schema:'triton-anchor-dashboard',tasks:[{task:task('a','2026-09-10'),
    result:{artifacts},artifact_urls:{missing:'https://gitee.com/missing',safe:'https://gitee.com/report',unsafe:'javascript:alert(1)'}}]});
  assert.deepEqual(data.runs[0].artifacts.map(a=>a.url),['','https://gitee.com/report','']);
});

test('full view excludes impact-only selection and preserves failing operator details', () => {
  const run = {task:task('a','2026-09-10'),result:{environment:{profile:'fixture'},checks:[{tool_id:'flaggems',status:'fail',details:{"flaggems-summary":{mode:'full',results:[{op:'softmax',test_status:'失败',first_failed_stage:'准确率验证',duration_seconds:2}]}}}]}};
  let data = business(normalize({schema:'triton-anchor-dashboard',tasks:[run]}));
  assert.equal(data.fullTest.operators[0].status,'failed');
  assert.equal(data.fullTest.operators[0].failure_stage,'准确率验证');
  run.result.checks[0].details["flaggems-summary"].mode='impact';
  data = business(normalize({schema:'triton-anchor-dashboard',tasks:[run]}));
  assert.equal(data.fullTest.operators.length,0);
});

test('empty initial feed does not create placeholder success data', () => {
  const data = business(normalize({schema:'triton-anchor-dashboard',tasks:[]}));
  assert.equal(data.fullTest.operators.length,0);
  assert.equal(data.backends.backends.length,0);
  assert.equal(data.performance.compile_time.kernels.length,0);
});


test('performance views read runner candidate and comparison report keys', () => {
  const data = business(normalize({schema:'triton-anchor-dashboard',tasks:[{
    task:task('a','2026-09-10'),status:'pass',result:{checks:[
      {tool_id:'compile_time',status:'pass',details:{candidate:{summary:{add:{compile_est:{median_ms:12}}}},comparison:{kernels:[{kernel:'add',change_ratio:0.2}]}}},
      {tool_id:'pass_profile',status:'pass',details:{candidate:{summary:{add:{passes:{canonicalize:{wall_ms:{median_ms:3}}}}}}}},
      {tool_id:'ir_serialization',status:'pass',details:{candidate:{summary:{add:{metrics:{serialize:{median_ms:2}}}}}}}
    ]}
  }]}));
  assert.equal(data.performance.compile_time.kernels[0].candidate_ms,12);
  assert.equal(data.performance.compile_time.kernels[0].delta_percent,20);
  assert.equal(data.performance.pass_profile.hotspots[0].median_ms,3);
  assert.equal(data.performance.ir_serialization.metrics[0].median_ms,2);
});


test('all Codex reviews retain their summaries and source references', () => {
  const reviews = ['pr_info','architecture','intent'].map(kind => ({kind,status:'pass',summary:kind+' reviewed',evidence:['src/example.py:7']}));
  const run = normalize({schema:'triton-anchor-dashboard',tasks:[{task:task('a','2026-09-10'),result:{reviews}}]}).runs[0];
  for (const review of reviews) {
    assert.equal(run.ai_review[review.kind].summary,review.summary);
    assert.deepEqual(run.ai_review[review.kind].evidence,review.evidence);
    assert.equal(run.ai_review[review.kind].status,'passed');
  }
});
