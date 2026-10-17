"""Native PySide6 window — no browser, 90° cards, dashboard palette."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QStackedWidget,
    QSystemTrayIcon, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QFormLayout,
)

from .. import __version__
from .. import updater
from ..resources import tray_icon_path
from .facade import DesktopFacade, FacadeError
from .ipc import start_ipc_server
from .theme import APP_QSS

log = logging.getLogger("apexity_bridge.desktop")

NAV = [
    ("overview", "Overview"),
    ("devices", "Devices"),
    ("device_users", "Device Users"),
    ("students", "People"),
    ("attendance", "Attendance"),
    ("logs", "Logs"),
    ("settings", "Settings"),
]


def _icon() -> QIcon:
    path = tray_icon_path()
    if path and Path(path).exists():
        return QIcon(str(path))
    return QIcon()


def _pixmap(size: int = 36) -> QPixmap:
    icon = _icon()
    if icon.isNull():
        pix = QPixmap(size, size)
        pix.fill(QColor("#0ea5e9"))
        return pix
    return icon.pixmap(QSize(size, size))


def _fmt_ts(value) -> str:
    if not value:
        return "—"
    return str(value).replace("T", " ").replace("+00:00", "")[:19]


def _fmt_uptime(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h {m:02d}m"
    return f"{max(1, seconds // 60)}m"


def _info(parent, text: str) -> None:
    QMessageBox.information(parent, "ApexityEdu Bridge", text)


def _warn(parent, text: str) -> None:
    QMessageBox.warning(parent, "ApexityEdu Bridge", text)


def _ask(parent, text: str) -> bool:
    return QMessageBox.question(parent, "ApexityEdu Bridge", text) == QMessageBox.Yes


class Worker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except FacadeError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            log.exception("desktop worker failed")
            self.failed.emit(str(exc))


class TitleBar(QWidget):
    def __init__(self, window: QMainWindow):
        super().__init__()
        self._window = window
        self.setObjectName("titlebar")
        self.setFixedHeight(40)
        self._drag = None
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 0, 0)
        row.setSpacing(8)
        logo = QLabel()
        logo.setPixmap(_pixmap(22))
        row.addWidget(logo)
        title = QLabel("ApexityEdu Bridge")
        title.setStyleSheet("font-weight: 600; color: #e2e8f0;")
        row.addWidget(title)
        row.addStretch()
        for name, slot, oid in (
            ("—", window.showMinimized, "winbtn"),
            ("□", self._toggle_max, "winbtn"),
            ("×", window.hide, "winclose"),
        ):
            btn = QPushButton(name)
            btn.setObjectName(oid)
            btn.setFixedSize(46, 40)
            btn.clicked.connect(slot)
            row.addWidget(btn)

    def _toggle_max(self):
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self._window.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event):
        self._drag = None

    def mouseDoubleClickEvent(self, event):
        self._toggle_max()


class Sidebar(QWidget):
    navigated = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("sidebar")
        self.setFixedWidth(220)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 18, 0, 12)
        layout.setSpacing(2)

        brand = QHBoxLayout()
        brand.setContentsMargins(18, 0, 18, 16)
        logo = QLabel()
        logo.setPixmap(_pixmap(36))
        brand.addWidget(logo)
        names = QVBoxLayout()
        t = QLabel("ApexityEdu")
        t.setObjectName("brandTitle")
        s = QLabel("Attendance Bridge")
        s.setObjectName("brandSub")
        names.addWidget(t)
        names.addWidget(s)
        brand.addLayout(names)
        layout.addLayout(brand)

        self._buttons: dict[str, QPushButton] = {}
        for key, label in NAV:
            btn = QPushButton(f"  {label}")
            btn.setObjectName("nav")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self._pick(k))
            layout.addWidget(btn)
            self._buttons[key] = btn
        layout.addStretch()
        self._pick("overview")

    def _pick(self, key: str) -> None:
        for k, btn in self._buttons.items():
            btn.setProperty("active", "true" if k == key else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self.navigated.emit(key)


class StatCard(QFrame):
    def __init__(self, label: str, hint: str = ""):
        super().__init__()
        self.setObjectName("statCard")
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 12, 14, 12)
        self._label = QLabel(label)
        self._label.setObjectName("statLabel")
        self._value = QLabel("…")
        self._value.setObjectName("statValue")
        self._hint = QLabel(hint)
        self._hint.setObjectName("statHint")
        box.addWidget(self._label)
        box.addWidget(self._value)
        box.addWidget(self._hint)
        self.setMinimumWidth(130)

    def set_value(self, text: str, tone: str = "") -> None:
        self._value.setText(text)
        self._value.setProperty("tone", tone)
        self._value.style().unpolish(self._value)
        self._value.style().polish(self._value)

    def set_hint(self, text: str) -> None:
        self._hint.setText(text)


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.setSelectionMode(QTableWidget.SingleSelection)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setAlternatingRowColors(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    table.setShowGrid(True)
    return table


def _set_rows(table: QTableWidget, rows: list[list[str]]) -> None:
    table.setRowCount(len(rows))
    for r, cols in enumerate(rows):
        for c, val in enumerate(cols):
            item = QTableWidgetItem(str(val if val is not None else ""))
            table.setItem(r, c, item)


# ------------------------------------------------------------------ pages --
class OverviewPage(QWidget):
    def __init__(self, facade: DesktopFacade):
        super().__init__()
        self.facade = facade
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 12)
        head = QHBoxLayout()
        title = QLabel("OVERVIEW")
        title.setObjectName("pageTitle")
        head.addWidget(title)
        head.addStretch()
        self.pill = QLabel("EMS …")
        self.pill.setObjectName("pillMute")
        head.addWidget(self.pill)
        root.addLayout(head)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.cards = {
            "uptime": StatCard("Bridge uptime", "Since last restart"),
            "ems": StatCard("EMS", "Connection"),
            "devices": StatCard("Devices", "Online"),
            "students": StatCard("People", "Students / teachers / staff"),
            "users": StatCard("Device users", "Unmapped PINs"),
            "pending": StatCard("Pending events", "Awaiting sync"),
            "synced": StatCard("Synced total", "Accepted by EMS"),
            "failed": StatCard("Failed / review", "Needs attention"),
        }
        for i, card in enumerate(self.cards.values()):
            grid.addWidget(card, 0, i)
        root.addLayout(grid)

        wrap = QFrame()
        wrap.setObjectName("card")
        v = QVBoxLayout(wrap)
        h = QLabel("Recent sync attempts")
        h.setStyleSheet("font-weight: 700;")
        v.addWidget(h)
        self.table = _table(["Time", "Type", "Events", "Result", "Detail"])
        v.addWidget(self.table)
        root.addWidget(wrap, 1)

    def refresh(self) -> None:
        d = self.facade.overview()
        ems = d["ems"]
        online = ems.get("online")
        self.pill.setText("EMS ONLINE" if online else "EMS OFFLINE")
        self.pill.setObjectName("pillOk" if online else "pillErr")
        self.pill.style().unpolish(self.pill)
        self.pill.style().polish(self.pill)
        self.cards["uptime"].set_value(_fmt_uptime(d["service"]["uptime_seconds"]))
        self.cards["ems"].set_value("ONLINE" if online else "OFFLINE",
                                    "ok" if online else "err")
        self.cards["ems"].set_hint(ems.get("last_error") or ems.get("protocol") or "not connected")
        self.cards["devices"].set_value(
            f"{d['devices'].get('online_now', 0)} / {d['devices'].get('registered', 0)}")
        self.cards["students"].set_value(str(d["students"]["total"]))
        by_type = d["students"].get("by_type") or {}
        others = (by_type.get("teacher", 0) + by_type.get("staff", 0)
                  + by_type.get("other", 0))
        self.cards["students"].set_hint(
            f"{by_type.get('student', 0)} students · {others} staff/teachers")
        users = d.get("device_users") or {}
        self.cards["users"].set_value(str(users.get("total", 0)))
        self.cards["users"].set_hint(
            f"{users.get('unmapped', 0)} not mapped to a person"
            if users.get("unmapped") else "all mapped")

        verified = ems.get("ingestion_verified")
        if online and not verified:
            self.pill.setText("EMS CONNECTED — NO DATA ACCEPTED YET")
            self.pill.setObjectName("pillWarn")
            self.pill.style().unpolish(self.pill)
            self.pill.style().polish(self.pill)
            self.cards["ems"].set_hint("handshake ok, but no attendance accepted yet")
        pending = d["events"].get("pending", 0) + d["events"].get("failed", 0)
        self.cards["pending"].set_value(str(pending), "warn" if pending else "")
        self.cards["synced"].set_value(f"{d['events'].get('synced', 0):,}")
        review = d["events"].get("review", 0)
        self.cards["failed"].set_value(str(review), "err" if review else "")
        rows = []
        for a in d.get("last_attempts") or []:
            rows.append([
                _fmt_ts(a.get("attempted_at")), a.get("attempt_type") or "",
                a.get("event_count") or "", a.get("result") or "",
                a.get("detail") or a.get("error") or "",
            ])
        _set_rows(self.table, rows)


class DevicesPage(QWidget):
    def __init__(self, facade: DesktopFacade, parent_window):
        super().__init__()
        self.facade = facade
        self._win = parent_window
        self._worker = None
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 12)
        title = QLabel("DEVICES")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        bar = QHBoxLayout()
        scan = QPushButton("Scan LAN now")
        scan.setObjectName("primary")
        scan.clicked.connect(self._scan)
        add = QPushButton("Manual registration (by IP)")
        add.clicked.connect(self._manual)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        bar.addWidget(scan)
        bar.addWidget(add)
        bar.addWidget(self.status)
        bar.addStretch()
        root.addLayout(bar)
        self.table = _table(["Status", "Label", "Serial", "EMS serial", "Model", "IP",
                             "Port", "Firmware", "Last seen", "Events", "Id"])
        self.table.setColumnHidden(9, True)
        self.table.setColumnHidden(10, True)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        root.addWidget(self.table, 1)
        acts = QHBoxLayout()
        for text, fn in (
            ("Register", lambda: self._act("register")),
            ("Test", lambda: self._act("test")),
            ("Disable", lambda: self._act("disable")),
            ("Enable", lambda: self._act("enable")),
            ("Rename", lambda: self._act("label")),
            ("EMS serial…", self._ems_serial),
            ("Remove", lambda: self._act("remove")),
        ):
            b = QPushButton(text)
            if text == "Register":
                b.setObjectName("primary")
            if text == "Remove":
                b.setObjectName("danger")
            b.clicked.connect(fn)
            acts.addWidget(b)
        acts.addStretch()
        root.addLayout(acts)
        self.scan_info = QLabel("no scan yet")
        self.scan_info.setObjectName("muted")
        self.scan_info.setWordWrap(True)
        root.addWidget(self.scan_info)

    def _selected_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 10)
        return int(item.text()) if item else None

    def refresh(self) -> None:
        data = self.facade.devices()
        rows = []
        for d in data["devices"]:
            if d.get("status") == "registered":
                st = "online" if d.get("online_now") else "registered"
            else:
                st = d.get("status") or ""
            ems_sn = (d.get("ems_serial") or "").strip()
            rows.append([
                st, d.get("label") or "", d.get("serial_number") or "?",
                ems_sn or "(same)", d.get("model") or "—", d.get("ip_address") or "—",
                d.get("port") or "", d.get("firmware_version") or "—",
                _fmt_ts(d.get("last_seen_at")), d.get("event_count") or 0, d["id"],
            ])
        _set_rows(self.table, rows)
        ls = data.get("last_scan")
        if ls:
            self.scan_info.setText(
                f"Scan at {_fmt_ts(ls.get('started_at'))} — probed {ls.get('hosts_probed')} hosts, "
                f"{ls.get('tcp_open')} with port 4370 open, {len(ls.get('identified') or [])} identified, "
                f"{len(ls.get('ip_changes') or [])} IP repairs."
            )

    def _scan(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self.status.setText("scanning LAN…")
        self._worker = Worker(self.facade.scan_now)
        self._worker.done.connect(self._scan_done)
        self._worker.failed.connect(self._scan_fail)
        self._worker.start()

    def _scan_done(self, report):
        self.status.setText("")
        _info(self, f"Scan complete: {report.get('hosts_probed')} hosts probed, "
                    f"{len(report.get('identified') or [])} terminals identified.")
        self.refresh()

    def _scan_fail(self, msg):
        self.status.setText("")
        _warn(self, f"Scan failed: {msg}")

    def _act(self, action: str) -> None:
        did = self._selected_id()
        if did is None:
            _warn(self, "Select a device first.")
            return
        kw = {}
        if action == "label":
            text, ok = QInputDialog.getText(self, "Device label", "Label:")
            if not ok:
                return
            kw["label"] = text
        if action == "register":
            text, ok = QInputDialog.getText(self, "Register device", "Label (optional):", text="K40")
            if not ok:
                return
            kw["label"] = text
        if action == "remove" and not _ask(self, "Remove this device?"):
            return

        def work():
            return self.facade.device_action(did, action, **kw)

        self._worker = Worker(work)
        self._worker.done.connect(lambda result: self._act_done(action, result))
        self._worker.failed.connect(lambda m: _warn(self, m))
        self._worker.start()

    def _act_done(self, action, result):
        if action == "test":
            _info(self, f"Test OK in {result.get('elapsed_ms')}ms — serial {result.get('serial')}")
        self.refresh()

    def _ems_serial(self) -> None:
        """The serial apexityedu.com knows this device by."""
        did = self._selected_id()
        if did is None:
            _warn(self, "Select a device first.")
            return
        device = self.facade.ctx.repos["devices"].get_by_id(did)
        current = (device or {}).get("ems_serial") or ""
        text, ok = QInputDialog.getText(
            self, "EMS device serial",
            "Serial registered in apexityedu.com → Biometric Devices.\n"
            "Leave empty to use the hardware serial.\n\n"
            f"Hardware serial: {(device or {}).get('serial_number') or '?'}",
            text=current)
        if not ok:
            return
        try:
            self.facade.set_ems_serial(did, text)
            self.refresh()
            _info(self, "EMS serial saved. The bridge will push under this serial.")
        except FacadeError as exc:
            _warn(self, str(exc))

    def _manual(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Manual device registration")
        form = QFormLayout(dlg)
        ip = QLineEdit()
        ip.setPlaceholderText("192.168.0.201")
        port = QSpinBox()
        port.setRange(1, 65535)
        port.setValue(4370)
        key = QSpinBox()
        key.setRange(0, 999999)
        form.addRow("IP address", ip)
        form.addRow("Port", port)
        form.addRow("Comm key", key)
        row = QHBoxLayout()
        okb = QPushButton("Verify & add")
        okb.setObjectName("primary")
        cancel = QPushButton("Cancel")
        row.addWidget(okb)
        row.addWidget(cancel)
        form.addRow(row)
        cancel.clicked.connect(dlg.reject)

        def go():
            try:
                self.facade.device_manual(ip.text(), port.value(), key.value())
                dlg.accept()
                self.refresh()
                _info(self, "Device verified. Register it from the table when ready.")
            except FacadeError as exc:
                _warn(dlg, str(exc))

        okb.clicked.connect(go)
        dlg.exec()


class StudentsPage(QWidget):
    def __init__(self, facade: DesktopFacade):
        super().__init__()
        self.facade = facade
        self.page = 1
        self._worker = None
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 12)
        title = QLabel("PEOPLE")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name / ID / roll / staff no…")
        self.search.returnPressed.connect(lambda: self.refresh(reset=True))
        self.cls = QComboBox()
        self.cls.addItem("Class: all", "")
        self.sec = QComboBox()
        self.sec.addItem("Section: all", "")
        self.act = QComboBox()
        self.act.addItem("Active+inactive", "all")
        self.act.addItem("Active", "1")
        self.act.addItem("Inactive", "0")
        self.ptype = QComboBox()
        self.ptype.addItem("Everyone", "")
        self.ptype.addItem("Students", "student")
        self.ptype.addItem("Teachers", "teacher")
        self.ptype.addItem("Staff", "staff")
        self.ptype.currentIndexChanged.connect(lambda: self.refresh(reset=True))
        refresh = QPushButton("Refresh from EMS")
        refresh.clicked.connect(self._refresh_roster)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.ptype)
        bar.addWidget(self.cls)
        bar.addWidget(self.sec)
        bar.addWidget(self.act)
        bar.addWidget(refresh)
        root.addLayout(bar)
        self.table = _table(["Name", "EMS ID", "Type", "Admission / Staff no", "Roll",
                             "Class", "Section", "Designation", "Status", "Biometric"])
        root.addWidget(self.table, 1)
        pager = QHBoxLayout()
        prev = QPushButton("‹")
        nxt = QPushButton("›")
        prev.clicked.connect(lambda: self._page(-1))
        nxt.clicked.connect(lambda: self._page(1))
        self.page_info = QLabel("")
        self.page_info.setObjectName("muted")
        pager.addWidget(prev)
        pager.addWidget(self.page_info)
        pager.addWidget(nxt)
        pager.addStretch()
        mapb = QPushButton("Map selected person…")
        mapb.setObjectName("primary")
        mapb.clicked.connect(self._map)
        pager.addWidget(mapb)
        root.addLayout(pager)

        push_row = QHBoxLayout()
        push_people = QPushButton("Push people to ApexityEdu")
        push_people.setObjectName("primary")
        push_people.clicked.connect(self._push_people)
        add_person = QPushButton("Add person to ApexityEdu…")
        add_person.clicked.connect(self._add_person)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        push_row.addWidget(push_people)
        push_row.addWidget(add_person)
        push_row.addWidget(self.status, 1)
        root.addLayout(push_row)
        self.unmapped = _table(["Device", "Device user ID", "Events", "Last seen"])
        um = QFrame()
        um.setObjectName("card")
        uv = QVBoxLayout(um)
        uv.addWidget(QLabel("Unmapped device users"))
        uv.addWidget(self.unmapped)
        root.addWidget(um)

    def _page(self, delta: int) -> None:
        self.page = max(1, self.page + delta)
        self.refresh()

    def refresh(self, reset: bool = False) -> None:
        if reset:
            self.page = 1
        active_q = self.act.currentData()
        active = None if active_q in (None, "", "all") else active_q == "1"
        data = self.facade.students(
            search=self.search.text().strip() or None,
            class_name=self.cls.currentData() or None,
            section=self.sec.currentData() or None,
            person_type=self.ptype.currentData() or None,
            active=active, page=self.page,
        )
        if self.cls.count() == 1:
            for c in data.get("classes") or []:
                self.cls.addItem(c.get("class") or "", c.get("class") or "")
        rows = []
        for s in data["students"]:
            rows.append([
                s.get("name") or "", s.get("ems_student_id") or "",
                s.get("person_type") or "student",
                s.get("admission_no") or s.get("employee_no") or "—",
                s.get("roll_no") or "—",
                s.get("class_name") or "—", s.get("section") or "—",
                s.get("designation") or s.get("department") or "—",
                "active" if s.get("active") else "inactive",
                s.get("device_users") or "—",
            ])
        _set_rows(self.table, rows)
        total = data["total"]
        pages = max(1, (total + data["page_size"] - 1) // data["page_size"])
        self.page_info.setText(f"page {data['page']} / {pages} ({total})")
        try:
            review = self.facade.mapping_review()
        except Exception:
            review = {}
        um = []
        for u in review.get("unmapped_device_users") or []:
            um.append([u.get("device_serial"), u.get("device_user_id"),
                       u.get("events"), _fmt_ts(u.get("last_ts"))])
        _set_rows(self.unmapped, um)

    def _refresh_roster(self) -> None:
        self._worker = Worker(self.facade.refresh_roster)
        self._worker.done.connect(lambda s: (_info(self, f"Roster refreshed: {s}"), self.refresh()))
        self._worker.failed.connect(lambda m: _warn(self, m))
        self._worker.start()

    # ------------------------------------------------- manual push to the EMS --
    def _push_people(self) -> None:
        """Push the cached directory (students, teachers, staff) to the EMS."""
        if self._worker and self._worker.isRunning():
            return
        only = self.ptype.currentData() or None
        types = [only] if only else None
        self.status.setText("pushing the people directory to ApexityEdu…")

        def work():
            return self.facade.push_people(person_types=types)

        self._worker = Worker(work)
        self._worker.done.connect(self._push_people_done)
        self._worker.failed.connect(
            lambda m: (self.status.setText(""), _warn(self, m)))
        self._worker.start()

    def _push_people_done(self, result) -> None:
        self.status.setText("")
        if result.get("unsupported"):
            _warn(self, result.get("error") or result.get("detail")
                  or "this EMS cannot store people")
            return
        if not result.get("ok"):
            _warn(self, result.get("error") or "The people push failed.")
            return
        by_type = result.get("by_type") or {}
        summary = ", ".join(f"{count} {kind}" for kind, count in sorted(by_type.items()))
        _info(self, f"Pushed {result.get('accepted', 0)} of {result.get('people', 0)} "
                    f"people to ApexityEdu ({summary}).")
        self.refresh()

    def _add_person(self) -> None:
        """Manual button: create a student / teacher / staff member in the EMS."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Add person to ApexityEdu")
        dlg.setMinimumWidth(420)
        form = QFormLayout(dlg)
        pid = QLineEdit()
        pid.setPlaceholderText("S1001 (student) · T2001 (teacher) · X3001 (staff)")
        name = QLineEdit()
        ptype = QComboBox()
        for label, value in (("Student", "student"), ("Teacher", "teacher"),
                             ("Staff", "staff"), ("Other", "other")):
            ptype.addItem(label, value)
        admission = QLineEdit()
        roll = QLineEdit()
        klass = QLineEdit()
        section = QLineEdit()
        employee = QLineEdit()
        designation = QLineEdit()
        department = QLineEdit()
        form.addRow("EMS id", pid)
        form.addRow("Name", name)
        form.addRow("Type", ptype)
        form.addRow("Admission № (student)", admission)
        form.addRow("Roll № (student)", roll)
        form.addRow("Class (student)", klass)
        form.addRow("Section (student)", section)
        form.addRow("Employee № (staff)", employee)
        form.addRow("Designation (staff)", designation)
        form.addRow("Department (staff)", department)
        okb = QPushButton("Create in ApexityEdu")
        okb.setObjectName("primary")
        form.addRow(okb)

        def go() -> None:
            payload = {
                "id": pid.text().strip(),
                "name": name.text().strip(),
                "person_type": ptype.currentData(),
                "admission_no": admission.text().strip() or None,
                "roll_no": roll.text().strip() or None,
                "class_name": klass.text().strip() or None,
                "section": section.text().strip() or None,
                "employee_no": employee.text().strip() or None,
                "designation": designation.text().strip() or None,
                "department": department.text().strip() or None,
                "active": True,
            }
            try:
                result = self.facade.create_person(payload)
            except FacadeError as exc:
                _warn(dlg, str(exc))
                return
            if result.get("unsupported"):
                _warn(dlg, result.get("error")
                      or "this EMS cannot store people yet")
                return
            if not result.get("ok"):
                _warn(dlg, result.get("error") or "ApexityEdu refused the person.")
                return
            dlg.accept()
            _info(self, f"{payload['name']} ({payload['person_type']}) is now in "
                        f"ApexityEdu.")
            self.refresh()

        okb.clicked.connect(go)
        dlg.exec()

    def _map(self) -> None:
        row = self.table.currentRow()
        student = self.table.item(row, 1).text() if row >= 0 and self.table.item(row, 1) else ""
        dlg = QDialog(self)
        dlg.setWindowTitle("Map device user to person")
        form = QFormLayout(dlg)
        serial = QLineEdit()
        uid = QLineEdit()
        ems = QLineEdit(student)
        form.addRow("Device serial", serial)
        form.addRow("Device user ID", uid)
        form.addRow("EMS person ID", ems)
        okb = QPushButton("Save mapping")
        okb.setObjectName("primary")
        form.addRow(okb)

        def go():
            try:
                self.facade.assign_mapping(serial.text(), uid.text(), ems.text())
                dlg.accept()
                self.refresh()
            except FacadeError as exc:
                _warn(dlg, str(exc))

        okb.clicked.connect(go)
        dlg.exec()


