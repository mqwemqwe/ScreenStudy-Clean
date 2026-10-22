from __future__ import annotations

import copy
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QTextEdit, QVBoxLayout, QWidget, QLayout, QSizePolicy)

from . import ai, native, chatgpt
from . import __version__
from .core import GROQ_MODEL, GROQ_URL, SceneGate, Settings, union_box, same_scene, overlay_lines, answer_marks
from .imaging import prepare_frame
from .theme import STYLE


def label(value, name=None):
    obj = QLabel(value)
    obj.setWordWrap(True)
    obj.setTextFormat(Qt.PlainText)
    if name:
        obj.setObjectName(name)
    return obj


def button(value, callback, name=None):
    obj = QPushButton(value)
    obj.clicked.connect(callback)
    if name:
        obj.setObjectName(name)
    return obj


class Jobs(QObject):
    result = Signal(object, object, object)

    def run(self, tag, fn):
        def work():
            try:
                value, error = fn(), None
            except Exception as exc:
                value, error = None, str(exc)
            try:
                self.result.emit(tag, value, error)
            except RuntimeError:
                pass  # The application was closed during an outstanding request.
        threading.Thread(target=work, daemon=True).start()


class ChatGPTDialog(QDialog):
    def __init__(self, auth, account="", model="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("ScreenStudy · подписка ChatGPT")
        self.resize(560, 530)
        self.auth, self.preferred_model = auth, model
        self.selection = None
        self.cancel = threading.Event()
        self.closed, self.busy = False, False
        self.jobs = Jobs(self)
        self.jobs.result.connect(self.finished_job)
        layout = QVBoxLayout(self)
        layout.addWidget(label("Подключить ChatGPT", "title"))
        layout.addWidget(label("Войдите в браузере и разрешите ScreenStudy использовать подписку. Выбранная область экрана или её текст будут передаваться OpenAI. Доступ зависит от аккаунта и тарифа.", "muted"))
        self.accounts = QComboBox()
        layout.addWidget(self.accounts)
        self.signin = button("Continue with ChatGPT", self.sign_in)
        self.signin.setToolTip("Войти через ChatGPT или повторно разрешить использование подписки")
        self.signin.setStyleSheet("background: white; color: #111; border: 1px solid #ddd; padding: 10px; border-radius: 8px;")
        layout.addWidget(self.signin)
        self.cancel_login = button("Отменить вход", self.cancel.set, "small")
        self.cancel_login.hide()
        layout.addWidget(self.cancel_login)
        self.state_label = label("", "muted")
        layout.addWidget(self.state_label)
        layout.addWidget(label("Модель из вашего аккаунта"))
        self.models = QComboBox()
        self.models.setMinimumContentsLength(24)
        self.models.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        layout.addWidget(self.models)
        actions = QHBoxLayout()
        self.refresh = button("Обновить модели", self.load_models, "small")
        self.logout = button("Выйти", self.sign_out, "small")
        actions.addWidget(self.refresh)
        actions.addWidget(self.logout)
        layout.addLayout(actions)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setAcceptRichText(False)
        self.details.setMaximumHeight(115)
        self.details.setPlaceholderText("Результат подключения и сообщения сервера появятся здесь.")
        layout.addWidget(self.details)
        layout.addWidget(button("Использование ChatGPT", lambda: self.open_usage(), "small"))
        layout.addWidget(label("Сеанс сохраняется в защищённом локальном файле. Запросы используют доступный объём вашей подписки или кредитов.", "muted"))
        self.use = button("Использовать подписку", self.choose, "primary")
        layout.addWidget(self.use)
        layout.addWidget(button("Закрыть", self.reject, "small"))
        self.accounts.currentIndexChanged.connect(self.account_changed)
        self.models.currentIndexChanged.connect(self.update_controls)
        self.refresh_accounts(account)

    @staticmethod
    def open_usage():
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(chatgpt.USAGE_URL))

    def refresh_accounts(self, selected):
        self.accounts.blockSignals(True)
        self.accounts.clear()
        self.accounts.addItem("Добавить аккаунт ChatGPT…", "")
        try:
            for p in self.auth.profiles():
                state = "подключён" if p["connected"] else "нужен вход"
                self.accounts.addItem(f"{p['email'] or 'Аккаунт'} · {p['id'][-6:]} · {state}", p["id"])
        except Exception as exc:
            self.details.setPlainText(str(exc))
        self.accounts.setCurrentIndex(max(0, self.accounts.findData(selected)))
        self.accounts.blockSignals(False)
        self.account_changed()

    def update_controls(self, *_):
        account = self.accounts.currentData() or ""
        for widget in (self.accounts, self.signin):
            widget.setEnabled(not self.busy)
        self.refresh.setEnabled(bool(account) and not self.busy)
        self.logout.setEnabled(bool(account) and not self.busy)
        self.models.setEnabled(not self.busy)
        self.use.setEnabled(bool(account and self.models.currentData()) and not self.busy)

    def account_changed(self, *_):
        self.models.clear()
        self.state_label.setText("Нажмите Continue with ChatGPT, чтобы открыть вход в браузере.")
        account = self.accounts.currentData()
        if account:
            try:
                if self.auth.ready(account):
                    self.state_label.setText("Используется подписка ChatGPT")
                    self.load_models()
                    return
                self.state_label.setText("Требуется вход или разрешение использовать подписку.")
            except Exception as exc:
                self.details.setPlainText(str(exc))
        self.update_controls()

    def run_job(self, kind, fn):
        self.busy = True
        self.update_controls()
        self.jobs.run((kind, self.accounts.currentData()), fn)

    def sign_in(self):
        account = self.accounts.currentData() or ""
        self.cancel = threading.Event()
        self.cancel_login.show()
        self.details.setPlainText("Открываю браузер. Войдите и разрешите использование подписки. Ожидание — до 3 минут.")
        self.run_job("login", lambda: self.auth.connect(account, self.cancel))

    def load_models(self):
        account = self.accounts.currentData()
        if account and not self.busy:
            self.models.clear()
            self.details.setPlainText("Загружаю доступные модели аккаунта…")
            self.run_job("models", lambda: self.auth.models(account))

    def sign_out(self):
        account = self.accounts.currentData()
        self.models.clear()
        self.run_job("logout", lambda: self.auth.sign_out(account))

    def finished_job(self, tag, value, error):
        if self.closed:
            return
        self.busy = False
        self.cancel_login.hide()
        if error:
            self.details.setPlainText(error)
        elif tag[0] == "login":
            if value["welcome"]:
                QMessageBox.information(self, "Используется подписка ChatGPT",
                    "Ответы ScreenStudy будут использовать доступный объём вашей подписки или кредитов. Управлять использованием можно в настройках ChatGPT.")
                self.auth.acknowledge(value["id"])
            self.refresh_accounts(value["id"])
            if not value["permitted"]:
                self.details.setPlainText("Вход выполнен, но использование подписки не разрешено. Повторите вход и предоставьте разрешение.")
        elif tag[0] == "models":
            if self.accounts.currentData() == tag[1]:
                for model in value:
                    self.models.addItem(model["display_name"], model["slug"])
                self.models.setCurrentIndex(max(0, self.models.findData(self.preferred_model)))
                self.details.setPlainText("Выберите модель и нажмите «Использовать подписку». Затем можно проверить подключение коротким запросом.")
        elif tag[0] == "logout":
            self.refresh_accounts(tag[1])
            self.details.setPlainText(value)
        self.update_controls()

    def choose(self):
        if self.use.isEnabled():
            self.selection = (self.accounts.currentData(), self.models.currentData())
            self.accept()

    def done(self, result):
        self.closed = True
        self.cancel.set()
        super().done(result)


