import contextlib
import io
import os
import unittest
from unittest.mock import patch
import mirror


def release(tag, **kw):
    return dict(tag_name=tag, draft=False, prerelease=False, **kw)


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


if __name__ == '__main__':
    unittest.main()