class DeviceUsersPage(QWidget):
    """Who does each K40 actually know about — and who is that, really?

    This page is the answer to "the software shows punches but not the user".
    A terminal only ever reports a PIN; until that PIN is bound to a person in
    the EMS directory, neither the bridge nor apexityedu can say who punched.
    """

    def __init__(self, facade: DesktopFacade, parent_window):
        super().__init__()
        self.facade = facade
        self._win = parent_window
        self._worker = None
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 12)
        title = QLabel("DEVICE USERS")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        hint = QLabel(
            "Users enrolled on each terminal, read over the ZK protocol (read-only). "
            "Bind every PIN to the student, teacher or staff member it belongs to — "
            "punches then reach ApexityEdu attributed to a real person."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        root.addWidget(hint)

        bar = QHBoxLayout()
        self.device = QComboBox()
        self.device.addItem("All devices", "")
        self.device.currentIndexChanged.connect(lambda: self.refresh())
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search PIN / name / person…")
        self.search.returnPressed.connect(self.refresh)
        self.unmapped = QComboBox()
        self.unmapped.addItem("Everyone", "")
        self.unmapped.addItem("Only unmapped", "1")
        self.unmapped.currentIndexChanged.connect(lambda: self.refresh())
        bar.addWidget(self.device, 1)
        bar.addWidget(self.search, 2)
        bar.addWidget(self.unmapped)
        root.addLayout(bar)

        self.table = _table(["Device", "PIN", "Name on device", "Role", "Punches",
                             "Last punch", "Mapped to", "Person type", "User id"])
        self.table.setColumnHidden(8, True)
        root.addWidget(self.table, 1)

        acts = QHBoxLayout()
        for text, fn in (
            ("Read from device", self._read),
            ("Push users to EMS", self._push),
            ("Auto-map matches", self._auto),
            ("Map to person…", self._map),
            ("Unmap", self._unmap),
        ):
            b = QPushButton(text)
            if text in ("Read from device", "Push users to EMS"):
                b.setObjectName("primary")
            b.clicked.connect(fn)
            acts.addWidget(b)
        acts.addStretch()
        root.addLayout(acts)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

    def refresh(self) -> None:
        data = self.facade.device_users(
            device_serial=self.device.currentData() or None,
            search=self.search.text().strip() or None,
            unmapped_only=self.unmapped.currentData() == "1",
        )
        if self.device.count() <= 1:
            for d in data.get("devices") or []:
                label = d.get("label") or d.get("serial_number") or "?"
                self.device.addItem(f"{label} ({d.get('serial_number')})",
                                    d.get("serial_number"))
        rows = []
        for u in data["device_users"]:
            person = u.get("person_name")
            ptype = u.get("person_type") or "—"
            rows.append([
                u.get("device_label") or u.get("device_serial") or "",
                u.get("user_id") or "",
                u.get("name") or "(no name on device)",
                "admin" if (u.get("privilege") or 0) >= 14 else "user",
                u.get("punch_count") or 0,
                _fmt_ts(u.get("last_punch_at")),
                person or ("" if u.get("mapping_id") else "NOT MAPPED"),
                ptype if u.get("mapping_id") else "—",
                u.get("user_id") or "",
            ])
        _set_rows(self.table, rows)
        self.status.setText(
            f"{data['total']} users · {data.get('unmapped', 0)} not mapped to a person")

    def _selected(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        serial_item = self.table.item(row, 0)
        pin_item = self.table.item(row, 1)
        if not pin_item:
            return None
        return {"pin": pin_item.text(), "device": serial_item.text() if serial_item else ""}

    def _device_picker(self) -> int | None:
        devices = self.facade.ctx.repos["devices"].list_active()
        if not devices:
            _warn(self, "No registered device. Register a K40 on the Devices page first.")
            return None
        if len(devices) == 1:
            return devices[0]["id"]
        labels = [f"{d.get('label') or d.get('serial_number')} ({d.get('serial_number')})"
                  for d in devices]
        choice, ok = QInputDialog.getItem(self, "Select device", "Device:", labels, 0, False)
        if not ok:
            return None
        return devices[labels.index(choice)]["id"]

    def _read(self) -> None:
        did = self._device_picker()
        if did is None:
            return
        self.status.setText("reading the user table from the device…")

        def work():
            return self.facade.read_device_users(did)

        self._worker = Worker(work)
        self._worker.done.connect(self._read_done)
        self._worker.failed.connect(lambda m: (self.status.setText(""), _warn(self, m)))
        self._worker.start()

    def _read_done(self, result):
        self.status.setText("")
        _info(self, f"Read {result.get('on_device', 0)} users from the device "
                    f"({result.get('inserted', 0)} new).")
        self.refresh()

    def _push(self) -> None:
        """Push the user tables to ApexityEdu right now — and show the result.

        The old button only queued the work, so a push that the server
        refused looked exactly like a push that worked. The server's answer
        is now shown verbatim.
        """
        if self._worker and self._worker.isRunning():
            return
        serial = self.device.currentData() or None
        device_id = None
        if serial:
            devices = self.facade.ctx.repos["devices"].list_active()
            match = [d for d in devices if d.get("serial_number") == serial]
            device_id = match[0]["id"] if match else None
        self.status.setText("publishing the user tables to ApexityEdu…")

        def work():
            return self.facade.push_device_users(device_id)

        self._worker = Worker(work)
        self._worker.done.connect(self._push_done)
        self._worker.failed.connect(
            lambda m: (self.status.setText(""), _warn(self, m)))
        self._worker.start()

    def _push_done(self, result) -> None:
        self.status.setText("")
        text = result.get("text") or ""
        if result.get("ok"):
            _info(self, text)
        else:
            _warn(self, text or "The push did not succeed.")
        self.refresh()

    def _auto(self) -> None:
        """Bind every PIN whose name (or number) matches exactly one person."""
        serial = self.device.currentData() or None
        try:
            result = self.facade.auto_map(serial)
        except FacadeError as exc:
            _warn(self, str(exc))
            return
        ambiguous = result.get("ambiguous") or []
        detail = f"{len(result['matched'])} bound automatically."
        if ambiguous:
            detail += ("\n\nLeft for you (" + str(len(ambiguous)) + " ambiguous):\n"
                       + "\n".join(f"PIN {a['device_user_id']} — "
                                    f"{', '.join(a['candidates']) or 'no match'}"
                                    for a in ambiguous[:12]))
        _info(self, detail)
        self.refresh()

    def _map(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            _warn(self, "Select a device user first.")
            return
        pin = self.table.item(row, 1).text()
        serial = self.table.item(row, 0).text()
        devices = self.facade.ctx.repos["devices"].list_active()
        serials = [d["serial_number"] for d in devices]
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Bind PIN {pin} to a person")
        form = QFormLayout(dlg)
        sbox = QComboBox()
        for sn in serials:
            sbox.addItem(sn, sn)
        if serial in serials:
            sbox.setCurrentIndex(serials.index(serial))
        elif serials:
            _warn(dlg, f"Pick the device serial — {serial!r} is a display label.")
        person = QLineEdit()
        person.setPlaceholderText("EMS person id (People page) — e.g. S1001 or T12")
        form.addRow("Device serial", sbox)
        form.addRow("PIN (device user id)", QLabel(pin))
        form.addRow("EMS person id", person)
        okb = QPushButton("Save mapping")
        okb.setObjectName("primary")
        form.addRow(okb)

        def go():
            try:
                self.facade.assign_mapping(sbox.currentData(), pin, person.text().strip())
                dlg.accept()
                self.refresh()
            except FacadeError as exc:
                _warn(dlg, str(exc))

        okb.clicked.connect(go)
        dlg.exec()

    def _unmap(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            _warn(self, "Select a device user first.")
            return
        pin = self.table.item(row, 1).text()
        serial = self.table.item(row, 0).text()
        devices = self.facade.ctx.repos["devices"].list_active()
        for d in devices:
            if (d.get("label") or d.get("serial_number")) == serial or \
                    d["serial_number"] == serial:
                mapping = self.facade.ctx.repos["mappings"].get(d["serial_number"], pin)
                if not mapping:
                    _info(self, "That device user is not mapped.")
                    return
                if not _ask(self, f"Remove the mapping for PIN {pin}?"):
                    return
                try:
                    self.facade.unassign_mapping(mapping["id"])
                    self.refresh()
                except FacadeError as exc:
                    _warn(self, str(exc))
                return
        _warn(self, "Select a row that belongs to a registered device.")



class AttendancePage(QWidget):
    def __init__(self, facade: DesktopFacade):
        super().__init__()
        self.facade = facade
        self.page = 1
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 12)
        title = QLabel("ATTENDANCE")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search user / student / device…")
        self.search.returnPressed.connect(lambda: self.refresh(reset=True))
        self.status = QComboBox()
        for label, val in (("All statuses", ""), ("Pending", "pending"),
                           ("Synced", "synced"), ("Failed", "failed"),
                           ("Review", "review"), ("Rejected", "rejected")):
            self.status.addItem(label, val)
        self.status.currentIndexChanged.connect(lambda: self.refresh(reset=True))
        retry = QPushButton("Retry selected")
        retry.setObjectName("primary")
        retry.clicked.connect(self._retry)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.status)
        bar.addWidget(retry)
        root.addLayout(bar)
        self.table = _table(["Id", "Event time", "Student", "Device user",
                             "Device", "Punch", "Sync", "Attempts", "Last error"])
        self.table.setColumnHidden(0, True)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        root.addWidget(self.table, 1)
        pager = QHBoxLayout()
        prev = QPushButton("‹")
        nxt = QPushButton("›")
        prev.clicked.connect(lambda: self._page(-1))
        nxt.clicked.connect(lambda: self._page(1))
        self.page_info = QLabel("")
        pager.addWidget(prev)
        pager.addWidget(self.page_info)
        pager.addWidget(nxt)
        pager.addStretch()
        root.addLayout(pager)

    def _page(self, delta: int) -> None:
        self.page = max(1, self.page + delta)
        self.refresh()

    def refresh(self, reset: bool = False) -> None:
        if reset:
            self.page = 1
        data = self.facade.events(
            status=self.status.currentData() or None,
            search=self.search.text().strip() or None, page=self.page,
        )
        rows = []
        for e in data["events"]:
            rows.append([
                e.get("id"), _fmt_ts(e.get("event_timestamp")),
                e.get("student_name") or e.get("ems_student_id") or "—",
                e.get("device_user_id") or "", e.get("device_serial") or "",
                f"{e.get('punch')}/{e.get('status')}",
                e.get("sync_status") or "", e.get("attempt_count") or 0,
                e.get("last_error") or "",
            ])
        _set_rows(self.table, rows)
        total = data["total"]
        pages = max(1, (total + data["page_size"] - 1) // data["page_size"])
        self.page_info.setText(f"page {data['page']} / {pages} ({total})")

    def _retry(self) -> None:
        ids = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if item:
                ids.append(int(item.text()))
        if not ids:
            _warn(self, "Select one or more events.")
            return
        try:
            n = self.facade.retry_events(ids)
            _info(self, f"Re-queued {n} event(s).")
            self.refresh()
        except FacadeError as exc:
            _warn(self, str(exc))


class LogsPage(QWidget):
    def __init__(self, facade: DesktopFacade):
        super().__init__()
        self.facade = facade
        self.page = 1
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 12)
        title = QLabel("LOGS")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search audit log…")
        self.search.returnPressed.connect(lambda: self.refresh(reset=True))
        bar.addWidget(self.search, 1)
        root.addLayout(bar)
        self.table = _table(["Time", "Actor", "Action", "Entity", "Detail"])
        root.addWidget(self.table, 1)
        self.app_log = QPlainTextEdit()
        self.app_log.setReadOnly(True)
        self.app_log.setMaximumHeight(180)
        root.addWidget(QLabel("Application log"))
        root.addWidget(self.app_log)

    def refresh(self, reset: bool = False) -> None:
        if reset:
            self.page = 1
        data = self.facade.logs(search=self.search.text().strip() or None, page=self.page)
        rows = [[_fmt_ts(l.get("created_at")), l.get("actor") or "",
                 l.get("action") or "", l.get("entity_id") or l.get("entity") or "",
                 l.get("detail") or ""] for l in data["logs"]]
        _set_rows(self.table, rows)
        self.app_log.setPlainText("\n".join(self.facade.app_log()) or "(log empty)")


class SettingsPage(QWidget):
    def __init__(self, facade: DesktopFacade):
        super().__init__()
        self.facade = facade
        self._worker = None
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 12)
        title = QLabel("SETTINGS")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        card = QFrame()
        card.setObjectName("card")
        form = QFormLayout(card)
        form.setContentsMargins(16, 16, 16, 16)
        self.fields = {}
        for key, label in (
            ("ems_base_url", "EMS base URL"),
            ("ems_protocol", "Protocol (auto / iclock / rest)"),
            ("device_timezone", "Device timezone"),
            ("scan_interval", "Scan interval (s)"),
            ("collect_interval", "Collect interval (s)"),
            ("sync_interval", "Sync retry base (s)"),
            ("roster_interval", "Roster interval (s)"),
            ("users_sync_interval", "Device-user sync interval (s)"),
            ("ems_push_users", "Publish device users to EMS (1/0)"),
            ("ems_execute_commands", "Answer read-only EMS commands (1/0)"),
        ):
            edit = QLineEdit()
            self.fields[key] = edit
            form.addRow(label, edit)
        save = QPushButton("Save settings")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        test = QPushButton("Test EMS connection")
        test.clicked.connect(self._test)
        verify = QPushButton("Verify what the EMS accepts")
        verify.clicked.connect(self._verify)
        diag2 = QPushButton("Verify + copy report")
        diag2.clicked.connect(lambda: self._verify(copy=True))
        doctor = QPushButton("Run backend self-check")
        doctor.setObjectName("primary")
        doctor.clicked.connect(self._doctor)
        row = QHBoxLayout()
        row.addWidget(save)
        row.addWidget(test)
        row.addWidget(verify)
        row.addWidget(diag2)
        row.addWidget(doctor)
        row.addStretch()
        form.addRow(row)
        self.token_lbl = QLabel("")
        self.token_lbl.setObjectName("muted")
        form.addRow("Token", self.token_lbl)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("paste EMS device token — leave empty to clear")
        tokb = QPushButton("Save token")
        tokb.clicked.connect(self._save_token)
        form.addRow(self.token, tokb)
        self.cur = QLineEdit()
        self.cur.setEchoMode(QLineEdit.Password)
        self.new = QLineEdit()
        self.new.setEchoMode(QLineEdit.Password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.Password)
        form.addRow("Current password", self.cur)
        form.addRow("New password", self.new)
        form.addRow("Repeat password", self.confirm)
        pwb = QPushButton("Change password")
        pwb.clicked.connect(self._pw)
        form.addRow(pwb)
        bak = QPushButton("Backup database now")
        bak.clicked.connect(self._backup)
        diag = QPushButton("Export diagnostics")
        diag.clicked.connect(self._diag)
        form.addRow(bak, diag)
        root.addWidget(card)

        # --- auto-start & scheduled sync ---------------------------------
        sched = QFrame()
        sched.setObjectName("card")
        sform = QVBoxLayout(sched)
        stitle = QLabel("Auto-start & scheduled sync")
        stitle.setStyleSheet("font-weight: 700;")
        sform.addWidget(stitle)
        shint = QLabel(
            "Auto-start registers a Windows Task Scheduler task so the bridge "
            "starts with Windows and restarts itself after a crash. The "
            "scheduled sync is a second task that pushes attendance, device "
            "users and the people directory to ApexityEdu every 15 minutes — "
            "useful when the desktop app is closed between shifts."
        )
        shint.setObjectName("muted")
        shint.setWordWrap(True)
        sform.addWidget(shint)
        srow = QHBoxLayout()
        for text, action in (
            ("Install auto-start", "install"),
            ("Start", "start"),
            ("Stop", "stop"),
            ("Status", "status"),
            ("Uninstall auto-start", "uninstall"),
            ("Add scheduled sync", "install-sync"),
            ("Remove scheduled sync", "uninstall-sync"),
        ):
            b = QPushButton(text)
            if text == "Install auto-start":
                b.setObjectName("primary")
            if action != "status":
                b.clicked.connect(lambda _=False, a=action: self._scheduler(a))
            else:
                b.clicked.connect(lambda _=False: self._scheduler_status())
            srow.addWidget(b)
        srow.addStretch()
        sform.addLayout(srow)
        sync_now = QPushButton("Sync now (roster + users + attendance)")
        sync_now.clicked.connect(self._sync_now)
        sform.addWidget(sync_now)
        self.sched_status = QLabel("")
        self.sched_status.setObjectName("muted")
        self.sched_status.setWordWrap(True)
        sform.addWidget(self.sched_status)
        root.addWidget(sched)
        root.addStretch()

    def refresh(self) -> None:
        d = self.facade.settings()
        for key, edit in self.fields.items():
            val = d["settings"].get(key)
            if val in (None, ""):
                val = d["effective"].get(key, "")
            edit.setText("" if val is None else str(val))
        self.token_lbl.setText(
            "configured (Credential Manager)" if d["token_configured"]
            else "NOT configured — paste a token below")
        self._scheduler_status(silent=True)

    # ------------------------------------------------- scheduler & manual sync --
    def _scheduler_status(self, silent: bool = False) -> None:
        try:
            overview = self.facade.scheduler_overview()
        except Exception as exc:            # pragma: no cover - defensive
            self.sched_status.setText(f"scheduler: {exc}")
            return
        if not overview.get("supported"):
            self.sched_status.setText(overview.get("message") or "")
            return
        lines = []
        for task in overview.get("tasks") or []:
            state = task.get("state") or ("installed" if task.get("installed")
                                          else "NOT installed")
            lines.append(f"{task.get('name')}: {state}")
        self.sched_status.setText("  ·  ".join(lines))
        if not silent:
            _info(self, "\n".join(lines) or "No scheduled task found.")

    def _scheduler(self, action: str) -> None:
        try:
            result = self.facade.scheduler_action(action)
        except FacadeError as exc:
            _warn(self, str(exc))
            return
        message = result.get("message") or ""
        if result.get("ok"):
            _info(self, message)
        else:
            _warn(self, message or f"The scheduler action {action!r} failed.")
        self._scheduler_status(silent=True)

    def _sync_now(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.sched_status.setText("syncing…")

        def work():
            return self.facade.sync_now()

        self._worker = Worker(work)
        self._worker.done.connect(self._sync_now_done)
        self._worker.failed.connect(
            lambda m: (self.sched_status.setText(""), _warn(self, m)))
        self._worker.start()

    def _sync_now_done(self, report) -> None:
        roster = report.get("roster") or {}
        users = report.get("users") or {}
        attendance = report.get("attendance") or {}
        errors = report.get("errors") or []
        text = (f"roster: {roster.get('fetched', 0)} people · "
                f"device users: {users.get('pushed', 0)} terminal(s) · "
                f"attendance: {attendance.get('pushed', 0)} event(s) pushed")
        if errors:
            text += "\n\n" + "\n".join(errors)
        self.sched_status.setText(text)
        if errors:
            _warn(self, text)
        else:
            _info(self, text)

    def _save(self) -> None:
        try:
            changed = self.facade.save_settings({k: e.text() for k, e in self.fields.items()})
            _info(self, f"Saved: {', '.join(changed) or 'no changes'}")
        except FacadeError as exc:
            _warn(self, str(exc))

    def _test(self) -> None:
        try:
            d = self.facade.ems_test()
            health = d.get("health") or {}
            _info(self, f"Probe: {d.get('probe')}\nHealth: {health}")
        except FacadeError as exc:
            _warn(self, str(exc))

    def _verify(self, copy: bool = False) -> None:
        """Handshake-green is not proof — show what the server really accepts."""
        try:
            report = self.facade.ems_verify()
        except FacadeError as exc:
            _warn(self, str(exc))
            return
        text = report.get("text") or str(report)
        if copy:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(text)
            _info(self, "Report copied to the clipboard.\n\n" + text)
        else:
            _info(self, text)

    def _doctor(self) -> None:
        """Read-only database + token + EMS + command self-check."""
        if getattr(self, "_doctor_worker", None) is not None and self._doctor_worker.isRunning():
            return

        def work():
            return self.facade.doctor(network=True)

        self._doctor_worker = Worker(work)
        self._doctor_worker.done.connect(
            lambda report: _info(self, report.get("text") or str(report)))
        self._doctor_worker.failed.connect(lambda message: _warn(self, message))
        self._doctor_worker.start()

    def _save_token(self) -> None:
        try:
            ok = self.facade.set_token(self.token.text())
            self.token.clear()
            self.refresh()
            _info(self, "Token saved." if ok else "Token cleared.")
        except FacadeError as exc:
            _warn(self, str(exc))

    def _pw(self) -> None:
        try:
            self.facade.change_password(self.cur.text(), self.new.text(), self.confirm.text())
            self.cur.clear()
            self.new.clear()
            self.confirm.clear()
            _info(self, "Password updated.")
        except FacadeError as exc:
            _warn(self, str(exc))
        except ValueError as exc:
            _warn(self, str(exc))

    def _backup(self) -> None:
        try:
            path = self.facade.backup()
            _info(self, f"Backup written:\n{path}")
        except Exception as exc:
            _warn(self, str(exc))

    def _diag(self) -> None:
        try:
            path = self.facade.diagnostics()
            _info(self, f"Diagnostics bundle:\n{path}")
        except Exception as exc:
            _warn(self, str(exc))


# ---------------------------------------------------------- auth dialogs --
class SetupDialog(QDialog):
    def __init__(self, facade: DesktopFacade):
        super().__init__()
        self.facade = facade
        self.setWindowTitle("ApexityEdu Bridge — first-run setup")
        self.setWindowIcon(_icon())
        self.setModal(True)
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        hello = QLabel("Welcome. Choose a dashboard password for this PC.")
        hello.setWordWrap(True)
        form.addRow(hello)
        self.user = QLineEdit("admin")
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.Password)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        form.addRow("Admin username", self.user)
        form.addRow("Password (min 8)", self.pw)
        form.addRow("Repeat password", self.pw2)
        form.addRow("EMS token (optional)", self.token)
        go = QPushButton("Finish setup")
        go.setObjectName("primary")
        go.clicked.connect(self._go)
        form.addRow(go)

    def _go(self) -> None:
        if self.pw.text() != self.pw2.text():
            _warn(self, "Passwords do not match.")
            return
        try:
            self.facade.initial_setup(self.user.text(), self.pw.text(), self.token.text())
            self.accept()
        except ValueError as exc:
            _warn(self, str(exc))


class LoginDialog(QDialog):
    def __init__(self, facade: DesktopFacade):
        super().__init__()
        self.facade = facade
        self.setWindowTitle("ApexityEdu Bridge — sign in")
        self.setWindowIcon(_icon())
        self.setModal(True)
        self.setMinimumWidth(380)
        form = QFormLayout(self)
        logo = QLabel()
        logo.setPixmap(_pixmap(48))
        logo.setAlignment(Qt.AlignCenter)
        form.addRow(logo)
        self.user = QLineEdit("admin")
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        form.addRow("Username", self.user)
        form.addRow("Password", self.pw)
        go = QPushButton("Sign in")
        go.setObjectName("primary")
        go.clicked.connect(self._go)
        form.addRow(go)
        self.pw.returnPressed.connect(self._go)

    def _go(self) -> None:
        if self.facade.check_login(self.user.text(), self.pw.text()):
            self.accept()
        else:
            _warn(self, "Invalid username or password.")


# ----------------------------------------------------------- main window --
class MainWindow(QMainWindow):
    show_requested = Signal()
    update_progress = Signal(int)

    def __init__(self, service, facade: DesktopFacade):
        super().__init__()
        self.service = service
        self.facade = facade
        self.setWindowTitle("ApexityEdu Bridge")
        self.setWindowIcon(_icon())
        self.setMinimumSize(1180, 720)
        self.resize(1440, 860)
        self.setWindowFlags(Qt.FramelessWindowHint)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(TitleBar(self))

        update_bar = QHBoxLayout()
        self.update_label = QLabel(f"  Version {__version__}")
        self.update_button = QPushButton("Check for updates")
        self.update_button.clicked.connect(self._update_clicked)
        update_bar.addWidget(self.update_label)
        update_bar.addStretch()
        update_bar.addWidget(self.update_button)
        outer.addLayout(update_bar)
        self._available_update = None
        self._update_worker = None
        self._pending_installer = None
        self.update_progress.connect(
            lambda percent: self.update_label.setText(f"Downloading update… {percent}%"))
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(lambda: self._check_update(False))
        self.update_timer.start(6 * 60 * 60 * 1000)
        QTimer.singleShot(10000, lambda: self._check_update(False))

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = Sidebar()
        body.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.pages = {
            "overview": OverviewPage(facade),
            "devices": DevicesPage(facade, self),
            "device_users": DeviceUsersPage(facade, self),
            "students": StudentsPage(facade),
            "attendance": AttendancePage(facade),
            "logs": LogsPage(facade),
            "settings": SettingsPage(facade),
        }
        for key, _label in NAV:
            self.stack.addWidget(self.pages[key])
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)

        status = QFrame()
        status.setObjectName("statusBar")
        status.setFixedHeight(28)
        sl = QHBoxLayout(status)
        sl.setContentsMargins(14, 0, 14, 0)
        self.status_lbl = QLabel("●  Running in background  ·  auto-start  ·  devices reconnect on boot")
        self.status_lbl.setStyleSheet("color: #4ade80; font-size: 12px;")
        sl.addWidget(self.status_lbl)
        sl.addStretch()
        outer.addWidget(status)

        self.sidebar.navigated.connect(self._nav)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(5000)
        self._nav("overview")

    def _update_clicked(self) -> None:
        if self._available_update is None:
            self._check_update(True)
            return
        if not updater.is_windows() or not updater.is_frozen():
            _info(self, "In-app installation is available in the installed Windows app only.")
            return
        if not _ask(self, "Download and install the update? The bridge will close during "
                          "installation and reopen afterwards. Your data and settings are kept."):
            return
        release = self._available_update
        self._start_update_job(
            lambda: updater.download_update(release, self.update_progress.emit),
            lambda path: setattr(self, "_pending_installer", path))

    def _start_update_job(self, fn, done) -> None:
        if self._update_worker is not None:
            return
        self.update_button.setEnabled(False)
        worker = Worker(fn)
        self._update_worker = worker
        worker.done.connect(done)
        worker.failed.connect(self._update_failed)
        worker.finished.connect(self._update_finished)
        worker.start()

    def _update_finished(self) -> None:
        worker = self._update_worker
        self._update_worker = None
        self.update_button.setEnabled(True)
        if worker:
            worker.deleteLater()
        if self._pending_installer is not None:
            installer = self._pending_installer
            self._pending_installer = None
            self._install_update(installer)

    def _check_update(self, manual: bool) -> None:
        if self._update_worker is not None:
            return
        self.update_label.setText("Checking for updates…")
        self._start_update_job(updater.check_update,
                               lambda release: self._update_checked(release, manual))

    def _update_checked(self, release, manual: bool) -> None:
        self._available_update = release
        if release:
            self.update_label.setText(f"Update available: {release.version}")
            self.update_button.setText("Download and install update")
        else:
            self.update_label.setText(f"Version {__version__} — up to date")
            self.update_button.setText("Check for updates")
            if manual:
                _info(self, "You already have the latest version.")

    def _update_failed(self, message: str) -> None:
        log.warning("Update failed: %s", message)
        self.update_label.setText("Update failed — check your connection and retry")
        # No disruptive dialogs for automatic offline checks.
        self.update_button.setText("Retry update" if self._available_update else "Check for updates")

    def _install_update(self, installer) -> None:
        try:
            updater.launch_update(installer)
        except Exception as exc:
            self._update_failed(str(exc))
            _warn(self, f"Could not start installer: {exc}")
            return
        self.timer.stop()
        self.update_timer.stop()
        self.service.stop()
        QApplication.instance().quit()

    def _nav(self, key: str) -> None:
        self.stack.setCurrentWidget(self.pages[key])
        try:
            self.pages[key].refresh()
        except Exception:
            log.exception("refresh %s failed", key)

    def _tick(self) -> None:
        try:
            current = self.stack.currentWidget()
            if hasattr(current, "refresh"):
                current.refresh()
            ov = self.facade.overview()
            n = ov["devices"].get("online_now", 0)
            ems = "EMS online" if ov["ems"].get("online") else "EMS offline"
            self.status_lbl.setText(
                f"●  Running in background  ·  auto-start  ·  {n} device(s) online  ·  {ems}"
            )
        except Exception:
            pass

    def raise_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


