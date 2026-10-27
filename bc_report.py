"""Temperature graph drawing (shared by the app and the report) and the PDF report."""

import html
import time

from bc_common import APP_NAME, VERSION, asset, fmt_duration
from bc_collect import spec_rows, verdict

CPU_COLOR = "#e0b25a"
GPU_COLOR = "#6fa8dc"
STATUS_LIGHT = {"PASS": "#2e7d32", "WARN": "#a86400", "FAIL": "#c62828", "INFO": "#5f5f5f", "SKIP": "#8a8a8a"}


def draw_graph(p, rect, series, light=False, window=None, ymin=None, ymax=None, marks=None, unit="°C"):
    """
    Line graph with QPainter.
    series: [(label, hexcolor, [(t_seconds, value_or_None), ...])]
    window: seconds shown (latest data at the right edge); None fits all data.
    marks:  [(t_seconds, label)] vertical markers, e.g. stress start/stop.
    """
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QPainterPath, QPen

    fg = QColor("#555555" if light else "#6b6862")
    grid = QColor("#e3e3e3" if light else "#242424")
    txt = QColor("#333333" if light else "#9a968f")
    p.save()
    p.setRenderHint(p.RenderHint.Antialiasing, True)
    r = QRectF(rect)
    plot = QRectF(r.left() + 38, r.top() + 22, r.width() - 50, r.height() - 44)

    pts_all = [(t, v) for _, _, pts in series for t, v in pts if v is not None]
    if pts_all:
        t_max = max(t for t, _ in pts_all)
        t_min = (t_max - window) if window else min(t for t, _ in pts_all)
        vals = [v for t, v in pts_all if t >= t_min]
    else:
        t_max, t_min, vals = (window or 60), 0, []
    if t_max - t_min < 1:
        t_min = t_max - (window or 60)
    lo = ymin if ymin is not None else (min(vals) if vals else 20)
    hi = ymax if ymax is not None else (max(vals) if vals else 100)
    lo = min(lo, 20 if unit == "°C" else 0)
    lo = (int(lo) // 10) * 10
    hi = max(hi + 5, lo + 30)
    hi = ((int(hi) + 9) // 10) * 10

    def X(t):
        return plot.left() + (t - t_min) / (t_max - t_min) * plot.width()

    def Y(v):
        return plot.bottom() - (v - lo) / (hi - lo) * plot.height()

    f = QFont()
    f.setPointSizeF(7.5)
    p.setFont(f)
    step = 10 if hi - lo <= 60 else 20
    v = lo
    while v <= hi:
        p.setPen(QPen(grid, 1))
        p.drawLine(QPointF(plot.left(), Y(v)), QPointF(plot.right(), Y(v)))
        p.setPen(txt)
        p.drawText(QRectF(r.left(), Y(v) - 7, 34, 14), Qt.AlignRight | Qt.AlignVCenter, f"{v}")
        v += step
    p.setPen(txt)
    p.drawText(QRectF(r.left(), r.top() + 2, 60, 14), Qt.AlignLeft | Qt.AlignVCenter, unit)
    span = t_max - t_min
    p.drawText(QRectF(plot.left(), plot.bottom() + 4, 120, 14), Qt.AlignLeft | Qt.AlignVCenter,
               f"-{fmt_duration(span)}" if window else "0:00")
    p.drawText(QRectF(plot.right() - 120, plot.bottom() + 4, 120, 14), Qt.AlignRight | Qt.AlignVCenter,
               "now" if window else fmt_duration(span))
    for t, label in (marks or []):
        if t_min <= t <= t_max:
            pen = QPen(fg, 1, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(X(t), plot.top()), QPointF(X(t), plot.bottom()))
            p.setPen(txt)
            p.drawText(QRectF(X(t) + 3, plot.top(), 120, 12), Qt.AlignLeft | Qt.AlignTop, label)
    p.setClipRect(plot.adjusted(-1, -3, 1, 3))
    for label, color, pts in series:
        path = QPainterPath()
        started = False
        for t, val in pts:
            if t < t_min or val is None:
                started = False
                continue
            pt = QPointF(X(t), Y(val))
            if started:
                path.lineTo(pt)
            else:
                path.moveTo(pt)
                started = True
        p.setPen(QPen(QColor(color), 2))
        p.drawPath(path)
    p.setClipping(False)
    x = plot.right()
    for label, color, pts in reversed(series):
        last = next((v for _, v in reversed(pts) if v is not None), None)
        text = f"{label}  {last:.0f}{unit}" if last is not None and window else label
        w = p.fontMetrics().horizontalAdvance(text) + 18
        x -= w
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(x, r.top() + 6, 9, 9), 2, 2)
        p.setPen(txt)
        p.drawText(QRectF(x + 13, r.top() + 2, w, 16), Qt.AlignLeft | Qt.AlignVCenter, text)
    p.restore()


def graph_image(series, w=1400, h=420, marks=None):
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.white)
    p = QPainter(img)
    f = p.font()
    f.setPointSizeF(15)
    p.setFont(f)
    p.scale(2, 2)
    draw_graph(p, QRectF(0, 0, w / 2, h / 2), series, light=True, marks=marks)
    p.end()
    return img


