import ast
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import mirror


class SafetyTests(unittest.TestCase):
    def test_runtime_checks_are_not_optimizable_asserts(self):
        tree = ast.parse(Path(mirror.__file__).read_text())
        self.assertEqual([n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)], [])

    def test_guard_survives_optimized_python(self):
        result = subprocess.run([sys.executable, '-B', '-O', '-c',
            'import mirror; mirror.require(False, "blocked")'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('RuntimeError: blocked', result.stderr)

    def test_credentials_are_temporary_and_removed_on_copy_failure(self):
        paths = []
        def fake_run(*args):
            if args[:2] == ('skopeo', 'copy'):
                auth = Path(args[args.index('--authfile') + 1])
                paths.append(auth.parent)
                self.assertTrue(auth.parent.is_dir())
                self.assertEqual(auth.parent.stat().st_mode & 0o777, 0o700)
                raise RuntimeError('copy failed')
        with patch.object(mirror.shutil, 'which', return_value='/bin/skopeo'), \
             patch.object(mirror.subprocess, 'run') as login, \
             patch.object(mirror, 'run', side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, 'copy failed'):
                mirror.publish_image('ghcr.io/example@sha256:test', 'test-only-password')
        self.assertTrue(paths)
        self.assertFalse(paths[0].exists())
        self.assertEqual(login.call_args.kwargs['input'], 'test-only-password')

    def test_no_change_summary_without_stdout_or_docker(self):
        import tempfile
        from test_mirror import release
        image = {'config': {'digest': 'sha256:config'}, 'layers': []}
        index = {'manifests': [{'digest': 'sha256:child', 'platform': {'os': 'linux', 'architecture': 'amd64'}}]}
        with tempfile.TemporaryDirectory() as d:
            summary = Path(d) / 'summary'
            with patch.dict(os.environ, {'PUBLISH': 'true', 'GITHUB_STEP_SUMMARY': str(summary)}), \
                 patch.object(mirror, 'get', side_effect=[([release('v1.18.0')], {}), ({'token': 'test'}, {})]), \
                 patch.object(mirror, 'manifest', side_effect=[(index, {}), (image, {})]), \
                 patch.object(mirror, 'hub_manifest', return_value=(image, {'Docker-Content-Digest': 'sha256:child'})), \
                 patch.object(mirror, 'run') as run, \
                 patch('sys.stdout', new_callable=io.StringIO) as stdout:
                mirror.main()
            self.assertEqual(stdout.getvalue(), '')
            run.assert_not_called()
            self.assertIn('unchanged', summary.read_text())

    def test_cleanup_preserves_original_error(self):
        with patch('sys.stderr', new_callable=io.StringIO) as errors:
            with self.assertRaisesRegex(RuntimeError, 'original probe error'):
                with mirror.cleanup_on_exit(lambda: (_ for _ in ()).throw(RuntimeError('cleanup failed')), 'probe'):
                    raise RuntimeError('original probe error')
        self.assertIn('cleanup', errors.getvalue())

    def test_publication_credential_cleanup_success_and_login_failure(self):
        for fails in (False, True):
            dirs = []
            def login(*args, **kwargs):
                dirs.append(Path(kwargs['env']['DOCKER_CONFIG']))
                if fails:
                    raise RuntimeError('login failed')
            with self.subTest(fails=fails), patch.object(mirror.shutil, 'which', return_value='/bin/skopeo'), \
                 patch.object(mirror.subprocess, 'run', side_effect=login), patch.object(mirror, 'run') as command:
                if fails:
                    with self.assertRaisesRegex(RuntimeError, 'login failed'):
                        mirror.publish_image('test-source', 'test-only-password')
                    command.assert_not_called()
                else:
                    mirror.publish_image('test-source', 'test-only-password')
                    self.assertEqual(command.call_args.args[:2], ('skopeo', 'copy'))
                self.assertTrue(dirs)
                self.assertFalse(dirs[0].exists())

    def test_real_main_rejects_image_health_version_and_cleanup_failures(self):
        import json
        from test_mirror import release
        image = {'config': {'digest': 'sha256:config'}, 'layers': []}
        index = {'manifests': [{'digest': 'sha256:child', 'platform': {'os': 'linux', 'architecture': 'amd64'}}]}
        for failure, message in [('architecture', 'platform'), ('config', 'digest'), ('health', 'Health'),
                                 ('version', 'version'), ('cleanup', 'cleanup')]:
            def command(*args):
                if args[:3] == ('docker', 'image', 'inspect'):
                    return json.dumps([{'Architecture': 'arm64' if failure == 'architecture' else 'amd64',
                        'Os': 'linux', 'Id': 'wrong' if failure == 'config' else 'sha256:config'}])
                if args[:2] == ('docker', 'port'):
                    return '127.0.0.1:32123'
                return ''
            responses = [([release('v1.18.0')], {}), ({'token': 'test'}, {}),
                         ({'ok': failure != 'health'}, {}),
                         ({'info': {'version': 'wrong' if failure == 'version' else '1.18.0'}}, {})]
            def browser(req, **kwargs):
                url = req.full_url
                if req.get_method() == 'DELETE': value = {}
                elif '/snapshot?' in url:
                    nonce = url.split('userId=probe-')[1]
                    value = {'url': 'http://127.0.0.1:9380/probe/' + nonce,
                             'snapshot': 'Camofox mirror probe ' + nonce + ' Probe link'}
                elif req.get_method() == 'POST': value = {'tabId': 'tab'}
                else: value = {'tabs': ['leaked']}
                return io.BytesIO(json.dumps(value).encode())
            with self.subTest(failure=failure), patch.dict(os.environ, {'PUBLISH': 'true'}), \
                 patch.object(mirror, 'get', side_effect=responses), \
                 patch.object(mirror, 'manifest', side_effect=[(index, {}), (image, {})]), \
                 patch.object(mirror, 'hub_manifest', return_value=(image, {'Docker-Content-Digest': 'sha256:old'})), \
                 patch.object(mirror, 'run', side_effect=command), \
                 patch.object(mirror.time, 'monotonic', side_effect=[0, 200]), \
                 patch.object(mirror.urllib.request, 'urlopen', side_effect=browser), \
                 patch.object(mirror, 'publish_image') as publish, \
                 patch('sys.stdout', new_callable=io.StringIO):
                with self.assertRaisesRegex(RuntimeError, message):
                    mirror.main()
                publish.assert_not_called()

    def test_workflow_secret_is_scoped_to_main_publication_step(self):
        text = Path('.github/workflows/mirror.yml').read_text()
        test_step = text.split('      - name: Validate official release without publication credentials')[1].split('      - name:')[0]
        publish_step = text.split('      - name: Validate official release and publish if changed')[1]
        self.assertNotIn('DOCKERHUB_TOKEN', test_step)
        self.assertIn("github.event_name != 'schedule' && !inputs.publish", test_step)
        self.assertEqual(text.count('${{ secrets.DOCKERHUB_TOKEN }}'), 1)
        self.assertIn("github.ref == 'refs/heads/main' && (github.event_name == 'schedule' || inputs.publish)", publish_step)
        self.assertIn("inputs.publish && github.ref != 'refs/heads/main'", text)
        self.assertIn('python3 -B -O -m unittest -q', text)

    def test_cleanup_error_alone_blocks_publication(self):
        with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
            with mirror.cleanup_on_exit(lambda: (_ for _ in ()).throw(RuntimeError('cleanup failed')), 'probe'):
                pass