def _ensure_auth(facade: DesktopFacade, parent=None) -> bool:
    if facade.needs_setup():
        dlg = SetupDialog(facade)
        return dlg.exec() == QDialog.Accepted
    dlg = LoginDialog(facade)
    return dlg.exec() == QDialog.Accepted


def run_desktop(service, *, show_window: bool = True) -> int:
    """Start the Qt event loop around an already-running BridgeService."""
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("ApexityEdu Bridge")
    app.setWindowIcon(_icon())
    app.setStyleSheet(APP_QSS)

    facade = DesktopFacade(service.ctx)
    window = MainWindow(service, facade)

    tray = QSystemTrayIcon(_icon(), app)
    tray.setToolTip("ApexityEdu Attendance Bridge")
    menu = QMenu()
    open_act = QAction("Open ApexityEdu Bridge")
    logs_act = QAction("Open logs folder")
    quit_act = QAction("Exit")
    menu.addAction(open_act)
    menu.addAction(logs_act)
    menu.addSeparator()
    menu.addAction(quit_act)
    tray.setContextMenu(menu)
    tray.show()

    def show_ui() -> None:
        if not facade.actor or facade.actor == "desktop":
            if not _ensure_auth(facade, window):
                return
        window.raise_window()

    def open_logs() -> None:
        folder = Path(service.ctx.config.log_dir or (service.ctx.config.data_dir / "logs"))
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            os.system(f'xdg-open "{folder}"')

    def quit_app() -> None:
        if window._update_worker is not None:
            _info(window, "Please wait for the update operation to finish before exiting.")
            return
        tray.hide()
        service.stop()
        app.quit()

    open_act.triggered.connect(show_ui)
    logs_act.triggered.connect(open_logs)
    quit_act.triggered.connect(quit_app)
    tray.activated.connect(lambda reason: show_ui() if reason == QSystemTrayIcon.Trigger else None)

    window.show_requested.connect(show_ui)
    start_ipc_server(lambda: window.show_requested.emit())

    if show_window:
        QTimer.singleShot(0, show_ui)

    code = app.exec()
    service.stop()
    return int(code)
