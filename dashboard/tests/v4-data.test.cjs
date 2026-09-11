const test = require('node:test');
const assert = require('node:assert/strict');
const { normalize, business } = require('../v4-data.js');
const task = (id, date) => ({task_id:id, repository:'example/repo',pr_number:7,target_branch:'main',tested_sha:id.repeat(40),captured_at:date});

test('old pass cannot appear current and pending delivery is not overall success', () => {
  const feed = {schema:'triton-anchor-dashboard/v4',tasks:[
    {task:task('a','2026-09-09'),result:{status:'pass',run_id:'old'},delivery_status:'ready'},
    {task:task('b','2026-09-10'),result:{status:'pass',run_id:'new'},delivery_status:'pending'}]};
  const data = normalize(feed);
  assert.equal(data.runs[0].is_current,true);
  assert.equal(data.runs[0].conclusion,'waiting');
  assert.equal(data.runs[0].local_conclusion,'passed');
  assert.equal(data.runs[1].is_current,false);
});

test('pending, expired and unsafe attachment URLs are never clickable', () => {
  const artifacts = ['pending','expired','ready','ready'].map((status,index) => ({artifact_id:String(index),status,url:index===3?'javascript:alert(1)':'https://gitee.com/example/'+index}));
  const data = normalize({schema:'triton-anchor-dashboard/v4',tasks:[{task:task('a','2026-09-10'),
    result:{artifacts:artifacts.map(a=>({artifact_id:a.artifact_id}))},delivery:{artifacts}}]});
  assert.deepEqual(data.runs[0].artifacts.map(a=>a.url),['','','https://gitee.com/example/2','']);
});

test('full view excludes impact-only selection and preserves failing operator details', () => {
  const run = {task:task('a','2026-09-10'),result:{environment:{profile:'fixture'},checks:[{tool_id:'flaggems',status:'fail',details:{flaggems:{mode:'full',results:[{op:'softmax',test_status:'失败',first_failed_stage:'准确率验证',duration_seconds:2}]}}}]}};
  let data = business(normalize({schema:'triton-anchor-dashboard/v4',tasks:[run]}));
  assert.equal(data.fullTest.operators[0].status,'failed');
  assert.equal(data.fullTest.operators[0].failure_stage,'准确率验证');
  run.result.checks[0].details.flaggems.mode='impact';
  data = business(normalize({schema:'triton-anchor-dashboard/v4',tasks:[run]}));
  assert.equal(data.fullTest.operators.length,0);
});

test('empty initial feed does not create placeholder success data', () => {
  const data = business(normalize({schema:'triton-anchor-dashboard/v4',tasks:[]}));
  assert.equal(data.fullTest.operators.length,0);
  assert.equal(data.backends.backends.length,0);
  assert.equal(data.performance.compile_time.kernels.length,0);
});
