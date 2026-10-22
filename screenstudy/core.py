from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

GROQ_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "qwen/qwen3.8-27b"


@dataclass
class Settings:
    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3-vl:4b"
    use_images: bool = True
    interval: int = 1
    fast_mode: bool = True
    max_questions: int = 1
    overlays: bool = True
    always_on_top: bool = True
    remember_key: bool = False
    chatgpt_account: str = ""
    display_id: int = 0
    crop: list = field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])

    def validate(self):
        if self.provider not in ("ollama", "groq", "compatible", "chatgpt"):
            raise ValueError("Неизвестный тип подключения.")
        p = urlsplit(self.base_url.strip())
        if not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError("Укажите адрес API без логина, пароля и параметров.")
        local = p.hostname in ("localhost", "127.0.0.1", "::1")
        if p.scheme != "https" and not (p.scheme == "http" and local):
            raise ValueError("Для облака нужен HTTPS. HTTP допустим только для localhost.")
        if self.provider == "ollama" and not local:
            raise ValueError("Локальный режим работает только с Ollama на этом Mac.")
        if self.provider == "groq" and self.base_url.strip().rstrip("/") != GROQ_URL:
            raise ValueError("Для режима Groq используйте https://api.groq.com/openai/v1.")
        if self.provider == "chatgpt":
            if self.base_url.strip().rstrip("/") != "https://api.openai.com/v1":
                raise ValueError("Для подписки ChatGPT требуется официальный адрес OpenAI.")
            if not isinstance(self.chatgpt_account, str) or not self.chatgpt_account:
                raise ValueError("Нажмите «Войти через ChatGPT» и выберите аккаунт и модель.")
        if not self.model.strip():
            raise ValueError("Укажите название модели.")
        if not 1 <= self.interval <= 120:
            raise ValueError("Интервал должен быть от 1 до 120 секунд.")
        if type(self.max_questions) is not int or not 1 <= self.max_questions <= 8:
            raise ValueError("Количество вопросов должно быть от 1 до 8.")
        if not valid_box(self.crop):
            raise ValueError("Неверная область экрана. Выберите её заново.")

    @classmethod
    def load(cls, path: Path):
        try:
            raw = json.loads(path.read_text("utf-8"))
            result = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
            result.validate()
            return result
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def save(self, path: Path):
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), "utf-8")
        temp.chmod(0o600)
        temp.replace(path)

    @property
    def key_account(self):
        return hashlib.sha256(self.base_url.strip().rstrip("/").encode()).hexdigest()


def valid_box(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) for v in box)
            and 0 <= box[0] < 1 and 0 <= box[1] < 1
            and box[2] > 0 and box[3] > 0
            and box[0] + box[2] <= 1.00001 and box[1] + box[3] <= 1.00001)


def text(value, limit=20000):
    return value[:limit].strip() if isinstance(value, str) else ""


def json_object(content):
    if not isinstance(content, str) or len(content) > 1_000_000:
        raise ValueError("Модель вернула ответ неподходящего формата.")
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    decoder = json.JSONDecoder()
    # Accept a fenced JSON object, but never evaluate model output as code.
    for match in re.finditer(r"\{", cleaned):
        try:
            obj, _ = decoder.raw_decode(cleaned[match.start():])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    raise ValueError("ИИ не вернула JSON. Попробуйте другую модель или повторите запрос.")


def line_ids(raw, known):
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(i for i in raw if type(i) is int and i in known))[:30]


def phrase_lines(phrase, lines, preferred=()):
    """Locate a literal OCR quote, including wraps; never guess duplicate locations."""
    phrase = normalized_line(phrase)
    if not phrase:
        return [], False

    def occurrences(rows):
        parts, spans, offset = [], [], 0
        for row in rows:
            value = normalized_line(row["text"])
            parts.append(value)
            spans.append((offset, offset + len(value), row["id"]))
            offset += len(value) + 1
        joined = " ".join(parts)
        pattern = (r"(?<!\w)" if phrase[0].isalnum() else "") + re.escape(phrase)
        pattern += r"(?!\w)" if phrase[-1].isalnum() else ""
        return [[i for start, end, i in spans if start < m.end() and end > m.start()]
                for m in re.finditer(pattern, joined)]

    hits = occurrences(lines)
    if preferred:
        preferred_hits = [hit for hit in hits if set(hit).issubset(preferred)]
        if len(preferred_hits) == 1:
            return preferred_hits[0], True
    return (hits[0] if len(hits) == 1 else []), bool(hits)


def structured_answer(question):
    if question["kind"] == "matching":
        return "\n\n".join(f"{i}. {m['source']} → {m['target']}"
                           for i, m in enumerate(question["matches"], 1))
    if question["kind"] == "highlight":
        return "\n".join(f"{i}. " + (h["group"] + ": " if h["group"] else "") + f"«{h['text']}»"
                         for i, h in enumerate(question["highlights"], 1))
    return question["answer"]


