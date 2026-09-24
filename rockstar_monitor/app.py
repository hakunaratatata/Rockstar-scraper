from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, QTimer, QUrl, Slot, Qt
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox, QSplitter, QStyle,
    QSystemTrayIcon, QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget, QInputDialog)

from .engine import CycleSummary, MonitorEngine
from .security import normalize_public_url
from .sources import BUILTIN_SOURCES
from .models import Confidence, SourceDefinition
from .storage import Storage
from .worker import CheckWorker

APP_NAME = "GTA VI Physical Product Monitor"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "RockstarNewsMonitor"


class MainWindow(QMainWindow):
    def __init__(self, app: QApplication):
        super().__init__(); self.app=app; self.settings=QSettings("LocalTools","RockstarNewsMonitor")
        try: self.storage=Storage(DATA_DIR/"monitor.db")
        except RuntimeError as exc: QMessageBox.critical(None,APP_NAME,str(exc)); raise
        self.storage.sync_sources(BUILTIN_SOURCES); self.engine=MonitorEngine(self.storage)
        self.thread: QThread|None=None; self.worker: CheckWorker|None=None; self.allow_close=False; self.pending_quit=False
        self.setWindowTitle(APP_NAME); self.resize(1180,720); self.setWindowIcon(app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self._build_ui(); self._build_tray(); self.refresh_all()
        self.timer=QTimer(self); self.timer.timeout.connect(self.check_now); self._apply_interval()
        self.countdown_timer=QTimer(self); self.countdown_timer.timeout.connect(self.refresh_countdown); self.countdown_timer.start(1000)
        QTimer.singleShot(800,self.check_now)

    def _build_ui(self):
        root=QWidget(); outer=QVBoxLayout(root); header=QLabel(APP_NAME); header.setStyleSheet("font-size:22px;font-weight:600"); outer.addWidget(header)
        self.status=QLabel("Monitoring: starting…"); self.status.setWordWrap(True); outer.addWidget(self.status)
        controls=QHBoxLayout(); self.check_button=QPushButton("Check now"); self.check_button.clicked.connect(self.check_now); controls.addWidget(self.check_button)
        self.official_button=QPushButton("Check official sources only"); self.official_button.clicked.connect(lambda: self.start_check(True)); controls.addWidget(self.official_button)
        export_json=QPushButton("Export JSON"); export_json.clicked.connect(lambda:self.export_data("json")); controls.addWidget(export_json)
        export_csv=QPushButton("Export CSV"); export_csv.clicked.connect(lambda:self.export_data("csv")); controls.addWidget(export_csv); controls.addStretch(); outer.addLayout(controls)
        self.tabs=QTabWidget(); self.tabs.addTab(self._status_tab(),"Status"); self.tabs.addTab(self._products_tab(),"Products"); self.tabs.addTab(self._activity_tab(),"Activity"); self.tabs.addTab(self._sources_tab(),"Sources"); self.tabs.addTab(self._settings_tab(),"Settings"); outer.addWidget(self.tabs); self.setCentralWidget(root)

    def _status_tab(self):
        page=QWidget(); layout=QFormLayout(page); self.summary_labels={}
        for key,label in (("monitoring","Monitoring"),("last","Last check"),("next","Next check"),("checked","Sources checked"),("successful","Successful sources"),("failed","Failed sources"),("new","New products"),("changed","Changed products")):
            value=QLabel("—"); self.summary_labels[key]=value; layout.addRow(label,value)
        return page

    def _products_tab(self):
        page=QWidget(); layout=QVBoxLayout(page); self.filter=QComboBox(); self.filter.addItems(["All","Official","Retailer","Reported","New","Changed","Available","Preorder","Collector/Special Edition","Merch","Music","Hardware","Possible match"]); self.filter.currentTextChanged.connect(self.refresh_products); layout.addWidget(self.filter)
        splitter=QSplitter(); self.products=QTableWidget(0,10); self.products.setHorizontalHeaderLabels(["Status","Product","Category","Price","Availability","Source","Confidence","First Seen","Last Changed","URL"]); self.products.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch); self.products.horizontalHeader().setSectionResizeMode(9,QHeaderView.ResizeMode.Stretch); self.products.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows); self.products.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers); self.products.itemSelectionChanged.connect(self.show_product); self.products.doubleClicked.connect(self.open_selected); splitter.addWidget(self.products)
        self.details=QTextBrowser(); self.details.setOpenExternalLinks(False); splitter.addWidget(self.details); splitter.setSizes([760,400]); layout.addWidget(splitter); return page

    def _activity_tab(self):
        page=QWidget(); layout=QVBoxLayout(page); self.activity=QTableWidget(0,3); self.activity.setHorizontalHeaderLabels(["Time","Level","Activity"]); self.activity.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch); self.activity.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers); layout.addWidget(self.activity); return page

    def _sources_tab(self):
        page=QWidget(); layout=QVBoxLayout(page); add=QPushButton("Add retailer or reporting source"); add.clicked.connect(self.add_source); layout.addWidget(add); self.sources=QTableWidget(0,9); self.sources.setHorizontalHeaderLabels(["Enabled","Source","Type","URL","Last checked","Last success","HTTP","Next eligible","Error/backoff"]); self.sources.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch); self.sources.horizontalHeader().setSectionResizeMode(8,QHeaderView.ResizeMode.Stretch); self.sources.itemChanged.connect(self.source_changed); layout.addWidget(self.sources); return page

    def _settings_tab(self):
        page=QWidget(); form=QFormLayout(page); self.interval=QSpinBox(); self.interval.setRange(15,1440); self.interval.setSuffix(" minutes"); self.interval.setValue(int(self.settings.value("interval_minutes",30))); self.interval.valueChanged.connect(self.save_settings); form.addRow("Normal monitoring interval",self.interval)
        self.notify_new=self._setting_box("Notify on new products","notify_new",True,form); self.notify_availability=self._setting_box("Notify on availability changes","notify_availability",True,form); self.notify_price=self._setting_box("Notify on price changes","notify_price",True,form); self.notify_metadata=self._setting_box("Notify on metadata changes","notify_metadata",False,form); self.notify_cycle=self._setting_box("Notify after every cycle, including no changes","notify_cycle",False,form)
        note=QLabel("Minimum interval: 15 minutes. Per-source cooldowns and Retry-After always take precedence. Possible matches do not create high-confidence alerts."); note.setWordWrap(True); form.addRow("",note); return page

    def _setting_box(self,label,key,default,form):
        box=QCheckBox(label); box.setChecked(self.settings.value(key,default,type=bool)); box.toggled.connect(lambda value,k=key:self.settings.setValue(k,value)); form.addRow("",box); return box

    def _build_tray(self):
        self.tray=QSystemTrayIcon(self.windowIcon(),self); menu=QMenu(); show=QAction("Show",self); show.triggered.connect(self.show_normal); menu.addAction(show); check=QAction("Check now",self); check.triggered.connect(self.check_now); menu.addAction(check); menu.addSeparator(); quit_action=QAction("Quit",self); quit_action.triggered.connect(self.quit_app); menu.addAction(quit_action); self.tray.setContextMenu(menu); self.tray.activated.connect(lambda reason:self.show_normal() if reason in (QSystemTrayIcon.ActivationReason.Trigger,QSystemTrayIcon.ActivationReason.DoubleClick) else None); self.tray.show()

    @Slot()
    def check_now(self): self.start_check(False)
    def start_check(self,official_only=False):
        if self.thread and self.thread.isRunning(): self.status.setText("A monitoring cycle is already running."); return
        due=self.storage.due_sources(official_only)
        if not due: self.status.setText("No sources are eligible yet; cooldowns and minimum intervals are being respected."); return
        self.check_button.setEnabled(False); self.official_button.setEnabled(False); self.thread=QThread(self); self.worker=CheckWorker(self.engine,official_only); self.worker.moveToThread(self.thread); self.thread.started.connect(self.worker.run); self.worker.progress.connect(self.status.setText); self.worker.succeeded.connect(self.cycle_finished); self.worker.failed.connect(self.cycle_failed); self.worker.finished.connect(self.thread.quit); self.worker.finished.connect(self.worker.deleteLater); self.thread.finished.connect(self.thread_done); self.thread.finished.connect(self.thread.deleteLater); self.thread.start()

    @Slot(object)
    def cycle_finished(self,summary:CycleSummary):
        self.status.setText(f"Cycle complete: {summary.successful}/{summary.checked} sources succeeded; {len(summary.events)} meaningful change(s).")
        for event in summary.events:
            if self.should_notify(event):
                prefix="Official" if event.confidence=="OFFICIAL" else "Unconfirmed"
                self.tray.showMessage(APP_NAME,f"{prefix} — {event.kind}:\n{event.product_name}",QSystemTrayIcon.MessageIcon.Information,10000)
        if not summary.events and self.notify_cycle.isChecked(): self.tray.showMessage(APP_NAME,"No new GTA VI physical-product development found.",QSystemTrayIcon.MessageIcon.Information,6000)
        self.refresh_all()

    def should_notify(self,event):
        if event.current and event.current.get("relevance")=="POSSIBLE MATCH": return False
        if event.kind=="NEW PRODUCT": return self.notify_new.isChecked()
        if event.kind in ("AVAILABLE","SOLD OUT","PREORDER OPEN"): return self.notify_availability.isChecked()
        if event.kind=="PRICE CHANGE": return self.notify_price.isChecked()
        return self.notify_metadata.isChecked()

    @Slot(str)
    def cycle_failed(self,message): self.status.setText(message); logging.error(message)
    @Slot()
    def thread_done(self):
        self.check_button.setEnabled(True); self.official_button.setEnabled(True); self.thread=None; self.worker=None
        if self.pending_quit: self._finish_quit()

    def refresh_all(self): self.refresh_status(); self.refresh_products(); self.refresh_activity(); self.refresh_sources()
    def refresh_status(self):
        run=self.storage.latest_run(); self.summary_labels["monitoring"].setText("Running"); self.summary_labels["last"].setText(display_time(run["finished_at"]) if run and run["finished_at"] else "Not yet"); rows=self.storage.source_rows(); eligible=[r["next_eligible"] for r in rows if r["enabled"] and r["next_eligible"]]; self.next_eligible_at=datetime.fromisoformat(min(eligible)) if eligible else None; self.refresh_countdown()
        for key,column in (("checked","sources_checked"),("successful","successful_sources"),("failed","failed_sources"),("new","new_products"),("changed","changed_products")): self.summary_labels[key].setText(str(run[column]) if run else "0")

    def refresh_products(self):
        rows=self.storage.product_rows(); mode=self.filter.currentText() if hasattr(self,"filter") else "All"
        def include(r):
            text=" ".join(str(r[k] or "") for k in r.keys()).lower()
            return mode=="All" or (mode=="Official" and r["confidence"]=="OFFICIAL") or (mode=="Retailer" and r["confidence"]=="RETAILER") or (mode=="Reported" and r["confidence"]=="SECONDARY REPORT") or (mode=="Available" and "instock" in str(r["availability"]).lower()) or (mode=="Preorder" and r["preorder_status"]=="PREORDER") or (mode=="Possible match" and r["relevance"]=="POSSIBLE MATCH") or mode.lower().split("/")[0] in text
        self.product_rows=[r for r in rows if include(r)]; self.products.setRowCount(len(self.product_rows))
        for i,r in enumerate(self.product_rows):
            status="REMOVED" if r["removed"] else (r["preorder_status"] or r["availability"] or r["relevance"]); values=(status,r["name"],r["category"]," ".join(x for x in (r["currency"],r["price"]) if x),r["availability"],r["source_name"],r["confidence"],display_time(r["first_seen"]),display_time(r["last_changed"]),r["canonical_url"])
            for j,value in enumerate(values): self.products.setItem(i,j,QTableWidgetItem(str(value or "Unknown")))

    def show_product(self):
        row=self.products.currentRow()
        if row<0 or row>=len(self.product_rows): return
        item=dict(self.product_rows[row]); events=[dict(e) for e in self.storage.event_rows() if e["product_key"]==item["product_key"]][:10]
        html=f"<h2>{escape(item['name'])}</h2><p><b>{escape(item['confidence'])}</b> · {escape(item['relevance'])}</p>"
        for key in ("category","price","currency","availability","preorder_status","release_date","sku","product_id","variants","purchase_limit","weight","dimensions","source_name","canonical_url","description"): html+=f"<p><b>{escape(key.replace('_',' ').title())}:</b> {escape(item.get(key) or 'Unknown')}</p>"
        if events:
            html+="<h3>Change history</h3>"
            for event in events:
                fields=json.loads(event["changed_fields"]); previous=json.loads(event["previous_json"]) if event["previous_json"] else {}; current=json.loads(event["current_json"]) if event["current_json"] else {}
                diff="<br>".join(f"{escape(field.replace('_',' ').title())}: {escape(previous.get(field) or 'Unknown')} → {escape(current.get(field) or 'Unknown')}" for field in fields)
                html+=f"<p><b>{escape(event['kind'])}</b> — {escape(display_time(event['created_at']))}<br>{diff or 'New observation'}</p>"
        self.details.setHtml(html)

    def refresh_activity(self):
        rows=self.storage.activity_rows(); self.activity.setRowCount(len(rows))
        for i,r in enumerate(rows):
            for j,value in enumerate((display_time(r["created_at"]),r["level"],r["message"])): self.activity.setItem(i,j,QTableWidgetItem(str(value)))

    def refresh_sources(self):
        self.sources.blockSignals(True); rows=self.storage.source_rows(); self.sources.setRowCount(len(rows))
        for i,r in enumerate(rows):
            enabled=QTableWidgetItem("Enabled" if r["enabled"] else "Disabled"); enabled.setCheckState(Qt.CheckState.Checked if r["enabled"] else Qt.CheckState.Unchecked); enabled.setData(Qt.ItemDataRole.UserRole,r["key"]); self.sources.setItem(i,0,enabled)
            values=(r["name"],r["kind"],r["url"],display_time(r["last_checked"]),display_time(r["last_success"]),r["last_http_status"],display_time(r["next_eligible"]),r["error"])
            for j,value in enumerate(values,1): self.sources.setItem(i,j,QTableWidgetItem(str(value or "—")))
        self.sources.blockSignals(False)

    def source_changed(self,item):
        if item.column()==0: self.storage.set_source_enabled(item.data(Qt.ItemDataRole.UserRole),item.checkState()==Qt.CheckState.Checked)

    def add_source(self):
        name,ok=QInputDialog.getText(self,"Add source","Source name")
        if not ok or not name.strip(): return
        url,ok=QInputDialog.getText(self,"Add source","Public HTTPS page URL")
        if not ok:return
        from urllib.parse import urlsplit
        try: host=(urlsplit(url).hostname or "").lower(); safe=normalize_public_url(url,(host,))
        except ValueError: safe=None
        if not safe: QMessageBox.warning(self,APP_NAME,"Enter a normal public HTTPS URL without credentials or a custom port."); return
        kind,ok=QInputDialog.getItem(self,"Add source","Source type",["Retailer","Secondary report","Unverified"],0,False)
        if not ok:return
        confidence={"Retailer":Confidence.RETAILER,"Secondary report":Confidence.SECONDARY_REPORT,"Unverified":Confidence.UNVERIFIED}[kind]
        minimum=60 if confidence==Confidence.RETAILER else 120
        key="custom_"+__import__('hashlib').sha256(safe.encode()).hexdigest()[:12]
        self.storage.add_source(SourceDefinition(key,name.strip(),kind.lower(),safe,confidence,(host,),minimum)); self.refresh_sources()

    def open_selected(self):
        row=self.products.currentRow()
        if row<0:return
        product=self.product_rows[row]; source=next((s for s in self.storage.source_rows() if s["key"]==product["source_key"]),None)
        allowed=tuple(json.loads(source["allowed_hosts"])) if source else ()
        safe=normalize_public_url(product["canonical_url"],allowed)
        if safe: QDesktopServices.openUrl(QUrl(safe))
        else: QMessageBox.warning(self,APP_NAME,"The selected URL failed its source domain policy.")

    def export_data(self,kind):
        path,_=QFileDialog.getSaveFileName(self,"Export",str(Path.home()/f"gtavi-monitor.{kind}"),f"{kind.upper()} (*.{kind})")
        if path:
            (self.storage.export_json if kind=="json" else self.storage.export_csv)(Path(path)); self.status.setText(f"Exported to {path}")

    def save_settings(self): self.settings.setValue("interval_minutes",self.interval.value()); self._apply_interval()
    def refresh_countdown(self):
        if not getattr(self,"next_eligible_at",None): self.summary_labels["next"].setText("Ready now"); return
        remaining=max(0,int((self.next_eligible_at-datetime.now(timezone.utc)).total_seconds()))
        if remaining==0: self.summary_labels["next"].setText("Ready now"); return
        hours,remainder=divmod(remaining,3600); minutes,seconds=divmod(remainder,60)
        countdown=f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"
        self.summary_labels["next"].setText(f"{countdown} remaining — {display_time(self.next_eligible_at.isoformat())}")
    def _apply_interval(self):
        if hasattr(self,"timer"): self.timer.start(max(15,int(self.settings.value("interval_minutes",30)))*60_000)
    def closeEvent(self,event:QCloseEvent):
        if self.allow_close:event.accept()
        else:event.ignore();self.hide();self.tray.showMessage(APP_NAME,"Still monitoring in the system tray.",QSystemTrayIcon.MessageIcon.Information,3000)
    def quit_app(self):
        self.pending_quit=True; self.timer.stop(); self.countdown_timer.stop(); self.status.setText("Waiting for the active request to finish before quitting…")
        if not self.thread or not self.thread.isRunning(): self._finish_quit()
    def _finish_quit(self): self.allow_close=True;self.tray.hide();self.app.quit()
    def show_normal(self): self.show();self.raise_();self.activateWindow()


def display_time(value):
    if not value:return "—"
    try:return datetime.fromisoformat(value).astimezone().strftime("%b %d, %Y %I:%M %p")
    except (TypeError,ValueError):return str(value)
def escape(value):
    import html
    return html.escape(str(value))
def run():
    DATA_DIR.mkdir(parents=True,exist_ok=True); logging.basicConfig(filename=DATA_DIR/"monitor.log",level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    app=QApplication(sys.argv);app.setApplicationName(APP_NAME);app.setQuitOnLastWindowClosed(False);window=MainWindow(app);window.show();return app.exec()
