"""Sharp 90° QSS — same palette as the original dashboard, zero rounding."""

PALETTE = {
    "bg": "#0f172a",
    "sidebar": "#0b1220",
    "panel": "#1e293b",
    "panel2": "#263548",
    "text": "#e2e8f0",
    "muted": "#94a3b8",
    "accent": "#38bdf8",
    "accent2": "#0ea5e9",
    "ok": "#4ade80",
    "warn": "#fbbf24",
    "err": "#f87171",
    "border": "#334155",
    "nav_active": "#0369a1",
}

APP_QSS = """
* { border-radius: 0; font-family: "Segoe UI", "Ubuntu", sans-serif; font-size: 13px; }

QMainWindow, QWidget#root, QWidget#page {
  background: #0f172a; color: #e2e8f0;
}
QWidget#sidebar {
  background: #0b1220; border-right: 1px solid #334155;
}
QWidget#titlebar {
  background: #0b1220; border-bottom: 1px solid #334155;
}
QLabel { color: #e2e8f0; background: transparent; }
QLabel#muted { color: #94a3b8; }
QLabel#brandTitle { font-size: 15px; font-weight: 700; }
QLabel#brandSub { color: #94a3b8; font-size: 11px; }
QLabel#pageTitle { font-size: 28px; font-weight: 800; letter-spacing: 1px; }
QLabel#statValue { font-size: 26px; font-weight: 700; color: #38bdf8; }
QLabel#statValue[tone="ok"] { color: #4ade80; }
QLabel#statValue[tone="warn"] { color: #fbbf24; }
QLabel#statValue[tone="err"] { color: #f87171; }
QLabel#statLabel { color: #94a3b8; font-size: 12px; }
QLabel#statHint { color: #64748b; font-size: 11px; }

QFrame#card {
  background: #1e293b; border: 1px solid #334155;
}
QFrame#statCard {
  background: #1e293b; border: 1px solid #334155;
}
QFrame#statusBar {
  background: #0b1220; border-top: 1px solid #334155;
}

QPushButton {
  background: #263548; color: #e2e8f0; border: 1px solid #334155;
  padding: 8px 14px; min-height: 18px;
}
QPushButton:hover { border-color: #38bdf8; }
QPushButton:disabled { color: #64748b; }
QPushButton#primary { background: #0ea5e9; border-color: #0ea5e9; color: #0f172a; font-weight: 700; }
QPushButton#primary:hover { background: #38bdf8; }
QPushButton#danger { color: #f87171; border-color: #f87171; }
QPushButton#nav {
  background: transparent; border: none; text-align: left;
  padding: 12px 18px; color: #94a3b8; font-size: 14px;
}
QPushButton#nav:hover { background: #1e293b; color: #e2e8f0; }
QPushButton#nav[active="true"] {
  background: #0369a1; color: #ffffff; font-weight: 700;
}
QPushButton#winbtn {
  background: transparent; border: none; color: #94a3b8;
  padding: 4px 14px; min-width: 36px;
}
QPushButton#winbtn:hover { background: #1e293b; color: #e2e8f0; }
QPushButton#winclose:hover { background: #b91c1c; color: #fff; }

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {
  background: #263548; color: #e2e8f0; border: 1px solid #334155;
  padding: 7px 10px; selection-background-color: #0ea5e9;
}
QComboBox QAbstractItemView {
  background: #1e293b; color: #e2e8f0; border: 1px solid #334155;
  selection-background-color: #0369a1;
}

QTableWidget {
  background: #1e293b; color: #e2e8f0; border: 1px solid #334155;
  gridline-color: #334155; outline: none;
  selection-background-color: #263548; selection-color: #e2e8f0;
}
QHeaderView::section {
  background: #0b1220; color: #94a3b8; border: none;
  border-bottom: 1px solid #334155; border-right: 1px solid #334155;
  padding: 8px; font-weight: 600;
}
QTableWidget::item { padding: 6px; border-bottom: 1px solid #334155; }
QTableCornerButton::section { background: #0b1220; border: none; }

QScrollBar:vertical {
  background: #0b1220; width: 10px; border: none;
}
QScrollBar::handle:vertical { background: #334155; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QDialog { background: #0f172a; color: #e2e8f0; }
QMessageBox { background: #0f172a; }
QStatusBar { background: #0b1220; color: #94a3b8; }

QLabel#pillOk { background: #14532d; color: #4ade80; padding: 6px 12px; font-weight: 700; }
QLabel#pillErr { background: #450a0a; color: #f87171; padding: 6px 12px; font-weight: 700; }
QLabel#pillMute { background: #1e293b; color: #94a3b8; padding: 6px 12px; font-weight: 700; }
"""
