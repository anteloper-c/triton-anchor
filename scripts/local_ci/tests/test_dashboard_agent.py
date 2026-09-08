"""Publication identity and safety contracts for the Local CI dashboard feed."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.dashboard.sync_agent_results import sync_agent_results, web_link
from scripts.dashboard.sync_gitee_results import sync_dashboard


NODE = shutil.which('node')


def example_result():
    return {'schema': 'triton-anchor-local-ci-result', 'repository': 'anteloper-c/triton-anchor',
            'task_id': 'fixture-pr-42', 'run_id': 'fixture-run-1', 'task_ref': 'ci/pr-42/main',
            'pr_number': 42, 'event_kind': 'pull_request', 'target_branch': 'main',
            'tested_sha': 'a' * 40, 'base_sha': 'b' * 40, 'head_sha': 'c' * 40,
            'worker_revision_sha': 'd' * 40, 'conclusion': 'failure',
            'completed_at': datetime.now(timezone.utc).isoformat(), 'source_unchanged': True,
            'checks': [
                {'id': 'environment', 'status': 'passed', 'required': True, 'reason': '界面样例：环境检查完成', 'evidence': ['command-0001']},
                {'id': 'frontend_build', 'status': 'failed', 'required': True, 'reason': '界面样例：构建命令退出码 1，后续依赖检查未执行。', 'evidence': ['command-0002']},
                {'id': 'frontend_install', 'status': 'skipped', 'required': True, 'reason': 'Frontend build 未完成。', 'evidence': []},
                {'id': 'frontend_smoke', 'status': 'skipped', 'required': True, 'reason': '没有可安装的候选 wheel。', 'evidence': []},
                {'id': 'backend_smoke', 'status': 'not_applicable', 'required': False, 'reason': '此版本没有后端测试能力。', 'evidence': []},
                {'id': 'architecture_review', 'status': 'passed', 'required': True, 'reason': '界面样例：架构证据展示。', 'evidence': []}],
            'blocking_reasons': ['界面样例：Frontend build 执行失败，需查看 command-0002 日志。'],
            'ai_review': {'summary': '界面样例：本次修改涉及 Python pipeline。已选择最低前端检查；构建失败后停止依赖项并保留证据。',
                          'architecture': {'status': 'passed', 'summary': '界面样例：审查报告引用仓库架构文档，供维护者复核。',
                                           'evidence': [{'path': 'docs/architecture.md', 'line': 12, 'reason': '架构边界说明'}]},
                          'findings': [{'severity': 'medium', 'blocking': False, 'summary': '界面样例：需进一步确认缓存变化的兼容性。',
                                        'code_evidence': [{'path': 'python/pipeline.py', 'line': 24, 'reason': '缓存调用路径'}]}]},
            'evidence': [{'id': 'command-0001', 'tool': 'environment', 'argv': ['python', '--version'], 'cwd': '/workspace/source',
                          'returncode': 0, 'elapsed_seconds': 0.2, 'termination': None, 'log_path': 'logs/command-0001.log', 'log_sha256': '0' * 64},
                         {'id': 'command-0002', 'tool': 'frontend_build', 'argv': ['python', '-m', 'build', '--wheel'], 'cwd': '/workspace/source',
                          'returncode': 1, 'elapsed_seconds': 14.8, 'termination': None, 'log_path': 'logs/command-0002.log', 'log_sha256': '1' * 64}],
            'performance': [], 'policy': {'docs_only': False, 'manual_full': False, 'changed_paths': ['python/pipeline.py'],
                                          'required': ['environment', 'frontend_build', 'frontend_install', 'frontend_smoke', 'architecture_review'],
                                          'reasons': {'frontend_build': '编译器变更最低要求'}}}


class DashboardAgentFeed(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'results'
        self.output = Path(self.temp.name) / 'data'
        self.root.mkdir()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def publish(self):
        result = example_result()
        relative = 'runs/fixture-pr-42/fixture-run-1/result.json'
        path = self.write(relative, result)
        pointer = {'task_id': result['task_id'], 'run_id': result['run_id'], 'result_path': relative,
                   'result_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.write('tasks/fixture-pr-42/latest.json', pointer)
        return result, pointer

    def sync(self):
        return sync_agent_results(self.root, self.output, 'https://gitee.com/example/new-ci-results')

    def test_published_result_keeps_checks_ai_evidence_and_commit_identity(self):
        result, pointer = self.publish()
        feed = self.sync()
        self.assertEqual(len(feed['runs']), 1)
        run = feed['runs'][0]
        self.assertTrue(run['is_latest'])
        self.assertEqual(run['tested_sha'], result['tested_sha'])
        self.assertEqual(run['checks'][2]['status'], 'skipped')
        self.assertIn('/logs/command-0002.log', run['evidence'][1]['log_url'])
        self.assertEqual(run['ai_review'], result['ai_review'])

    def test_latest_digest_mismatch_is_visible_and_does_not_show_success(self):
        self.publish()
        path = self.root / 'runs/fixture-pr-42/fixture-run-1/result.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['conclusion'] = 'success'
        path.write_text(json.dumps(data), encoding='utf-8')
        feed = self.sync()
        self.assertEqual(feed['runs'], [])
        self.assertIn('digest mismatch', feed['warnings'][0]['reason'])

    def test_traversal_in_latest_pointer_cannot_read_outside_checkout(self):
        result, pointer = self.publish()
        pointer['result_path'] = '../../private.json'
        self.write('tasks/fixture-pr-42/latest.json', pointer)
        feed = self.sync()
        self.assertFalse(feed['runs'][0]['is_latest'])
        self.assertTrue(feed['warnings'])

    def test_health_and_empty_result_feed_do_not_manufacture_passes(self):
        self.write('health/server-1.json', {'schema': 'triton-anchor-local-ci-worker-health',
                   'worker_id': 'server-1', 'heartbeat_at': 10, 'state': 'degraded',
                   'issues': [{'code': 'poller_stale', 'message': 'Poller 心跳过期'}], 'workers': []})
        feed = self.sync()
        self.assertEqual(feed['runs'], [])
        self.assertEqual(feed['workers'][0]['state'], 'degraded')
        self.assertEqual(feed['total_runs'], 0)

    def test_legacy_entry_supports_task_results_only_and_no_retired_runtime_import(self):
        self.publish()
        sync_dashboard(self.root, self.output, 'ci/push/main', 'ci/full/main')
        self.assertEqual(len(json.loads((self.output / 'local-ci.json').read_text(encoding='utf-8'))['runs']), 1)

    def test_bad_result_identity_and_unsafe_log_urls_are_not_promoted(self):
        result, pointer = self.publish()
        result['repository'] = 'different/repository'
        self.write(pointer['result_path'], result)
        (self.root / 'tasks/fixture-pr-42/latest.json').unlink()
        self.assertEqual(self.sync()['runs'], [])
        self.assertEqual(web_link('javascript:alert(1)', 'main', 'result.json'), '')


@unittest.skipUnless(NODE, 'Node.js is required to execute the dashboard renderer')
class DashboardWorkerSnapshot(unittest.TestCase):
    def test_actual_renderer_distinguishes_old_snapshot_from_fresh_offline_state(self):
        script = Path(__file__).resolve().parents[3] / 'dashboard/local-ci.js'
        harness = r'''
const fs=require('fs'),vm=require('vm');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.value='all';this.listeners={};}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  addEventListener(name,callback){this.listeners[name]=callback;}
}
const roots={},intervals=[],now=Date.parse('2026-09-08T12:00:00Z');
class Clock extends Date {static now(){return now;}}
const sandbox={document:{createElement:tag=>new Element(tag),getElementById:id=>roots[id]||(roots[id]=new Element('div'))},
  Date:Clock,URL,URLSearchParams,location:{search:''},fetch:()=>new Promise(()=>{}),
  setInterval:(callback,delay)=>intervals.push(delay)};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
const text=node=>[node.textContent,...node.children.map(text)].join(' ');
const cases=[
  {id:'old-healthy',state:'healthy',heartbeat_at:(now-16*60000)/1000},
  {id:'old-offline',state:'offline',heartbeat_at:new Date(now-16*60000).toISOString()},
  {id:'fresh-healthy',state:'healthy',heartbeat_at:(now-30000)/1000},
  {id:'fresh-offline',state:'offline',heartbeat_at:new Date(now-30000).toISOString()},
  {id:'invalid-time',state:'offline',heartbeat_at:'not-a-date'},
  {id:'boundary',state:'healthy',heartbeat_at:(now-15*60000)/1000},
  {id:'past-boundary',state:'healthy',heartbeat_at:(now-15*60000-1)/1000},
];
const rendered=cases.map(worker=>{
  sandbox.fixture={workers:[{...worker,worker_id:worker.id,workers:[],issues:[]}]};
  vm.runInContext('model.data=fixture;renderWorkers();',sandbox);
  const card=roots.workers.children[0],badge=card.children[0].children[1];
  return {id:worker.id,label:badge.textContent,tone:badge.className,text:text(card)};
});
process.stdout.write(JSON.stringify({rendered,intervals,manual_refresh:typeof roots.refresh.listeners.click==='function'}));
'''
        output = subprocess.run([NODE, '-e', harness, str(script)], check=True, capture_output=True, text=True)
        data = json.loads(output.stdout)
        rows = {row['id']: row for row in data['rendered']}
        for name in ('old-healthy', 'old-offline', 'past-boundary'):
            with self.subTest(name=name):
                self.assertEqual(rows[name]['label'], '快照已过期')
                self.assertIn('warn', rows[name]['tone'])
                self.assertIn('状态待刷新', rows[name]['text'])
                self.assertIn('不能据此判断主机当前是否离线', rows[name]['text'])
        for name in ('fresh-healthy', 'boundary'):
            self.assertEqual(rows[name]['label'], '正常')
            self.assertNotIn('状态待刷新', rows[name]['text'])
        self.assertEqual(rows['fresh-offline']['label'], '离线')
        self.assertIn('bad', rows['fresh-offline']['tone'])
        self.assertEqual(rows['invalid-time']['label'], '状态未知')
        self.assertEqual(data['intervals'], [60000])
        self.assertTrue(data['manual_refresh'])


if __name__ == '__main__':
    unittest.main()
