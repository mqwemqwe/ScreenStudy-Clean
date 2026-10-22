import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from screenstudy.core import GROQ_MODEL, GROQ_URL, Settings
from screenstudy.demo import QUESTIONS, populate
from screenstudy.ui import MainWindow, SettingsDialog, RegionPicker, ChatGPTDialog
from screenstudy.theme import STYLE


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.w = MainWindow(Path(self.temp.name)/'config.json', demo=True)
        self.w.show()
        populate(self.w)
        QTest.qWait(40)

    def tearDown(self):
        self.w.close()
        self.w.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_cards_layout_and_edit_copy(self):
        self.assertEqual(len(self.w.cards), 2)
        for card in self.w.cards:
            self.assertGreaterEqual(card.height(), card.minimumSizeHint().height())
            self.assertGreaterEqual(card.answer.height(), 80)
        card = self.w.cards[0]
        card.answer.setPlainText('Мой исправленный ответ')
        card.copy()
        self.assertEqual(self.app.clipboard().text(), 'Мой исправленный ответ')

    def test_chatgpt_provider_requires_account_hides_key_and_preserves_other_modes(self):
        dialog = SettingsDialog(self.w.settings, 'test-groq-key', self.w)
        dialog.show()
        dialog.provider.setCurrentIndex(dialog.provider.findData('chatgpt'))
        self.assertTrue(dialog.key.isHidden())
        self.assertFalse(dialog.chatgpt_button.isHidden())
        self.assertEqual(dialog.key.text(), '')
        with self.assertRaises(ValueError):
            dialog.values()
        dialog.chatgpt_account = 'oaiapp_test'
        dialog.chatgpt_model = 'test-model'
        dialog.model.setText('test-model')
        cfg = dialog.values()
        self.assertEqual(cfg.provider, 'chatgpt')
        self.assertEqual(cfg.base_url, 'https://api.openai.com/v1')
        self.assertFalse(cfg.remember_key)
        dialog.provider.setCurrentIndex(1)
        self.assertFalse(dialog.key.isHidden())
        self.assertTrue(dialog.chatgpt_button.isHidden())
        self.assertEqual(dialog.values().model, GROQ_MODEL)
        dialog.close()

    def test_chatgpt_account_change_refreshes_models_and_choose(self):
        class Auth:
            def profiles(self):
                return [{'id':a,'email':'demo@example.test','connected':True,'permitted':True} for a in ('one','two')]
            def ready(self, account): return True
            def models(self, account): return [{'slug':account+'-model','display_name':account+' model'}]
        dialog = ChatGPTDialog(Auth(), 'one', parent=self.w)
        dialog.show()
        for _ in range(50):
            QTest.qWait(10)
            if not dialog.busy:
                break
        self.assertEqual(dialog.models.currentData(), 'one-model')
        dialog.accounts.setCurrentIndex(dialog.accounts.findData('two'))
        self.assertFalse(dialog.use.isEnabled())
        for _ in range(50):
            QTest.qWait(10)
            if not dialog.busy:
                break
        self.assertEqual(dialog.models.currentData(), 'two-model')
        dialog.use.click()
        self.assertEqual(dialog.selection, ('two','two-model'))

    def test_closed_chatgpt_dialog_cancels_login_and_ignores_completion(self):
        class Auth:
            def profiles(self): return []
        dialog = ChatGPTDialog(Auth(), parent=self.w)
        dialog.reject()
        self.assertTrue(dialog.cancel.is_set())
        with patch('screenstudy.ui.QMessageBox.information') as welcome:
            dialog.finished_job(('login',''), {'id':'test','welcome':True,'permitted':True}, None)
            welcome.assert_not_called()

    def test_stale_answer_discarded_after_pause(self):
        self.w.gate.force('screen-a')
        revision = self.w.gate.mark_sent()
        self.w.ai_busy = True
        self.w.stop()
        self.w.empty('empty', 'empty')
        self.w.job_result(('answer', revision), QUESTIONS, None)
        self.assertFalse(self.w.ai_busy)
        self.assertEqual(len(self.w.cards), 0)

    def test_stale_answer_discarded_after_scene_change(self):
        self.w.gate.force('screen-a')
        revision = self.w.gate.mark_sent()
        for now in (0, .4, .8):
            self.w.gate.observe('screen-b', now=now)
        self.w.empty('empty', 'empty')
        self.w.job_result(('answer', revision), QUESTIONS, None)
        self.assertEqual(len(self.w.cards), 0)

    def test_valid_answer_and_api_error_pause(self):
        self.w.gate.force('a')
        revision = self.w.gate.mark_sent()
        self.w.job_result(('answer', revision), QUESTIONS, None)
        self.assertEqual(len(self.w.cards), 2)
        self.w.running = True
        self.w.job_result(('answer', revision), None, 'HTTP 429')
        self.assertFalse(self.w.running)
        self.assertIn('429', self.w.status.text())

    def test_rewrite_button_recovers_after_cancel(self):
        card = self.w.cards[0]
        card.rewrite_button.setEnabled(False)
        revision = self.w.gate.revision
        self.w.stop()
        self.w.job_result(('rewrite', revision, card), 'new', None)
        self.assertTrue(card.rewrite_button.isEnabled())
        self.assertNotEqual(card.answer.toPlainText(), 'new')

    def test_paste_cancel_and_clipboard_change(self):
        self.w.paste_text = 'original'
        self.w.paste_remaining = 1
        self.app.clipboard().setText('changed')
        with patch('screenstudy.native.paste') as paste:
            self.w.paste_tick()
            paste.assert_not_called()
        self.assertIn('отменена', self.w.status.text())
        self.w.cancel_insert()
        self.assertFalse(self.w.paste_timer.isActive())

    def test_paste_occurs_only_after_countdown(self):
        self.app.clipboard().setText('test')
        self.w.paste_text, self.w.paste_remaining = 'test', 2
        with patch('screenstudy.native.paste') as paste:
            self.w.paste_tick(); paste.assert_not_called()
            self.w.paste_tick(); paste.assert_called_once()

    def test_clear_forgets_screen(self):
        self.w.frame = {'private': 'data'}
        self.w.clear()
        self.assertIsNone(self.w.frame)
        self.assertEqual(self.w.cards, [])

    def test_region_picker_scales_in_image_coordinates(self):
        image = QImage(2880, 1800, QImage.Format_RGB32)
        picker = RegionPicker(image, self.w)
        picker.show()
        self.app.processEvents()
        r = picker.image_rect()
        picker.selection = QRect(r.x()+r.width()//4, r.y()+r.height()//4, r.width()//2, r.height()//2)
        QTest.keyClick(picker, Qt.Key_Return)
        for actual, expected in zip(picker.crop, [.25, .25, .5, .5]):
            self.assertAlmostEqual(actual, expected, places=2)
        picker.close()

    def test_settings_preset_drops_previous_key(self):
        dialog = SettingsDialog(Settings(), 'secret', self.w)
        dialog.provider.setCurrentIndex(2)
        self.assertEqual(dialog.key.text(), '')
        self.assertEqual(dialog.url.text(), '')
        dialog.close()

    def test_groq_preset_replaces_old_slow_settings(self):
        dialog = SettingsDialog(Settings(interval=6), 'old-key', self.w)
        dialog.provider.setCurrentIndex(1)
        cfg = dialog.values()
        self.assertEqual((cfg.provider, cfg.model, cfg.base_url), ('groq', GROQ_MODEL, GROQ_URL))
        self.assertTrue(cfg.fast_mode)
        self.assertFalse(cfg.use_images)
        self.assertEqual((cfg.interval, cfg.max_questions), (1, 1))
        self.assertEqual(dialog.key.text(), '')
        dialog.close()

    def test_connection_error_text_is_copyable_and_not_html(self):
        import time
        dialog = SettingsDialog(Settings(), '', self.w)
        dialog.check_started = time.monotonic()
        message = 'HTTP 403\nОтвет сервера: <b>denied</b>'
        dialog.test_result('check', None, message)
        self.assertEqual(dialog.check_status.toPlainText(), message)
        dialog.check_status.selectAll()
        dialog.check_status.copy()
        self.assertEqual(self.app.clipboard().text(), message)
        dialog.close()

    def test_explanation_requested_only_on_click_and_cached(self):
        q = copy.deepcopy(QUESTIONS[0])
        q['question_explanation'] = q['explanation'] = ''
        with patch.object(self.w.jobs, 'run') as run:
            self.w.show_questions([q])
            run.assert_not_called()
            self.w.explain(q)
            run.assert_called_once()
            tag = run.call_args.args[0]
            self.assertTrue(self.w.ai_busy)
            self.w.job_result(tag, {'question_explanation': 'Задание', 'explanation': 'Обоснование'}, None)
            self.assertFalse(self.w.ai_busy)
            self.assertIn('Обоснование', tag[4].toPlainText())
            tag[3].close()
            self.w.explain(q)
            run.assert_called_once()

    def test_closed_explanation_can_complete_without_crash(self):
        q = copy.deepcopy(QUESTIONS[0])
        q['explanation'] = ''
        with patch.object(self.w.jobs, 'run') as run:
            self.w.explain(q)
            tag = run.call_args.args[0]
            tag[3].close()
            QTest.qWait(10)
            self.w.job_result(tag, None, 'HTTP 429')
            self.assertFalse(self.w.ai_busy)
            self.assertTrue(self.w.once_button.isEnabled())
            self.assertEqual(q['explanation'], '')

    def test_stable_new_question_sent_after_one_second_not_six(self):
        import time
        self.w.running = True
        self.w.last_request = time.monotonic() - 1.1
        frame = {'fingerprint': 'new-screen', 'lines': [], 'jpeg': b''}
        tag = ('capture', self.w.capture_epoch, False, time.monotonic())
        base = time.monotonic()
        with patch.object(self.w, 'ask') as ask, patch('screenstudy.core.time.monotonic') as clock:
            clock.return_value = base
            self.w.job_result(tag, frame, None)
            ask.assert_not_called()
            clock.return_value = base+.4
            self.w.job_result(tag, frame, None)
            ask.assert_not_called()
            clock.return_value = base+.8
            self.w.job_result(tag, frame, None)
            ask.assert_called_once()
        self.assertLessEqual(self.w.timer.interval(), 400)

    def test_image_switch_invalidates_previous_answer(self):
        self.w.frame = {'private': 'data'}
        self.image_before = self.w.settings.use_images
        self.w.image_toggle.setChecked(not self.image_before)
        self.assertEqual(self.w.settings.use_images, not self.image_before)
        self.assertIsNone(self.w.frame)
        self.assertEqual(self.w.cards, [])

    def test_no_overlap_between_card_widgets(self):
        card = self.w.cards[0]
        previous_bottom = -1
        layout = card.layout()
        for i in range(layout.count()):
            geom = layout.itemAt(i).geometry()
            self.assertGreaterEqual(geom.top(), previous_bottom)
            previous_bottom = geom.bottom()

    def frame(self, name='question', offset=0):
        return {'fingerprint': name, 'lines': [
            {'id': 0, 'text': 'Question', 'box': [.1, .1+offset, .5, .1]},
            {'id': 1, 'text': 'Answer', 'box': [.1, .3+offset, .5, .1]}],
            'bounds': [0, 0, 800, 500], 'jpeg': name.encode()}

    def capture(self, frame, now):
        with patch('screenstudy.core.time.monotonic', return_value=now):
            self.w.job_result(('capture', self.w.capture_epoch, False, now), frame, None)

    def test_cards_edits_and_explanation_survive_transient_and_confirmed_changes(self):
        source = self.frame()
        self.w.frame = source
        self.w.gate.force(source['fingerprint'])
        self.w.gate.mark_sent()
        self.w.show_questions(copy.deepcopy(QUESTIONS), source)
        card = self.w.cards[0]
        card.answer.setPlainText('Моя правка')
        self.w.running = True
        with patch.object(self.w, 'ask') as ask:
            self.capture(self.frame('tooltip'), 10)
            self.capture(source, 10.4)
            ask.assert_not_called()
            self.assertNotIn('Проверяю изменения', self.w.status.text())
            for now in (11, 11.4, 11.8):
                self.capture(self.frame('new-question'), now)
            ask.assert_called_once()
        self.assertIs(self.w.cards[0], card)
        self.assertEqual(card.answer.toPlainText(), 'Моя правка')
        self.assertTrue(card.question['explanation'])
        self.assertIn('предыдущему', self.w.answer_note.text())

    def test_hover_keeps_overlay_objects_scroll_hides_only_overlays(self):
        from unittest.mock import MagicMock
        source = self.frame()
        self.w.frame = source
        self.w.gate.force(source['fingerprint'])
        self.w.gate.mark_sent()
        self.w.show_questions(copy.deepcopy(QUESTIONS), source)
        self.w.running = True
        marker = MagicMock()
        self.w.markers = [marker]
        card = self.w.cards[0]
        self.capture(self.frame(offset=.002), 10)
        marker.close.assert_not_called()
        self.capture(self.frame(offset=.1), 10.4)
        marker.close.assert_called_once()
        self.assertIs(self.w.cards[0], card)
        self.assertIn('метки скрыты', self.w.answer_note.text())
        with patch.object(self.w, 'show_markers') as show:
            self.capture(self.frame(offset=.1), 10.8)
            show.assert_not_called()
            self.capture(self.frame(offset=.1), 11.2)
            show.assert_called_once()
            self.assertAlmostEqual(show.call_args.args[0][0]['box'][1], .2)
        self.assertIs(self.w.answer_frame, source)

    def test_return_to_last_answer_reuses_it_without_api_request(self):
        source = self.frame()
        self.w.show_questions(copy.deepcopy(QUESTIONS), source)
        self.w.gate.force('other')
        self.w.gate.mark_sent()
        self.w.running = True
        with patch.object(self.w, 'ask') as ask:
            for now in (10, 10.4, 10.8):
                self.capture(source, now)
            ask.assert_not_called()
        self.assertFalse(self.w.gate.ready())

    def test_overlay_positions_are_refreshed_when_returning_after_scroll(self):
        from unittest.mock import MagicMock
        source = self.frame()
        self.w.frame = source
        self.w.show_questions([copy.deepcopy(QUESTIONS[0])], source)
        self.w.gate.force('question')
        self.w.gate.mark_sent()
        self.w.running = True
        self.w.demo = False
        created = []
        def marker(*args):
            item = MagicMock()
            item.width.return_value = 27
            created.append((args, item))
            return item
        with patch('screenstudy.ui.Marker', side_effect=marker):
            self.w.sync_answer_state()
            first = created[0][1]
            self.capture(self.frame(offset=.1), 10)
            first.close.assert_called_once()
            self.assertFalse(self.w.markers)
            self.capture(self.frame(offset=.1), 10.4)
            self.capture(self.frame(offset=.1), 10.8)
            shifted = self.w.markers[0]
            self.assertAlmostEqual(created[-2][0][2], 100)
            self.capture(source, 11.2)
            shifted.close.assert_called_once()
            self.assertAlmostEqual(created[-2][0][2], 50)

    def test_lock_blocks_new_requests_and_late_answer_replacement(self):
        self.w.frame = self.frame()
        self.w.show_questions(copy.deepcopy(QUESTIONS), self.w.frame)
        old_card = self.w.cards[0]
        old_capture_epoch = self.w.capture_epoch
        self.w.gate.force('question')
        revision = self.w.gate.mark_sent()
        self.w.ai_busy = self.w.answer_inflight = True
        self.w.question_button.click()
        self.assertTrue(self.w.question_locked)
        self.assertFalse(self.w.once_button.isEnabled())
        self.assertIn('Следующий', self.w.question_button.text())
        with patch.object(self.w, 'ask') as ask:
            for now in (10, 10.4, 10.8, 11.2):
                self.capture(self.frame('new'), now)
            ask.assert_not_called()
        self.w.job_result(('capture', old_capture_epoch, False, 0), self.frame('old'), None)
        self.assertEqual(self.w.frame['fingerprint'], 'new')
        self.w.job_result(('answer', revision, self.frame('old')), [copy.deepcopy(QUESTIONS[1])], None)
        self.assertIs(self.w.cards[0], old_card)
        self.assertFalse(self.w.once_button.isEnabled())
        self.w.clear()
        self.assertFalse(self.w.question_locked)
        self.assertIsNone(self.w.answer_frame)

    def test_next_question_resumes_observation_and_keeps_previous_until_ready(self):
        self.w.frame = self.frame()
        self.w.show_questions(copy.deepcopy(QUESTIONS), self.w.frame)
        card = self.w.cards[0]
        self.w.question_button.click()
        self.w.demo = False
        with patch('screenstudy.native.request_screen_permission', return_value=True), patch.object(self.w, 'tick'):
            self.w.question_button.click()
        self.w.timer.stop()
        self.assertFalse(self.w.question_locked)
        self.assertTrue(self.w.running)
        self.assertIs(self.w.cards[0], card)
        new_frame = self.frame('new')
        self.w.frame = new_frame
        self.w.gate.force('new')
        revision = self.w.gate.mark_sent()
        self.w.settings.overlays = False
        self.w.job_result(('answer', revision, new_frame), [copy.deepcopy(QUESTIONS[1])], None)
        self.assertIsNot(self.w.cards[0], card)
        self.assertIs(self.w.answer_frame, new_frame)

    def test_empty_new_screen_keeps_last_answer(self):
        source = self.frame()
        self.w.show_questions(copy.deepcopy(QUESTIONS), source)
        card = self.w.cards[0]
        self.w.frame = self.frame('empty')
        self.w.gate.force('empty')
        self.w.job_result(('answer', self.w.gate.mark_sent(), self.w.frame), [], None)
        self.assertIs(self.w.cards[0], card)
        self.assertIs(self.w.answer_frame, source)

    def test_explanation_uses_answer_snapshot_not_new_screen(self):
        source = self.frame('original')
        q = copy.deepcopy(QUESTIONS[0])
        q['explanation'] = ''
        self.w.show_questions([q], source)
        self.w.frame = self.frame('different-question')
        with patch.object(self.w.jobs, 'run') as run, patch('screenstudy.ai.explain') as explain:
            self.w.explain(q)
            run.call_args.args[1]()
            self.assertIs(explain.call_args.args[3], source)

    def test_explanation_click_queues_after_inflight_analysis(self):
        source = self.frame('original')
        q = copy.deepcopy(QUESTIONS[0])
        q['explanation'] = ''
        self.w.show_questions([q], source)
        self.w.gate.force('new')
        revision = self.w.gate.mark_sent()
        self.w.ai_busy = self.w.answer_inflight = True
        with patch.object(self.w.jobs, 'run') as run, patch('screenstudy.ai.explain') as explain:
            self.w.explain(q)
            run.assert_not_called()
            self.assertIs(self.w.pending_explanation, q)
            self.w.job_result(('answer', revision, self.frame('new')), [], None)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][0], 'explain')
            run.call_args.args[1]()
            self.assertIs(explain.call_args.args[3], source)

    def test_manual_request_uses_fixed_snapshot_without_further_capture(self):
        self.w.demo = False
        with patch('screenstudy.native.request_screen_permission', return_value=True), patch.object(self.w.jobs, 'run') as run:
            self.w.once()
            self.assertTrue(self.w.force_pending)
            self.capture(self.frame('manual'), 10)
            self.assertEqual(run.call_args.args[0][0], 'answer')
            self.assertFalse(self.w.timer.isActive())
            run.reset_mock()
            self.w.tick()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