def parse_answer(content, known_ids, lines=None):
    data = json_object(content)
    if not isinstance(data.get("questions"), list):
        raise ValueError("В ответе ИИ отсутствует список questions.")
    known = set(known_ids)
    questions = []
    for q in data["questions"][:8]:
        if not isinstance(q, dict) or not text(q.get("question")):
            continue
        opts = []
        for o in (q.get("options") if isinstance(q.get("options"), list) else [])[:16]:
            if isinstance(o, dict) and text(o.get("text")):
                opts.append({"text": text(o["text"], 4000),
                             "correct": o.get("correct") is True,
                             "line_ids": line_ids(o.get("line_ids"), known)})
        kind = q.get("kind", "text")
        kind = kind if kind in ("text", "single", "multiple", "matching", "highlight") else "text"
        confidence = q.get("confidence", "low")
        if kind == "single" and sum(o["correct"] for o in opts) > 1:
            # A contradictory single-choice answer must not produce two ticks.
            for option in opts:
                option["correct"] = False
            confidence = "low"
        question = {
            "question": text(q["question"], 8000), "kind": kind,
            "answer": text(q.get("answer")), "options": opts,
            "question_explanation": text(q.get("question_explanation"), 12000),
            "explanation": text(q.get("explanation"), 20000),
            "confidence": confidence if confidence in ("high", "medium", "low") else "low",
            "line_ids": line_ids(q.get("line_ids"), known),
            "matches": [], "highlights": [], "notices": [],
        }
        if kind == "matching":
            raw = q.get("matches") if isinstance(q.get("matches"), list) else []
            for item in raw[:32]:
                if not isinstance(item, dict):
                    question["notices"].append("Часть пар имеет неверный формат и скрыта. Повторите запрос.")
                    continue
                source, target = text(item.get("source"), 4000), text(item.get("target"), 4000)
                if not source or not target:
                    question["notices"].append("Часть пар имеет неверный формат и скрыта. Повторите запрос.")
                    continue
                match = {"source": source, "target": target}
                for side in ("source", "target"):
                    ids = line_ids(item.get(side + "_line_ids"), known)
                    if lines is not None:
                        ids, found = phrase_lines(match[side], lines, ids)
                        if not found:
                            question["notices"].append("Часть подписей не подтверждена текстом экрана. Сверьте пары с заданием.")
                    match[side + "_line_ids"] = ids
                if match not in question["matches"]:
                    question["matches"].append(match)
            # A draggable item cannot be assigned to two different destinations.
            destinations = defaultdict(set)
            for m in question["matches"]:
                destinations[(m["source"], tuple(m["source_line_ids"]))].add(m["target"])
            conflicts = {key for key, targets in destinations.items() if len(targets) > 1}
            if conflicts:
                question["matches"] = [m for m in question["matches"]
                    if (m["source"], tuple(m["source_line_ids"])) not in conflicts]
                question["notices"].append("В ответе модели были противоречащие пары. Они скрыты; повторите запрос.")
            if len(raw) > 32:
                question["notices"].append("Показаны первые 32 пары. Выделите меньшую область для остальных.")
        elif kind == "highlight":
            raw = q.get("highlights") if isinstance(q.get("highlights"), list) else []
            for item in raw[:48]:
                if not isinstance(item, dict) or not text(item.get("text")):
                    question["notices"].append("Часть фраз имеет неверный формат и скрыта. Повторите запрос.")
                    continue
                phrase = text(item["text"], 2000)
                ids = line_ids(item.get("line_ids"), known)
                if lines is not None:
                    ids, found = phrase_lines(phrase, lines, ids)
                    if not found and lines:
                        question["notices"].append("Фразы, не найденные в тексте экрана, скрыты. Проверьте распознавание или повторите запрос.")
                        continue
                    if not found:
                        question["notices"].append("Текст не распознан: проверьте предложенные фразы по изображению.")
                highlight = {"text": phrase, "group": text(item.get("group"), 200), "line_ids": ids}
                if highlight not in question["highlights"]:
                    question["highlights"].append(highlight)
            if len(raw) > 48:
                question["notices"].append("Показаны первые 48 фраз. Выделите меньшую область для остальных.")
        if kind in ("matching", "highlight"):
            question["options"] = []
            question["answer"] = structured_answer(question)
            if question["answer"] and question["confidence"] == "low" and text(q.get("answer")):
                question["notices"].append(text(q["answer"], 2000))
            if not question["answer"]:
                question["answer"] = (text(q.get("answer")) or "Не удалось получить проверяемый ответ. Выделите задание целиком и повторите запрос.")
                question["notices"].append("Модель не вернула подходящих пар или фраз для этого задания.")
            question["notices"] = list(dict.fromkeys(question["notices"]))
            if question["notices"]:
                question["confidence"] = "low"
        questions.append(question)
    if data["questions"] and not questions:
        raise ValueError("ИИ вернула вопросы без читаемого текста.")
    return questions