# ------------------------------------------------------------------ report
def _e(x):
    return html.escape(str(x if x is not None else ""))


def _num(v, fmt="{:.0f}", unit=""):
    return "-" if v is None else fmt.format(v) + unit


def build_html(d):
    """d: customer, build, tech, notes, info, checks, stress, bench, idle, has_graph"""
    info = d["info"]
    checks = d["checks"]
    v, vtext = verdict(checks)
    vcol = STATUS_LIGHT[v]
    date = time.strftime("%B %d, %Y")
    h = []
    h.append(f"""
<table width="100%" cellspacing="0" cellpadding="10" bgcolor="#141414">
<tr><td width="70"><img src="bc://skull" height="64"></td>
<td><span style="color:#e6e3dd; font-size:20pt; font-weight:800; letter-spacing:3px;">BUILD HEALTH REPORT</span><br>
<span style="color:#9a968f; font-size:9pt; letter-spacing:2px;">LANDULL BUILDCHECK</span></td>
<td align="right"><span style="color:#9a968f; font-size:9pt;">{_e(date)}</span></td></tr></table>
<br>""")
    rows = [("Customer", d.get("customer")), ("Build", d.get("build")), ("Checked by", d.get("tech")),
            ("Computer", info.get("computer", {}).get("name"))]
    rows = [(k, val) for k, val in rows if val]
    h.append('<table width="100%" cellspacing="0" cellpadding="4"><tr><td width="55%" valign="top">')
    h.append('<table cellspacing="0" cellpadding="3">')
    for k, val in rows:
        h.append(f'<tr><td><span style="color:#777777;">{_e(k)}</span></td><td><b>{_e(val)}</b></td></tr>')
    h.append('</table></td><td valign="top" align="right">')
    h.append(f'<table cellspacing="0" cellpadding="10" style="border: 2px solid {vcol};"><tr><td align="center">'
             f'<span style="color:{vcol}; font-size:15pt; font-weight:800;">{_e({"PASS": "PASSED", "WARN": "PASSED", "FAIL": "NEEDS WORK"}[v])}</span><br>'
             f'<span style="color:#444444;">{_e(vtext)}</span></td></tr></table>')
    h.append("</td></tr></table>")

    def section(title):
        h.append(f'<p style="margin-top:16px; margin-bottom:4px;"><span style="font-size:12pt; font-weight:700; '
                 f'letter-spacing:1px;">{_e(title.upper())}</span></p>'
                 '<hr style="border:0; height:1px; background:#cccccc;">')

    section("System")
    h.append('<table width="100%" cellspacing="0" cellpadding="3">')
    for k, val in spec_rows(info):
        h.append(f'<tr><td width="18%" valign="top"><span style="color:#777777;">{_e(k)}</span></td><td>{_e(val)}</td></tr>')
    h.append("</table>")

    idle = d.get("idle") or {}
    stress = d.get("stress")
    section("Thermals")
    h.append('<table width="100%" cellspacing="0" cellpadding="4" border="0">'
             '<tr bgcolor="#f2f2f2"><td><b>Sensor</b></td><td align="right"><b>Idle</b></td>'
             '<td align="right"><b>Under load (max)</b></td><td align="right"><b>Under load (avg)</b></td></tr>')
    st = (stress or {}).get("stats", {})
    for label, key in (("CPU temperature", "cpu_temp"), ("GPU temperature", "gpu_temp"), ("GPU hot spot", "gpu_hotspot"),
                       ("CPU power", "cpu_power"), ("GPU power", "gpu_power"), ("CPU clock", "cpu_clock")):
        unit = " W" if "power" in key else " MHz" if "clock" in key else " °C"
        mx = st.get(key, {}).get("max")
        av = st.get(key, {}).get("avg")
        if idle.get(key) is None and mx is None:
            continue
        h.append(f'<tr><td>{label}</td><td align="right">{_num(idle.get(key), unit=unit)}</td>'
                 f'<td align="right">{_num(mx, unit=unit)}</td><td align="right">{_num(av, unit=unit)}</td></tr>')
    h.append("</table>")
    if stress:
        sc = STATUS_LIGHT.get(stress["status"], "#333333")
        h.append(f'<p><span style="color:{sc}; font-weight:700;">Stress test: {_e(stress["status"])}</span> - '
                 f'{_e(stress["summary"])}</p>')
    if d.get("has_graph"):
        h.append('<p><img src="bc://graph" width="640"></p>')
    if not stress:
        h.append('<p><span style="color:#777777;">No stress test was run.</span></p>')

    bench = d.get("bench")
    if bench and bench.get("rows"):
        section("Benchmarks")
        h.append('<table width="100%" cellspacing="0" cellpadding="4"><tr bgcolor="#f2f2f2"><td><b>Test</b></td>'
                 '<td align="right"><b>Result</b></td><td><b>&nbsp;&nbsp;Notes</b></td></tr>')
        for name, value, note in bench["rows"]:
            h.append(f'<tr><td>{_e(name)}</td><td align="right"><b>{_e(value)}</b></td>'
                     f'<td>&nbsp;&nbsp;<span style="color:#777777;">{_e(note)}</span></td></tr>')
        h.append("</table>")

    section("Health checks")
    h.append('<table width="100%" cellspacing="0" cellpadding="4">')
    cat = None
    for c in checks:
        if c["cat"] != cat:
            cat = c["cat"]
            h.append(f'<tr bgcolor="#f2f2f2"><td colspan="3"><b>{_e(cat)}</b></td></tr>')
        col = STATUS_LIGHT.get(c["status"], "#333333")
        fix = f'<br><span style="color:#777777;">{_e(c["fix"])}</span>' if c["fix"] and c["status"] in ("WARN", "FAIL") else ""
        h.append(f'<tr><td width="8%" valign="top"><span style="color:{col}; font-weight:800;">{_e(c["status"])}</span></td>'
                 f'<td width="30%" valign="top">{_e(c["title"])}</td><td valign="top">{_e(c["detail"])}{fix}</td></tr>')
    h.append("</table>")

    if d.get("notes"):
        section("Notes")
        h.append("<p>" + _e(d["notes"]).replace("\n", "<br>") + "</p>")

    h.append(f'<br><p align="center"><span style="color:#999999; font-size:8pt;">Generated by {APP_NAME} v{VERSION}'
             f' &middot; github.com/Landull &middot; {_e(info.get("collected", ""))}</span></p>')
    return "\n".join(h)


