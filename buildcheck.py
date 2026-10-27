#!/usr/bin/env python3
"""
Landull BuildCheck - a post-build health check for Windows PCs.

Shows the specs, live temperatures, clocks, loads and fans, runs a CPU/GPU stress test and quick
benchmarks, checks for common build mistakes, and exports a branded PDF report for the customer.

Run from source:   python buildcheck.py      (needs: pip install -r requirements.txt)
Preview anywhere:  BUILDCHECK_DEMO=1 python buildcheck.py   (simulated sensors)

Benchmarks and stress workers run in their own processes: the app re-launches itself with
"--worker <name> <args...>" (see bc_workers.py), which keeps the window responsive.
"""

import json
import os
import sys
import time
import traceback

sys.path.insert(0, getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))))

from bc_common import (APP_NAME, DEMO, GITHUB, IS_WIN, VERSION, asset, fmt_bytes,  # noqa: E402
                       fmt_duration, fmt_gb_decimal, is_admin, log, log_path, worker_command)


def _fix_stdio():
    """Windowed builds on Windows start with sys.stdout = None; workers need real pipes."""
    for name, fd in (("stdout", 1), ("stderr", 2)):
        if getattr(sys, name) is None:
            try:
                setattr(sys, name, os.fdopen(fd, "w", encoding="utf-8", errors="replace", buffering=1))
            except OSError:
                setattr(sys, name, open(os.devnull, "w"))
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass


