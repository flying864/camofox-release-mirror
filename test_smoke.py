import unittest
import mirror


class SmokeTests(unittest.TestCase):
    def test_controlled_page_content_not_third_party_heading(self):
        token = 'a' * 32
        url = 'http://127.0.0.1:9380/probe/' + token
        snapshot = {'url': url, 'snapshot': '- heading "Camofox mirror probe"\n- paragraph: ' + token + '\n- link "Probe link"'}
        mirror.validate_snapshot(snapshot, url, token)

    def test_wrong_blank_or_error_page_fails(self):
        token = 'b' * 32
        url = 'http://127.0.0.1:9380/probe/' + token
        good = 'Camofox mirror probe ' + token + ' Probe link'
        for response in [
            {}, {'url': url, 'snapshot': ''},
            {'url': url, 'snapshot': 'Error: service unavailable'},
            {'url': url, 'snapshot': 'Camofox mirror probe stale-token Probe link'},
            {'url': url + '/redirect', 'snapshot': good},
            {'url': 'https://example.com', 'snapshot': good},
            {'url': url, 'snapshot': None},
        ]:
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                mirror.validate_snapshot(response, url, token)
