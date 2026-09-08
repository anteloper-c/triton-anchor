"""Public feedback keeps execution facts and unresolved blockers distinct."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_github_result import receiver, result_fixture
from runtime.report import build_result


class PRCommentTests(unittest.TestCase):
    def test_unstarted_required_control_check_survives_the_result_to_comment_path(self):
        task, expected, seed = result_fixture()
        task['changed_paths'] = ['scripts/local_ci/runtime/engine.py']
        policy = receiver.minimum_checks(task['changed_paths'], {'triton_version': '3.0'})
        receipt = dict(id='command-1', tool='environment', returncode=0, termination=None)
        broker = SimpleNamespace(
            checks={'environment': dict(id='environment', required=True, status='passed',
                                        reason='Environment verified', evidence=['command-1'])},
            review=seed['ai_review'], receipts=[receipt], context={})
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / 'README.md').write_text('Reviewed scope\n', encoding='utf-8')
            broker.context['source_host_dir'] = temporary
            result = build_result(task, 'review-proof', policy, broker,
                                  started_at='2026-09-08T12:00:00Z', source_unchanged=True)
        self.assertEqual(receiver.validate_result(result, task, expected), 'failure')
        self.assertEqual(result['blocking_reasons'], ['control_plane: missing required check'])
        body = receiver.render_comment(result, 'https://example.test/report')
        self.assertIn('CI 流程回归检查: 必需检查尚未执行，合入前需要补齐验证', body)
        performed = body.split('**已执行的检查与审查**')[1].split('**合入阻塞与重要限制**')[0]
        self.assertNotIn('CI 流程回归检查', performed)
        self.assertNotIn('missing required check', body)

    def test_documentation_review_lists_only_performed_reviews(self):
        _, _, result = result_fixture()
        result['ai_review']['summary'] = '本次澄清纯文档的验证范围，已核对新增说明与现有规则一致。'
        body = receiver.render_comment(result, 'https://example.test/report', 'https://github.com/anteloper-c/triton-anchor/actions/runs/123')
        performed = body.split('**已执行的检查与审查**')[1].split('**合入阻塞与重要限制**')[0]
        self.assertEqual(performed.count('- **'), 2)
        self.assertIn('PR 说明与改动一致性：通过', performed)
        self.assertIn('架构与接口约束审查：通过', performed)
        self.assertIn('本次澄清纯文档的验证范围', body)
        self.assertIn('不包含编译器构建与运行行为验证', body)
        self.assertIn(f"/blob/{result['tested_sha']}/README.md", body)
        self.assertIn('[完整执行报告（需要访问权限）](https://example.test/report)', body)
        self.assertIn('[查看 GitHub 检查记录](https://github.com/anteloper-c/triton-anchor/actions/runs/123)', body)
        for internal in (*receiver.TOOLS, 'skipped', 'not_applicable', 'success', 'Task ', '被测提交'):
            self.assertNotIn(internal, body)
        self.assertNotIn('| --- |', body)

    def test_partial_build_is_listed_but_unexecuted_required_install_blocks(self):
        _, _, result = result_fixture()
        result['conclusion'] = 'failure'
        build = next(c for c in result['checks'] if c['id'] == 'frontend_build')
        build.update(status='failed', required=True, evidence=['command-1'], reason='C++ 编译器报告类型不匹配。')
        install = next(c for c in result['checks'] if c['id'] == 'frontend_install')
        install.update(required=True, reason='required check was not completed')
        result['evidence'] = [{'id': 'command-1', 'tool': 'frontend_build', 'returncode': 1}]
        result['blocking_reasons'] = ['frontend_build: command-1', 'frontend_install: required check was not completed']
        result['ai_review']['uncompleted'] = [{'summary': '安装依赖本次构建产物，编译失败后尚未验证导入行为。'}]
        body = receiver.render_comment(result, 'https://example.test/report')
        performed, blockers = body.split('**已执行的检查与审查**')[1].split('**合入阻塞与重要限制**')
        self.assertIn('前端构建：未通过', performed)
        self.assertNotIn('前端安装', performed)
        self.assertIn('前端安装与导入验证尚未通过', blockers)
        self.assertIn('C++ 编译器报告类型不匹配', blockers)
        self.assertIn('尚未验证导入行为', blockers)
        self.assertNotIn('检查已通过', body)
        self.assertNotIn('frontend_install', body)

    def test_failed_review_and_capacity_interruption_cannot_read_as_success(self):
        _, _, result = result_fixture()
        result.update(conclusion='error', ai_review={}, blocking_reasons=['Codex service error: capacity exhausted after retry'])
        result['checks'] = [dict(id='architecture_review', status='error', required=True,
                                reason='no valid architecture review', evidence=[])]
        body = receiver.render_comment(result, 'https://example.test/report')
        self.assertIn('尚无可核对的检查或审查记录', body)
        self.assertIn('审查服务暂时繁忙', body)
        self.assertIn('架构审查尚未完成', body)
        self.assertNotIn('：通过', body)
        self.assertNotIn('capacity', body)

    def test_code_findings_keep_evidence_and_unconfirmed_attribution(self):
        _, _, result = result_fixture()
        result['ai_review']['findings'] = [{
            'summary': '默认实例可能继承前一实例的允许列表。', 'blocking': False,
            'caused_by_change': False,
            'code_evidence': [{'path': 'python/triton_anchor/anchor_ir.py', 'line': 44,
                               'reason': '这里创建验证器的允许列表。'}],
        }]
        before = copy.deepcopy(result)
        body = receiver.render_comment(result, 'https://example.test/report')
        self.assertIn('默认实例可能继承', body)
        self.assertIn('anchor\\_ir.py:44', body)
        self.assertIn('#L44', body)
        self.assertIn('尚未确认由本次改动引入', body)
        self.assertEqual(result, before)

    def test_legacy_codes_and_markup_do_not_leak_into_public_prose(self):
        text = receiver.comment_text('context.changed_paths 与冻结 diff 一致。预检 pr_information/basic/api/security 均为 success。'
                                     '<script>alert(1)</script> @reviewer [click](https://evil.test) command-0003 ' + 'a' * 40)
        for hidden in ('context', 'pr_information', 'success', 'command-0003', 'a' * 40, '<script>', '@reviewer', '[click]'):
            self.assertNotIn(hidden, text)
        self.assertIn('基础检查、接口兼容性与安全检查', text)
        self.assertIn('均为通过', text)

    def test_environment_only_does_not_claim_compiler_verification(self):
        _, _, result = result_fixture()
        check = next(c for c in result['checks'] if c['id'] == 'environment')
        check.update(status='passed', evidence=['command-1'])
        result['evidence'] = [{'id': 'command-1', 'tool': 'environment', 'returncode': 0}]
        body = receiver.render_comment(result, 'https://example.test/report')
        self.assertIn('构建环境与依赖检查：通过', body)
        self.assertIn('不包含编译器构建与运行行为验证', body)

    def test_code_link_keeps_the_exact_filename(self):
        _, _, result = result_fixture()
        text = receiver.comment_evidence([{'path': 'scripts/local_ci/runtime/environment.py', 'line': 2}], result)
        self.assertIn('runtime/environment.py:2]', text)
        self.assertNotIn('构建环境', text)

    def test_compound_tool_names_are_not_partially_translated(self):
        for name, label in receiver.CHECK_NAMES.items():
            with self.subTest(name=name):
                self.assertEqual(receiver.comment_text(name), label)

if __name__ == '__main__':
    unittest.main()