def export_pdf(path, d, graph=None):
    """Write the report to a PDF. graph: (series, marks) for the stress-test temperature chart."""
    from PySide6.QtCore import QMarginsF, QUrl
    from PySide6.QtGui import QFont, QImage, QPageLayout, QPageSize, QPdfWriter, QTextDocument

    doc = QTextDocument()
    f = QFont("Segoe UI")
    f.setPointSizeF(9)
    doc.setDefaultFont(f)
    skull = QImage(asset("skull.png"))
    doc.addResource(QTextDocument.ImageResource, QUrl("bc://skull"), skull)
    d = dict(d)
    d["has_graph"] = False
    if graph and graph[0] and any(pts for _, _, pts in graph[0]):
        doc.addResource(QTextDocument.ImageResource, QUrl("bc://graph"), graph_image(graph[0], marks=graph[1]))
        d["has_graph"] = True
    doc.setHtml(build_html(d))
    w = QPdfWriter(path)
    w.setTitle(f"Build Health Report - {d.get('customer') or d['info'].get('computer', {}).get('name', '')}")
    w.setCreator(f"{APP_NAME} {VERSION}")
    w.setPageSize(QPageSize(QPageSize.Letter))
    w.setPageMargins(QMarginsF(12, 12, 12, 12), QPageLayout.Millimeter)
    w.setResolution(144)
    doc.print_(w)
    return path