GPU_VERT = """#version 130
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""
GPU_FRAG = """#version 130
uniform float u_time;
uniform vec2 u_res;
uniform int u_iter;
void main() {
    vec2 uv = (gl_FragCoord.xy - 0.5 * u_res) / u_res.y;
    float zoom = 1.6 + 0.4 * sin(u_time * 0.2);
    vec2 c = vec2(-0.7436447860, 0.1318252536) + uv * 0.02 * zoom;
    vec2 z = vec2(0.0);
    int n = 0;
    for (int i = 0; i < u_iter; i++) {
        z = vec2(z.x * z.x - z.y * z.y, 2.0 * z.x * z.y) + c;
        if (dot(z, z) > 64.0) break;
        n++;
    }
    float acc = uv.x + u_time;
    for (int j = 0; j < u_iter / 2; j++) {
        acc = sin(acc * 1.0001 + uv.y) * cos(acc - uv.x) + 0.5;
    }
    float t = float(n) / float(u_iter);
    vec3 col = 0.5 + 0.5 * cos(6.28318 * (3.0 * t + vec3(0.0, 0.15, 0.3)) + u_time * 0.5);
    if (n == u_iter) col = vec3(0.05);
    gl_FragColor = vec4(col + acc * 0.0001, 1.0);
}
"""


# ============================================================== GUI
def gui_main():
    from collections import deque

    from PySide6.QtCore import QObject, QProcess, QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal
    from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPixmap,
                               QSurfaceFormat, QVector2D)
    from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
                                   QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                                   QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
                                   QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QTableWidget,
                                   QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

    import bc_collect
    import bc_report
    import bc_sensors

    BG, PANEL, SURFACE, BORDER = "#141414", "#101010", "#1c1c1c", "#2c2c2c"
    TEXT, MUTED, DIM, BONE = "#e6e3dd", "#9a968f", "#6b6862", "#e6e3dd"
    RED, AMBER, GREEN, BLUE = "#e06a6a", "#e0b25a", "#86c98a", "#6fa8dc"
    SECTIONS = [("MONITOR", "#d9a441", [("system", "System"), ("sensors", "Sensors")]),
                ("TEST", "#6fa8dc", [("stress", "Stress test"), ("bench", "Benchmarks")]),
                ("RESULTS", "#7cc47f", [("health", "Health checks"), ("report", "Report")])]
    STATUS_COLOR = {"PASS": GREEN, "WARN": AMBER, "FAIL": RED, "INFO": MUTED, "SKIP": DIM}
    MONO = 'Menlo, Consolas, "DejaVu Sans Mono", monospace'

    def rgba(hexcolor, a):
        c = QColor(hexcolor)
        return f"rgba({c.red()},{c.green()},{c.blue()},{a})"

    def qss():
        chk = asset("check.svg").replace("\\", "/")
        chev = asset("chevron.svg").replace("\\", "/")
        return f"""
        * {{ color: {TEXT}; font-size: 13px; }}
        QMainWindow, #root, #page, #home {{ background: {BG}; }}
        QToolTip {{ background: #232323; color: {TEXT}; border: 1px solid #3a3a3a; padding: 6px; }}
        #sidebar {{ background: {PANEL}; border-right: 1px solid #222; }}
        #brand {{ font-size: 15px; font-weight: 700; letter-spacing: 3px; }}
        #brandSub {{ color: {DIM}; font-size: 11px; letter-spacing: 2px; }}
        #nav {{ background: transparent; border: none; outline: none; }}
        #nav::item {{ padding: 5px 10px; border-radius: 6px; margin: 1px 6px; }}
        #nav::item:hover {{ background: #1b1b1b; }}
        #nav::item:selected {{ background: #262626; color: #ffffff; }}
        QLineEdit, QPlainTextEdit, QComboBox {{
            background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 8px;
            selection-background-color: #4a4a4a; }}
        QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: #6a675f; }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox::down-arrow {{ image: url({chev}); width: 12px; height: 12px; }}
        QComboBox QAbstractItemView {{ background: #202020; border: 1px solid {BORDER}; selection-background-color: #333; }}
        QPushButton, QToolButton {{ background: #232323; border: 1px solid #343434; border-radius: 6px; padding: 6px 12px; }}
        QPushButton:hover, QToolButton:hover {{ background: #2c2c2c; border-color: #444; }}
        QPushButton:pressed {{ background: #1a1a1a; }}
        QPushButton:disabled {{ color: #5a5a5a; background: #1b1b1b; border-color: #262626; }}
        #runButton {{ background: {BONE}; color: #141414; font-weight: 700; font-size: 14px; border: none; padding: 10px 18px; }}
        #runButton:hover {{ background: #ffffff; }}
        #runButton:disabled {{ background: #3a3a3a; color: #777; }}
        #stopButton {{ background: #3a1616; border-color: #6e2a2a; color: #ff9b9b; font-weight: 600; padding: 10px 16px; }}
        #stopButton:hover {{ background: #4a1c1c; }}
        #stopButton:disabled {{ background: #1b1b1b; border-color: #262626; color: #5a5a5a; }}
        #linkButton {{ background: transparent; border: none; color: {MUTED}; padding: 4px 6px; }}
        #linkButton:hover {{ color: {TEXT}; }}
        QCheckBox {{ spacing: 8px; }}
        QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid #555; background: {SURFACE}; }}
        QCheckBox::indicator:hover {{ border-color: #888; }}
        QCheckBox::indicator:checked {{ background: {BONE}; border-color: {BONE}; image: url({chk}); }}
        #fieldLabel {{ font-weight: 600; color: #d8d5cf; }}
        #help {{ color: {DIM}; font-size: 12px; }}
        #key {{ color: {MUTED}; }}
        #sectionTitle {{ color: {MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 2px; }}
        #toolTitle {{ font-family: {MONO}; font-size: 26px; font-weight: 700; }}
        #tagline {{ color: #cfcbc4; font-size: 15px; }}
        #warning {{ background: {rgba(AMBER, 0.08)}; border: 1px solid {rgba(AMBER, 0.35)}; color: {AMBER};
                    border-radius: 6px; padding: 7px 10px; }}
        #errors {{ background: {rgba(RED, 0.08)}; border: 1px solid {rgba(RED, 0.4)}; color: {RED};
                   border-radius: 6px; padding: 7px 10px; }}
        #formCard, #outCard {{ background: #181818; border: 1px solid #252525; border-radius: 10px; }}
        #output {{ background: #0d0d0d; border: 1px solid #222; border-radius: 8px; padding: 8px;
                   font-family: {MONO}; font-size: 12px; }}
        #status {{ font-weight: 600; }}
        #homeTitle {{ font-size: 30px; font-weight: 800; letter-spacing: 6px; }}
        #homeSub {{ color: {MUTED}; font-size: 14px; }}
        #kitCard {{ background: #181818; border: 1px solid #252525; border-radius: 12px; }}
        #tile {{ background: #181818; border: 1px solid #252525; border-radius: 10px; }}
        #tileLabel {{ color: {MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; }}
        #tileValue {{ font-family: {MONO}; font-size: 26px; font-weight: 700; }}
        #tileSub {{ color: {DIM}; font-size: 11px; }}
        #checkRow {{ background: #181818; border: 1px solid #252525; border-radius: 8px; }}
        #checkTitle {{ font-weight: 700; }}
        #checkDetail {{ color: #cfcbc4; }}
        #checkFix {{ color: {MUTED}; font-size: 12px; }}
        #footer {{ color: {DIM}; font-size: 11px; }}
        QTreeWidget, QTableWidget {{ background: #0f0f0f; border: 1px solid #222; border-radius: 8px;
                                     alternate-background-color: #141414; gridline-color: #222; }}
        QTreeWidget::item, QTableWidget::item {{ padding: 3px 4px; }}
        QTreeWidget::item:selected, QTableWidget::item:selected {{ background: #262626; color: #fff; }}
        QHeaderView::section {{ background: #181818; color: {MUTED}; border: none; border-bottom: 1px solid #262626;
                                padding: 6px; font-weight: 700; font-size: 11px; }}
        QProgressBar {{ background: #0d0d0d; border: 1px solid #262626; border-radius: 5px; height: 10px; text-align: center;
                        color: transparent; }}
        QProgressBar::chunk {{ background: {BONE}; border-radius: 4px; }}
        QScrollArea {{ border: none; background: transparent; }}
        QScrollArea > QWidget > QWidget {{ background: transparent; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: #333; border-radius: 4px; min-height: 30px; }}
        QScrollBar::handle:vertical:hover {{ background: #444; }}
        QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
        QScrollBar::handle:horizontal {{ background: #333; border-radius: 4px; min-width: 30px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        QMenuBar {{ background: {PANEL}; }}
        QMenu {{ background: #1e1e1e; border: 1px solid #333; }}
        QMenu::item:selected {{ background: #333; }}
        """

    def label(text, obj=None, wrap=False, sel=False):
        lab = QLabel(text)
        if obj:
            lab.setObjectName(obj)
        lab.setWordWrap(wrap)
        if sel:
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return lab

    def chip(text, color):
        c = QLabel(text)
        c.setStyleSheet(f"background:{rgba(color, 0.13)}; color:{color}; border:1px solid {rgba(color, 0.35)};"
                        "border-radius:9px; padding:2px 9px; font-size:11px; font-weight:700;")
        c.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        c.setAlignment(Qt.AlignCenter)
        return c

    def button(text, obj=None, slot=None):
        b = QPushButton(text)
        if obj:
            b.setObjectName(obj)
        if slot:
            b.clicked.connect(slot)
        return b

    def card(obj="formCard", margins=16, spacing=10):
        f = QFrame()
        f.setObjectName(obj)
        lay = QVBoxLayout(f)
        lay.setContentsMargins(margins, margins, margins, margins)
        lay.setSpacing(spacing)
        return f, lay

    def clear_layout(lay):
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
            elif it.layout():
                clear_layout(it.layout())

    def temp_color(v, warn=75, hot=88):
        if v is None:
            return DIM
        return RED if v >= hot else AMBER if v >= warn else TEXT

    def sentence(text):
        return text[:1].upper() + text[1:]

    def fmt(v, unit="", digits=0):
        return "-" if v is None else f"{v:,.{digits}f}{unit}"

    def skull(height, dpr):
        pm = QPixmap(asset("skull.png"))
        pm = pm.scaledToHeight(int(height * dpr), Qt.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
        return pm

    # ------------------------------------------------------------ background work
    class FuncThread(QThread):
        done = Signal(object)

        def __init__(self, fn, *args):
            super().__init__()
            self.fn, self.args = fn, args

        def run(self):
            try:
                self.done.emit(self.fn(*self.args))
            except Exception as e:                   # noqa: BLE001
                log("thread error: " + traceback.format_exc())
                self.done.emit(e)

    class SensorThread(QThread):
        snapshot = Signal(dict)
        backend = Signal(str, str)

        def __init__(self):
            super().__init__()
            self._stop = False

        def stop(self):
            self._stop = True

        def run(self):
            b, note = bc_sensors.open_backend()
            self.backend.emit(b.name, note or "")
            fails = 0
            while not self._stop:
                t0 = time.time()
                try:
                    snap = b.read()
                    snap["t"] = t0
                    self.snapshot.emit(snap)
                    fails = 0
                except Exception:                    # noqa: BLE001
                    fails += 1
                    log("sensor read: " + traceback.format_exc())
                    if fails == 5 and b.name != "basic":
                        b.close()
                        b = bc_sensors.BasicBackend()
                        self.backend.emit(b.name, b.note)
                while not self._stop and time.time() - t0 < 1.0:
                    self.msleep(50)
            b.close()

    class Worker(QObject):
        """One worker process: collects progress lines and the final RESULT json."""
        finished = Signal(object, object)            # (self, result dict or None)
        line = Signal(str)

        def __init__(self, args, parent=None):
            super().__init__(parent)
            self.result = None
            self.buf = ""
            self.p = QProcess(self)
            self.p.setProcessChannelMode(QProcess.MergedChannels)
            self.p.readyReadStandardOutput.connect(self._read)
            self.p.finished.connect(self._done)
            self.p.errorOccurred.connect(self._error)
            prog, argv = worker_command(args)
            self.p.start(prog, argv)

        def _read(self):
            self.buf += bytes(self.p.readAllStandardOutput()).decode("utf-8", "replace")
            while "\n" in self.buf:
                ln, self.buf = self.buf.split("\n", 1)
                ln = ln.strip()
                if ln.startswith("RESULT "):
                    try:
                        self.result = json.loads(ln[7:])
                    except ValueError:
                        pass
                elif ln:
                    self.line.emit(ln)

        def _done(self, *a):
            self._read()
            self.finished.emit(self, self.result)

        def _error(self, err):
            if err == QProcess.FailedToStart:
                self.line.emit("FAILED to start worker")
                self.finished.emit(self, None)

        def kill(self):
            if self.p.state() != QProcess.NotRunning:
                self.p.kill()

    # ------------------------------------------------------------ widgets
    class Tile(QFrame):
        def __init__(self, title, sub=""):
            super().__init__()
            self.setObjectName("tile")
            lay = QVBoxLayout(self)
            lay.setContentsMargins(14, 10, 14, 10)
            lay.setSpacing(2)
            self.t = label(title.upper(), "tileLabel")
            self.v = label("-", "tileValue")
            self.s = label(sub, "tileSub")
            lay.addWidget(self.t)
            lay.addWidget(self.v)
            lay.addWidget(self.s)
            self.setMinimumWidth(130)

        def set(self, text, color=TEXT, sub=None):
            self.v.setText(text)
            self.v.setStyleSheet(f"color:{color};")
            if sub is not None:
                self.s.setText(sub)

    class Graph(QWidget):
        def __init__(self, source, window=300, height=210):
            super().__init__()
            self.source = source                     # callable -> (series, marks)
            self.window = window
            self.setMinimumHeight(height)

        def paintEvent(self, e):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QColor("#222222"))
            p.setBrush(QColor("#0d0d0d"))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
            series, marks = self.source()
            bc_report.draw_graph(p, QRectF(self.rect()).adjusted(6, 4, -10, -4), series, window=self.window, marks=marks)
            p.end()

    class Page(QScrollArea):
        def __init__(self, title, tagline):
            super().__init__()
            self.setObjectName("page")
            self.setWidgetResizable(True)
            w = QWidget()
            w.setObjectName("page")
            self.v = QVBoxLayout(w)
            self.v.setContentsMargins(34, 26, 34, 24)
            self.v.setSpacing(12)
            head = QHBoxLayout()
            tl = QVBoxLayout()
            tl.setSpacing(2)
            tl.addWidget(label(title, "toolTitle"))
            tl.addWidget(label(tagline, "tagline", wrap=True))
            head.addLayout(tl, 1)
            self.actions = QHBoxLayout()
            self.actions.setSpacing(8)
            head.addLayout(self.actions)
            self.v.addLayout(head)
            self.v.addSpacing(4)
            self.setWidget(w)

    has_gl = False
    GpuStress = None
    try:
        from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram, QOpenGLVertexArrayObject
        from PySide6.QtOpenGLWidgets import QOpenGLWidget

        class GpuStress(QOpenGLWidget):          # noqa: F811
            """Full-window fractal shader that keeps the GPU busy. Work per frame adapts to hold ~20-40 fps."""
            failed = Signal(str)
            fps = Signal(float)
            closed = Signal()

            def __init__(self):
                super().__init__()
                self.setWindowTitle(f"{APP_NAME} - GPU load (closing this window stops the GPU test)")
                self.setWindowIcon(QIcon(asset("icon.png")))
                self.resize(1280, 720)
                self.iters = 120.0
                self.frames = 0
                self.t0 = self.last = time.perf_counter()
                self.prog = self.vao = None
                self.ok = False
                self.frameSwapped.connect(self.update)

            def initializeGL(self):
                try:
                    p = QOpenGLShaderProgram(self)
                    ok = (p.addShaderFromSourceCode(QOpenGLShader.Vertex, GPU_VERT)
                          and p.addShaderFromSourceCode(QOpenGLShader.Fragment, GPU_FRAG) and p.link())
                    if not ok:
                        QTimer.singleShot(0, lambda: self.failed.emit("OpenGL shader didn't compile: " + p.log()[:300]))
                        return
                    self.prog = p
                    self.vao = QOpenGLVertexArrayObject(self)
                    self.vao.create()
                    self.ok = True
                except Exception as e:           # noqa: BLE001
                    QTimer.singleShot(0, lambda: self.failed.emit(f"OpenGL error: {e}"))

            def _uniforms(self):
                t = time.perf_counter() - self.t0
                dpr = self.devicePixelRatioF()
                try:
                    self.prog.setUniformValue1f("u_time", t)
                    self.prog.setUniformValue1i("u_iter", int(self.iters))
                except AttributeError:
                    self.prog.setUniformValue(self.prog.uniformLocation("u_time"), float(t))
                    self.prog.setUniformValue(self.prog.uniformLocation("u_iter"), int(self.iters))
                self.prog.setUniformValue("u_res", QVector2D(self.width() * dpr, self.height() * dpr))

            def paintGL(self):
                f = self.context().functions()
                f.glClearColor(0.0, 0.0, 0.0, 1.0)
                f.glClear(0x4000)
                if not self.ok:
                    return
                self.prog.bind()
                if self.vao.isCreated():
                    self.vao.bind()
                self._uniforms()
                f.glDrawArrays(4, 0, 3)
                if self.vao.isCreated():
                    self.vao.release()
                self.prog.release()
                self.frames += 1
                now = time.perf_counter()
                if now - self.last >= 1.0:
                    fps = self.frames / (now - self.last)
                    self.frames, self.last = 0, now
                    if fps > 40:                  # keep each frame heavy but short (< ~50 ms)
                        self.iters = min(self.iters * 1.35, 60000)
                    elif fps < 18:
                        self.iters = max(self.iters / 1.35, 40)
                    self.fps.emit(fps)

            def closeEvent(self, e):
                self.closed.emit()
                super().closeEvent(e)
        has_gl = True
    except ImportError:
        pass

    # ------------------------------------------------------------ pages
    class HomePage(QScrollArea):
        def __init__(self, main):
            super().__init__()
            self.main = main
            self.setObjectName("home")
            self.setWidgetResizable(True)
            w = QWidget()
            w.setObjectName("home")
            v = QVBoxLayout(w)
            v.setContentsMargins(40, 24, 40, 24)
            v.setSpacing(6)
            logo = QLabel()
            logo.setPixmap(skull(130, self.devicePixelRatioF()))
            logo.setAlignment(Qt.AlignCenter)
            v.addWidget(logo)
            t = label("LANDULL BUILDCHECK", "homeTitle")
            t.setAlignment(Qt.AlignCenter)
            v.addWidget(t)
            s = label("Post-build health check: specs, thermals, stress test, benchmarks, and a report to hand over.",
                      "homeSub")
            s.setAlignment(Qt.AlignCenter)
            v.addWidget(s)
            v.addSpacing(10)
            self.banner = label("", "warning", wrap=True)
            self.banner.hide()
            v.addWidget(self.banner)

            tiles = QHBoxLayout()
            tiles.setSpacing(12)
            self.tiles = {k: Tile(n) for k, n in (("cpu_temp", "CPU temp"), ("gpu_temp", "GPU temp"),
                                                  ("cpu_load", "CPU load"), ("gpu_load", "GPU load"),
                                                  ("ram_pct", "Memory used"))}
            for tl in self.tiles.values():
                tiles.addWidget(tl, 1)
            v.addLayout(tiles)
            v.addSpacing(8)

            cols = QHBoxLayout()
            cols.setSpacing(16)
            # full check card
            fc, fl = card("kitCard")
            head = QLabel("full check")
            head.setStyleSheet(f"color:{BONE}; font-size:18px; font-weight:800; letter-spacing:1px; font-family:{MONO};")
            fl.addWidget(head)
            fl.addWidget(label("Runs everything in order and saves the PDF report when it's done.", "help", wrap=True))
            g = QGridLayout()
            g.setHorizontalSpacing(10)
            g.setVerticalSpacing(8)
            for row, (key, lab, ph) in enumerate((("customer", "Customer", "name"),
                                                  ("build", "Build", "order # or build name"),
                                                  ("tech", "Checked by", ""))):
                g.addWidget(label(lab, "fieldLabel"), row, 0)
                e = QLineEdit()
                e.setPlaceholderText(ph)
                main.bind(key, e)
                g.addWidget(e, row, 1)
            g.addWidget(label("Stress test", "fieldLabel"), 3, 0)
            self.duration = QComboBox()
            for text, sec in (("Off", 0), ("2 minutes (quick)", 120), ("5 minutes", 300), ("10 minutes", 600),
                              ("15 minutes", 900), ("30 minutes", 1800)):
                self.duration.addItem(text, sec)
            self.duration.setCurrentIndex(3)
            g.addWidget(self.duration, 3, 1)
            fl.addLayout(g)
            opts = QHBoxLayout()
            self.o_cpu = QCheckBox("CPU load")
            self.o_gpu = QCheckBox("GPU load")
            self.o_bench = QCheckBox("Benchmarks")
            self.o_winsat = QCheckBox("WinSAT scores")
            self.o_winsat.setToolTip("Windows Experience Index (adds about 2 minutes)")
            for c in (self.o_cpu, self.o_gpu, self.o_bench):
                c.setChecked(True)
            for c in (self.o_cpu, self.o_gpu, self.o_bench, self.o_winsat):
                opts.addWidget(c)
            opts.addStretch(1)
            fl.addLayout(opts)
            br = QHBoxLayout()
            self.run = button("Run full check", "runButton", main.start_full_check)
            self.stop = button("Stop", "stopButton", main.stop_full_check)
            self.stop.setEnabled(False)
            br.addWidget(self.run)
            br.addWidget(self.stop)
            br.addStretch(1)
            fl.addLayout(br)
            self.steps_box = QVBoxLayout()
            self.steps_box.setSpacing(3)
            fl.addLayout(self.steps_box)
            self.status = label("", "help", wrap=True)
            fl.addWidget(self.status)
            self.result_links = QHBoxLayout()
            fl.addLayout(self.result_links)
            fl.addStretch(1)
            cols.addWidget(fc, 3)
            # system summary card
            sc, self.sl = card("kitCard")
            head2 = QLabel("this pc")
            head2.setStyleSheet(f"color:#d9a441; font-size:18px; font-weight:800; letter-spacing:1px; font-family:{MONO};")
            self.sl.addWidget(head2)
            self.spec_box = QVBoxLayout()
            self.spec_box.setSpacing(6)
            self.sl.addLayout(self.spec_box)
            self.sl.addStretch(1)
            self.spec_box.addWidget(label("Reading system info...", "help"))
            cols.addWidget(sc, 4)
            v.addLayout(cols)
            v.addSpacing(12)
            foot = label(f"v{VERSION}  ·  github.com/Landull  ·  Everything runs on this computer - nothing is uploaded.",
                         "footer")
            foot.setAlignment(Qt.AlignCenter)
            v.addWidget(foot)
            v.addStretch(1)
            self.setWidget(w)
            self.step_labels = []

        def set_info(self, info):
            clear_layout(self.spec_box)
            for k, val in bc_collect.spec_rows(info):
                row = QHBoxLayout()
                kl = label(k, "key")
                kl.setFixedWidth(92)
                kl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
                row.addWidget(kl)
                row.addWidget(label(val, wrap=True, sel=True), 1)
                self.spec_box.addLayout(row)

        def on_snapshot(self, sm):
            ct, gt = sm.get("cpu_temp"), sm.get("gpu_temp")
            self.tiles["cpu_temp"].set(fmt(ct, " °C"), temp_color(ct), "approximate" if sm.get("cpu_temp_approx") else "")
            self.tiles["gpu_temp"].set(fmt(gt, " °C"), temp_color(gt, 75, 85), (sm.get("gpu_name") or "")[:30])
            self.tiles["cpu_load"].set(fmt(sm.get("cpu_load"), " %"))
            self.tiles["gpu_load"].set(fmt(sm.get("gpu_load"), " %"))
            self.tiles["ram_pct"].set(fmt(sm.get("ram_pct"), " %"))

        def set_steps(self, steps, current, states):
            clear_layout(self.steps_box)
            icons = {"pending": ("○", DIM), "running": ("◐", AMBER), "done": ("✓", GREEN), "failed": ("✕", RED),
                     "skipped": ("–", DIM)}
            for i, name in enumerate(steps):
                ic, col = icons[states[i]]
                lab = QLabel(f'<span style="color:{col}; font-weight:700;">{ic}</span>&nbsp;&nbsp;'
                             f'<span style="color:{TEXT if states[i] == "running" else MUTED};">{name}</span>')
                self.steps_box.addWidget(lab)

    class SystemPage(Page):
        def __init__(self, main):
            super().__init__("System", "Everything Windows reports about the hardware, drivers and setup.")
            self.main = main
            self.refresh = button("Refresh", slot=main.collect_info)
            self.actions.addWidget(self.refresh)
            self.body = QVBoxLayout()
            self.body.setSpacing(12)
            self.v.addLayout(self.body)
            self.v.addStretch(1)
            self.body.addWidget(label("Reading system info... (this takes 10-30 seconds on Windows)", "help"))

        def kv_card(self, title, rows):
            f, lay = card()
            lay.addWidget(label(title.upper(), "sectionTitle"))
            g = QGridLayout()
            g.setHorizontalSpacing(16)
            g.setVerticalSpacing(6)
            for r, (k, val) in enumerate(rows):
                kl = label(k, "key")
                kl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
                g.addWidget(kl, r, 0)
                g.addWidget(label(str(val) if val not in (None, "") else "-", wrap=True, sel=True), r, 1)
            g.setColumnStretch(1, 1)
            g.setColumnMinimumWidth(0, 130)
            lay.addLayout(g)
            return f

        def table_card(self, title, headers, rows):
            f, lay = card()
            lay.addWidget(label(title.upper(), "sectionTitle"))
            t = QTableWidget(len(rows), len(headers))
            t.setHorizontalHeaderLabels(headers)
            t.verticalHeader().hide()
            t.setEditTriggers(QAbstractItemView.NoEditTriggers)
            t.setSelectionMode(QAbstractItemView.NoSelection)
            t.setAlternatingRowColors(True)
            t.setShowGrid(False)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            t.horizontalHeader().setStretchLastSection(True)
            for r, row in enumerate(rows):
                for c, val in enumerate(row):
                    t.setItem(r, c, QTableWidgetItem(str(val) if val not in (None, "") else "-"))
            t.setFixedHeight(34 + 30 * max(1, len(rows)))
            t.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            lay.addWidget(t)
            return f

        def set_info(self, info):
            clear_layout(self.body)
            if info.get("errors") and not info.get("cpu", {}).get("name"):
                self.body.addWidget(label("Couldn't read system info: " + "; ".join(info["errors"][:3]), "errors", wrap=True))
            cpu = info.get("cpu", {})
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(12)
            cards = []
            cache = " / ".join(x for x in (f"L2 {fmt_bytes((cpu.get('l2') or 0) * 1024, 0)}" if cpu.get("l2") else "",
                                           f"L3 {fmt_bytes((cpu.get('l3') or 0) * 1024, 0)}" if cpu.get("l3") else "") if x)
            cards.append(self.kv_card("Processor", [
                ("Model", cpu.get("name")), ("Cores / threads", f"{cpu.get('cores')} / {cpu.get('threads')}"),
                ("Base clock", f"{cpu.get('mhz')} MHz" if cpu.get("mhz") else None), ("Cache", cache or None),
                ("Socket", cpu.get("socket")),
                ("Virtualization", None if cpu.get("virt") is None else ("Enabled" if cpu.get("virt") else "Disabled"))]))
            b, bi, comp = info.get("board", {}), info.get("bios", {}), info.get("computer", {})
            cards.append(self.kv_card("Motherboard", [
                ("Board", f"{b.get('manufacturer', '')} {b.get('product', '')}".strip() or None),
                ("Revision", b.get("version")), ("BIOS", bi.get("version")), ("BIOS date", (bi.get("date") or "")[:10]),
                ("BIOS vendor", bi.get("vendor")), ("Computer name", comp.get("name"))]))
            for gpu in [g for g in info.get("gpus", []) if not g["virtual"]] or info.get("gpus", [])[:1]:
                cards.append(self.kv_card("Graphics", [
                    ("Model", gpu["name"]), ("Memory", fmt_bytes(gpu["vram"], 0) if gpu["vram"] else None),
                    ("Driver", gpu["driver"]), ("Driver date", gpu["driver_date"][:10]),
                    ("Output", f"{gpu['res']} @ {gpu['hz']} Hz" if gpu["res"] else "not driving a display"),
                    ("Type", "Dedicated" if gpu["discrete"] else "Integrated")]))
            o = info.get("os", {})
            cards.append(self.kv_card("Windows" if info.get("platform") == "windows" else "Operating system", [
                ("Edition", o.get("name")), ("Build", o.get("build")), ("Architecture", o.get("arch")),
                ("Installed", o.get("install")), ("Last boot", o.get("boot")),
                ("Running as admin", "Yes" if info.get("admin") else "No")]))
            for i, c in enumerate(cards):
                grid.addWidget(c, i // 2, i % 2)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            self.body.addLayout(grid)
            ram = info.get("ram", {})
            sticks = ram.get("sticks", [])
            if sticks:
                self.body.addWidget(self.table_card(
                    f"Memory  ·  {fmt_bytes(ram.get('total'), 0)}  ·  {len(sticks)} of {ram.get('slots') or '?'} slots",
                    ["Slot", "Size", "Type", "Running at", "Rated (SPD)", "Maker", "Part number"],
                    [(s["slot"] or s["bank"], fmt_bytes(s["size"], 0), s["type"], f"{s['configured']} MT/s",
                      f"{s['rated']} MT/s", s["maker"], s["part"]) for s in sticks]))
            disks = info.get("disks", [])
            if disks:
                self.body.addWidget(self.table_card("Drives", ["Drive", "Size", "Type", "Health", "Temp", "Wear",
                                                               "Power-on hours", "Windows"],
                    [(d["name"], fmt_gb_decimal(d["size"]), f"{d['bus']} {d['media']}".strip(), d["health"],
                      f"{d['temp']} °C" if d["temp"] else None, f"{d['wear']}%" if d["wear"] is not None else None,
                      d["hours"], "Yes" if d["boot"] else "") for d in disks]))
            vols = info.get("volumes", [])
            if vols:
                self.body.addWidget(self.table_card("Volumes", ["Drive", "Label", "File system", "Size", "Free"],
                    [(v.get("letter") + (":" if len(str(v.get("letter"))) == 1 else ""), v.get("label"), v.get("fs"),
                      fmt_bytes(v.get("size")), fmt_bytes(v.get("free"))) for v in vols]))
            disp = info.get("displays", [])
            if disp:
                self.body.addWidget(self.table_card("Displays", ["Display", "Connected to", "Resolution", "Refresh",
                                                                 "Max refresh"],
                    [(d.get("monitor") or d["device"], d["adapter"], f"{d['w']}x{d['h']}", f"{d['hz']} Hz",
                      f"{d['max_hz']} Hz") for d in disp]))
            net = info.get("net", [])
            if net:
                self.body.addWidget(self.table_card("Network", ["Adapter", "Device", "Link speed"],
                    [(n.get("name"), n.get("desc"), n.get("speed")) for n in net]))

    class SensorsPage(Page):
        def __init__(self, main):
            super().__init__("Sensors", "Live temperatures, clocks, loads, power and fans. Updates every second.")
            self.main = main
            self.reset = button("Reset min/max", slot=self.reset_minmax)
            self.actions.addWidget(self.reset)
            self.banner = QFrame()
            self.banner.setObjectName("warning")
            bl = QHBoxLayout(self.banner)
            bl.setContentsMargins(10, 6, 10, 6)
            self.banner_text = label("", wrap=True)
            self.banner_text.setStyleSheet(f"color:{AMBER}; background: transparent; border: none;")
            bl.addWidget(self.banner_text, 1)
            self.pawn_btn = button("Install PawnIO", slot=main.install_pawnio)
            bl.addWidget(self.pawn_btn)
            self.banner.hide()
            self.v.addWidget(self.banner)
            tiles = QGridLayout()
            tiles.setSpacing(12)
            self.tiles = {}
            for i, (k, n) in enumerate((("cpu_temp", "CPU temp"), ("cpu_clock", "CPU clock"), ("cpu_power", "CPU power"),
                                        ("cpu_load", "CPU load"), ("gpu_temp", "GPU temp"), ("gpu_hotspot", "GPU hot spot"),
                                        ("gpu_power", "GPU power"), ("gpu_load", "GPU load"))):
                self.tiles[k] = Tile(n)
                tiles.addWidget(self.tiles[k], i // 4, i % 4)
            self.v.addLayout(tiles)
            self.graph = Graph(main.temp_series, window=300)
            self.v.addWidget(self.graph)
            self.tree = QTreeWidget()
            self.tree.setColumnCount(4)
            self.tree.setHeaderLabels(["Sensor", "Value", "Min", "Max"])
            self.tree.setAlternatingRowColors(True)
            self.tree.setUniformRowHeights(True)
            self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
            for c in (1, 2, 3):
                self.tree.header().setSectionResizeMode(c, QHeaderView.Fixed)
                self.tree.setColumnWidth(c, 110)
            self.tree.setMinimumHeight(420)
            self.v.addWidget(self.tree, 1)
            self.items = {}
            self.minmax = {}

        def reset_minmax(self):
            self.minmax.clear()

        def set_backend(self, name, note):
            if note:
                self.banner_text.setText(note)
                self.pawn_btn.setVisible(IS_WIN and "PawnIO" in note)
                self.banner.show()
            else:
                self.banner.hide()

        def on_snapshot(self, snap):
            sm = snap["summary"]
            ct = sm.get("cpu_temp")
            self.tiles["cpu_temp"].set(fmt(ct, " °C"), temp_color(ct), "approximate" if sm.get("cpu_temp_approx") else "")
            self.tiles["cpu_clock"].set(fmt(sm.get("cpu_clock"), " MHz"))
            self.tiles["cpu_power"].set(fmt(sm.get("cpu_power"), " W"))
            self.tiles["cpu_load"].set(fmt(sm.get("cpu_load"), " %"))
            gt = sm.get("gpu_temp")
            self.tiles["gpu_temp"].set(fmt(gt, " °C"), temp_color(gt, 75, 85))
            hs = sm.get("gpu_hotspot")
            self.tiles["gpu_hotspot"].set(fmt(hs, " °C"), temp_color(hs, 90, 100))
            self.tiles["gpu_power"].set(fmt(sm.get("gpu_power"), " W"))
            self.tiles["gpu_load"].set(fmt(sm.get("gpu_load"), " %"))
            if not self.isVisible():
                self._minmax(snap)
                return
            self.graph.update()
            self._minmax(snap)
            first = not self.items
            for g in snap["groups"]:
                gkey = (g["hw"], g["name"])
                gi = self.items.get(gkey)
                if gi is None:
                    gi = QTreeWidgetItem([f"{bc_sensors.HW_LABEL.get(g['hw'], g['hw'])}  ·  {g['name']}", "", "", ""])
                    f = gi.font(0)
                    f.setBold(True)
                    gi.setFont(0, f)
                    self.tree.addTopLevelItem(gi)
                    self.items[gkey] = gi
                    gi.setExpanded(True)
                for s in g["sensors"]:
                    tkey = gkey + (s["type"],)
                    ti = self.items.get(tkey)
                    if ti is None:
                        ti = QTreeWidgetItem([s["type"] if s["type"] != "SmallData" else "Data", "", "", ""])
                        ti.setForeground(0, QColor(MUTED))
                        gi.addChild(ti)
                        ti.setExpanded(s["type"] in ("Temperature", "Load", "Clock", "Power", "Fan", "Control"))
                        self.items[tkey] = ti
                    skey = tkey + (s["name"],)
                    si = self.items.get(skey)
                    if si is None:
                        si = QTreeWidgetItem([s["name"], "", "", ""])
                        for c in (1, 2, 3):
                            si.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
                        ti.addChild(si)
                        self.items[skey] = si
                    mn, mx = self.minmax.get(skey, (None, None))
                    si.setText(1, self._fmt(s["value"], s))
                    si.setText(2, self._fmt(mn, s))
                    si.setText(3, self._fmt(mx, s))
                    if s["type"] == "Temperature" and s["value"] is not None and "distance" not in s["name"].lower():
                        si.setForeground(1, QColor(temp_color(s["value"], 80, 92)))
            if first:
                self.tree.expandToDepth(0)

        def _minmax(self, snap):
            for g in snap["groups"]:
                for s in g["sensors"]:
                    v = s["value"]
                    if v is None:
                        continue
                    k = (g["hw"], g["name"], s["type"], s["name"])
                    mn, mx = self.minmax.get(k, (v, v))
                    self.minmax[k] = (min(mn, v), max(mx, v))

        @staticmethod
        def _fmt(v, s):
            if v is None:
                return "-"
            unit = s["unit"]
            if s["type"] == "Voltage":
                return f"{v:.3f} {unit}"
            if s["type"] in ("Power", "Data", "Current"):
                return f"{v:.1f} {unit}"
            if s["type"] == "Throughput":
                return fmt_bytes(v) + "/s"
            return f"{v:.0f} {unit}".strip()

    class StressPage(Page):
        def __init__(self, main):
            super().__init__("Stress test", "Loads the CPU and GPU, watches temperatures and clocks, and flags "
                                            "throttling or calculation errors.")
            self.main = main
            f, lay = card()
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(label("Duration", "fieldLabel"))
            self.duration = QComboBox()
            for text, sec in (("1 minute", 60), ("2 minutes", 120), ("5 minutes", 300), ("10 minutes", 600),
                              ("15 minutes", 900), ("30 minutes", 1800), ("60 minutes", 3600)):
                self.duration.addItem(text, sec)
            self.duration.setCurrentIndex(3)
            row.addWidget(self.duration)
            row.addSpacing(10)
            self.c_cpu = QCheckBox(f"CPU - all {os.cpu_count()} threads")
            self.c_gpu = QCheckBox("GPU - opens a render window")
            self.c_cpu.setChecked(True)
            self.c_gpu.setChecked(True)
            row.addWidget(self.c_cpu)
            row.addWidget(self.c_gpu)
            row.addStretch(1)
            self.start = button("Start", "runButton", self.start_clicked)
            self.stop = button("Stop", "stopButton", main.stress_stop)
            self.stop.setEnabled(False)
            row.addWidget(self.start)
            row.addWidget(self.stop)
            lay.addLayout(row)
            self.prog = QProgressBar()
            self.prog.setRange(0, 1000)
            self.prog.setValue(0)
            self.prog.setFixedHeight(10)
            lay.addWidget(self.prog)
            self.status = label("Close other heavy apps first. Stress tests make the fans loud - that's expected.", "help",
                                wrap=True)
            lay.addWidget(self.status)
            self.v.addWidget(f)
            tiles = QGridLayout()
            tiles.setSpacing(12)
            self.tiles = {}
            for i, (k, n) in enumerate((("cpu_temp", "CPU temp"), ("cpu_clock", "CPU clock"), ("cpu_power", "CPU power"),
                                        ("gpu_temp", "GPU temp"), ("gpu_hotspot", "GPU hot spot"), ("gpu_power", "GPU power"))):
                self.tiles[k] = Tile(n, "max -")
                tiles.addWidget(self.tiles[k], i // 3, i % 3)
            self.v.addLayout(tiles)
            self.graph = Graph(main.temp_series, window=600, height=240)
            self.v.addWidget(self.graph)
            rc, rl = card("outCard")
            rl.addWidget(label("RESULT", "sectionTitle"))
            self.result = label("No stress test run yet.", wrap=True, sel=True)
            self.result.setObjectName("status")
            rl.addWidget(self.result)
            self.fix = label("", "help", wrap=True)
            rl.addWidget(self.fix)
            self.log = QPlainTextEdit()
            self.log.setObjectName("output")
            self.log.setReadOnly(True)
            self.log.setMaximumBlockCount(2000)
            self.log.setFixedHeight(150)
            rl.addWidget(self.log)
            self.v.addWidget(rc)
            self.v.addStretch(1)

        def start_clicked(self):
            self.main.stress_start(self.duration.currentData(), self.c_cpu.isChecked(), self.c_gpu.isChecked())

        def running(self, on, duration=600):
            self.start.setEnabled(not on)
            self.stop.setEnabled(on)
            self.duration.setEnabled(not on)
            self.c_cpu.setEnabled(not on)
            self.c_gpu.setEnabled(not on)
            if on:
                self.graph.window = max(300, duration + 90)

        def on_snapshot(self, sm, stats):
            for k, unit in (("cpu_temp", " °C"), ("cpu_clock", " MHz"), ("cpu_power", " W"), ("gpu_temp", " °C"),
                            ("gpu_hotspot", " °C"), ("gpu_power", " W")):
                v = sm.get(k)
                mx = (stats.get(k) or {}).get("max") if stats else None
                col = temp_color(v, 80, 92) if k == "cpu_temp" else temp_color(v, 78, 87) if k == "gpu_temp" else \
                    temp_color(v, 95, 105) if k == "gpu_hotspot" else TEXT
                self.tiles[k].set(fmt(v, unit), col, f"max {fmt(mx, unit)}")
            if self.isVisible():
                self.graph.update()

        def show_result(self, res):
            col = STATUS_COLOR.get(res["status"], TEXT)
            self.result.setText(f'<span style="color:{col}; font-weight:800;">{res["status"]}</span>&nbsp;&nbsp;'
                                f'{res["summary"]}')
            self.fix.setText(res.get("fix", ""))

    class BenchPage(Page):
        def __init__(self, main):
            super().__init__("Benchmarks", "Quick CPU, memory and drive tests. Scores are for comparing BuildCheck runs "
                                           "against each other, so keep them with the build records.")
            self.main = main
            f, lay = card()
            g = QGridLayout()
            g.setHorizontalSpacing(14)
            g.setVerticalSpacing(8)
            self.b_cpu1 = QCheckBox("CPU single-core")
            self.b_cpun = QCheckBox("CPU multi-core")
            self.b_mem = QCheckBox("Memory bandwidth")
            self.b_disk = QCheckBox("Drive speed")
            self.b_winsat = QCheckBox("Windows Experience scores (WinSAT, about 2 minutes, includes graphics)")
            for c in (self.b_cpu1, self.b_cpun, self.b_mem, self.b_disk):
                c.setChecked(True)
            self.b_winsat.setEnabled(IS_WIN)
            g.addWidget(self.b_cpu1, 0, 0)
            g.addWidget(self.b_cpun, 0, 1)
            g.addWidget(self.b_mem, 0, 2)
            dl = QHBoxLayout()
            dl.addWidget(self.b_disk)
            self.drive = QComboBox()
            self.size = QComboBox()
            for t, mb in (("1 GB test file", 1024), ("4 GB test file", 4096)):
                self.size.addItem(t, mb)
            dl.addWidget(self.drive)
            dl.addWidget(self.size)
            dl.addStretch(1)
            g.addLayout(dl, 1, 0, 1, 3)
            g.addWidget(self.b_winsat, 2, 0, 1, 3)
            lay.addLayout(g)
            row = QHBoxLayout()
            self.run = button("Run benchmarks", "runButton", self.run_clicked)
            self.stop = button("Stop", "stopButton", main.bench_stop)
            self.stop.setEnabled(False)
            row.addWidget(self.run)
            row.addWidget(self.stop)
            self.status = label("", "help", wrap=True)
            row.addWidget(self.status, 1)
            lay.addLayout(row)
            self.prog = QProgressBar()
            self.prog.setRange(0, 100)
            self.prog.setFixedHeight(10)
            lay.addWidget(self.prog)
            self.v.addWidget(f)
            self.table = QTableWidget(0, 3)
            self.table.setHorizontalHeaderLabels(["Test", "Result", "Details"])
            self.table.verticalHeader().hide()
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.table.setSelectionMode(QAbstractItemView.NoSelection)
            self.table.setShowGrid(False)
            self.table.setAlternatingRowColors(True)
            self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            self.table.horizontalHeader().setStretchLastSection(True)
            self.table.setMinimumHeight(300)
            self.v.addWidget(self.table, 1)
            self.v.addWidget(label("CPU scores come from data compression and hashing on every thread. Drive tests write "
                                   "a temporary file (deleted afterwards) and bypass the Windows cache.", "help", wrap=True))
            self.load_drives()

        def load_drives(self, info=None):
            cur = self.drive.currentData()
            self.drive.clear()
            vols = (info or {}).get("volumes") or []
            if vols:
                for vo in vols:
                    letter = str(vo.get("letter"))
                    path = letter + ":\\" if len(letter) == 1 else letter
                    self.drive.addItem(f"{path}  {vo.get('label') or ''}  ({fmt_bytes(vo.get('free'))} free)".replace("  (", " ("),
                                       path)
            else:
                home = os.path.expanduser("~")
                self.drive.addItem(f"{os.path.splitdrive(home)[0] or '/'} (home folder)", home)
            if cur:
                i = self.drive.findData(cur)
                if i >= 0:
                    self.drive.setCurrentIndex(i)
            if IS_WIN:
                i = self.drive.findData(os.environ.get("SystemDrive", "C:") + "\\")
                if i >= 0 and not cur:
                    self.drive.setCurrentIndex(i)

        def selection(self):
            return {"cpu1": self.b_cpu1.isChecked(), "cpun": self.b_cpun.isChecked(), "mem": self.b_mem.isChecked(),
                    "disk": self.b_disk.isChecked(), "winsat": self.b_winsat.isChecked() and IS_WIN,
                    "drive": self.drive.currentData(), "size": self.size.currentData()}

        def run_clicked(self):
            self.main.bench_start(self.selection())

        def running(self, on):
            self.run.setEnabled(not on)
            self.stop.setEnabled(on)

        def show_rows(self, rows):
            self.table.setRowCount(len(rows))
            for r, (name, val, note) in enumerate(rows):
                a = QTableWidgetItem(name)
                b = QTableWidgetItem(val)
                f = b.font()
                f.setBold(True)
                b.setFont(f)
                b.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                c = QTableWidgetItem(note)
                c.setForeground(QColor(MUTED))
                self.table.setItem(r, 0, a)
                self.table.setItem(r, 1, b)
                self.table.setItem(r, 2, c)

    class HealthPage(Page):
        def __init__(self, main):
            super().__init__("Health checks", "Common post-build mistakes and problems: drivers, memory setup, display "
                                              "cables, drives, Windows, security and stability.")
            self.main = main
            self.run = button("Run checks", "runButton", main.run_checks)
            self.actions.addWidget(self.run)
            self.summary = QHBoxLayout()
            self.summary.setSpacing(8)
            self.v.addLayout(self.summary)
            self.body = QVBoxLayout()
            self.body.setSpacing(6)
            self.v.addLayout(self.body)
            self.v.addStretch(1)
            self.body.addWidget(label("Press Run checks. The stress test and benchmarks add their results here too.",
                                      "help"))

        def show_checks(self, checks):
            clear_layout(self.summary)
            clear_layout(self.body)
            v, vtext = bc_collect.verdict(checks)
            big = QLabel(vtext)
            big.setStyleSheet(f"color:{STATUS_COLOR[v]}; font-size:16px; font-weight:800;")
            self.summary.addWidget(big)
            self.summary.addSpacing(10)
            for st in ("FAIL", "WARN", "PASS", "INFO", "SKIP"):
                n = sum(1 for c in checks if c["status"] == st)
                if n:
                    self.summary.addWidget(chip(f"{n} {st}", STATUS_COLOR[st]))
            self.summary.addStretch(1)
            cat = None
            for c in checks:
                if c["cat"] != cat:
                    cat = c["cat"]
                    h = label(cat.upper(), "sectionTitle")
                    h.setContentsMargins(0, 10, 0, 2)
                    self.body.addWidget(h)
                row = QFrame()
                row.setObjectName("checkRow")
                rl = QHBoxLayout(row)
                rl.setContentsMargins(12, 9, 12, 9)
                rl.setSpacing(12)
                ch = chip(c["status"], STATUS_COLOR.get(c["status"], MUTED))
                ch.setFixedWidth(54)
                rl.addWidget(ch, 0, Qt.AlignTop)
                col = QVBoxLayout()
                col.setSpacing(2)
                col.addWidget(label(c["title"], "checkTitle"))
                col.addWidget(label(c["detail"], "checkDetail", wrap=True, sel=True))
                if c["fix"] and c["status"] in ("WARN", "FAIL", "INFO"):
                    col.addWidget(label("→ " + c["fix"], "checkFix", wrap=True))
                rl.addLayout(col, 1)
                self.body.addWidget(row)

    class ReportPage(Page):
        def __init__(self, main):
            super().__init__("Report", "A branded PDF with the specs, health checks, thermals and benchmark results.")
            self.main = main
            f, lay = card()
            g = QGridLayout()
            g.setHorizontalSpacing(12)
            g.setVerticalSpacing(8)
            for row, (key, lab, ph) in enumerate((("customer", "Customer", "name"),
                                                  ("build", "Build", "order # or build name"),
                                                  ("tech", "Checked by", ""))):
                g.addWidget(label(lab, "fieldLabel"), row, 0)
                e = QLineEdit()
                e.setPlaceholderText(ph)
                main.bind(key, e)
                g.addWidget(e, row, 1)
            g.addWidget(label("Notes", "fieldLabel"), 3, 0, Qt.AlignTop)
            self.notes = QPlainTextEdit()
            self.notes.setPlaceholderText("Anything the customer should know: parts used, BIOS settings changed, "
                                          "warranty info...")
            self.notes.setFixedHeight(110)
            g.addWidget(self.notes, 3, 1)
            g.setColumnStretch(1, 1)
            lay.addLayout(g)
            self.v.addWidget(f)
            ic, il = card()
            il.addWidget(label("INCLUDED", "sectionTitle"))
            self.included = QVBoxLayout()
            il.addLayout(self.included)
            self.v.addWidget(ic)
            row = QHBoxLayout()
            self.export = button("Export PDF", "runButton", self.export_clicked)
            self.save_json = button("Save raw data (JSON)", slot=main.save_json)
            row.addWidget(self.export)
            row.addWidget(self.save_json)
            row.addStretch(1)
            self.v.addLayout(row)
            self.links = QHBoxLayout()
            self.v.addLayout(self.links)
            self.v.addStretch(1)

        def refresh(self):
            clear_layout(self.included)
            m = self.main
            for ok, text in ((m.info is not None, "System specs"),
                             (bool(m.checks), "Health checks"),
                             (m.stress_result is not None, "Stress test and temperature graph"),
                             (m.bench_result is not None, "Benchmarks")):
                self.included.addWidget(QLabel(f'<span style="color:{GREEN if ok else DIM}; font-weight:700;">'
                                               f'{"✓" if ok else "–"}</span>&nbsp;&nbsp;'
                                               f'<span style="color:{TEXT if ok else DIM};">{text}'
                                               f'{"" if ok else " (not run yet)"}</span>'))

        def export_clicked(self):
            name = self.main.default_report_name()
            path, _ = QFileDialog.getSaveFileName(self, "Save report", name, "PDF (*.pdf)")
            if path:
                if not path.lower().endswith(".pdf"):
                    path += ".pdf"
                self.main.export_report(path)

        def show_links(self, path):
            clear_layout(self.links)
            self.links.addWidget(label(f"Saved {os.path.basename(path)}", "help"))
            self.links.addWidget(button("Open", "linkButton",
                                        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path))))
            self.links.addWidget(button("Show in folder", "linkButton",
                                        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))))
            self.links.addStretch(1)

    # ------------------------------------------------------------ main window
    class Main(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(APP_NAME)
            geo = QApplication.primaryScreen().availableGeometry()
            self.resize(min(1480, int(geo.width() * 0.9)), min(960, int(geo.height() * 0.9)))
            self.setMinimumSize(1060, 680)
            self.info = None
            self.checks = []
            self.history = deque(maxlen=4000)        # (t, summary)
            self.last_summary = {}
            self.last_fans = []
            self.t_start = time.time()
            self.backend_name, self.sensor_note = "", ""
            self.fields = {"customer": "", "build": "", "tech": "Landull"}
            self.bound = {k: [] for k in self.fields}
            self.stress = None                       # running stress state
            self.stress_result = None
            self.stress_marks = []
            self.bench = None
            self.bench_result = None
            self.pipeline = None
            self.busy_spans = []                     # [start, end] while tests ran, excluded from "idle"
            self.collector = None
            self.info_waiters = []
            self.threads = []

            root = QWidget()
            root.setObjectName("root")
            h = QHBoxLayout(root)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(0)
            side = QFrame()
            side.setObjectName("sidebar")
            side.setFixedWidth(236)
            sl = QVBoxLayout(side)
            sl.setContentsMargins(0, 16, 0, 12)
            sl.setSpacing(10)
            brand = QHBoxLayout()
            brand.setContentsMargins(16, 0, 12, 4)
            brand.setSpacing(10)
            ic = QLabel()
            ic.setPixmap(skull(40, self.devicePixelRatioF()))
            ic.setCursor(Qt.PointingHandCursor)
            ic.mousePressEvent = lambda e: self.go("home")
            brand.addWidget(ic)
            bt = QVBoxLayout()
            bt.setSpacing(0)
            bt.addWidget(label("LANDULL", "brand"))
            bt.addWidget(label("BUILDCHECK", "brandSub"))
            brand.addLayout(bt)
            brand.addStretch(1)
            sl.addLayout(brand)
            sl.addSpacing(6)
            self.nav = QListWidget()
            self.nav.setObjectName("nav")
            self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.nav.setFocusPolicy(Qt.NoFocus)
            home = QListWidgetItem("⌂   Home")
            home.setData(Qt.UserRole, "home")
            self.nav.addItem(home)
            mono = QFont()
            mono.setFamilies(["Menlo", "Consolas", "DejaVu Sans Mono", "monospace"])
            mono.setPointSize(max(QFont().pointSize(), 12))
            for title, color, items in SECTIONS:
                hdr = QListWidgetItem(title)
                hdr.setFlags(Qt.NoItemFlags)
                hf = QFont()
                hf.setBold(True)
                hf.setPointSize(max(hf.pointSize() - 2, 9))
                hf.setLetterSpacing(QFont.AbsoluteSpacing, 1.5)
                hdr.setFont(hf)
                hdr.setForeground(QColor(color))
                hdr.setSizeHint(QSize(200, 34))
                self.nav.addItem(hdr)
                for key, name in items:
                    it = QListWidgetItem(key)
                    it.setData(Qt.UserRole, key)
                    it.setToolTip(name)
                    it.setFont(mono)
                    self.nav.addItem(it)
            self.nav.currentItemChanged.connect(self.nav_changed)
            sl.addWidget(self.nav, 1)
            self.side_status = label("", "footer", wrap=True)
            self.side_status.setContentsMargins(18, 0, 12, 0)
            sl.addWidget(self.side_status)
            ver = label(f"v{VERSION}", "footer")
            ver.setContentsMargins(18, 0, 0, 0)
            sl.addWidget(ver)
            h.addWidget(side)

            self.stack = QStackedWidget()
            self.pages = {"home": HomePage(self), "system": SystemPage(self), "sensors": SensorsPage(self),
                          "stress": StressPage(self), "bench": BenchPage(self), "health": HealthPage(self),
                          "report": ReportPage(self)}
            for p in self.pages.values():
                self.stack.addWidget(p)
            h.addWidget(self.stack, 1)
            self.setCentralWidget(root)
            self.fields_init()

            m = self.menuBar()
            fm = m.addMenu("File")
            a = QAction("Export report (PDF)...", self)
            a.setShortcut(QKeySequence("Ctrl+E"))
            a.triggered.connect(self.pages["report"].export_clicked)
            fm.addAction(a)
            a = QAction("Save raw data (JSON)...", self)
            a.triggered.connect(self.save_json)
            fm.addAction(a)
            fm.addSeparator()
            a = QAction("Quit", self)
            a.setShortcut(QKeySequence.Quit)
            a.triggered.connect(self.close)
            fm.addAction(a)
            hm = m.addMenu("Help")
            a = QAction("Open log file", self)
            a.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(log_path())))
            hm.addAction(a)
            a = QAction("Landull on GitHub", self)
            a.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(GITHUB)))
            hm.addAction(a)
            a = QAction(f"About {APP_NAME}", self)
            a.triggered.connect(self.about)
            hm.addAction(a)

            self.nav.setCurrentRow(0)
            if IS_WIN and not is_admin():
                self.pages["home"].banner.setText("Not running as administrator, so some sensors and checks will be "
                                                  "missing. Right-click the app and choose Run as administrator.")
                self.pages["home"].banner.show()
            self.sensors = SensorThread()
            self.sensors.snapshot.connect(self.on_snapshot)
            self.sensors.backend.connect(self.on_backend)
            self.sensors.start()
            self.tick = QTimer(self)
            self.tick.timeout.connect(self.on_tick)
            self.tick.start(1000)
            QTimer.singleShot(300, self.collect_info)

        # ---------------- fields shared between Home and Report
        def bind(self, key, edit):
            self.bound[key].append(edit)
            edit.textChanged.connect(lambda text, k=key, e=edit: self._field_changed(k, e, text))

        def _field_changed(self, key, src, text):
            self.fields[key] = text
            for e in self.bound[key]:
                if e is not src and e.text() != text:
                    e.blockSignals(True)
                    e.setText(text)
                    e.blockSignals(False)

        def fields_init(self):
            for k, v in self.fields.items():
                for e in self.bound[k]:
                    e.setText(v)

        # ---------------- navigation
        def go(self, key):
            for i in range(self.nav.count()):
                if self.nav.item(i).data(Qt.UserRole) == key:
                    self.nav.setCurrentRow(i)
                    return

        def nav_changed(self, cur, prev):
            if not cur:
                return
            key = cur.data(Qt.UserRole)
            if key is None:
                if prev:
                    self.nav.setCurrentItem(prev)
                return
            page = self.pages[key]
            self.stack.setCurrentWidget(page)
            if key == "report":
                page.refresh()

        def about(self):
            QMessageBox.about(self, APP_NAME, f"<b>{APP_NAME}</b> v{VERSION}<br><br>Post-build health check for Windows "
                                              f"PCs.<br>Sensors by LibreHardwareMonitor (MPL 2.0).<br><br>"
                                              f"<a href='{GITHUB}'>{GITHUB}</a>")

        # ---------------- system info
        def collect_info(self, after=None):
            if callable(after) and after is not False:
                self.info_waiters.append(after)
            if self.collector and self.collector.isRunning():
                return
            self.pages["system"].refresh.setEnabled(False)
            self.side_status.setText("Reading system info...")
            self.collector = FuncThread(bc_collect.collect)
            self.collector.done.connect(self.info_done)
            self.collector.start()

        def info_done(self, res):
            self.pages["system"].refresh.setEnabled(True)
            self.side_status.setText("")
            if isinstance(res, Exception):
                res = {"platform": "windows" if IS_WIN else "other", "errors": [str(res)], "cpu": {}, "raw": {}}
            self.info = res
            self.pages["system"].set_info(res)
            self.pages["home"].set_info(res)
            self.pages["bench"].load_drives(res)
            waiters, self.info_waiters = self.info_waiters, []
            for fn in waiters:
                fn(True)

        # ---------------- sensors
        def on_backend(self, name, note):
            self.backend_name, self.sensor_note = name, note
            self.pages["sensors"].set_backend(name, note)

        def on_snapshot(self, snap):
            sm = snap["summary"]
            self.last_summary = sm
            self.last_fans = sm.get("fans", [])
            self.history.append((snap.get("t", time.time()), sm))
            self.pages["home"].on_snapshot(sm)
            self.pages["sensors"].on_snapshot(snap)
            if self.stress:
                self.stress_sample(sm)
            self.pages["stress"].on_snapshot(sm, self.stress["stats"] if self.stress else
                                             (self.stress_result or {}).get("stats"))

        def temp_series(self):
            cpu = [(t - self.t_start, s.get("cpu_temp")) for t, s in self.history]
            gpu = [(t - self.t_start, s.get("gpu_temp")) for t, s in self.history]
            series = []
            if any(v is not None for _, v in cpu):
                series.append(("CPU", bc_report.CPU_COLOR, cpu))
            if any(v is not None for _, v in gpu):
                series.append(("GPU", bc_report.GPU_COLOR, gpu))
            return series, self.stress_marks

        def idle_summary(self, use_result=True):
            """Average of up to 30 s of readings taken while nothing was being stressed."""
            if use_result and self.stress_result and self.stress_result.get("idle", {}).get("cpu_temp") is not None:
                return self.stress_result["idle"]
            busy = [(a, b) for a, b in self.busy_spans]
            pts = [s for t, s in list(self.history)[-60:] if not any(a <= t <= (b or 1e18) for a, b in busy)][-30:]
            out = {}
            for k in ("cpu_temp", "gpu_temp", "gpu_hotspot", "cpu_power", "gpu_power", "cpu_clock"):
                vals = [s.get(k) for s in pts if s.get(k) is not None]
                out[k] = sum(vals) / len(vals) if vals else None
            return out

        def install_pawnio(self):
            r = QMessageBox.question(self, "Install PawnIO",
                                     "PawnIO is a small signed driver that LibreHardwareMonitor uses to read CPU and "
                                     "motherboard sensors (it replaces the old WinRing0 driver).\n\n"
                                     "Install it now with winget (package namazso.PawnIO)?")
            if r != QMessageBox.Yes:
                return
            self.pages["sensors"].pawn_btn.setEnabled(False)
            self.pages["sensors"].banner_text.setText("Installing PawnIO with winget...")
            p = QProcess(self)
            self._pawn = p

            def done(code, _status):
                if code == 0:
                    self.pages["sensors"].banner_text.setText("PawnIO installed. Reloading sensors...")
                    self.restart_sensors()
                else:
                    out = bytes(p.readAll()).decode("utf-8", "replace")[-300:]
                    self.pages["sensors"].banner_text.setText(
                        f"winget couldn't install PawnIO (code {code}). Install it from pawnio.eu, then restart "
                        f"BuildCheck. {out}")
                    self.pages["sensors"].pawn_btn.setEnabled(True)
            p.finished.connect(done)
            p.setProcessChannelMode(QProcess.MergedChannels)
            p.start("winget", ["install", "-e", "--id", "namazso.PawnIO", "--silent",
                               "--accept-package-agreements", "--accept-source-agreements"])

        def restart_sensors(self):
            self.sensors.stop()
            self.sensors.wait(3000)
            self.sensors = SensorThread()
            self.sensors.snapshot.connect(self.on_snapshot)
            self.sensors.backend.connect(self.on_backend)
            self.sensors.start()

        # ---------------- stress test
        def stress_start(self, duration, cpu, gpu, on_done=None):
            if self.stress:
                return
            if not cpu and not gpu:
                QMessageBox.information(self, APP_NAME, "Pick CPU, GPU, or both.")
                return
            page = self.pages["stress"]
            idle = self.idle_summary(use_result=False)
            t = time.time()
            self.busy_spans.append([t, None])
            self.stress = {"t0": t, "dur": duration, "cpu": cpu, "gpu": gpu, "workers": [], "errors": 0,
                           "crashed": 0, "aborted": "", "idle": idle, "on_done": on_done, "gpu_win": None,
                           "fps": None, "gpu_fail": "", "hot": 0,
                           "series": {k: [] for k in ("cpu_temp", "gpu_temp", "gpu_hotspot", "cpu_power", "gpu_power",
                                                      "cpu_clock", "tjmax_dist", "cpu_load", "gpu_load")},
                           "stats": {}}
            self.stress_marks = [m for m in self.stress_marks if m[0] > (t - self.t_start) - 3600]
            self.stress_marks.append((t - self.t_start, "start"))
            page.log.clear()
            page.running(True, duration)
            page.result.setText("Running...")
            page.fix.setText("")
            self.slog(f"Stress test started: {fmt_duration(duration)}, "
                      f"{'CPU ' if cpu else ''}{'+ ' if cpu and gpu else ''}{'GPU' if gpu else ''}")
            if idle.get("cpu_temp") is not None:
                self.slog(f"Idle before start: CPU {idle['cpu_temp']:.0f} °C" +
                          (f", GPU {idle['gpu_temp']:.0f} °C" if idle.get("gpu_temp") is not None else ""))
            if DEMO:
                bc_sensors.DemoBackend.stress_cpu = cpu
                bc_sensors.DemoBackend.stress_gpu = gpu
            if cpu:
                n = os.cpu_count() or 4
                for _ in range(n):
                    w = Worker(["burn", str(duration + 5)], self)
                    w.line.connect(self.stress_line)
                    w.finished.connect(self.stress_worker_done)
                    self.stress["workers"].append(w)
                self.slog(f"Started {n} CPU workers")
            if gpu:
                if has_gl:
                    gw = GpuStress()
                    gw.failed.connect(self.gpu_failed)
                    gw.fps.connect(lambda f: self.stress and self.stress.update(fps=f))
                    gw.closed.connect(self.gpu_closed)
                    gw.show()
                    self.stress["gpu_win"] = gw
                    self.raise_()
                    self.slog("Opened the GPU render window")
                else:
                    self.gpu_failed("Qt's OpenGL module isn't available in this build.")

        def slog(self, text):
            self.pages["stress"].log.appendPlainText(time.strftime("%H:%M:%S  ") + text)

        def stress_line(self, ln):
            if not self.stress:
                return
            if ln.startswith("ERROR"):
                self.stress["errors"] += 1
                self.slog("CPU worker: " + ln)
            elif ln.startswith("FAILED"):
                self.slog("CPU worker: " + ln)

        def stress_worker_done(self, w, result):
            if not self.stress:
                return
            if result is None and not self.stress.get("stopping"):
                self.stress["crashed"] += 1
                self.slog(f"A CPU worker stopped unexpectedly (exit code {w.p.exitCode()})")

        def gpu_failed(self, msg):
            if self.stress:
                self.stress["gpu_fail"] = msg
                self.slog("GPU test unavailable: " + msg)
                gw = self.stress.get("gpu_win")
                if gw:
                    gw.close()

        def gpu_closed(self):
            if self.stress and self.stress.get("gpu_win") and not self.stress.get("stopping"):
                self.slog("GPU window closed - GPU load stopped")
                self.stress["gpu_win"] = None

        def stress_sample(self, sm):
            st = self.stress
            el = time.time() - st["t0"]
            for k in st["series"]:
                v = sm.get(k)
                if v is not None and el > 3:
                    st["series"][k].append((el, v))
            for k, pts in st["series"].items():
                if pts:
                    vals = [v for _, v in pts]
                    st["stats"][k] = {"max": max(vals), "min": min(vals), "avg": sum(vals) / len(vals)}
            ct, gt, hs = sm.get("cpu_temp"), sm.get("gpu_temp"), sm.get("gpu_hotspot")
            if (ct is not None and ct >= 100 and not sm.get("cpu_temp_approx")) or (gt is not None and gt >= 95) \
                    or (hs is not None and hs >= 110):
                st["hot"] += 1
                if st["hot"] >= 5:
                    st["aborted"] = (f"Stopped for safety: CPU {fmt(ct, ' °C')}, GPU {fmt(gt, ' °C')}"
                                     f"{', hot spot ' + fmt(hs, ' °C') if hs else ''}.")
                    self.slog(st["aborted"])
                    self.stress_finish()
            else:
                st["hot"] = 0

        def on_tick(self):
            if self.stress:
                st = self.stress
                el = time.time() - st["t0"]
                page = self.pages["stress"]
                page.prog.setValue(int(min(1.0, el / st["dur"]) * 1000))
                parts = [f"{fmt_duration(el)} / {fmt_duration(st['dur'])}"]
                if st["cpu"]:
                    alive = sum(1 for w in st["workers"] if w.p.state() != QProcess.NotRunning)
                    parts.append(f"{alive} CPU workers")
                if st.get("gpu_win") and st.get("fps"):
                    parts.append(f"GPU window {st['fps']:.0f} fps")
                if st["errors"]:
                    parts.append(f"{st['errors']} calculation error(s)")
                page.status.setText("  ·  ".join(parts))
                if self.pipeline:
                    self.pages["home"].status.setText("Stress test: " + parts[0])
                if el >= st["dur"]:
                    self.stress_finish()
            if self.bench and self.bench.get("winsat_t0"):
                el = time.time() - self.bench["winsat_t0"]
                self.pages["bench"].status.setText(f"WinSAT running... {fmt_duration(el)} (usually 1-3 minutes)")

        def stress_stop(self):
            if self.stress:
                self.stress["aborted"] = self.stress["aborted"] or "Stopped early."
                self.slog("Stopped by user")
                self.stress_finish(user=True)

        def stress_finish(self, user=False):
            st = self.stress
            if not st or st.get("stopping"):
                return
            st["stopping"] = True
            for w in st["workers"]:
                w.kill()
            if st.get("gpu_win"):
                st["gpu_win"].close()
            if DEMO:
                bc_sensors.DemoBackend.stress_cpu = bc_sensors.DemoBackend.stress_gpu = False
            el = time.time() - st["t0"]
            self.busy_spans[-1][1] = time.time() + 20
            self.stress_marks.append((time.time() - self.t_start, "stop"))
            res = self.stress_verdict(st, el, user)
            self.stress_result = res
            self.stress = None
            page = self.pages["stress"]
            page.running(False)
            page.prog.setValue(1000 if not st["aborted"] else page.prog.value())
            page.status.setText(f"Finished after {fmt_duration(el)}.")
            page.show_result(res)
            self.slog(f"Result: {res['status']} - {res['summary']}")
            if st.get("on_done"):
                st["on_done"](not user)

        def stress_verdict(self, st, el, user):
            s = st["stats"]
            cpu_t, gpu_t, hs = (s.get(k, {}).get("max") for k in ("cpu_temp", "gpu_temp", "gpu_hotspot"))
            dist = s.get("tjmax_dist", {}).get("min")
            status, fix, notes = "PASS", "", []
            parts = [f"{fmt_duration(el)} of {'CPU' if st['cpu'] else ''}{' + ' if st['cpu'] and st['gpu'] else ''}"
                     f"{'GPU' if st['gpu'] else ''} load"]
            if cpu_t is not None:
                parts.append(f"CPU max {cpu_t:.0f} °C (avg {s['cpu_temp']['avg']:.0f})")
            if gpu_t is not None:
                parts.append(f"GPU max {gpu_t:.0f} °C" + (f", hot spot {hs:.0f} °C" if hs is not None else ""))
            clocks = st["series"].get("cpu_clock", [])
            drop = None
            if st["cpu"] and len(clocks) > 30:
                third = len(clocks) // 3
                a = sum(v for _, v in clocks[:third]) / third
                b = sum(v for _, v in clocks[-third:]) / third
                if a > 0:
                    drop = (a - b) / a * 100
            if st["errors"]:
                status = "FAIL"
                notes.append(f"{st['errors']} calculation error(s)")
                fix = ("The CPU or memory returned wrong results under load, so the system isn't stable. Load BIOS "
                       "defaults (turn off XMP/EXPO and any overclock or undervolt) and test again.")
            elif st["crashed"]:
                status = "FAIL"
                notes.append(f"{st['crashed']} worker(s) crashed")
                fix = "Workers crashing under load points to instability. Check memory and CPU settings in the BIOS."
            elif st["aborted"] and not user:
                status = "FAIL"
                notes.append(st["aborted"])
                fix = "Check that the cooler is mounted firmly with paste, the pump/fans are powered, and case airflow."
            else:
                if (dist is not None and dist <= 1) or (cpu_t is not None and cpu_t >= 99):
                    status = "WARN"
                    notes.append("CPU hit its thermal limit (throttling)")
                    fix = "Re-seat the cooler, check paste coverage and fan curves, or lower power limits."
                elif cpu_t is not None and cpu_t >= 90:
                    status = "WARN"
                    notes.append("CPU running hot")
                    fix = ("Ryzen 7000/9000 chips run up to 95 °C by design. On other CPUs check the cooler mount "
                           "and fan curve.")
                if gpu_t is not None and (gpu_t >= 87 or (hs is not None and hs >= 105)):
                    status = "WARN"
                    notes.append("GPU running hot")
                    fix = (fix + " " if fix else "") + "Improve case airflow or check the GPU fan curve."
                if drop is not None and drop >= 15:
                    status = "WARN" if status == "PASS" else status
                    notes.append(f"CPU clocks dropped {drop:.0f}% during the test")
                    fix = fix or "Clocks falling over time usually means heat soak or a power limit."
                if st["gpu"] and st["gpu_fail"]:
                    notes.append("GPU load didn't run")
                if user:
                    notes.append("stopped early")
            if not notes:
                notes.append("no errors, no throttling")
            summary = ", ".join(parts) + ". " + sentence("; ".join(notes)) + "."
            return {"status": status, "summary": summary, "fix": fix, "stats": s, "idle": st["idle"],
                    "duration": el, "errors": st["errors"], "cpu": st["cpu"], "gpu": st["gpu"],
                    "series": {"cpu_temp": st["series"]["cpu_temp"], "gpu_temp": st["series"]["gpu_temp"]}}

        # ---------------- benchmarks
        def bench_start(self, sel, on_done=None):
            if self.bench:
                return
            page = self.pages["bench"]
            steps = []
            if sel["cpu1"]:
                steps.append("cpu1")
            if sel["cpun"]:
                steps.append("cpun")
            if sel["mem"]:
                steps.append("mem")
            if sel["disk"] and sel.get("drive"):
                steps.append("disk")
            if sel["winsat"]:
                steps.append("winsat")
            if not steps:
                return
            self.bench = {"sel": sel, "steps": steps, "i": -1, "res": {}, "workers": [], "on_done": on_done,
                          "stopped": False}
            t = time.time()
            self.busy_spans.append([t, None])
            page.running(True)
            page.prog.setValue(0)
            self.bench_next()

        def bench_next(self):
            b = self.bench
            page = self.pages["bench"]
            b["i"] += 1
            page.show_rows(self.bench_rows(b["res"]))
            if b["stopped"] or b["i"] >= len(b["steps"]):
                return self.bench_finish()
            page.prog.setValue(int(b["i"] / len(b["steps"]) * 100))
            step = b["steps"][b["i"]]
            b["workers"] = []
            label_ = {"cpu1": "CPU single-core (8 s)", "cpun": f"CPU multi-core, {os.cpu_count()} threads (8 s)",
                      "mem": "Memory bandwidth", "disk": f"Drive speed on {b['sel']['drive']}",
                      "winsat": "Windows Experience scores (WinSAT)"}[step]
            page.status.setText(f"Running {label_}...")
            if self.pipeline:
                self.pages["home"].status.setText(f"Benchmark: {label_}")
            if step == "winsat":
                p = QProcess(self)
                b["winsat"] = p
                b["winsat_t0"] = time.time()

                def done(*_a):
                    b["winsat_t0"] = None
                    th = FuncThread(bc_collect.winsat_scores)
                    th.done.connect(lambda r: (b["res"].__setitem__("winsat", r if isinstance(r, dict) else {}),
                                               self.bench_next()))
                    self.threads.append(th)
                    th.start()
                p.finished.connect(done)
                p.errorOccurred.connect(lambda e: e == QProcess.FailedToStart and done())
                p.start("winsat", ["formal", "-restart", "clean"])
                return
            n = (os.cpu_count() or 4) if step == "cpun" else 1
            args = {"cpu1": ["cpu", "8"], "cpun": ["cpu", "8"], "mem": ["mem"],
                    "disk": ["disk", b["sel"]["drive"], str(b["sel"]["size"])]}[step]
            pending = {"n": n, "results": []}

            def one_done(w, result, step=step):
                pending["n"] -= 1
                if result:
                    pending["results"].append(result)
                if pending["n"] == 0:
                    r = pending["results"]
                    if step in ("cpu1", "cpun") and r:
                        b["res"][step] = {"mbps": sum(x["mbps"] for x in r), "n": len(r)}
                    elif r:
                        b["res"][step] = r[0]
                    QTimer.singleShot(200, self.bench_next)
            for _ in range(n):
                w = Worker(args, self)
                w.finished.connect(one_done)
                w.line.connect(lambda ln: ln.startswith("FAILED") and page.status.setText(ln))
                b["workers"].append(w)

        def bench_rows(self, r):
            rows = []
            if "cpu1" in r:
                rows.append(("CPU single-core", f"{r['cpu1']['mbps'] * 10:,.0f} pts",
                             f"{r['cpu1']['mbps']:.0f} MB/s compression + hashing on one thread"))
            if "cpun" in r:
                sc = (r["cpun"]["mbps"] / r["cpu1"]["mbps"]) if "cpu1" in r and r["cpu1"]["mbps"] else None
                rows.append(("CPU multi-core", f"{r['cpun']['mbps'] * 10:,.0f} pts",
                             f"{r['cpun']['n']} threads" + (f"  ·  {sc:.1f}x single-core" if sc else "")))
            if "mem" in r:
                rows.append(("Memory copy", f"{r['mem']['gbps']:.1f} GB/s", "256 MB block copy"))
            if "disk" in r:
                d = r["disk"]
                where = f"{d.get('folder')}  ·  {d.get('size_mb', 0) // 1024} GB file"
                rows.append(("Drive sequential write", f"{d.get('write_mbps', 0):,.0f} MB/s", where))
                rows.append(("Drive sequential read", f"{d.get('read_mbps', 0):,.0f} MB/s",
                             where + ("  ·  includes OS cache" if d.get("cached") else "")))
                if d.get("rand4k_iops"):
                    rows.append(("Drive 4K random read", f"{d['rand4k_iops']:,.0f} IOPS",
                                 f"{d['rand4k_mbps']:.0f} MB/s  ·  queue depth 1"))
            if "winsat" in r:
                w = r["winsat"] or {}
                if w.get("base"):
                    rows.append(("WinSAT base score", f"{w['base']}",
                                 f"CPU {w.get('cpu')}  ·  Memory {w.get('mem')}  ·  Graphics {w.get('gfx')}  ·  "
                                 f"Gaming {w.get('d3d')}  ·  Disk {w.get('disk')}  (scale 1-9.9)"))
                else:
                    rows.append(("WinSAT", "-", "Didn't produce scores (needs administrator)"))
            return rows

        def bench_stop(self):
            if self.bench:
                self.bench["stopped"] = True
                for w in self.bench["workers"]:
                    w.kill()
                p = self.bench.get("winsat")
                if p and p.state() != QProcess.NotRunning:
                    p.kill()
                self.bench_finish()

        def bench_finish(self):
            b = self.bench
            if not b or b.get("finished"):
                return
            b["finished"] = True
            page = self.pages["bench"]
            rows = self.bench_rows(b["res"])
            page.show_rows(rows)
            page.running(False)
            page.prog.setValue(100 if not b["stopped"] else page.prog.value())
            page.status.setText("Stopped." if b["stopped"] else "Done.")
            if self.busy_spans:
                self.busy_spans[-1][1] = time.time() + 10
            if rows:
                r = b["res"]
                bits = []
                if "cpu1" in r or "cpun" in r:
                    bits.append("CPU " + " / ".join(f"{r[k]['mbps'] * 10:,.0f}" for k in ("cpu1", "cpun") if k in r) + " pts")
                if "mem" in r:
                    bits.append(f"memory {r['mem']['gbps']:.1f} GB/s")
                if "disk" in r:
                    bits.append(f"drive {r['disk'].get('read_mbps', 0):,.0f} read / {r['disk'].get('write_mbps', 0):,.0f} "
                                "write MB/s")
                if (r.get("winsat") or {}).get("base"):
                    bits.append(f"WinSAT {r['winsat']['base']}")
                self.bench_result = {"rows": rows, "raw": r, "summary": sentence(", ".join(bits)) + "."}
            self.bench = None
            if b.get("on_done"):
                b["on_done"](not b["stopped"])

        # ---------------- health checks
        def check_context(self):
            return {"idle": self.idle_summary(), "fans": self.last_fans, "stress": self.stress_result,
                    "bench": self.bench_result, "sensor_note": self.sensor_note or "No temperature sensor found."}

        def run_checks(self, after=None):
            if self.info is None:
                self.pages["health"].run.setEnabled(False)
                return self.collect_info(after=lambda ok: (self.pages["health"].run.setEnabled(True),
                                                           self.run_checks(after)))
            self.checks = bc_collect.build_checks(self.info, self.check_context())
            self.pages["health"].show_checks(self.checks)
            if after:
                after(True)

        # ---------------- report
        def default_report_name(self):
            who = self.fields.get("customer") or (self.info or {}).get("computer", {}).get("name") or "PC"
            safe = "".join(c for c in who if c.isalnum() or c in " -_").strip() or "PC"
            folder = os.path.join(os.path.expanduser("~"), "Documents", "Landull BuildCheck")
            return os.path.join(folder, f"BuildCheck - {safe} - {time.strftime('%Y-%m-%d %H%M')}.pdf")

        def report_data(self):
            self.checks = bc_collect.build_checks(self.info or {}, self.check_context())
            self.pages["health"].show_checks(self.checks)
            return {"customer": self.fields.get("customer"), "build": self.fields.get("build"),
                    "tech": self.fields.get("tech"), "notes": self.pages["report"].notes.toPlainText().strip(),
                    "info": self.info or {}, "checks": self.checks, "stress": self.stress_result,
                    "bench": self.bench_result, "idle": self.idle_summary()}

        def export_report(self, path):
            if self.info is None:
                QMessageBox.information(self, APP_NAME, "Still reading system info - try again in a few seconds.")
                return None
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                graph = None
                if self.stress_result:
                    sr = self.stress_result["series"]
                    series = [(n, c, sr[k]) for n, c, k in (("CPU", bc_report.CPU_COLOR, "cpu_temp"),
                                                            ("GPU", bc_report.GPU_COLOR, "gpu_temp")) if sr.get(k)]
                    graph = (series, [])
                bc_report.export_pdf(path, self.report_data(), graph)
            except Exception as e:                   # noqa: BLE001
                log("export: " + traceback.format_exc())
                QMessageBox.critical(self, APP_NAME, f"Couldn't save the report:\n\n{e}")
                return None
            self.pages["report"].show_links(path)
            self.pages["report"].refresh()
            log(f"report saved {path}")
            return path

        def save_json(self):
            if self.info is None:
                return
            name = self.default_report_name()[:-4] + ".json"
            path, _ = QFileDialog.getSaveFileName(self, "Save raw data", name, "JSON (*.json)")
            if not path:
                return
            d = self.report_data()
            d["sensors_last"] = self.last_summary
            d["app"] = f"{APP_NAME} {VERSION}"
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(d, fh, indent=2, default=str)
            except OSError as e:
                QMessageBox.critical(self, APP_NAME, f"Couldn't save:\n\n{e}")

        # ---------------- full check pipeline
        def start_full_check(self):
            if self.pipeline or self.stress or self.bench:
                return
            home = self.pages["home"]
            steps = [("Read system info", self.p_info), ("Measure idle temperatures", self.p_idle)]
            if home.o_bench.isChecked() or home.o_winsat.isChecked():
                steps.append(("Benchmarks", self.p_bench))
            if home.duration.currentData() and (home.o_cpu.isChecked() or home.o_gpu.isChecked()):
                steps.append((f"Stress test ({home.duration.currentText().split(' (')[0]})", self.p_stress))
            steps += [("Health checks", self.p_checks), ("Save PDF report", self.p_report)]
            self.pipeline = {"steps": steps, "i": -1, "states": ["pending"] * len(steps), "stopped": False}
            clear_layout(home.result_links)
            home.run.setEnabled(False)
            home.stop.setEnabled(True)
            self.p_next(True)

        def p_next(self, ok=True):
            pl = self.pipeline
            if not pl:
                return
            if pl["i"] >= 0:
                pl["states"][pl["i"]] = "done" if ok else "failed"
            pl["i"] += 1
            home = self.pages["home"]
            if pl["stopped"] or pl["i"] >= len(pl["steps"]):
                for j in range(pl["i"], len(pl["steps"])):
                    pl["states"][j] = "skipped"
                home.set_steps([s[0] for s in pl["steps"]], None, pl["states"])
                home.run.setEnabled(True)
                home.stop.setEnabled(False)
                if pl["stopped"]:
                    home.status.setText("Stopped.")
                self.pipeline = None
                return
            pl["states"][pl["i"]] = "running"
            home.set_steps([s[0] for s in pl["steps"]], pl["i"], pl["states"])
            home.status.setText(pl["steps"][pl["i"]][0] + "...")
            QTimer.singleShot(50, lambda: pl["steps"][pl["i"]][1]())

        def stop_full_check(self):
            if self.pipeline:
                self.pipeline["stopped"] = True
                if self.stress:
                    self.stress_stop()
                elif self.bench:
                    self.bench_stop()
                else:
                    self.p_next(False)

        def p_info(self):
            self.collect_info(after=self.p_next)

        def p_idle(self):
            start = time.time()

            def wait():
                if not self.pipeline:
                    return
                n = sum(1 for t, _ in self.history if t >= start - 30)
                if n >= 15 or time.time() - start > 25:
                    self.p_next(True)
                else:
                    self.pages["home"].status.setText(f"Measuring idle temperatures... {int(time.time() - start)} s")
                    QTimer.singleShot(1000, wait)
            wait()

        def p_bench(self):
            home = self.pages["home"]
            sel = self.pages["bench"].selection()
            if not home.o_bench.isChecked():
                sel.update(cpu1=False, cpun=False, mem=False, disk=False)
            sel["winsat"] = home.o_winsat.isChecked() and IS_WIN
            self.bench_start(sel, on_done=lambda ok: self.p_next(ok))
            if not self.bench:
                self.p_next(True)

        def p_stress(self):
            home = self.pages["home"]
            self.stress_start(home.duration.currentData(), home.o_cpu.isChecked(), home.o_gpu.isChecked(),
                              on_done=lambda ok: self.p_next(ok))

        def p_checks(self):
            self.run_checks(after=self.p_next)

        def p_report(self):
            path = self.export_report(self.default_report_name())
            home = self.pages["home"]
            if path:
                v, vtext = bc_collect.verdict(self.checks)
                home.status.setText(f'<span style="color:{STATUS_COLOR[v]}; font-weight:700;">{vtext}.</span> '
                                    f'Report saved to {path}')
                clear_layout(home.result_links)
                home.result_links.addWidget(button("Open report", "linkButton",
                                                   lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path))))
                home.result_links.addWidget(button("Show in folder", "linkButton",
                                                   lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))))
                home.result_links.addWidget(button("See health checks", "linkButton", lambda: self.go("health")))
                home.result_links.addStretch(1)
            self.p_next(bool(path))

        # ---------------- shutdown
        def closeEvent(self, e):
            busy = self.stress or self.bench
            if busy:
                r = QMessageBox.question(self, APP_NAME, "A test is still running. Stop it and quit?")
                if r != QMessageBox.Yes:
                    e.ignore()
                    return
                if self.stress:
                    self.stress_finish(user=True)
                if self.bench:
                    self.bench_stop()
            self.sensors.stop()
            self.sensors.wait(3000)
            e.accept()

    # ------------------------------------------------------------ start
    fmt_ = QSurfaceFormat()
    fmt_.setSwapInterval(0)                          # no vsync for the GPU load window
    QSurfaceFormat.setDefaultFormat(fmt_)
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName("Landull")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(asset("icon.png")))
    app.setStyleSheet(qss())

    def excepthook(t, v, tb):
        msg = "".join(traceback.format_exception(t, v, tb))
        log("ERROR " + msg)
        sys.__stderr__ and sys.__stderr__.write(msg)
        try:
            QMessageBox.critical(None, APP_NAME, f"Something went wrong:\n\n{v}\n\nDetails were written to:\n{log_path()}")
        except Exception:
            pass
    sys.excepthook = excepthook

    win = Main()
    win.show()
    shot = os.environ.get("BUILDCHECK_SCREENSHOT")     # automated visual checks: folder to save page images to
    if shot:
        os.makedirs(shot, exist_ok=True)
        order = ["home", "system", "sensors", "stress", "bench", "health", "report"]

        def snap(i=0):
            if i == 0 and os.environ.get("BUILDCHECK_SCREENSHOT_STRESS"):
                win.stress_start(60, True, False)
            if i < len(order):
                win.go(order[i])
                if order[i] == "health":
                    win.run_checks()
                QTimer.singleShot(900, lambda: (win.grab().save(os.path.join(shot, f"{i}_{order[i]}.png")), snap(i + 1)))
            else:
                if win.stress:
                    win.stress_stop()
                win.export_report(os.path.join(shot, "report.pdf"))
                app.quit()
        QTimer.singleShot(int(os.environ.get("BUILDCHECK_SCREENSHOT_DELAY", "9000")), snap)
    log(f"start v{VERSION} frozen={getattr(sys, 'frozen', False)} admin={is_admin()} demo={DEMO}")
    return app.exec()


def selftest(args):
    """
    --selftest <out.json>: read the inventory and two rounds of sensors, then write them as JSON.
    Used by the CI build to prove the packaged exe can load LibreHardwareMonitor, and handy for debugging.
    """
    import bc_collect
    import bc_sensors
    out = {"app": f"{APP_NAME} {VERSION}", "admin": is_admin()}
    b, note = bc_sensors.open_backend()
    out["sensor_backend"], out["sensor_note"] = b.name, note
    try:
        b.read()
        time.sleep(1)
        snap = b.read()
        out["sensor_groups"] = [{"hw": g["hw"], "name": g["name"], "sensors": len(g["sensors"])} for g in snap["groups"]]
        out["summary"] = snap["summary"]
    finally:
        b.close()
    if "--no-inventory" not in args:
        info = bc_collect.collect()
        info.pop("raw", None)
        out["inventory"] = info
        out["checks"] = bc_collect.build_checks(info, {})
    path = next((a for a in args if not a.startswith("--")), "buildcheck-selftest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(f"sensor backend: {out['sensor_backend']}  groups: {len(out.get('sensor_groups', []))}  -> {path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        _fix_stdio()
        import bc_workers
        sys.exit(bc_workers.main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        _fix_stdio()
        sys.exit(selftest(sys.argv[2:]))
    sys.exit(gui_main())