def union_box(ids, lines):
    boxes = [line["box"] for line in lines if line["id"] in ids and valid_box(line["box"])]
    if not boxes:
        return None
    x = min(b[0] for b in boxes)
    y = min(b[1] for b in boxes)
    return [x, y, max(b[0] + b[2] for b in boxes) - x,
            max(b[1] + b[3] for b in boxes) - y]


def answer_marks(question, lines):
    """Shared numbered anchors. Highlight anchors identify rows, not word boxes."""
    marks = []
    def add(value, ids):
        box = union_box(ids, lines)
        if box:
            marks.append({"text": value, "box": box})
    if question["kind"] == "matching":
        for i, match in enumerate(question.get("matches", []), 1):
            add(str(i), match["source_line_ids"])
            add(str(i), match["target_line_ids"])
    elif question["kind"] == "highlight":
        # Several phrases on one line share one marker, with all sidebar numbers.
        grouped = defaultdict(list)
        for i, item in enumerate(question.get("highlights", []), 1):
            if item["line_ids"]:
                grouped[item["line_ids"][0]].append(str(i))
        for line_id, numbers in grouped.items():
            add(",".join(numbers), [line_id])
    else:
        for option in question["options"]:
            if option["correct"]:
                add("✓", option["line_ids"])
    return marks


def normalized_line(value):
    # Keep numbers, operators, case and punctuation: one changed symbol can matter.
    return " ".join(unicodedata.normalize("NFC", value).split())


@dataclass(frozen=True)
class SceneSignature:
    texts: tuple
    edges: bytes


def same_scene(first, second):
    if first is None or second is None:
        return first is second
    if not isinstance(first, SceneSignature) or not isinstance(second, SceneSignature):
        return first == second
    if first.texts != second.texts or len(first.edges) != len(second.edges):
        return False
    # Ignore small rendering changes; compare against the committed reference,
    # never the last frame, so gradual changes cannot accumulate unnoticed.
    budget = max(4, int(len(first.edges) * (0.003 if first.texts else 0.001)))
    changed = 0
    for a, b in zip(first.edges, second.edges):
        if abs(a-b) >= 40:
            changed += 1
            if changed > budget:
                return False
    return True


def overlay_lines(source, current, allow_move=False):
    """Map original model line IDs only when screen geometry is still safe."""
    if not source or not current or not same_scene(source.get("fingerprint"), current.get("fingerprint")):
        return None
    bounds = source.get("bounds")
    if not bounds or bounds != current.get("bounds"):
        return None
    old, new = source["lines"], current["lines"]
    counts = Counter(normalized_line(l["text"]) for l in old)
    if counts != Counter(normalized_line(l["text"]) for l in new):
        return None
    if allow_move and any(count > 1 for count in counts.values()):
        return None  # Do not guess which of repeated identical lines moved where.
    matches = defaultdict(list)
    for line in new:
        matches[normalized_line(line["text"])].append(line)
    result = []
    for line in old:
        candidates = matches[normalized_line(line["text"])]
        other = min(candidates, key=lambda l: sum(abs(a-b) for a, b in zip(l["box"], line["box"])))
        candidates.remove(other)
        if not allow_move and any(abs(a-b)*scale > 4 for a, b, scale in zip(line["box"], other["box"], (bounds[2], bounds[3])*2)):
            return None
        result.append({**other, "id": line["id"]})
    return result


class SceneGate:
    """Commit changes after three observations and a short settling period."""
    def __init__(self):
        self.revision = 0
        self.fingerprint = None
        self.stable = 0
        self.sent = None
        self.candidate = None
        self.candidate_since = 0.0
        self.pending = False

    def reset(self):
        self.revision += 1
        self.fingerprint = None
        self.stable = 0
        self.sent = None
        self.candidate = None
        self.candidate_since = 0.0
        self.pending = False

    def observe(self, fingerprint, now=None):
        now = time.monotonic() if now is None else now
        if self.fingerprint is not None and same_scene(fingerprint, self.fingerprint):
            self.candidate = None
            self.stable = 0
            self.pending = False
            return False
        self.pending = True
        if self.candidate is None or not same_scene(fingerprint, self.candidate):
            self.candidate = fingerprint
            self.candidate_since = now
            self.stable = 1
        else:
            self.stable += 1
        if self.stable >= 3 and now-self.candidate_since >= 0.55:
            self.revision += 1
            self.fingerprint = fingerprint
            self.sent = None
            self.candidate = None
            self.pending = False
            return True
        return False

    def force(self, fingerprint):
        """A manual snapshot is explicit; it does not need settling frames."""
        self.reset()
        self.fingerprint = fingerprint

    def ready(self):
        return self.fingerprint is not None and not self.pending and not same_scene(self.sent, self.fingerprint)

    def mark_sent(self):
        self.sent = self.fingerprint
        return self.revision

    def accepts(self, revision):
        return revision == self.revision
