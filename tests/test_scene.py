import copy
import unittest

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from screenstudy.core import SceneGate, SceneSignature, overlay_lines, same_scene
from screenstudy.imaging import scene_signature


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.lines = [
            {'id': 0, 'text': 'Сколько будет 2 + 2?', 'box': [.1, .2, .5, .08]},
            {'id': 1, 'text': '4', 'box': [.1, .4, .1, .08]},
        ]
        self.image = QImage(800, 500, QImage.Format_RGB32)
        self.image.fill(Qt.white)

    def frame(self, lines=None):
        lines = copy.deepcopy(self.lines if lines is None else lines)
        return {'lines': lines, 'bounds': [0, 0, 800, 500],
                'fingerprint': scene_signature(self.image, lines)}

    def test_hover_colour_and_small_ocr_coordinate_jitter_are_ignored(self):
        before = self.frame()
        painter = QPainter(self.image)
        painter.fillRect(76, 196, 88, 48, QColor('#dbeafe'))
        painter.end()
        lines = copy.deepcopy(self.lines)
        lines[1]['box'][0] += .002
        after = self.frame(lines)
        self.assertTrue(same_scene(before['fingerprint'], after['fingerprint']))
        self.assertIsNotNone(overlay_lines(before, after))

    def test_numeric_operator_negation_and_case_changes_are_not_ignored(self):
        before = self.frame()
        for text in ('Сколько будет 2 + 3?', 'Сколько будет 2 - 2?',
                     'НЕ Сколько будет 2 + 2?', 'сколько будет 2 + 2?'):
            with self.subTest(text=text):
                lines = copy.deepcopy(self.lines)
                lines[0]['text'] = text
                self.assertFalse(same_scene(before['fingerprint'], self.frame(lines)['fingerprint']))

    def test_ocr_order_ids_and_whitespace_do_not_move_markers(self):
        before = self.frame()
        lines = list(reversed(copy.deepcopy(self.lines)))
        for i, line in enumerate(lines):
            line['id'] = i+10
            line['text'] = '  '+line['text']+'\n'
        after = self.frame(lines)
        self.assertTrue(same_scene(before['fingerprint'], after['fingerprint']))
        mapped = overlay_lines(before, after)
        self.assertEqual([l['id'] for l in mapped], [0, 1])
        self.assertEqual([l['box'] for l in mapped], [l['box'] for l in before['lines']])

    def test_scroll_and_display_changes_hide_overlays(self):
        before = self.frame()
        after = copy.deepcopy(before)
        for line in after['lines']:
            line['box'][1] += .1
        self.assertIsNone(overlay_lines(before, after))
        after = copy.deepcopy(before)
        after['bounds'][0] = 800
        self.assertIsNone(overlay_lines(before, after))

    def test_changed_diagram_with_same_text_is_detected(self):
        before = scene_signature(self.image, self.lines)
        painter = QPainter(self.image)
        painter.setPen(QPen(Qt.black, 8))
        painter.drawEllipse(440, 280, 280, 180)
        painter.drawLine(440, 370, 720, 370)
        painter.end()
        self.assertFalse(same_scene(before, scene_signature(self.image, self.lines)))

    def test_diagram_without_ocr_is_detected(self):
        before = scene_signature(self.image, [])
        painter = QPainter(self.image)
        painter.setPen(QPen(Qt.black, 8))
        painter.drawLine(80, 80, 720, 420)
        painter.end()
        self.assertFalse(same_scene(before, scene_signature(self.image, [])))

    def test_gradual_visual_changes_compared_to_committed_reference(self):
        gate = SceneGate()
        base = SceneSignature(('question',), bytes(16000))
        gate.force(base)
        gate.mark_sent()
        for count in (12, 24, 36, 48):
            signature = SceneSignature(('question',), bytes([255])*count+bytes(16000-count))
            gate.observe(signature, now=count)
            self.assertFalse(gate.pending)
        changed = SceneSignature(('question',), bytes([255])*60+bytes(15940))
        gate.observe(changed, now=60)
        self.assertTrue(gate.pending)
        self.assertIs(gate.fingerprint, base)
        gate.observe(changed, now=60.4)
        gate.observe(changed, now=60.8)
        self.assertTrue(gate.ready())


if __name__ == '__main__':
    unittest.main()
