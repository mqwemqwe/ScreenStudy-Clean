"""Structured response fixtures based on the two supplied task layouts.

These test the contract and UI, not a live model's history knowledge or native OCR.
"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QLabel

from screenstudy import ai
from screenstudy.core import Settings, answer_marks, parse_answer, phrase_lines
from screenstudy.ui import MainWindow


def row(i, value, x=.05, y=None):
    return {'id': i, 'text': value, 'box': [x, y if y is not None else .03 + i * .04, .4, .025]}


MATCH_LINES = [
    row(0, 'Выберите ключевое новшество. Перенести его к нужному источнику.'),
    row(1, 'Правда Ярослава', y=.25),
    row(2, 'Устав Владимира Мономаха', y=.45),
    row(3, 'Правда Ярославичей', y=.70),
    row(4, 'Правила закабаления в холопы, уточнение положения закупа', x=.6, y=.25),
    row(5, 'Запрет кровной мести, замена на виру (штраф)', x=.6, y=.50),
    row(6, 'Ограничивало кровную месть кругом ближайших родственников', x=.6, y=.75),
]
MATCH = {'question': 'Сопоставьте источники и новшества', 'kind': 'matching',
         'confidence': 'high', 'answer': '', 'line_ids': [0], 'matches': [
    {'source': MATCH_LINES[s]['text'], 'target': MATCH_LINES[t]['text'],
     'source_line_ids': [s], 'target_line_ids': [t]} for s, t in [(6, 1), (4, 2), (5, 3)]
]}
HIGHLIGHT_LINES = [
    row(0, 'Выделите ключевые слова/словосочетания, обосновывающие выбор в предыдущем вопросе'),
    row(1, '1) Регент при сыне Рюрика в Новгороде. По преданию, умер от укуса змеи'),
    row(2, '2) По приглашению сел княжить в Новгороде и стал основателем правящей династии'),
    row(3, '3) Его дружины обрушились на Хазарию. Из черепа князя печенежский хан велел изготовить чашу'),
    row(4, '4) Получил в жены принцессу Анну и крестился в Херсонесе (Корсунь). Вернувшись в Киев, крестил киевлян'),
    row(5, '5) Флот русов был сожжен «греческим огнем». Во время полюдья в землях древлян был убит'),
    row(6, '6) Были введены уроки, определявшие размеры дани, и установлены места, куда она свозилась, — погосты.'),
]
PHRASES = [
    (1, 'Олег', 'Регент при сыне Рюрика'), (1, 'Олег', 'умер от укуса змеи'),
    (2, 'Рюрик', 'По приглашению'), (2, 'Рюрик', 'основателем правящей династии'),
    (3, 'Святослав', 'обрушились на Хазарию'), (3, 'Святослав', 'изготовить чашу'),
    (4, 'Владимир', 'крестился в Херсонесе'), (4, 'Владимир', 'крестил киевлян'),
    (5, 'Игорь', '«греческим огнем»'), (5, 'Игорь', 'Во время полюдья'),
    (6, 'Ольга', 'уроки'), (6, 'Ольга', 'погосты'),
]
HIGHLIGHT = {'question': 'Выделите ключевые слова в шести описаниях', 'kind': 'highlight',
             'confidence': 'high', 'line_ids': [0], 'highlights': [
    {'text': text, 'group': f'{i}) {name}', 'line_ids': [i]} for i, name, text in PHRASES
]}


def parsed(question, lines):
    return parse_answer(json.dumps({'questions': [question]}), [l['id'] for l in lines], lines)[0]


class TaskContractTests(unittest.TestCase):
    def test_three_pairs_direction_and_numbered_anchors(self):
        q = parsed(MATCH, MATCH_LINES)
        self.assertEqual(len(q['matches']), 3)
        self.assertEqual(q['confidence'], 'high')
        self.assertIn('Ограничивало кровную месть кругом ближайших родственников → Правда Ярослава', q['answer'])
        self.assertEqual([m['text'] for m in answer_marks(q, MATCH_LINES)], ['1', '1', '2', '2', '3', '3'])

    def test_all_six_paragraphs_keep_exact_quotes_and_share_row_markers(self):
        q = parsed(HIGHLIGHT, HIGHLIGHT_LINES)
        self.assertEqual(len(q['highlights']), 12)
        self.assertEqual(len({h['group'] for h in q['highlights']}), 6)
        self.assertEqual([h['text'] for h in q['highlights']], [x[2] for x in PHRASES])
        self.assertEqual([m['text'] for m in answer_marks(q, HIGHLIGHT_LINES)],
                         ['1,2', '3,4', '5,6', '7,8', '9,10', '11,12'])

    def test_invented_highlight_is_removed_and_reported(self):
        q = copy.deepcopy(HIGHLIGHT)
        q['highlights'].append({'text': 'совсем другая фраза', 'line_ids': [1]})
        result = parsed(q, HIGHLIGHT_LINES)
        self.assertEqual(len(result['highlights']), 12)
        self.assertEqual(result['confidence'], 'low')
        self.assertNotIn('совсем другая фраза', result['answer'])
        self.assertTrue(result['notices'])

    def test_wrong_anchor_is_repaired_only_by_literal_match(self):
        q = copy.deepcopy(MATCH)
        q['matches'][0]['source_line_ids'] = [True, 999, 1]
        self.assertEqual(parsed(q, MATCH_LINES)['matches'][0]['source_line_ids'], [6])

    def test_missing_matching_label_has_no_anchor_and_low_confidence(self):
        q = copy.deepcopy(MATCH)
        q['matches'][0]['target'] = 'Невидимый источник'
        q['matches'][0]['target_line_ids'] = [1]
        result = parsed(q, MATCH_LINES)
        self.assertEqual(result['matches'][0]['target_line_ids'], [])
        self.assertEqual(result['confidence'], 'low')

    def test_duplicates_never_get_guessed_positions(self):
        rows = [row(1, 'уроки'), row(2, 'уроки')]
        self.assertEqual(phrase_lines('уроки', rows), ([], True))
        self.assertEqual(phrase_lines('уроки', rows, [2]), ([2], True))
        self.assertEqual(phrase_lines('уроки', [row(1, 'уроки и уроки')]), ([], True))

    def test_line_wrap_and_unicode_normalization(self):
        rows = [row(1, 'правящей'), row(2, 'династии')]
        self.assertEqual(phrase_lines('правящей\nдинастии', rows), ([1, 2], True))
        self.assertEqual(phrase_lines('й', [row(1, 'и\u0306')]), ([1], True))

    def test_subword_not_a_selectable_quote(self):
        self.assertEqual(phrase_lines('князь', [row(1, 'князья')]), ([], False))

    def test_preferred_ids_cannot_join_non_contiguous_fragments(self):
        rows = [row(1, 'выбранная'), row(2, 'совсем другая строка'), row(3, 'фраза')]
        self.assertEqual(phrase_lines('выбранная фраза', rows, [1, 3]), ([], False))

    def test_conflicting_assignments_are_hidden_but_many_to_one_is_allowed(self):
        q = copy.deepcopy(MATCH)
        q['matches'].append({**q['matches'][0], 'target': MATCH_LINES[2]['text'], 'target_line_ids': [2]})
        result = parsed(q, MATCH_LINES)
        self.assertEqual(len(result['matches']), 2)
        self.assertEqual(result['confidence'], 'low')
        q = copy.deepcopy(MATCH)
        q['matches'][1]['target'] = MATCH_LINES[1]['text']
        q['matches'][1]['target_line_ids'] = [1]
        self.assertEqual(len(parsed(q, MATCH_LINES)['matches']), 3)

    def test_empty_and_malformed_structures_do_not_claim_success(self):
        for kind, key in [('matching', 'matches'), ('highlight', 'highlights')]:
            for value in (None, 'wrong', [None, {}, 3]):
                with self.subTest(kind=kind, value=value):
                    q = parsed({'question': 'Q', 'kind': kind, 'confidence': 'high', key: value}, MATCH_LINES)
                    self.assertEqual(q[key], [])
                    self.assertEqual(q['confidence'], 'low')
                    self.assertTrue(q['answer'])
                    self.assertEqual(answer_marks(q, MATCH_LINES), [])

    def test_missing_context_warning_survives_structured_answer(self):
        q = {**HIGHLIGHT, 'confidence': 'low', 'answer': 'Нужен предыдущий вопрос.'}
        result = parsed(q, HIGHLIGHT_LINES)
        self.assertIn('Нужен предыдущий вопрос.', result['notices'])
        self.assertEqual(len(result['highlights']), 12)

    def test_image_only_highlights_are_unverified(self):
        result = parsed(HIGHLIGHT, [])
        self.assertEqual(len(result['highlights']), 12)
        self.assertEqual(result['confidence'], 'low')
        self.assertTrue(all(not h['line_ids'] for h in result['highlights']))

    def test_task_detection_uses_instruction(self):
        self.assertEqual(ai.task_hint(MATCH_LINES), 'matching')
        self.assertEqual(ai.task_hint(HIGHLIGHT_LINES), 'highlight')
        self.assertEqual(ai.task_hint([row(0, 'Сколько будет 2 + 2?')]), '')

    def test_previous_context_is_conditional_bounded_and_not_recursive(self):
        previous = [{'question': 'Q' * 1000, 'answer': 'A' * 3000,
                     'previous_context': [{'answer': 'old private task'}]}] * 8
        self.assertEqual(ai.previous_answer_context(MATCH_LINES, previous), [])
        context = ai.previous_answer_context(HIGHLIGHT_LINES, previous)
        self.assertEqual(len(context), 1)
        self.assertEqual(len(context[0]['question']), 800)
        self.assertEqual(len(context[0]['answer']), 2000)
        self.assertNotIn('old private task', json.dumps(context))

    @patch('screenstudy.ai.call')
    def test_matching_one_call_sends_layout_in_text_only_mode(self, call):
        call.return_value = json.dumps({'questions': [MATCH]})
        frame = {'lines': MATCH_LINES, 'jpeg': b'sensitive-image'}
        result = ai.analyze(Settings(use_images=False), '', frame)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]['matches']), 3)
        call.assert_called_once()
        args, kwargs = call.call_args
        self.assertIsNone(args[3])
        self.assertIn('"position":[0.6,0.75]', args[2])
        self.assertEqual(kwargs['max_tokens'], 4096)

    @patch('screenstudy.ai.call')
    def test_six_paragraphs_one_question_and_missing_previous_context(self, call):
        call.return_value = json.dumps({'questions': [HIGHLIGHT]})
        q = ai.analyze(Settings(use_images=False), '', {'lines': HIGHLIGHT_LINES, 'jpeg': b''})[0]
        call.assert_called_once()
        self.assertEqual(len(q['highlights']), 12)
        self.assertEqual(q['confidence'], 'low')
        self.assertIn('Предыдущий вопрос не передан', q['notices'][-1])

    @patch('screenstudy.ai.call')
    def test_previous_answer_is_available_to_explanation(self, call):
        call.return_value = json.dumps({'questions': [HIGHLIGHT]})
        frame = {'lines': HIGHLIGHT_LINES, 'jpeg': b''}
        q = ai.analyze(Settings(use_images=False), '', frame,
                       previous_questions=[{'question': 'Назовите князей', 'answer': '1 — Олег'}])[0]
        self.assertIn('Назовите князей', call.call_args.args[2])
        call.return_value = '{"explanation":"Учебное объяснение"}'
        ai.explain(Settings(use_images=False), '', q, frame)
        prompt = json.loads(call.call_args.args[2])
        self.assertEqual(len(prompt['highlights']), 12)
        self.assertEqual(prompt['previous_context'][0]['answer'], '1 — Олег')


class TaskUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.w = MainWindow(Path(self.temp.name) / 'settings.json', demo=True)
        self.w.show()

    def tearDown(self):
        self.w.close()
        self.w.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_matching_copy_contains_all_pairs_and_no_paste_or_rewrite(self):
        self.w.show_questions([parsed(MATCH, MATCH_LINES)])
        card = self.w.cards[0]
        self.assertEqual(len(card.task_items), 3)
        self.assertTrue(card.insert_button.isHidden())
        self.assertTrue(card.rewrite_button.isHidden())
        card.copy()
        self.assertEqual(self.app.clipboard().text(), card.question['answer'])
        self.assertIn('→ Правда Ярославичей', self.app.clipboard().text())

    def test_highlight_lists_every_paragraph_and_exact_text_as_plain_text(self):
        self.w.show_questions([parsed(HIGHLIGHT, HIGHLIGHT_LINES)])
        card = self.w.cards[0]
        self.assertEqual(len(card.task_items), 12)
        self.assertIn('6) Ольга', card.task_items[-1].text())
        self.assertIn('«погосты»', card.task_items[-1].text())
        from PySide6.QtCore import Qt
        self.assertTrue(all(item.textFormat() == Qt.PlainText for item in card.task_items))
        self.assertTrue(card.answer.isHidden())
        self.assertTrue(card.answer.isReadOnly())

    def test_incomplete_structured_answer_is_visible(self):
        q = parsed({'question': 'Q', 'kind': 'matching', 'answer': 'Покажите карточки целиком.'}, [])
        self.w.show_questions([q])
        self.assertFalse(self.w.cards[0].answer.isHidden())
        self.assertEqual(self.w.cards[0].answer.toPlainText(), 'Покажите карточки целиком.')

    def test_explanation_keeps_pairs_and_previous_context(self):
        q = parsed(MATCH, MATCH_LINES)
        q['previous_context'] = [{'question': 'Источники?', 'answer': 'Правда Ярослава'}]
        value = self.w.explanation_text(q)
        self.assertIn('→ Правда Ярослава', value)
        self.assertIn('ИСПОЛЬЗОВАННЫЙ ПРЕДЫДУЩИЙ ОТВЕТ', value)

    def test_same_scene_retry_does_not_feed_itself_as_previous_question(self):
        q = parsed(HIGHLIGHT, HIGHLIGHT_LINES)
        q['previous_context'] = [{'question': 'Назовите князей', 'answer': 'Олег'}]
        self.w.frame = {'fingerprint': 'same', 'lines': HIGHLIGHT_LINES, 'jpeg': b''}
        self.w.show_questions([q], self.w.frame)
        with patch.object(self.w.jobs, 'run') as run, patch('screenstudy.ai.analyze') as analyze:
            self.w.ask()
            run.call_args.args[1]()
        self.assertEqual(analyze.call_args.kwargs['previous_questions'], q['previous_context'])

    def test_new_scene_passes_last_answer_and_clear_forgets_it(self):
        self.w.frame = {'fingerprint': 'old', 'lines': MATCH_LINES, 'jpeg': b''}
        self.w.show_questions([parsed(MATCH, MATCH_LINES)], self.w.frame)
        self.w.frame = {'fingerprint': 'new', 'lines': HIGHLIGHT_LINES, 'jpeg': b''}
        with patch.object(self.w.jobs, 'run') as run, patch('screenstudy.ai.analyze') as analyze:
            self.w.ask()
            run.call_args.args[1]()
        self.assertIn('→ Правда Ярослава', analyze.call_args.kwargs['previous_questions'][0]['answer'])
        self.w.clear()
        self.assertEqual(self.w.cards, [])
        self.assertIsNone(self.w.answer_frame)


if __name__ == '__main__':
    unittest.main()