class SettingsDialog(QDialog):
    def __init__(self, settings, key, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Подключение ИИ")
        self.resize(580, 760)
        self.original = copy.deepcopy(settings)
        self.key_value = key
        self.value = None
        self.auth = getattr(parent, "chatgpt", None) or chatgpt.manager()
        self.chatgpt_account = settings.chatgpt_account
        self.chatgpt_model = settings.model if settings.provider == "chatgpt" else ""
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        layout.addWidget(label("Подключение ИИ", "title"))
        layout.addWidget(label("Локальная модель работает на вашем Mac. Облачная получает выбранную область экрана или распознанный текст.", "muted"))
        form = QFormLayout()
        self.provider = QComboBox()
        self.provider.addItem("Ollama · на этом Mac", "ollama")
        self.provider.addItem("Groq · быстрый облачный режим", "groq")
        self.provider.addItem("Другой / совместимый API", "compatible")
        self.provider.addItem("ChatGPT · моя подписка", "chatgpt")
        self.provider.setCurrentIndex(self.provider.findData(settings.provider))
        self.url = QLineEdit(settings.base_url)
        self.model = QLineEdit(settings.model)
        self.url.setReadOnly(settings.provider == "groq")
        self.model.setReadOnly(settings.provider == "groq")
        self.key = QLineEdit(key)
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("Вставьте API-ключ Groq" if settings.provider == "groq" else "Для Ollama ключ не нужен")
        self.remember = QCheckBox("Сохранить ключ в Связке ключей macOS")
        self.remember.setChecked(settings.remember_key)
        self.images = QCheckBox("Передавать изображение области (для vision-моделей)")
        self.images.setChecked(settings.use_images)
        self.interval = QSpinBox()
        self.interval.setRange(1, 120)
        self.interval.setSuffix(" с")
        self.interval.setValue(settings.interval)
        self.speed = QComboBox()
        self.speed.addItems(["Быстро", "Подумать точнее"])
        self.speed.setCurrentIndex(0 if settings.fast_mode else 1)
        self.speed.setEnabled(settings.provider == "groq")
        self.count = QSpinBox()
        self.count.setRange(1, 8)
        self.count.setValue(settings.max_questions)
        self.count.setToolTip("Для минимальной задержки оставьте один вопрос и выделите его область.")
        self.overlay = QCheckBox("Показывать ⓘ, галочки и номера пар/фраз поверх экрана")
        self.overlay.setChecked(settings.overlays)
        self.screens = QComboBox()
        self.screens.addItem("Основной экран", 0)
        if sys.platform == "darwin":
            try:
                for d in native.displays():
                    self.screens.addItem(d["name"], d["id"])
            except Exception:
                pass
        index = self.screens.findData(settings.display_id)
        self.screens.setCurrentIndex(max(0, index))
        form.addRow("Режим", self.provider)
        form.addRow("Адрес API", self.url)
        form.addRow("Модель", self.model)
        form.addRow("API-ключ", self.key)
        form.addRow("", self.remember)
        self.chatgpt_button = button("Войти через ChatGPT / выбрать модель", self.configure_chatgpt)
        form.addRow("", self.chatgpt_button)
        self.chatgpt_summary = label("", "muted")
        form.addRow("", self.chatgpt_summary)
        self.form = form
        form.addRow("", self.images)
        form.addRow("Режим Groq", self.speed)
        form.addRow("Вопросов за запрос", self.count)
        form.addRow("Между запросами", self.interval)
        form.addRow("Экран", self.screens)
        form.addRow("", self.overlay)
        layout.addLayout(form)
        self.provider_hint = label("", "muted")
        layout.addWidget(self.provider_hint)
        self.key_link = QLabel('<a style="color:#80e9c7" href="https://console.groq.com/keys">Создать API-ключ Groq</a>')
        self.key_link.setOpenExternalLinks(True)
        layout.addWidget(self.key_link)
        self.check_status = QTextEdit()
        self.check_status.setReadOnly(True)
        self.check_status.setAcceptRichText(False)
        self.check_status.setFixedHeight(85)
        self.check_status.setPlaceholderText("Здесь появится результат проверки. Текст можно выделить и скопировать.")
        layout.addWidget(self.check_status)
        self.test = button("Проверить подключение", self.test_connection)
        layout.addWidget(self.test)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.provider.currentIndexChanged.connect(self.preset)
        self.url.textEdited.connect(lambda _: self.key.clear())
        self.jobs = Jobs(self)
        self.jobs.result.connect(self.test_result)
        self.update_chatgpt_controls()

    def update_chatgpt_controls(self):
        enabled = self.provider.currentData() == "chatgpt"
        for widget in (self.url, self.model, self.key, self.remember):
            self.form.setRowVisible(widget, not enabled)
        self.form.setRowVisible(self.chatgpt_button, enabled)
        self.form.setRowVisible(self.chatgpt_summary, enabled)
        self.form.setRowVisible(self.speed, not enabled)
        self.key_link.setVisible(not enabled)
        self.provider_hint.setText(
            "Ответы используют вашу подписку ChatGPT или кредиты. Один вопрос за запрос; объяснение — по ⓘ. Для формул и рисунков включите изображение. Проверка подключения тоже использует подписку."
            if enabled else "Для скорости: Groq → «Быстро» → один вопрос. Объяснения загружаются по ⓘ. Для формул и рисунков включите изображение. Бесплатный API имеет лимиты.")
        account_label = ""
        try:
            account = next((p for p in self.auth.profiles() if p['id'] == self.chatgpt_account), None)
            if account:
                account_label = f"{account['email'] or 'Аккаунт'} · {account['id'][-6:]}\n"
        except (ValueError, OSError):
            pass
        self.chatgpt_summary.setText(
            f"{account_label}Модель: {self.chatgpt_model}" if self.chatgpt_account and self.chatgpt_model else "Подключите аккаунт с доступной подпиской.")

    def configure_chatgpt(self):
        dialog = ChatGPTDialog(self.auth, self.chatgpt_account, self.chatgpt_model, self)
        if dialog.exec() == QDialog.Accepted:
            self.chatgpt_account, self.chatgpt_model = dialog.selection
            self.model.setText(self.chatgpt_model)
        self.update_chatgpt_controls()

    def preset(self, index):
        self.key.clear()
        self.key.setPlaceholderText("Для Ollama ключ не нужен" if index == 0 else "Вставьте API-ключ Groq" if index == 1 else "API-ключ провайдера")
        self.url.setReadOnly(index == 1)
        self.model.setReadOnly(index == 1)
        self.speed.setEnabled(index == 1)
        if index == 0:
            self.url.setText("http://127.0.0.1:11434")
            self.model.setText("qwen3-vl:4b")
            self.images.setChecked(True)
        elif index == 1:
            self.url.setText(GROQ_URL)
            self.model.setText(GROQ_MODEL)
            self.images.setChecked(False)
            self.speed.setCurrentIndex(0)
            self.interval.setValue(1)
            self.count.setValue(1)
        elif index == 3:
            self.url.setText(chatgpt.API)
            self.model.setText(self.chatgpt_model)
            self.images.setChecked(False)
            self.interval.setValue(1)
            self.count.setValue(1)
        else:
            self.url.clear()
            self.url.setPlaceholderText("https://адрес-провайдера/v1")
            self.model.clear()
            self.model.setPlaceholderText("Точное название модели из кабинета API")
        self.update_chatgpt_controls()

    def values(self):
        value = copy.deepcopy(self.original)
        value.provider = self.provider.currentData()
        value.base_url = self.url.text().strip().rstrip("/")
        value.model = self.model.text().strip()
        value.chatgpt_account = self.chatgpt_account
        value.remember_key = self.remember.isChecked() and value.provider != "chatgpt"
        value.use_images = self.images.isChecked()
        value.interval = self.interval.value()
        value.fast_mode = self.speed.currentIndex() == 0
        value.max_questions = self.count.value()
        value.overlays = self.overlay.isChecked()
        value.display_id = self.screens.currentData()
        if value.display_id != self.original.display_id:
            value.crop = [0.0, 0.0, 1.0, 1.0]
        value.validate()
        return value

    def test_connection(self):
        try:
            value, key = self.values(), self.key.text()
        except ValueError as exc:
            self.check_status.setPlainText(str(exc))
            return
        self.test.setEnabled(False)
        self.check_started = time.monotonic()
        self.check_status.setPlainText("Проверяю… Первый запуск локальной модели может занять время.")
        self.jobs.run("check", lambda: ai.call(value, key, "Ответь словом OK.",
                                               system="Ты проверяешь подключение.", structured=False,
                                               max_tokens=2048 if not value.fast_mode else 32, auth=self.auth))

    def test_result(self, tag, value, error):
        self.test.setEnabled(True)
        elapsed = time.monotonic() - self.check_started
        self.check_status.setPlainText(error or f"Подключение работает · {elapsed:.1f} с на короткий текст. Скорость решения вопроса может отличаться.")

    def save(self):
        try:
            value = self.values()
            if value.provider == "chatgpt" and not self.auth.ready(value.chatgpt_account):
                raise ValueError("Войдите в ChatGPT и разрешите использование подписки.")
            key = self.key.text().strip()
            if value.remember_key:
                native.save_key(value.key_account, key)
            elif self.original.remember_key and value.provider != "chatgpt":
                native.save_key(self.original.key_account, "")
            self.value, self.key_value = value, key
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Настройки", str(exc))


class RegionPicker(QDialog):
    """Select a normalized region in a fitted preview, avoiding Retina offsets."""
    def __init__(self, image, parent):
        super().__init__(parent)
        self.setWindowTitle("Выделите область с заданием · Enter — применить · Esc — отмена")
        self.image = image
        self.start = None
        self.selection = QRect()
        self.crop = None
        self.resize(1000, 680)
        self.setMinimumSize(400, 300)

    def image_rect(self):
        area = self.rect().adjusted(15, 50, -15, -15)
        size = self.image.size().scaled(area.size(), Qt.KeepAspectRatio)
        return QRect(area.center()-QPoint(size.width()//2, size.height()//2), size)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111a26"))
        painter.setPen(QColor("#d7e8ee"))
        painter.drawText(QRect(15, 8, self.width()-30, 35), Qt.AlignCenter,
                         "Обведите вопрос вместе с вариантами. Enter — применить, Esc — отмена.")
        painter.drawImage(self.image_rect(), self.image)
        if not self.selection.isNull():
            painter.setPen(QPen(QColor("#44e3ba"), 3))
            painter.fillRect(self.selection, QColor(68, 227, 186, 35))
            painter.drawRect(self.selection)

    def mousePressEvent(self, event):
        if self.image_rect().contains(event.position().toPoint()):
            self.start = event.position().toPoint()
            self.selection = QRect(self.start, self.start)

    def mouseMoveEvent(self, event):
        if self.start is not None:
            self.selection = QRect(self.start, event.position().toPoint()).normalized().intersected(self.image_rect())
            self.update()

    def mouseReleaseEvent(self, event):
        self.start = None

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.selection.width() > 20 and self.selection.height() > 20:
            r, s = self.image_rect(), self.selection
            self.crop = [(s.x()-r.x())/r.width(), (s.y()-r.y())/r.height(),
                         s.width()/r.width(), s.height()/r.height()]
            self.accept()
        else:
            super().keyPressEvent(event)


class QuestionCard(QFrame):
    def __init__(self, question, index, owner):
        super().__init__()
        self.setObjectName("card")
        self.question, self.owner = question, owner
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        top = QHBoxLayout()
        conf = {"high": "", "medium": " · проверьте ответ", "low": " · мало уверенности"}
        top.addWidget(label(f"ВОПРОС {index+1}" + conf[question["confidence"]], "eyebrow"), 1)
        info = button("i", lambda: owner.explain(question), "small")
        info.setFixedSize(28, 28)
        info.setToolTip("Объяснение вопроса и ответа")
        top.addWidget(info)
        layout.addLayout(top)
        qtext = label(question["question"])
        qtext.setTextInteractionFlags(Qt.TextSelectableByMouse)
        question_font = QFont(qtext.font())
        question_font.setBold(True)
        qtext.setFont(question_font)
        layout.addWidget(qtext)
        self.task_items = []
        structured = question["kind"] in ("matching", "highlight")
        if question["kind"] == "matching":
            layout.addWidget(label("ЧТО ПЕРЕНЕСТИ → КУДА", "eyebrow"))
            layout.addWidget(label("Одинаковые номера на экране обозначают пару. Перенесите карточки мышью.", "muted"))
            for i, pair in enumerate(question.get("matches", []), 1):
                item = label(f"{i}. {pair['source']}\n→ {pair['target']}")
                item.setTextInteractionFlags(Qt.TextSelectableByMouse)
                item.setStyleSheet("background: #18352f; color: #b8f4dd; border-radius: 7px; padding: 10px;")
                self.task_items.append(item)
                layout.addWidget(item)
        elif question["kind"] == "highlight":
            layout.addWidget(label("СЛОВА И ФРАЗЫ ДЛЯ ВЫДЕЛЕНИЯ", "eyebrow"))
            layout.addWidget(label("Нажмите эти слова в задании. Номера на экране отмечают строки; точные фразы приведены ниже.", "muted"))
            for i, phrase in enumerate(question.get("highlights", []), 1):
                caption = f"Фраза {i}" + (" · " + phrase["group"] if phrase["group"] else "") + "\n"
                item = label(caption + f"«{phrase['text']}»")
                item.setTextInteractionFlags(Qt.TextSelectableByMouse)
                item.setStyleSheet("background: #18352f; color: #b8f4dd; border-radius: 7px; padding: 10px;")
                self.task_items.append(item)
                layout.addWidget(item)
        for notice in question.get("notices", []):
            warning = label(notice)
            warning.setStyleSheet("color: #f3ce8b;")
            layout.addWidget(warning)
        if question.get("previous_context"):
            layout.addWidget(label("Учтён предыдущий ответ. Значок i откроет объяснение и использованный контекст.", "muted"))
        for option in question["options"]:
            choice = label(("✓  " if option["correct"] else "○  ") + option["text"])
            choice.setStyleSheet("color: #80e9c7;" if option["correct"] else "color: #bdcbd8;")
            layout.addWidget(choice)
        if not self.task_items:
            layout.addWidget(label("РЕКОМЕНДУЕМЫЙ ОТВЕТ", "muted"))
        self.answer = QTextEdit()
        self.answer.setAcceptRichText(False)
        self.answer.setPlainText(question["answer"])
        self.answer.setFixedHeight(95)
        self.answer.setTabChangesFocus(True)
        self.answer.setReadOnly(structured)
        self.answer.setVisible(not self.task_items)
        layout.addWidget(self.answer)
        row = QHBoxLayout()
        row.addWidget(button("Копировать", self.copy, "small"))
        self.insert_button = button("Вставить · 3 с", lambda: owner.insert(self.answer.toPlainText()), "small")
        self.insert_button.setVisible(not structured)
        row.addWidget(self.insert_button)
        layout.addLayout(row)
        self.rewrite_button = button("Переписать ответ…", lambda: owner.rewrite(self), "small")
        self.rewrite_button.setVisible(not structured)
        layout.addWidget(self.rewrite_button)

    def copy(self):
        QApplication.clipboard().setText(self.answer.toPlainText())
        self.owner.status.setText("Ответ скопирован. Вставьте его с помощью ⌘V.")


class Marker(QWidget):
    def __init__(self, text, x, y, callback=None):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if callback is None:
            self.setAttribute(Qt.WA_TransparentForMouseEvents)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        mark = button(text, callback) if callback else label(text)
        mark.setStyleSheet("background: #73e3bf; color: #102b24; border: 1px solid #142c28; border-radius: 13px; font-size: 17px; font-weight: 700; padding: 0;")
        mark.setFixedSize(max(27, mark.fontMetrics().horizontalAdvance(text) + 12), 27)
        if callback:
            mark.setToolTip("Объяснить вопрос и ответ")
        row.addWidget(mark)
        self.move(round(x), round(y))
        self.show()
        native.configure_overlay(self, callback is not None)


class MainWindow(QMainWindow):
    def __init__(self, settings_path=None, demo=False):
        super().__init__()
        self.setWindowTitle(f"ScreenStudy {__version__} · быстрые ответы")
        self.setMinimumSize(420, 560)
        self.resize(460, 800)
        self.path = settings_path or Path.home()/"Library/Application Support/ScreenStudy/settings.json"
        self.settings = Settings.load(self.path)
        self.chatgpt = chatgpt.manager(self.path.parent)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.settings.always_on_top)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self.key = ""
        self.key_error = ""
        if self.settings.remember_key and not demo:
            try:
                self.key = native.read_key(self.settings.key_account)
            except Exception:
                self.key_error = "Не удалось прочитать API-ключ из Связки ключей. Введите его в настройках."
        self.gate = SceneGate()
        self.running = False
        self.capture_busy = False
        self.ai_busy = False
        self.answer_inflight = False
        self.pending_explanation = None
        self.closing = False
        self.capture_epoch = 0
        self.force_pending = False
        self.select_pending = False
        self.last_request = 0.0
        self.scene_started = 0.0
        self.capture_seconds = 0.0
        self.frame = None
        self.answer_frame = None
        self.layout_reference = None
        self.layout_stable = 0
        self.question_locked = False
        self.cards = []
        self.markers = []
        self.marker_frame = None
        self.demo = demo
        self.jobs = Jobs(self)
        self.jobs.result.connect(self.job_result)
        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.tick)
        self.paste_timer = QTimer(self)
        self.paste_timer.setInterval(1000)
        self.paste_timer.timeout.connect(self.paste_tick)
        self.paste_remaining = 0
        self.paste_text = ""
        self.build_ui()
        self.status.setText(self.key_error or "Пауза · выберите область и подключите ИИ")

    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(12)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(label("SCREEN / STUDY", "eyebrow"))
        titles.addWidget(label("Разберём задание", "title"))
        header.addLayout(titles, 1)
        header.addWidget(button("⚙", self.configure))
        layout.addLayout(header)
        window_controls = QHBoxLayout()
        self.pin_button = button("", self.toggle_pin, "small")
        self.update_pin_button()
        window_controls.addWidget(self.pin_button)
        window_controls.addStretch()
        self.minimize_button = button("Свернуть", self.showMinimized, "small")
        self.minimize_button.setToolTip("Свернуть окно. Для остановки наблюдения нажмите «Пауза».")
        window_controls.addWidget(self.minimize_button)
        layout.addLayout(window_controls)
        self.provider_label = label("", "muted")
        self.update_provider_label()
        layout.addWidget(self.provider_label)
        self.usage_button = button("Использование ChatGPT", ChatGPTDialog.open_usage, "small")
        self.usage_button.setVisible(self.settings.provider == "chatgpt")
        layout.addWidget(self.usage_button)
        quick = QHBoxLayout()
        self.speed_mode = QComboBox()
        self.speed_mode.addItems(["Быстро", "Подумать точнее"])
        self.speed_mode.setCurrentIndex(0 if self.settings.fast_mode else 1)
        self.speed_mode.setEnabled(self.settings.provider == "groq")
        self.speed_mode.setVisible(self.settings.provider != "chatgpt")
        self.speed_mode.setToolTip("Режим рассуждения Groq. Для сложных задач выберите «Подумать точнее».")
        self.image_toggle = QCheckBox("С изображением")
        self.image_toggle.setChecked(self.settings.use_images)
        self.image_toggle.setToolTip("Включите для формул, схем и сопоставления карточек. Нужна модель с поддержкой изображений.")
        quick.addWidget(self.speed_mode)
        quick.addWidget(self.image_toggle)
        layout.addLayout(quick)
        self.speed_mode.currentIndexChanged.connect(self.quick_options_changed)
        self.image_toggle.toggled.connect(self.quick_options_changed)
        controls = QHBoxLayout()
        self.start_button = button("▶ Наблюдать", self.toggle, "primary")
        controls.addWidget(self.start_button)
        self.once_button = button("Ответить сейчас", self.once)
        controls.addWidget(self.once_button)
        layout.addLayout(controls)
        self.question_button = button("Закрепить вопрос", self.toggle_question_lock, "small")
        self.question_button.setEnabled(False)
        layout.addWidget(self.question_button)
        secondary = QHBoxLayout()
        self.region_button = button("Область…", self.select_region, "small")
        secondary.addWidget(self.region_button)
        secondary.addWidget(button("Весь экран", self.reset_region, "small"))
        self.preview_button = button("Показать экран", self.toggle_preview, "small")
        secondary.addWidget(self.preview_button)
        layout.addLayout(secondary)
        self.status = label("", "status")
        layout.addWidget(self.status)
        self.timing = label("", "muted")
        self.timing.hide()
        layout.addWidget(self.timing)
        self.answer_note = label("", "muted")
        self.answer_note.hide()
        layout.addWidget(self.answer_note)
        self.splitter = QSplitter(Qt.Horizontal)
        self.preview = QLabel("Снимок появится после запуска")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(300, 200)
        self.preview.hide()
        self.splitter.addWidget(self.preview)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.card_container = QWidget()
        self.card_layout = QVBoxLayout(self.card_container)
        self.card_layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.setSpacing(12)
        self.scroll.setWidget(self.card_container)
        self.splitter.addWidget(self.scroll)
        layout.addWidget(self.splitter, 1)
        self.empty("Ваш вопрос → понятный ответ", "Откройте задание, выделите область с вопросом и нажмите «Наблюдать». Объяснение откроется по значку ⓘ. Ответ можно отредактировать перед вставкой.")
        footer = label("Ответы ИИ могут быть неточными. Снимки и история не сохраняются.", "muted")
        layout.addWidget(footer)
        quit_row = QHBoxLayout()
        self.cancel_paste = button("Отменить вставку", self.cancel_insert, "small")
        self.cancel_paste.hide()
        quit_row.addWidget(self.cancel_paste)
        quit_row.addStretch()
        quit_row.addWidget(button("Очистить", self.clear, "small"))
        layout.addLayout(quit_row)

    def update_pin_button(self):
        pinned = self.settings.always_on_top
        self.pin_button.setText("Открепить" if pinned else "Закрепить поверх приложений")
        self.pin_button.setToolTip(
            "Окно закреплено поверх приложений. Нажмите, чтобы открепить."
            if pinned else "Обычное окно. Нажмите, чтобы держать его поверх приложений.")

    def toggle_pin(self):
        geometry = self.saveGeometry()
        state = self.windowState()
        visible = self.isVisible()
        self.settings.always_on_top = not self.settings.always_on_top
        # Qt may hide/recreate the native window when its flags change.
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.settings.always_on_top)
        self.restoreGeometry(geometry)
        self.setWindowState(state)
        if visible:
            self.show()
        self.update_pin_button()
        self.save_settings()

    def update_provider_label(self):
        mode = {"ollama": "Локально на Mac", "groq": "Groq", "compatible": "Облачный API", "chatgpt": "Подписка ChatGPT"}[self.settings.provider]
        payload = "изображение + текст" if self.settings.use_images else "только текст OCR"
        self.provider_label.setText(f"{mode} · {self.settings.model}\nПередаётся: {payload} · вопросов за запрос: {self.settings.max_questions}")
        if hasattr(self, "usage_button"):
            self.usage_button.setVisible(self.settings.provider == "chatgpt")

    def quick_options_changed(self, *_):
        self.settings.fast_mode = self.speed_mode.currentIndex() == 0
        self.settings.use_images = self.image_toggle.isChecked()
        self.save_settings()
        self.clear()
        self.update_provider_label()
        self.status.setText("Режим изменён · нажмите «Ответить сейчас» или «Наблюдать»")

    def check_ready(self):
        self.settings.validate()
        if self.settings.provider == "chatgpt" and not self.chatgpt.ready(self.settings.chatgpt_account):
            raise ValueError("Откройте ⚙ → ChatGPT · моя подписка → Войти через ChatGPT.")
        if self.settings.provider == "groq" and not self.key.strip():
            raise ValueError("Откройте ⚙ и введите API-ключ Groq. В настройках есть ссылка для создания ключа.")

    def clear_cards(self):
        self.cards = []
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.update_question_button()

    def empty(self, title, detail):
        self.clear_cards()
        self.card_layout.addSpacing(22)
        self.card_layout.addWidget(label(title, "title"))
        self.card_layout.addWidget(label(detail, "muted"))
        self.card_layout.addStretch()

    def clear_markers(self):
        for mark in self.markers:
            mark.close()
            mark.deleteLater()
        self.markers = []
        self.marker_frame = None

    def update_question_button(self):
        self.question_button.setText("Следующий вопрос →" if self.question_locked else "Закрепить вопрос")
        self.question_button.setEnabled(bool(self.cards) or self.question_locked)
        self.question_button.setToolTip(
            "Снять закрепление и продолжить автоматическое наблюдение."
            if self.question_locked else "Сохранить этот ответ. Новые задания не будут отправляться ИИ.")
        self.once_button.setEnabled(not self.ai_busy and not self.question_locked)

    def toggle_question_lock(self):
        if self.question_locked:
            self.question_locked = False
            self.update_question_button()
            self.toggle()
            self.sync_answer_state()
            return
        if not self.cards:
            return
        self.question_locked = True
        self.running = False
        self.force_pending = False
        self.capture_epoch += 1
        # Invalidate older work without a new settling delay on the current screen.
        if self.frame is not None:
            self.gate.force(self.frame["fingerprint"])
        else:
            self.gate.reset()
        self.start_button.setText("▶ Наблюдать")
        self.update_question_button()
        self.sync_answer_state()
        self.status.setText("Вопрос закреплён · новые задания не отправляются ИИ")
        if not self.demo:
            self.timer.start()  # Local geometry checks only; hide misplaced overlays.
            self.tick()

    def sync_answer_state(self):
        """Never destroy cards for screen changes; overlays have stricter rules."""
        self.answer_note.setVisible(bool(self.cards))
        if not self.cards:
            self.clear_markers()
            return
        same = bool(self.answer_frame and self.frame and same_scene(
            self.answer_frame.get("fingerprint"), self.frame.get("fingerprint")))
        lines = overlay_lines(self.answer_frame, self.frame) if same else None
        if lines is None and same and self.layout_stable >= 3:
            lines = overlay_lines(self.answer_frame, self.frame, allow_move=True)
        if self.question_locked:
            note = "Вопрос закреплён · автоматические ответы выключены."
        elif self.gate.pending:
            note = "Проверяю изменения · последний ответ и объяснение сохранены."
        elif not same and self.answer_frame:
            note = "Сохранён ответ к предыдущему заданию · объяснение относится к нему."
        elif same and lines is None:
            note = "Ответ сохранён · положение задания изменилось, метки скрыты."
        else:
            note = "Ответ сохранён · объяснение доступно по i."
        self.answer_note.setText(note)
        if self.markers and self.marker_frame and overlay_lines(self.marker_frame, self.frame) is None:
            self.clear_markers()
        if lines is None or not (self.running or self.question_locked) or self.gate.pending:
            self.clear_markers()
        elif not self.markers:
            self.show_markers(lines)

    def stop(self, clear=False):
        self.running = False
        self.timer.stop()
        self.capture_epoch += 1
        self.gate.reset()
        self.force_pending = False
        self.clear_markers()
        self.start_button.setText("▶ Наблюдать")
        if clear:
            self.frame = None
            self.answer_frame = None
            self.layout_reference = None
            self.layout_stable = 0
            self.pending_explanation = None
            self.question_locked = False
            self.preview.clear()
        self.update_question_button()
        self.status.setText("Пауза · новые снимки не отправляются")

    def toggle(self):
        if self.running:
            self.stop()
            return
        if self.demo:
            self.status.setText("Демонстрация интерфейса. Для реального захвата запустите на Mac.")
            return
        try:
            self.check_ready()
            if not native.request_screen_permission():
                raise ValueError("Разрешите запись экрана в настройках macOS и перезапустите ScreenStudy.")
        except Exception as exc:
            self.status.setText(str(exc))
            return
        self.gate.reset()
        self.question_locked = False
        self.update_question_button()
        self.running = True
        self.start_button.setText("Ⅱ Пауза")
        self.status.setText("Наблюдаю · жду неподвижное задание")
        self.timer.start()
        self.tick()

    def once(self):
        if self.demo:
            return
        if self.ai_busy:
            self.status.setText("Предыдущий запрос ещё выполняется. Подождите его завершения.")
            return
        self.question_locked = False
        self.update_question_button()
        self.stop()
        try:
            self.check_ready()
            if not native.request_screen_permission():
                raise ValueError("Нужно разрешение записи экрана. После выдачи разрешения перезапустите приложение.")
        except Exception as exc:
            self.status.setText(str(exc))
            return
        self.force_pending = True
        self.status.setText("Снимаю текущий вопрос…")
        self.tick()

    def tick(self):
        if self.capture_busy or self.closing:
            return
        if not (self.running or self.force_pending or self.select_pending or self.question_locked):
            return
        self.capture_busy = True
        self.region_button.setEnabled(False)
        crop = [0, 0, 1, 1] if self.select_pending else self.settings.crop[:]
        display = self.settings.display_id
        tag = ("capture", self.capture_epoch, self.select_pending, time.monotonic())
        previous = None if self.select_pending else self.frame
        images = self.settings.use_images
        self.jobs.run(tag, lambda: prepare_frame(native.capture(display, crop, previous), images))

    def job_result(self, tag, value, error):
        if self.closing:
            return
        kind = tag[0]
        if kind == "capture":
            self.capture_busy = False
            self.region_button.setEnabled(True)
            if tag[1] != self.capture_epoch:
                # A pending manual capture/selection must survive an older job.
                if self.force_pending or self.select_pending:
                    QTimer.singleShot(0, self.tick)
                return
            if error:
                self.stop()
                self.select_pending = False
                self.status.setText(error)
                return
            if tag[2]:
                self.select_pending = False
                picker = RegionPicker(value["image"], self)
                if picker.exec() == QDialog.Accepted:
                    self.settings.crop = picker.crop
                    self.save_settings()
                    self.clear()
                    self.status.setText("Область выбрана. Нажмите «Наблюдать» или «Ответить сейчас».")
                return
            self.frame = value
            if overlay_lines(self.layout_reference, value) is None:
                self.layout_reference, self.layout_stable = value, 1
            else:
                self.layout_stable += 1
            captured_at = tag[3] if len(tag) > 3 else time.monotonic()
            self.render_preview()
            was_pending = self.gate.pending
            changed = self.gate.observe(value["fingerprint"])
            if changed:
                self.scene_started = captured_at
                self.capture_seconds = time.monotonic() - captured_at
                if not self.question_locked:
                    self.status.setText("Новое задание подтверждено · готовлю ответ")
            elif self.gate.pending and not self.question_locked:
                self.status.setText("Проверяю изменения · последний ответ сохранён")
            elif was_pending and not self.question_locked:
                self.status.setText("ИИ разбирает задание…" if self.ai_busy else "Наблюдаю · задание не изменилось")
            self.sync_answer_state()
            if self.force_pending:
                if not self.ai_busy:
                    self.force_pending = False
                    self.gate.force(value["fingerprint"])
                    self.scene_started = captured_at
                    self.capture_seconds = time.monotonic() - captured_at
                    self.ask()
            elif self.running and not self.question_locked and self.gate.ready() and not self.ai_busy:
                if self.cards and self.answer_frame and same_scene(
                        self.answer_frame.get("fingerprint"), value["fingerprint"]):
                    self.gate.mark_sent()
                    self.status.setText("Наблюдаю · ответ для этого задания уже готов")
                elif time.monotonic()-self.last_request >= self.settings.interval:
                    self.ask()
        elif kind == "answer":
            self.ai_busy = False
            self.answer_inflight = False
            if not self.running and not self.question_locked:
                self.timer.stop()
                self.capture_epoch += 1
            self.update_question_button()
            if self.pending_explanation is not None:
                question, self.pending_explanation = self.pending_explanation, None
                self.explain(question)
                return
            if not self.gate.accepts(tag[1]):
                return
            if error:
                self.stop()
                self.status.setText(error + " Наблюдение приостановлено.")
                return
            if not value and self.cards:
                self.sync_answer_state()
                self.status.setText("На новом снимке заданий не найдено · последний ответ сохранён")
                return
            self.show_questions(value, tag[2] if len(tag) > 2 else self.frame)
            elapsed = round(time.monotonic()-self.last_request, 1)
            state = "Наблюдаю" if self.running else "Разовый снимок"
            self.status.setText(f"{state} · вопросов: {len(value)} · ответ за {elapsed} с")
            if self.scene_started:
                total = time.monotonic() - self.scene_started
                self.timing.setText(f"Экран/OCR: {self.capture_seconds:.1f} с · ИИ: {elapsed:.1f} с\nОт начала первого снимка: {total:.1f} с")
                self.timing.show()
        elif kind == "explain":
            self.ai_busy = False
            self.update_question_button()
            question, dialog, output = tag[2:]
            if not error:
                question.update(value)
            if self.gate.accepts(tag[1]):
                self.status.setText(error or "Объяснение готово · нажмите «Наблюдать», чтобы продолжить")
            try:
                if dialog.isVisible():
                    output.setPlainText(self.explanation_text(question) if not error else
                                        self.explanation_text(question) + "\n\n" + error + "\nЗакройте окно и нажмите ⓘ, чтобы повторить.")
            except RuntimeError:
                pass  # The explanation window was closed while the request was pending.
        elif kind == "rewrite":
            self.ai_busy = False
            self.update_question_button()
            card = tag[2]
            if card in self.cards:
                card.rewrite_button.setEnabled(True)
            if not self.gate.accepts(tag[1]) or card not in self.cards:
                return
            if error:
                self.status.setText(error)
            else:
                card.answer.setPlainText(value)
                self.status.setText("Ответ переписан. Можно отредактировать и вставить.")

    def ask(self):
        if self.ai_busy or self.frame is None or self.question_locked:
            return
        if not self.settings.use_images and not self.frame["lines"]:
            self.gate.mark_sent()
            self.status.setText("OCR не нашёл текст. Выберите область крупнее или включите изображения.")
            return
        self.ai_busy = True
        self.answer_inflight = True
        self.once_button.setEnabled(False)
        revision = self.gate.mark_sent()
        self.last_request = time.monotonic()
        self.status.setText("ИИ разбирает задание… Можно поставить наблюдение на паузу.")
        if self.running:
            self.timer.start()
        else:
            self.timer.stop()  # A manual request always answers its original snapshot.
        cfg, key, frame = copy.deepcopy(self.settings), self.key, self.frame
        previous = [{"question": card.question["question"], "answer": card.answer.toPlainText()}
                    for card in self.cards[:1]]
        if self.cards and self.answer_frame and same_scene(
                frame.get("fingerprint"), self.answer_frame.get("fingerprint")):
            previous = copy.deepcopy(self.cards[0].question.get("previous_context", []))
        self.jobs.run(("answer", revision, frame), lambda: ai.analyze(
            cfg, key, frame, auth=self.chatgpt, previous_questions=previous))

    def show_questions(self, questions, frame=None):
        self.clear_markers()
        self.clear_cards()
        self.answer_frame = (frame if frame is not None else self.frame) if questions else None
        if not questions:
            self.answer_note.hide()
            self.empty("Заданий не найдено", "Откройте вопрос или выделите область с заданием.")
            return
        for i, question in enumerate(questions):
            card = QuestionCard(question, i, self)
            self.cards.append(card)
            self.card_layout.addWidget(card)
        self.card_layout.addStretch()
        self.update_question_button()
        self.sync_answer_state()

    def show_markers(self, lines):
        if self.settings.overlays and self.frame and not self.demo:
            self.marker_frame = {**self.frame, "lines": lines}
            x, y, w, h = self.frame["bounds"]
            for card in self.cards:
                question = card.question
                box = union_box(question["line_ids"], lines)
                if box:
                    self.markers.append(Marker("i", max(x, x+box[0]*w-30), y+box[1]*h,
                                               lambda checked=False, q=question: self.explain(q)))
                for mark in answer_marks(question, lines):
                    box = mark["box"]
                    marker = Marker(mark["text"], max(x, x+box[0]*w-30), y+box[1]*h)
                    marker.move(round(max(x, x+box[0]*w-marker.width()-3)), round(y+box[1]*h))
                    self.markers.append(marker)

    def explain(self, question):
        if self.ai_busy and not question["explanation"]:
            if self.answer_inflight:
                self.pending_explanation = question
                self.stop()
                self.status.setText("Открою объяснение сохранённого вопроса после завершения текущего запроса.")
                return
            self.status.setText("Дождитесь завершения текущего запроса.")
            return
        frame = self.answer_frame
        self.stop()
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setWindowTitle("Объяснение задания")
        dialog.resize(570, 550)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("Разбираем по шагам", "title"))
        explanation = QTextEdit()
        explanation.setReadOnly(True)
        explanation.setPlainText(self.explanation_text(question))
        layout.addWidget(explanation)
        layout.addWidget(button("Понятно", dialog.accept, "primary"))
        dialog.show()
        if not question["explanation"]:
            explanation.append("\nЗагружаю объяснение…")
            self.ai_busy = True
            self.once_button.setEnabled(False)
            self.status.setText("Объясняю вопрос · наблюдение на паузе")
            cfg, key = copy.deepcopy(self.settings), self.key
            snapshot = copy.deepcopy(question)
            self.jobs.run(("explain", self.gate.revision, question, dialog, explanation),
                          lambda: ai.explain(cfg, key, snapshot, frame, auth=self.chatgpt))

    @staticmethod
    def explanation_text(question):
        context = ""
        if question.get("previous_context"):
            context = "\n\nИСПОЛЬЗОВАННЫЙ ПРЕДЫДУЩИЙ ОТВЕТ\n" + "\n".join(
                q["question"] + "\n" + q["answer"] for q in question["previous_context"])
        return ("ВОПРОС\n" + question["question"] + "\n\nЧТО НУЖНО СДЕЛАТЬ\n" +
                question["question_explanation"] + "\n\nОТВЕТ\n" + question["answer"] +
                "\n\nПОЧЕМУ\n" + question["explanation"] + context)

    def rewrite(self, card):
        if self.ai_busy:
            self.status.setText("Дождитесь завершения текущего запроса.")
            return
        self.stop()
        instruction, ok = QInputDialog.getText(self, "Переписать ответ", "Как изменить ответ?",
                                               text="Проще и короче, сохрани смысл")
        if not ok or not instruction.strip():
            return
        self.ai_busy = True
        self.once_button.setEnabled(False)
        card.rewrite_button.setEnabled(False)
        self.status.setText("Переписываю ответ…")
        cfg, key = copy.deepcopy(self.settings), self.key
        q, answer = card.question["question"], card.answer.toPlainText()
        self.jobs.run(("rewrite", self.gate.revision, card),
                      lambda: ai.rewrite(cfg, key, q, answer, instruction, auth=self.chatgpt))

    def insert(self, value):
        if not value.strip():
            return
        self.stop()
        QApplication.clipboard().setText(value)
        try:
            if not native.accessibility(True):
                self.status.setText("Включите «Универсальный доступ» для ScreenStudy. Сейчас ответ можно вставить вручную: ⌘V.")
                return
        except Exception as exc:
            self.status.setText(str(exc))
            return
        self.paste_text = value
        self.paste_remaining = 3
        self.status.setText("Вставка через 3 с · нажмите в нужное поле ответа")
        self.cancel_paste.show()
        self.paste_timer.start()

    def paste_tick(self):
        self.paste_remaining -= 1
        if self.paste_remaining > 0:
            self.status.setText(f"Вставка через {self.paste_remaining} с · выберите поле ответа")
            return
        self.paste_timer.stop()
        self.cancel_paste.hide()
        if QApplication.clipboard().text() != self.paste_text:
            self.status.setText("Буфер обмена изменился. Вставка отменена.")
            return
        try:
            native.paste()
            self.status.setText("Команда вставки отправлена. Проверьте текст в выбранном поле.")
        except Exception as exc:
            self.status.setText(str(exc))
        self.paste_text = ""

    def cancel_insert(self):
        self.paste_timer.stop()
        self.paste_text = ""
        self.cancel_paste.hide()
        self.status.setText("Вставка отменена.")

    def configure(self):
        self.cancel_insert()
        self.stop()
        dialog = SettingsDialog(self.settings, self.key, self)
        if dialog.exec() == QDialog.Accepted:
            self.settings, self.key = dialog.value, dialog.key_value
            for control in (self.speed_mode, self.image_toggle):
                control.blockSignals(True)
            self.speed_mode.setCurrentIndex(0 if self.settings.fast_mode else 1)
            self.speed_mode.setEnabled(self.settings.provider == "groq")
            self.speed_mode.setVisible(self.settings.provider != "chatgpt")
            self.image_toggle.setChecked(self.settings.use_images)
            for control in (self.speed_mode, self.image_toggle):
                control.blockSignals(False)
            self.save_settings()
            self.update_provider_label()
            self.clear()

    def save_settings(self):
        try:
            self.settings.save(self.path)
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось сохранить настройки", str(exc))

    def select_region(self):
        if self.demo:
            return
        self.stop()
        try:
            if not native.request_screen_permission():
                raise ValueError("Разрешите запись экрана и перезапустите ScreenStudy.")
        except Exception as exc:
            self.status.setText(str(exc))
            return
        self.select_pending = True
        self.status.setText("Подготавливаю выбор области…")
        self.tick()

    def reset_region(self):
        self.settings.crop = [0.0, 0.0, 1.0, 1.0]
        self.save_settings()
        self.clear()
        self.status.setText("Выбран весь экран. Нажмите «Наблюдать».")

    def toggle_preview(self):
        visible = self.preview.isHidden()
        self.preview.setVisible(visible)
        self.preview_button.setText("Скрыть экран" if visible else "Показать экран")
        self.resize(1100 if visible else 460, self.height())
        self.splitter.setSizes([650, 420])
        self.render_preview()

    def render_preview(self):
        if self.frame and not self.preview.isHidden():
            self.preview.setPixmap(QPixmap.fromImage(self.frame["image"]).scaled(
                self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "frame"):
            self.render_preview()

    def clear(self):
        self.cancel_insert()
        self.stop(clear=True)
        self.select_pending = False
        self.timing.clear()
        self.timing.hide()
        self.answer_note.hide()
        self.empty("Готов к новому заданию", "Откройте вопрос и начните наблюдение.")

    def closeEvent(self, event):
        self.closing = True
        self.stop(clear=True)
        self.paste_timer.stop()
        self.clear_markers()
        event.accept()


def run(demo=False):
    app = QApplication(sys.argv)
    app.setApplicationName("ScreenStudy")
    app.setOrganizationName("ScreenStudy")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MainWindow(demo=demo)
    screen = app.primaryScreen().availableGeometry()
    window.resize(460, min(800, screen.height()-50))
    window.move(screen.right()-window.width()-24, screen.top()+35)
    window.show()
    if demo:
        from .demo import populate
        populate(window)
    return app.exec()
