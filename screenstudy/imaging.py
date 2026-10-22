from __future__ import annotations

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage
from .core import SceneSignature, normalized_line


def scene_signature(image, lines):
    # Edges ignore flat hover colours. Text is compared separately, without
    # OCR coordinates; padding masks antialiasing/box jitter around glyphs.
    width, height = 160, 100
    sample = image.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).convertToFormat(QImage.Format_Grayscale8)
    pixels = bytes(sample.constBits())
    stride = sample.bytesPerLine()
    edges = bytearray(width*height)
    for y in range(1, height-1):
        for x in range(1, width-1):
            at = y*stride+x
            strength = max(abs(pixels[at+1]-pixels[at-1]), abs(pixels[at+stride]-pixels[at-stride]))
            edges[y*width+x] = strength if strength >= 48 else 0
    for line in lines:
        x, y, w, h = line["box"]
        left, right = max(0, int(x*width)-3), min(width, int((x+w)*width)+4)
        top, bottom = max(0, int(y*height)-2), min(height, int((y+h)*height)+3)
        for row in range(top, bottom):
            edges[row*width+left:row*width+right] = bytes(right-left)
    texts = tuple(sorted(normalized_line(line["text"]) for line in lines))
    return SceneSignature(texts, bytes(edges))


def prepare_frame(raw, include_jpeg=True):
    image = QImage.fromData(raw["png"])
    if image.isNull():
        raise ValueError("Не удалось открыть снимок экрана.")
    data = QByteArray()
    if include_jpeg:
        reduced = image.scaled(1920, 1920, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        buf = QBuffer(data)
        buf.open(QIODevice.WriteOnly)
        reduced.save(buf, "JPEG", 87)
        buf.close()
    fingerprint = scene_signature(image, raw["lines"])
    return {**raw, "image": image, "jpeg": bytes(data), "fingerprint": fingerprint}
