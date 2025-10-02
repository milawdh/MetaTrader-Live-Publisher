import sys
import os
import asyncio
import threading
import logging
import time
import queue
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler, QueueHandler, QueueListener
from datetime import timedelta

import MetaTrader5 as mt5
from nats.aio.client import Client as NATS
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QFileDialog, QCheckBox, QHBoxLayout
)
from PyQt6.QtCore import pyqtSignal, QObject
from PyQt6.QtGui import QTextCursor
import json

GUI_MAX_LINES = 15
NATS_RETRY_DELAY = 5
MT5_RETRY_DELAY = 5


class QtHandler(QObject, logging.Handler):
    log_signal = pyqtSignal(str)

    def __init__(self):
        QObject.__init__(self)
        logging.Handler.__init__(self)

    def emit(self, record):
        msg = self.format(record)
        self.log_signal.emit(msg)


class Worker(QObject):
    def __init__(self, logger: logging.Logger):
        super().__init__()
        self.logger = logger
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.nats = None
        self.tasks = []
        self.running = False
        self.health_task = None
        self.health_enabled = False
        self.health_nats_url = ""
        self.health_subject = ""
        self.start_time = time.time()
        self.mt5_check_task = None
        self.mt5_path = ""
        self.mt5_login = 0
        self.mt5_password = ""
        self.mt5_server = ""

    def _log(self, level, msg, *args, **kwargs):
        if self.logger.isEnabledFor(level):
            self.logger.log(level, msg, *args, **kwargs)

    async def _mt5_monitor_loop(self):
        while self.running:
            await asyncio.sleep(5)
            try:
                info = mt5.terminal_info()
                if not info:
                    self._log(logging.WARNING, "MT5 appears to be uninitialized or has shut down.")
                    self._log(logging.INFO, "Attempting to reinitialize MT5...")
                    mt5.shutdown()
                    success = mt5.initialize(
                        path=self.mt5_path,
                        login=self.mt5_login,
                        password=self.mt5_password,
                        server=self.mt5_server
                    )
                    if success:
                        self._log(logging.INFO, "MT5 successfully reinitialized.")
                    else:
                        code, msg = mt5.last_error()
                        self._log(logging.ERROR, f"MT5 reinit failed [{code}]: {msg}")
            except Exception as e:
                self._log(logging.ERROR, f"MT5 monitor error: {e}")

    def _init_mt5(self, path: str, login: int, password: str, server: str):
        mt5.shutdown()
        last_err = (None, None)
        for attempt in range(1, 4):
            if mt5.initialize(path=path, login=login, password=password, server=server):
                self._log(logging.INFO, f"MT5 initialized at {path} (attempt {attempt})")
                return
            code, msg = mt5.last_error()
            last_err = (code, msg)
            self._log(logging.WARNING, f"MT5 init failed (#{attempt}) [{code}]: {msg}, retrying in {MT5_RETRY_DELAY}s")
            time.sleep(MT5_RETRY_DELAY)
        code, msg = last_err
        raise RuntimeError(f"MT5 init ultimately failed [{code}]: {msg}")

    async def _connect_nats(self, url: str):
        if self.nats and self.nats.is_connected:
            try:
                await self.nats.close()
            except Exception as e:
                self._log(logging.WARNING, f"Error closing old NATS connection: {e}")
        self.nats = NATS()
        for attempt in range(1, 6):
            try:
                await self.nats.connect(servers=[url])
                self._log(logging.INFO, f"NATS connected on attempt {attempt}")
                return
            except Exception as e:
                self._log(logging.WARNING, f"NATS connect failed (#{attempt}): {e}")
                await asyncio.sleep(NATS_RETRY_DELAY)
        raise ConnectionError(f"Failed to connect to NATS at {url}")

    async def _publish(self, symbol: str):
        if not mt5.symbol_select(symbol, True):
            self._log(logging.ERROR, f"Symbol select failed: {symbol}")
            return


        last_message = None
        self._log(logging.INFO, f"Start publishing: {symbol}")
        try:
            while self.running:
                tick = await asyncio.to_thread(mt5.symbol_info_tick, symbol)
                if not tick:
                    await asyncio.sleep(0.05)
                    continue

                timeframes = {
                    "M1": mt5.TIMEFRAME_M1,
                    "M5": mt5.TIMEFRAME_M5,
                    "M10": mt5.TIMEFRAME_M10,
                    "M15": mt5.TIMEFRAME_M15,
                    "M30": mt5.TIMEFRAME_M30,
                    "H1": mt5.TIMEFRAME_H1,
                    "H2": mt5.TIMEFRAME_H2,
                    "H4": mt5.TIMEFRAME_H4
                }

                data = ""
                for label, tf in timeframes.items():
                    try:
                        rates = mt5.copy_rates_from_pos(symbol, tf, 0, 1)
                        if rates:
                            candle_data = f"Timeframe:{label},Time:{rates[0][0]},Open:{rates[0][1]},High:{rates[0][2]},Low:{rates[0][3]},Close:{rates[0][4]},TickVolume:{rates[0][5]}"
                            data += candle_data + "|"
                        else:
                            self._log(logging.WARNING, f"No data for symbol {symbol} on timeframe {label}")
                    except Exception as e:
                        self._log(logging.ERROR, f"Error fetching data for {symbol} on timeframe {label}: {e}")
                if data != last_message:
                    last_message = data
                    if data:
                        message = data.strip()  # Remove the trailing separator
                        subject = "ticks." + symbol.replace(" ", "")
                        await self.nats.publish(subject, message.encode())
                        self._log(logging.INFO, f"Published: {message}")

                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self._log(logging.INFO, f"Publishing cancelled: {symbol}")
        except Exception as e:
            self._log(logging.ERROR, f"Error publishing {symbol}: {e}")

    async def start(self, nats_url, symbols, mt5_path, login, password, server, health_enabled=False, health_url="",
                    health_subject=""):
        self.health_enabled = health_enabled
        self.health_nats_url = health_url
        self.health_subject = health_subject
        self.mt5_path = mt5_path
        self.mt5_login = login
        self.mt5_password = password
        self.mt5_server = server

        await self.stop()
        self.running = True
        try:
            self._init_mt5(mt5_path, login, password, server)
            await self._connect_nats(nats_url)

            # ✅ START MONITOR AFTER MT5 INIT
            self.mt5_check_task = asyncio.create_task(self._mt5_monitor_loop())

        except Exception as e:
            self._log(logging.ERROR, f"Startup error: {e}")
            self.running = False
            return

        self.tasks = []
        for sym in symbols:
            task = asyncio.create_task(self._publish(sym), name=sym)
            self.tasks.append(task)

        if self.health_enabled:
            self.health_task = asyncio.create_task(self._health_check_loop())

    async def _health_check_loop(self):
        health_nats = NATS()
        try:
            await health_nats.connect(servers=[self.health_nats_url])
            self._log(logging.INFO, f"Health NATS connected: {self.health_nats_url}")
        except Exception as e:
            self._log(logging.ERROR, f"Health NATS connection failed: {e}")
            return

        try:
            while self.running:
                health = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "mt5_initialized": mt5.terminal_info() is not None,
                    "nats_connected": self.nats and self.nats.is_connected,
                    "symbols": [f"TASK-{id(t)}:{t.get_name()}" for t in self.tasks if not t.done()],
                    "uptime_sec": int(time.time() - self.start_time)
                }
                try:
                    await health_nats.publish(self.health_subject, str(health).encode())
                except Exception as e:
                    self._log(logging.WARNING, f"Health publish failed: {e}")
                await asyncio.sleep(5)
        finally:
            await health_nats.close()
            self._log(logging.INFO, "Health NATS connection closed")

    async def stop(self):
        self.running = False
        for t in self.tasks:
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

        if self.nats and self.nats.is_connected:
            try:
                await self.nats.close()
                self._log(logging.INFO, "NATS closed")
            except Exception as e:
                self._log(logging.WARNING, f"Error closing NATS: {e}")

        mt5.shutdown()
        self._log(logging.INFO, "MT5 shutdown")
        if self.health_task:
            self.health_task.cancel()
            await asyncio.gather(self.health_task, return_exceptions=True)
            self.health_task = None

        if self.mt5_check_task:
            self.mt5_check_task.cancel()
            await asyncio.gather(self.mt5_check_task, return_exceptions=True)
            self.mt5_check_task = None

    def start_worker(self, nats_url, symbols, mt5_path, login, password, server, health_enabled=False, health_url="",
                     health_subject=""):
        asyncio.run_coroutine_threadsafe(
            self.start(nats_url, symbols, mt5_path, login, password, server, health_enabled, health_url,
                       health_subject),
            self.loop
        )

    def stop_worker(self):
        asyncio.run_coroutine_threadsafe(self.stop(), self.loop)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MT5 Tick Publisher")
        self.resize(600, 540)

        self.logger = logging.getLogger("MT5Publisher")
        self.logger.setLevel(logging.INFO)
        qt_handler = QtHandler()
        qt_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        qt_handler.log_signal.connect(self._append_log)
        self.logger.addHandler(qt_handler)

        self.worker = Worker(self.logger)

        self.nats_url = QLineEdit("nats://localhost:4222")
        self.symbols = QLineEdit("BITCOIN")
        self.mt5_path = QLineEdit()
        self.mt5_login = QLineEdit()
        self.mt5_pass = QLineEdit();
        self.mt5_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.mt5_server = QLineEdit()
        self.log_folder = QLineEdit(os.getcwd())
        self.rotation_when = QLineEdit("midnight")
        self.rotation_interval = QLineEdit("1")
        self.rotation_backup = QLineEdit("10")
        self.verbose_cb = QCheckBox("Show Debug Logs")
        self.btn_browse_mt5 = QPushButton("Browse MT5…")
        self.btn_browse_dir = QPushButton("Browse Log Folder…")
        self.btn_start = QPushButton("Start")
        self.log_view = QTextEdit(readOnly=True)
        self.health_cb = QCheckBox("Enable Health Check (every 5s)")
        self.health_nats_url = QLineEdit("nats://localhost:4222")
        self.health_subject = QLineEdit("health.mt5publisher")
        self._build_layout()
        self.btn_browse_mt5.clicked.connect(self._browse_mt5)
        self.btn_browse_dir.clicked.connect(self._browse_folder)
        self.btn_start.clicked.connect(self._on_start_stop)
        self.verbose_cb.stateChanged.connect(self._on_debug_toggle)
        self.is_running = False

    def _build_layout(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("NATS URL:"));
        layout.addWidget(self.nats_url)
        layout.addWidget(QLabel("Symbols (CSV):"));
        layout.addWidget(self.symbols)
        layout.addWidget(QLabel("MT5 EXE Path:"))
        h1 = QHBoxLayout();
        h1.addWidget(self.mt5_path);
        h1.addWidget(self.btn_browse_mt5)
        layout.addLayout(h1)
        layout.addWidget(QLabel("MT5 Login:"));
        layout.addWidget(self.mt5_login)
        layout.addWidget(QLabel("MT5 Password:"));
        layout.addWidget(self.mt5_pass)
        layout.addWidget(QLabel("MT5 Server:"));
        layout.addWidget(self.mt5_server)
        layout.addWidget(QLabel("Log Folder:"))
        h2 = QHBoxLayout();
        h2.addWidget(self.log_folder);
        h2.addWidget(self.btn_browse_dir)
        layout.addLayout(h2)
        layout.addWidget(QLabel("Log Rotation: When (e.g. midnight, S, M, H, D, W0-W6):"))
        layout.addWidget(self.rotation_when)
        layout.addWidget(QLabel("Log Rotation: Interval:"))
        layout.addWidget(self.rotation_interval)
        layout.addWidget(QLabel("Log Rotation: Backup Count:"))
        layout.addWidget(self.rotation_backup)
        layout.addWidget(QLabel("Health Check NATS URL:"))
        layout.addWidget(self.health_nats_url)
        layout.addWidget(QLabel("Health Check Subject:"))
        layout.addWidget(self.health_subject)
        layout.addWidget(self.health_cb)
        layout.addWidget(self.verbose_cb)
        layout.addWidget(self.btn_start)
        layout.addWidget(QLabel("Log Output:"));
        layout.addWidget(self.log_view)

    def _browse_mt5(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select MT5 Terminal", "", "Executables (*.exe)")
        if path: self.mt5_path.setText(path)

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Log Folder", self.log_folder.text())
        if folder: self.log_folder.setText(folder)

    def _append_log(self, msg: str):
        self.log_view.append(msg)
        doc = self.log_view.document()
        while doc.blockCount() > GUI_MAX_LINES:
            cursor = self.log_view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.select(QTextCursor.SelectionType.LineUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()

    def _setup_file_logging(self):
        if hasattr(self, 'log_queue_listener') and self.log_queue_listener:
            self.log_queue_listener.stop()

        for h in list(self.logger.handlers):
            if isinstance(h, (TimedRotatingFileHandler, QueueHandler)):
                self.logger.removeHandler(h)

        self.log_queue = queue.Queue(-1)
        folder = self.log_folder.text().strip()
        os.makedirs(folder, exist_ok=True)
        base_path = os.path.join(folder, "ticks.log")

        when = self.rotation_when.text().strip() or "midnight"
        try:
            interval = int(self.rotation_interval.text().strip())
        except ValueError:
            interval = 1
        try:
            backup_count = int(self.rotation_backup.text().strip())
        except ValueError:
            backup_count = 10

        file_handler = TimedRotatingFileHandler(
            filename=base_path,
            when=when,
            interval=interval,
            backupCount=backup_count,
            encoding="utf-8",
            utc=False
        )
        file_handler.suffix = "%Y-%m-%d.txt"
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
        level = logging.DEBUG if self.verbose_cb.isChecked() else logging.INFO
        file_handler.setLevel(level)

        queue_handler = QueueHandler(self.log_queue)
        queue_handler.setLevel(level)
        self.logger.addHandler(queue_handler)

        self.log_queue_listener = QueueListener(self.log_queue, file_handler, respect_handler_level=True)
        self.log_queue_listener.start()
        self.logger.info(
            f"Async logging initialized. Logs will rotate ({when}, interval={interval}, backups={backup_count}) in: {folder}")

    def _on_debug_toggle(self):
        level = logging.DEBUG if self.verbose_cb.isChecked() else logging.INFO
        self.logger.setLevel(level)
        for h in self.logger.handlers:
            h.setLevel(level)
        if hasattr(self, 'log_queue_listener') and isinstance(self.log_queue_listener, QueueListener):
            for lh in self.log_queue_listener.handlers:
                lh.setLevel(level)
        self.logger.info(f"Logging level changed to: {'DEBUG' if level == logging.DEBUG else 'INFO'}")

    def _on_start_stop(self):
        if not self.is_running:
            url = self.nats_url.text().strip()
            syms = [s.strip() for s in self.symbols.text().split(",") if s.strip()]
            mt5p = self.mt5_path.text().strip()
            login = self.mt5_login.text().strip()
            passwd = self.mt5_pass.text().strip()
            server = self.mt5_server.text().strip()

            if not (url and syms and mt5p and login and passwd and server):
                self.logger.error("Please fill all required fields (NATS, symbols, MT5 path, credentials).")
                return

            self._setup_file_logging()
            self._on_debug_toggle()

            self.worker.start_worker(
                url, syms, mt5p, int(login), passwd, server,
                self.health_cb.isChecked(),
                self.health_nats_url.text().strip(),
                self.health_subject.text().strip()
            )
            self.btn_start.setText("Stop")
            self.is_running = True
            self.logger.info("Publisher started.")
        else:
            self.worker.stop_worker()
            self.btn_start.setText("Start")
            self.is_running = False
            self.logger.info("Publisher stopping...")

    def closeEvent(self, event):
        if self.is_running:
            self.worker.stop_worker()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()