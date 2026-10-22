import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from screenstudy.ai import analyze, call, explain
from screenstudy.core import Settings


class Handler(BaseHTTPRequestHandler):
    status = 200
    payload = {'message': {'content': '{"questions":[]}'}}
    captured = None
    captured_headers = None
    raw_payload = None
    extra_headers = {}
    redirect = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        Handler.captured = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Handler.captured_headers = dict(self.headers)
        self.send_response(Handler.status)
        if Handler.redirect:
            self.send_header('Location', 'http://localhost:1/leak')
        self.send_header('Content-Type', 'application/json')
        for name, value in Handler.extra_headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(Handler.raw_payload if Handler.raw_payload is not None else json.dumps(Handler.payload).encode())


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.status = 200
        Handler.redirect = False
        Handler.raw_payload = None
        Handler.extra_headers = {}
        Handler.payload = {'message': {'content': '{"questions":[]}'}}
        self.cfg = Settings(base_url=f'http://127.0.0.1:{self.server.server_port}')

    def test_local_round_trip(self):
        self.assertEqual(call(self.cfg, '', 'Q'), '{"questions":[]}')

    def test_cloud_round_trip(self):
        self.cfg.provider = 'compatible'
        Handler.payload = {'choices': [{'message': {'content': 'answer'}}]}
        self.assertEqual(call(self.cfg, 'fake-key', 'Q'), 'answer')

    def test_analyze_ocr_only_does_not_send_image(self):
        self.cfg.use_images = False
        frame = {'lines': [{'id': 0, 'text': 'Q', 'box': [0, 0, .5, .1]}], 'jpeg': b'secret-image'}
        self.assertEqual(analyze(self.cfg, '', frame), [])
        self.assertNotIn('images', Handler.captured['messages'][1])

    def test_invalid_content_rejected(self):
        Handler.payload = {'message': {'content': None}}
        with self.assertRaisesRegex(ValueError, 'пустой'):
            call(self.cfg, '', 'Q')

    def test_rate_limit_is_actionable(self):
        Handler.status = 429
        with self.assertRaisesRegex(ValueError, '429.*лимит'):
            call(self.cfg, '', 'Q')

    def test_key_not_in_error(self):
        Handler.status = 401
        with self.assertRaises(ValueError) as error:
            call(self.cfg, 'top-secret', 'Q')
        self.assertNotIn('top-secret', str(error.exception))

    def test_redirect_never_forwards(self):
        Handler.status, Handler.redirect = 307, True
        with self.assertRaisesRegex(ValueError, 'перенаправляет'):
            call(self.cfg, 'secret', 'Q')

    def test_analyze_has_small_payload_without_boxes_or_explanations(self):
        frame = {'lines': [{'id': 2, 'text': '2 + 2?', 'box': [0, 0, .5, .1]}], 'jpeg': b'x'}
        self.cfg.use_images = False
        Handler.payload = {'message': {'content': json.dumps({'questions': [
            {'question': '2 + 2?', 'answer': '4'}, {'question': 'second', 'answer': 'other'}]})}}
        questions = analyze(self.cfg, '', frame)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]['answer'], '4')
        self.assertEqual(questions[0]['explanation'], '')
        prompt = Handler.captured['messages'][1]['content']
        self.assertNotIn('box', prompt)
        self.assertIn('"id":2', prompt)

    def test_explanation_is_separate_and_uses_original_context(self):
        Handler.payload = {'message': {'content': '{"question_explanation":"сложить", "explanation":"две пары дают четыре"}'}}
        frame = {'lines': [{'text': 'original context'}], 'jpeg': b'diagram'}
        result = explain(self.cfg, '', {'question': 'Q', 'answer': 'A', 'options': []}, frame)
        self.assertIn('четыре', result['explanation'])
        self.assertIn('original context', Handler.captured['messages'][1]['content'])
        self.assertIn('images', Handler.captured['messages'][1])

    def test_incomplete_explanation_is_rejected(self):
        Handler.payload = {'message': {'content': '{}'}}
        with self.assertRaisesRegex(ValueError, 'объяснение'):
            explain(self.cfg, '', {'question': 'Q', 'answer': 'A', 'options': []})

    def test_truncated_response_is_never_displayed_as_complete(self):
        self.cfg.provider = 'compatible'
        Handler.payload = {'choices': [{'finish_reason': 'length', 'message': {'content': '{"questions":[]}'}}]}
        with self.assertRaisesRegex(ValueError, 'обрезан'):
            call(self.cfg, '', 'Q')

    def test_request_identifies_application(self):
        call(self.cfg, '', 'Q')
        self.assertTrue(Handler.captured_headers['User-Agent'].startswith('ScreenStudy/'))

    def test_model_permission_denial_keeps_specific_cause(self):
        for level in ('org', 'project'):
            with self.subTest(level=level):
                Handler.status = 403
                Handler.payload = {'error': {'message': 'This model is blocked',
                                  'code': 'model_permission_blocked_' + level}}
                with self.assertRaises(ValueError) as caught:
                    call(self.cfg, 'test-key', 'Q')
                message = str(caught.exception)
                self.assertIn('HTTP 403', message)
                self.assertIn('model_permission_blocked_' + level, message)
                self.assertIn('This model is blocked', message)

    def test_cloudflare_error_is_reported_distinctly(self):
        Handler.status = 403
        Handler.raw_payload = b'error code: 1010'
        with self.assertRaises(ValueError) as caught:
            call(self.cfg, 'key', 'Q')
        self.assertIn('Cloudflare', str(caught.exception))
        self.assertIn('[1010]', str(caught.exception))

    def test_server_echoed_credentials_are_redacted(self):
        Handler.status = 401
        Handler.payload = {'error': {'message': 'Denied opaque-key-123 Bearer another-secret gsk_privatevalue'}}
        with self.assertRaises(ValueError) as caught:
            call(self.cfg, 'opaque-key-123', 'Q')
        message = str(caught.exception)
        for secret in ('opaque-key-123', 'another-secret', 'gsk_privatevalue'):
            self.assertNotIn(secret, message)
        self.assertIn('скрыт', message)

    def test_html_block_page_is_not_dumped(self):
        Handler.status = 403
        Handler.raw_payload = b'<!DOCTYPE html><html><body>Private diagnostic <script>run()</script></body></html>'
        with self.assertRaises(ValueError) as caught:
            call(self.cfg, '', 'Q')
        message = str(caught.exception)
        self.assertNotIn('<script>', message)
        self.assertNotIn('Private diagnostic', message)
        self.assertIn('страницу отказа', message)

    def test_error_detail_is_bounded(self):
        Handler.status = 400
        Handler.payload = {'error': {'message': 'A' * 20000}}
        with self.assertRaises(ValueError) as caught:
            call(self.cfg, '', 'Q')
        self.assertLess(len(str(caught.exception)), 1000)

    def test_rate_limit_retains_retry_after(self):
        Handler.status = 429
        Handler.extra_headers = {'Retry-After': '12'}
        Handler.payload = {'error': {'message': 'Tokens per minute limit reached', 'code': 'rate_limit_exceeded'}}
        with self.assertRaises(ValueError) as caught:
            call(self.cfg, '', 'Q')
        self.assertIn('12 с', str(caught.exception))
        self.assertIn('rate_limit_exceeded', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
