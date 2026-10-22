import json
import tempfile
import unittest
from pathlib import Path

from screenstudy.ai import make_request
from screenstudy.core import GROQ_MODEL, GROQ_URL, Settings, SceneGate, json_object, parse_answer, union_box, valid_box


class CoreTests(unittest.TestCase):
    def test_region_validation(self):
        self.assertTrue(valid_box([0, 0, 1, 1]))
        for box in ([0, 0, 0, 1], [-.1, 0, 1, 1], [0, .9, 1, .2],
                    [0, 0, float('nan'), 1], [True, 0, 1, 1], [0, 0, 1]):
            with self.subTest(box=box):
                self.assertFalse(valid_box(box))

    def test_fenced_json_and_thinking_prefix(self):
        self.assertEqual(json_object('<think>test</think>\n```json\n{"questions":[]}\n```'), {'questions': []})

    def test_no_eval(self):
        with self.assertRaises(ValueError):
            json_object("__import__('os').system('false')")

    def test_bad_schema(self):
        for data in ('{}', '{"questions":null}', '{"questions":[{}]}'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_answer(data, [])

    def test_empty_screen_valid(self):
        self.assertEqual(parse_answer('{"questions":[]}', []), [])

    def test_untrusted_line_ids_and_booleans(self):
        raw = {'questions': [{'question': 'Q', 'line_ids': [0, 0, 99, True],
                            'options': [{'text': 'A', 'correct': 'true', 'line_ids': [2, '3']},
                                        {'text': 'B', 'correct': True, 'line_ids': [3]}]}]}
        q = parse_answer(json.dumps(raw), [0, 2, 3])[0]
        self.assertEqual(q['line_ids'], [0])
        self.assertFalse(q['options'][0]['correct'])
        self.assertTrue(q['options'][1]['correct'])
        self.assertEqual(q['options'][0]['line_ids'], [2])

    def test_single_choice_contradiction_not_marked(self):
        raw = {'questions': [{'question': 'Q', 'kind': 'single', 'confidence': 'high',
                'options': [{'text': 'A', 'correct': True}, {'text': 'B', 'correct': True}]}]}
        q = parse_answer(json.dumps(raw), [])[0]
        self.assertEqual(q['confidence'], 'low')
        self.assertFalse(any(o['correct'] for o in q['options']))

    def test_multiple_choice_keeps_two_correct(self):
        raw = {'questions': [{'question': 'Q', 'kind': 'multiple',
                'options': [{'text': 'A', 'correct': True}, {'text': 'B', 'correct': True}]}]}
        self.assertTrue(all(o['correct'] for o in parse_answer(json.dumps(raw), [])[0]['options']))

    def test_union_coordinate_mapping(self):
        lines = [{'id': 1, 'box': [.1, .2, .3, .1]}, {'id': 2, 'box': [.2, .4, .4, .1]}]
        result = union_box([1, 2], lines)
        for actual, expected in zip(result, [.1, .2, .5, .3]):
            self.assertAlmostEqual(actual, expected)
        self.assertIsNone(union_box([9], lines))

    def test_requires_stable_scene_and_deduplicates(self):
        gate = SceneGate()
        gate.observe('a', now=0)
        self.assertFalse(gate.ready())
        gate.observe('a', now=.4)
        self.assertFalse(gate.ready())
        gate.observe('a', now=.8)
        self.assertTrue(gate.ready())
        rev = gate.mark_sent()
        self.assertFalse(gate.ready())
        gate.observe('a', now=1.2)
        self.assertTrue(gate.accepts(rev))
        self.assertFalse(gate.ready())

    def test_change_and_pause_reject_delayed_answer(self):
        gate = SceneGate()
        gate.force('a')
        rev = gate.mark_sent()
        gate.observe('b', now=0)
        self.assertTrue(gate.accepts(rev))
        gate.observe('b', now=.4)
        gate.observe('b', now=.8)
        self.assertFalse(gate.accepts(rev))
        rev = gate.mark_sent()
        gate.reset()
        self.assertFalse(gate.accepts(rev))

    def test_return_to_previous_screen_not_lost(self):
        gate = SceneGate()
        gate.force('a'); gate.mark_sent()
        gate.observe('b', now=0); gate.observe('b', now=.4); gate.observe('b', now=.8)
        gate.observe('a', now=1); gate.observe('a', now=1.4); gate.observe('a', now=1.8)
        self.assertTrue(gate.ready())

    def test_transient_tooltip_does_not_invalidate_or_resend(self):
        gate = SceneGate()
        gate.force('question')
        rev = gate.mark_sent()
        gate.observe('tooltip', now=0)
        self.assertTrue(gate.pending)
        self.assertFalse(gate.ready())
        gate.observe('question', now=.4)
        self.assertFalse(gate.pending)
        self.assertTrue(gate.accepts(rev))
        self.assertFalse(gate.ready())

    def test_many_frames_without_settling_time_do_not_commit(self):
        gate = SceneGate()
        gate.force('old')
        for now in (0, .01, .02, .03):
            gate.observe('new', now=now)
        self.assertEqual(gate.fingerprint, 'old')
        self.assertFalse(gate.ready())
        gate.observe('new', now=.6)
        self.assertTrue(gate.ready())

    def test_settings_no_keys_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'cfg.json'
            cfg = Settings()
            cfg.save(path)
            self.assertEqual(Settings.load(path), cfg)
            self.assertNotIn('api_key', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_settings_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'cfg.json'
            path.write_text('{broken')
            self.assertEqual(Settings.load(path), Settings())

    def test_url_boundary(self):
        for url in ('http://remote.test/v1', 'file:///tmp', 'https://user:pass@api.test',
                    'https://api.test/v1?key=secret', 'https://api.test/v1#fragment'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Settings(provider='compatible', base_url=url).validate()
        Settings(provider='compatible', base_url='https://api.test/v1').validate()
        Settings(provider='compatible', base_url='http://localhost:1234/v1').validate()
        with self.assertRaises(ValueError):
            Settings(provider='ollama', base_url='https://remote.test').validate()

    def test_ollama_request(self):
        req = make_request(Settings(), '', 'Q', b'image')
        body = json.loads(req.data)
        self.assertEqual(req.full_url, 'http://127.0.0.1:11434/api/chat')
        self.assertEqual(body['format'], 'json')
        self.assertFalse(body['stream'])
        self.assertEqual(body['messages'][1]['images'], ['aW1hZ2U='])

    def test_cloud_request_and_ocr_only(self):
        cfg = Settings(provider='compatible', base_url='https://api.test/v1/')
        req = make_request(cfg, 'secret', 'question')
        self.assertEqual(req.full_url, 'https://api.test/v1/chat/completions')
        self.assertEqual(req.get_header('Authorization'), 'Bearer secret')
        self.assertEqual(json.loads(req.data)['messages'][1]['content'], 'question')
        self.assertNotIn('images', req.data.decode())

    def test_cloud_vision(self):
        cfg = Settings(provider='compatible', base_url='https://api.test/v1')
        body = json.loads(make_request(cfg, '', 'Q', b'x').data)
        self.assertTrue(body['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,'))

    def test_rewrite_is_not_forced_json(self):
        body = json.loads(make_request(Settings(), '', 'Q', structured=False).data)
        self.assertNotIn('format', body)

    def test_groq_fast_mode_disables_reasoning_and_requests_json(self):
        cfg = Settings(provider='groq', base_url=GROQ_URL, model=GROQ_MODEL)
        req = make_request(cfg, 'test-key', 'Q')
        body = json.loads(req.data)
        self.assertEqual(req.full_url, GROQ_URL + '/chat/completions')
        self.assertEqual(body['reasoning_effort'], 'none')
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertNotIn('explanation', body['messages'][0]['content'])

    def test_groq_careful_and_plain_text_modes(self):
        cfg = Settings(provider='groq', base_url=GROQ_URL, model=GROQ_MODEL, fast_mode=False)
        body = json.loads(make_request(cfg, 'key', 'Q', structured=False).data)
        self.assertEqual(body['reasoning_effort'], 'default')
        self.assertNotIn('response_format', body)

    def test_groq_requires_key_and_fixed_destination(self):
        cfg = Settings(provider='groq', base_url=GROQ_URL, model=GROQ_MODEL)
        with self.assertRaisesRegex(ValueError, 'ключ'):
            make_request(cfg, '', 'Q')
        cfg.base_url = 'https://another.test'
        with self.assertRaises(ValueError):
            make_request(cfg, 'key', 'Q')

    def test_original_config_migrates_without_losing_local_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'cfg.json'
            path.write_text(json.dumps({'provider': 'ollama', 'interval': 6, 'crop': [.1, .1, .8, .8]}))
            cfg = Settings.load(path)
            self.assertEqual(cfg.provider, 'ollama')
            self.assertEqual(cfg.interval, 6)
            self.assertEqual(cfg.crop, [.1, .1, .8, .8])
            self.assertEqual(cfg.max_questions, 1)


if __name__ == '__main__':
    unittest.main()
