STYLE = '''
QWidget { background: #111a26; color: #edf3f7; font-size: 13px; }
QMainWindow, QDialog { background: #111a26; }
QLabel { background: transparent; }
QLabel#title { font-size: 25px; font-weight: 700; color: #f7fbff; }
QLabel#eyebrow { color: #71e0c1; font-size: 11px; font-weight: 700; }
QLabel#muted { color: #a3b3c5; font-size: 12px; }
QLabel#status { color: #a8c3d4; padding: 10px; background: #182536; border-radius: 9px; }
QFrame#card { background: #1a2939; border: 1px solid #2c4055; border-radius: 13px; }
QFrame#card QLabel { background: transparent; }
QPushButton { background: #26394d; border: 1px solid #37516a; border-radius: 8px;
 padding: 8px 11px; color: #edf3f7; }
QPushButton:hover { background: #314d65; }
QPushButton:pressed { background: #203347; }
QPushButton:disabled { color: #788a9b; background: #1b2835; border-color: #263748; }
QPushButton#primary { background: #71e0c1; color: #102923; border-color: #71e0c1; font-weight: 700; }
QPushButton#primary:hover { background: #94ecd5; }
QPushButton#primary:disabled { background: #26394d; color: #788a9b; border-color: #37516a; }
QPushButton#small { padding: 5px 8px; font-size: 12px; }
QTextEdit, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
 background: #0f1b28; color: #eef5fa; border: 1px solid #3b5066;
 border-radius: 7px; padding: 7px; selection-background-color: #2f7567;
}
QComboBox QAbstractItemView { background: #182a3b; color: #eef5fa; selection-background-color: #30576c; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: #13202e; width: 8px; border-radius: 4px; }
QScrollBar::handle:vertical { background: #385268; border-radius: 4px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QCheckBox { spacing: 7px; padding: 4px; }
QToolTip { background: #eff8f5; color: #142923; border: none; padding: 8px; }
'''
