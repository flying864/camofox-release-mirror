import contextlib
import io
import os
import tempfile
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch
import mirror


def release(tag, **kw):
    return dict(tag_name=tag, draft=False, prerelease=False, **kw)


def http_error(code, body):
    return urllib.error.HTTPError('https://ghcr.io/test', code, 'error', Message(), io.BytesIO(body))


class MirrorTests(unittest.TestCase):
    def test_stable_numeric_filter(self):
        records = [release('v1.9.9'), release('v1.10.0'), release('camoufox-backup-999'),
                   dict(tag_name='v99.0.0', draft=True, prerelease=False),
                   dict(tag_name='v98.0.0', draft=False, prerelease=True)]
        self.assertEqual(mirror.stable(records)['tag_name'], 'v1.10.0')

    def test_empty_fails_closed(self):
        with self.assertRaises(ValueError):
            mirror.stable([])

    def test_no_change_is_silent_and_does_not_run_docker(self):
        image = {'config': {'digest': 'sha256:config'}, 'layers': [{'digest': 'sha256:layer'}]}
        index = {'manifests': [{'digest': 'sha256:child', 'platform': {'os': 'linux', 'architecture': 'amd64'}}]}
        with patch.dict(os.environ, {'PUBLISH': 'true'}), \
             patch.object(mirror, 'get', side_effect=[([release('v1.16.0')], {}), ({'token': 'test'}, {})]), \
             patch.object(mirror, 'manifest', side_effect=[(index, {}), (image, {})]), \
             patch.object(mirror, 'hub_manifest', return_value=(image, {'Docker-Content-Digest': 'sha256:child'})), \
             patch.object(mirror, 'run') as run, \
             patch.object(mirror.subprocess, 'run') as subprocess_run:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                mirror.main()
            self.assertEqual(output.getvalue(), '')
            run.assert_not_called()
            subprocess_run.assert_not_called()

    def test_missing_version_manifest_waits_without_downstream_or_docker(self):
        body = b'{"errors":[{"code":"MANIFEST_UNKNOWN","message":"missing"}]}'
        for publish in ('true', 'false'):
            with self.subTest(publish=publish), tempfile.NamedTemporaryFile(mode='r+') as summary, \
                 patch.dict(os.environ, {'PUBLISH': publish, 'GITHUB_STEP_SUMMARY': summary.name}), \
                 patch.object(mirror, 'get', side_effect=[([release('v1.16.0')], {}), ({'token': 'test'}, {})]), \
                 patch.object(mirror, 'manifest', side_effect=lambda *args: (_ for _ in ()).throw(http_error(404, body))), \
                 patch.object(mirror, 'hub_manifest') as hub_manifest, \
                 patch.object(mirror, 'run') as run, \
                 patch.object(mirror.subprocess, 'run') as subprocess_run:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    mirror.main()
                self.assertEqual(output.getvalue(), 'Official image for v1.16.0 is not available yet; no tests or publication were performed.\n')
                summary.seek(0)
                self.assertIn('no tests or publication were performed', summary.read())
                hub_manifest.assert_not_called()
                run.assert_not_called()
                subprocess_run.assert_not_called()

    def test_missing_manifest_requires_404_and_only_manifest_unknown_errors(self):
        valid = b'{"errors":[{"code":"MANIFEST_UNKNOWN"},{"code":"MANIFEST_UNKNOWN"}]}'
        self.assertTrue(mirror.missing_manifest(http_error(404, valid)))
        for code, body in (
            (401, valid),
            (403, valid),
            (429, valid),
            (500, valid),
            (502, valid),
            (503, valid),
            (504, valid),
            (404, b'not json'),
            (404, b'null'),
            (404, b'{"errors":true}'),
            (404, b'{"errors":"MANIFEST_UNKNOWN"}'),
            (404, b'{"errors":{"code":"MANIFEST_UNKNOWN"}}'),
            (404, b'{"errors":[null]}'),
            (404, b'{"errors":[]}'),
            (404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"},{"code":"DENIED"}]}'),
            (404, b'x' * 65537),
        ):
            with self.subTest(code=code, body=body[:30]):
                self.assertFalse(mirror.missing_manifest(http_error(code, body)))

    def test_child_manifest_404_still_fails(self):
        index = {'manifests': [{'digest': 'sha256:child', 'platform': {'os': 'linux', 'architecture': 'amd64'}}]}
        missing = b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}'
        with patch.object(mirror, 'get', side_effect=[([release('v1.16.0')], {}), ({'token': 'test'}, {})]), \
             patch.object(mirror, 'manifest', side_effect=[(index, {}), http_error(404, missing)]), \
             patch.object(mirror, 'hub_manifest') as hub_manifest, \
             patch.object(mirror, 'run') as run:
            with self.assertRaises(urllib.error.HTTPError):
                mirror.main()
            hub_manifest.assert_not_called()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
