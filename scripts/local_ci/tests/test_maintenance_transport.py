"""Host-only credential flow and actual local Git artifact publication contracts."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.local_ci.maintenance.transport import git_environment, validate_relay_url
from scripts.local_ci.maintenance.workers import atomic_json
from scripts.local_ci.control.runtime.common import digest
from scripts.local_ci.control.runtime.relay import Relay
from scripts.local_ci.control.runtime.result_paths import run_relative


class GitCredentialEnvironment(unittest.TestCase):
    def test_credentials_are_scoped_env_and_trace_overrides_are_removed(self):
        config = {'url': 'https://gitee.com/heron-mc/fixture-repo.git',
                  'username_env': 'CI_FIXTURE_USERNAME', 'token_env': 'CI_FIXTURE_TOKEN'}
        with patch.dict(os.environ, {'CI_FIXTURE_USERNAME': 'fixture-user', 'CI_FIXTURE_TOKEN': 'fixture-secret',
                                     'GIT_TRACE_CURL': 'sensitive.log', 'GIT_DIR': '/unexpected', 'GIT_CONFIG_PARAMETERS': 'bad'}):
            env = git_environment(config)
        self.assertNotIn('GIT_TRACE_CURL', env)
        self.assertNotIn('GIT_DIR', env)
        self.assertNotIn('GIT_CONFIG_PARAMETERS', env)
        self.assertEqual(env['GIT_CONFIG_KEY_1'], 'http.' + config['url'] + '.extraheader')
        self.assertEqual(env['GIT_CONFIG_VALUE_1'], 'Authorization: Basic ' + base64.b64encode(b'fixture-user:fixture-secret').decode())
        # Use actual Git URL matching without contacting a network server.
        matched = subprocess.run(['git', 'config', '--get-urlmatch', 'http.extraheader', config['url'] + '/info/refs'],
                                 env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(matched.returncode, 0)
        self.assertEqual(matched.stdout.strip(), env['GIT_CONFIG_VALUE_1'])
        other = subprocess.run(['git', 'config', '--get-urlmatch', 'http.extraheader', 'https://gitee.com/other/repo.git'],
                               env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(other.stdout.strip(), '')

    def test_rejects_embedded_credentials_and_protected_repository_case_variants(self):
        for url in ('https://user:token@gitee.com/heron-mc/repo.git',
                    'https://github.com/race-ORG/TRITON-anchor.git', 'http://gitee.com/heron-mc/repo.git'):
            with self.assertRaises(ValueError):
                validate_relay_url(url)


class RealArtifactPublication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'relay.git'
        subprocess.run(['git', 'init', '--bare', str(self.remote)], check=True, capture_output=True)
        self.relay = Relay({'url': str(self.remote)}, self.root / 'state')
        self.output = self.root / 'run'
        self.output.mkdir()
        self.result = {'task_id': 'task-1', 'run_id': 'run-1', 'evidence': [],
                       'event_kind': 'push', 'pr_number': 0, 'target_branch': 'main', 'task_ref': 'ci/push/main'}
        atomic_json(self.output / 'result.json', self.result)

    def git_show(self, relative):
        result = subprocess.run(['git', '-C', str(self.remote), 'show', 'local-ci-results:' + relative],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def artifact(self):
        path = self.output / 'artifacts/custom/repro.py'
        path.parent.mkdir(parents=True)
        path.write_text('assert 1 + 1 == 2\n', encoding='utf-8')
        entry = {'path': 'artifacts/custom/repro.py', 'sha256': digest(path), 'size': path.stat().st_size}
        atomic_json(self.output / 'artifact-manifest.json',
                    {'schema': 'triton-anchor-local-ci-artifacts', 'files': [entry], 'omitted': []})
        return path, entry

    def test_custom_evidence_and_manifest_are_published_and_immutable(self):
        path, entry = self.artifact()
        relative = self.relay.publish(self.output, self.result)
        manifest = json.loads(self.git_show(relative + '/publish-manifest.json'))
        self.assertIn(entry, manifest['files'])
        self.assertIn('artifact-manifest.json', {item['path'] for item in manifest['files']})
        self.assertEqual(self.git_show(relative + '/artifacts/custom/repro.py'), path.read_text())
        self.relay.publish(self.output, self.result)
        path.write_text('changed after publication\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'digest/size changed'):
            self.relay.publish(self.output, self.result)

    def test_manifest_path_traversal_is_rejected(self):
        self.artifact()
        manifest = json.loads((self.output / 'artifact-manifest.json').read_text())
        manifest['files'][0]['path'] = 'artifacts/../../private.txt'
        atomic_json(self.output / 'artifact-manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.relay.publish(self.output, self.result)

    def test_oversized_log_blocks_publication_without_silent_omission(self):
        log = self.output / 'logs/command-0001.log'
        log.parent.mkdir()
        with log.open('wb') as stream:
            stream.truncate(20 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, 'retained locally'):
            self.relay.publish(self.output, self.result)
        self.assertTrue(log.exists())

    def test_client_hook_policy_does_not_disable_local_receive_protection(self):
        self.relay.publish(self.output, self.result)
        hook = self.remote / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8', newline='\n')
        hook.chmod(0o755)
        self.result['run_id'] = 'run-rejected'
        atomic_json(self.output / 'result.json', self.result)
        with self.assertRaisesRegex(RuntimeError, 'publication failed'):
            self.relay.publish(self.output, self.result)
        self.assertTrue((self.output / 'result.json').is_file())
        hook.unlink()
        self.relay.publish(self.output, self.result)
        self.assertEqual(json.loads(self.git_show(run_relative(self.result, 'run-rejected') + '/result.json')), self.result)

    def test_relay_subprocess_uses_env_and_redacts_server_error(self):
        self.relay.config = {'url': 'https://gitee.com/heron-mc/fixture-repo.git',
                             'username_env': 'CI_FIXTURE_USERNAME', 'token_env': 'CI_FIXTURE_TOKEN'}
        self.relay.url = self.relay.config['url']
        captured = []
        def fail(argv, **kwargs):
            captured.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 128, '', 'fixture-secret should not appear in error')
        with patch.dict(os.environ, {'CI_FIXTURE_USERNAME': 'fixture-user', 'CI_FIXTURE_TOKEN': 'fixture-secret'}):
            with patch('scripts.local_ci.control.runtime.relay.subprocess.run', side_effect=fail):
                with self.assertRaises(RuntimeError) as failure:
                    self.relay.fetch()
        self.assertNotIn('fixture-secret', str(failure.exception))
        self.assertNotIn('fixture-secret', ' '.join(captured[0][0]))
        self.assertFalse(any('core.hooksPath' in item for item in captured[0][0]))
        self.assertIn('Authorization: Basic ', captured[0][1]['env']['GIT_CONFIG_VALUE_1'])


if __name__ == '__main__':
    unittest.main()
