"""Round 32: what a careful side-by-side with TradingView (RELIANCE 1D on its page) still showed.
- volume bars painted on a Retina Mac (no 2e9-unit bounding rect: Qt's integer culling overflowed on arm64)
- the symbol's own precision (NSE stocks: the tick of the price level - 1,358.5 / 809.20 / 10,587), U+2212 minus
- price steps 1 / 2 / 2.5 / 4 / 5 x 10^n, bold labels at 5x (1/2/4) or 2x (2.5/5) the step
- numbers under the last-price / volume labels are not drawn
- time labels: TradingView's rules measured at 13 zoom levels (dates every 1-4 bars from the month start or none,
  months every 1 / 2 / 6, hours / quarters / days intraday, the most important ones bold)
- daily countdown to the session close (not midnight), legend '· 5 ·', 'Limited' names
- the symbol pill (128 px, text left, data-source button inside), a failed load leaves the old symbol in it,
  Symbol Search knows every NSE stock, the top bar's sizes, no favourite intervals by default
"""
import datetime as dt
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np
import pandas as pd
from PySide6 import QtCore, QtTest, QtWidgets

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import theme  # noqa: E402

theme.apply_pg_config()
app.setStyle("Fusion")
app.setStyleSheet(theme.STYLESHEET)
from chartlab.chart import DateAxis, price_decimals, tv_price_step  # noqa: E402
from chartlab.mainwindow import MainWindow, quit_cleanly  # noqa: E402


def pump(ms=50):
    QtTest.QTest.qWait(ms)


HOL = pd.DatetimeIndex(["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
                        "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
                        "2026-10-20", "2026-11-10", "2025-10-02", "2025-10-22", "2025-11-05", "2025-12-25"])


def nse_days(n, end="2026-11-18"):
    d = pd.bdate_range(end=end, periods=n + 40)
    return d[~d.isin(HOL)][-n:]


def labels(idx, spx, W=1100, right=0.0):
    ax = DateAxis("bottom")
    ax.set_index(idx)
    x1 = len(idx) - 1 + right
    sp, vals = ax.tickValues(x1 - W / spx, x1, W)[0]
    strs = ax.tickStrings(vals, 1, sp)
    return [("*" if s in ax._bold else "") + s for s in strs]


# ---------------------------------------------------------------- 1) precision
r = np.random.default_rng(5)
one = np.round(1300 + np.cumsum(r.normal(0, 4, 60)), 1)
df1 = pd.DataFrame({"Open": one, "High": one + 3.1, "Low": one - 2.7, "Close": one, "Volume": 1e6},
                   index=nse_days(60))
for c in ("Open", "High", "Low", "Close"):                      # Yahoo's float32 prices
    df1[c] = df1[c].astype(np.float32).astype(np.float64)
assert price_decimals(df1) == 1
df2 = df1.copy()
df2[["Open", "High", "Low", "Close"]] = np.round(df1[["Open", "High", "Low", "Close"]].to_numpy() * 18.73 + 0.05, 2)
assert price_decimals(df2) == 2
df0 = df1.copy()
df0[["Open", "High", "Low", "Close"]] = np.round(df1[["Open", "High", "Low", "Close"]].to_numpy() * 8)
assert price_decimals(df0) == 0
assert [tv_price_step(x, 1) for x in (0.03, 0.07)] == [0.1, 0.1]  # never finer than the symbol's tick
from chartlab.symbols import band_decimals, name_of, nse_symbols, tick_of, variable_tick  # noqa: E402
assert len(nse_symbols()) > 2000 and tick_of("RELIANCE") == 0.1 and name_of("BHARATFORG")
assert variable_tick("RELIANCE") and not variable_tick("NIFTYBEES") and not variable_tick("NIFTY")
assert [band_decimals(p) for p in (240, 809.2, 1358.5, 9123.5, 10587, 128310)] == [2, 2, 1, 1, 0, 0]

# ---------------------------------------------------------------- 2) time scale (TradingView, measured)
d = nse_days(700)
assert labels(d, 12.6) and all(not x.strip("*").isdigit() for x in labels(d, 12.6))     # 12.6 px: months only
lab = labels(d, 13.5)
assert any(x.isdigit() for x in lab)                                                     # 13.5 px: dates appear
ax = DateAxis("bottom")
ax.set_index(d)
pos = ax._ti["pos"]


def strides(spx):
    x1 = len(d) - 1
    vals = ax.tickValues(x1 - 1100 / spx, x1, 1100)[0][1]
    return sorted({int(pos[int(v)]) for v in vals if pos[int(v)] > 0})


assert all(p % 4 == 0 for p in strides(13.5)) and strides(13.5)[:2] == [4, 8]
assert all(p % 3 == 0 for p in strides(25.4)) and all(p % 2 == 0 for p in strides(25.8))
assert strides(26)[:3] == [2, 4, 6]
assert "*Oct" in labels(d, 16) and "Oct" not in labels(d, 16)                          # months bold among dates
m2 = [x for x in labels(d, 2.0) if not x.startswith("*")]
months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
assert m2 and all((months.index(x) % 2) == 0 for x in m2), m2                           # 2 px: every 2nd month
w = pd.date_range(end="2026-11-16", periods=600, freq="W-MON")
assert [x for x in labels(w, 6) if not x.startswith("*")] and all(x == "Jul" for x in labels(w, 6) if not x.startswith("*"))
assert all(months.index(x) % 2 == 0 for x in labels(w, 12) if not x.startswith("*"))
assert all(not x.startswith("*") or x[1:].isdigit() for x in labels(w, 1.5))            # years only
days = nse_days(30)
i5 = pd.DatetimeIndex([t for dd in days for t in pd.date_range(dd + pd.Timedelta(hours=9, minutes=15), periods=75,
                                                                  freq="5min")])
l3 = labels(i5, 3)
assert all(":" not in x for x in l3) and [x for x in l3 if x.startswith("*")] == ["*16"], l3   # Monday bold
l20 = labels(i5, 20)
assert all(x[1:].endswith(":00") for x in l20 if x.startswith("*")) and any(x.endswith(":15") for x in l20), l20
l6 = labels(i5, 6)
assert "*18" in l6 and "10:00" in l6 and "09:30" not in l6                               # hours, day bold
i1 = pd.DatetimeIndex([dd + pd.Timedelta(hours=9, minutes=15) + pd.Timedelta(hours=h) for dd in days for h in range(7)])
assert "12:15" in labels(i1, 30) and "11:15" not in labels(i1, 30)                      # TradingView: 06:45 UTC

# ---------------------------------------------------------------- 3) the window
from chartlab.watchlist import session_close  # noqa: E402
c = session_close("RELIANCE", dt.datetime(2026, 11, 18, 7, 39, 1, tzinfo=dt.timezone.utc))
assert c == dt.datetime(2026, 11, 18, 10, 0, tzinfo=dt.timezone.utc)                     # 2:20:59 left, like TV
assert session_close("RELIANCE", dt.datetime(2026, 11, 18, 11, 0, tzinfo=dt.timezone.utc)) is None
assert session_close("BTC-USD", dt.datetime(2026, 11, 18, 11, 0, tzinfo=dt.timezone.utc)).hour == 0

ini = tempfile.mktemp(suffix=".ini")
w = MainWindow(QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat))
w.resize(1600, 878)
w.show()
pump(150)
p = w.active
ch = p.chart
rel = df1.copy()
rel.index = nse_days(len(rel), end=pd.Timestamp.today().normalize().strftime("%Y-%m-%d"))
p.params.update(symbol="RELIANCE", interval="1d", source="yahoo")
w.symbol.setText("RELIANCE")
p.set_dataframe(rel, "RELIANCE.NS · 1d")
pump(200)
assert ch.decimals() == 1 and ch.fp(1358.5) == "1,358.5" and ch.fp(809.2) == "809.20" and ch.fp(10587) == "10,587"
assert ch.fd(-4.5, ref=1362.5) == "−4.5" and ch.display_name() == "Reliance Industries Limited"
hdr = ch.header.text()
import re  # noqa: E402
assert " · 1D · NSE" in hdr and re.search(r"[+−][\d,]+\.\d \([+−]\d+\.\d\d%\)", hdr), hdr[-200:]
assert ch.spread_lbl.text().count("0.0<") == 1                                           # '0.0': the stock's tick
# volume bars: a finite rect in the visible strip, and they really paint
br = ch.volbars.boundingRect()
(vx0, vx1), (vy0, vy1) = ch.price.vb.viewRange()
assert np.isfinite([br.top(), br.bottom()]).all() and br.top() >= vy0 - (vy1 - vy0) and br.height() < (vy1 - vy0)
img = ch.glw.grab().toImage()
vcol = ch.volbars._c(True).name(), ch.volbars._c(False).name()
seen = {QtWidgets.QApplication.instance() and img.pixelColor(x, y).name()
        for x in range(0, img.width(), 3) for y in range(int(img.height() * 0.75), img.height(), 2)}
assert vcol[0] in seen or vcol[1] in seen, "volume bars are not painted"
# the scale draws no number under the last-price / volume labels
ax = ch.price.getAxis("right")
ch._place_last_tag()
assert ax._blocked and all(b > a for a, b in ax._blocked)
# the pill: TradingView's 128 px, the symbol on the left, the data-source button inside
pill = w.sym_btn.parentWidget()
assert pill.objectName() == "symbolpill" and pill.width() == 128 and w.dtype_btn.size() == QtCore.QSize(32, 28)
assert w.sym_btn.text() == "RELIANCE" and w.dtype_btn.menu() is w.src_menu
# a symbol that cannot be loaded does not stay in the pill
w.symbol.setText("BH")
w._on_load_failed(p, "No data returned for 'BH.NS' (1d, 2y). Check the symbol / internet.")
assert w.symbol.text() == "RELIANCE" and w.sym_btn.text() == "RELIANCE"
# top bar: TradingView's sizes, labels at 1600 px, no favourites by default ('D' opens the list)
w._fit_topbar()
pump(50)
assert w.alert_btn.toolButtonStyle() == QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
assert w.replay_btn.toolButtonStyle() == QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
assert w.interval_btn.text() == "D" and not any(a.isVisible() for a in w.interval_btn_actions.values())
assert w.menu_btn.size() == QtCore.QSize(52, 38) and w.layout_btn.size() == QtCore.QSize(38, 38)
assert w.ind_fav_btn.width() == 24 and w.alert_btn.height() == 38
assert not w.api_label.isVisible()                                                      # no 'Claude link' text
# Symbol Search knows every NSE stock, by symbol and by name
from chartlab.tvui import SymbolSearch  # noqa: E402
dlg = SymbolSearch(w, w.watchlist.symbols() + list(w._symbol_model.stringList()), "BH")
found = [dlg.lst.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(dlg.lst.count())]
assert {"BHEL", "BHARATFORG", "BHARTIARTL"} <= set(found) and found[0] != "BH", found[:8]
dlg.edit.setText("bharat")
found = [dlg.lst.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(dlg.lst.count())]
assert "BEL" in found and "BHARATFORG" in found, found[:12]
dlg.close()
# intraday legend: 'Nifty 50 Index · 5 · NSE' like TradingView
idx5 = pd.date_range("2026-11-17 09:15", periods=150, freq="5min")
c5 = 25400 + np.cumsum(r.normal(0, 3, 150))
d5 = pd.DataFrame({"Open": c5, "High": c5 + 2, "Low": c5 - 2, "Close": c5, "Volume": 0.0}, index=idx5)
p.params.update(symbol="NIFTY", interval="5m")
p.set_dataframe(d5, "^NSEI · 5m")
pump(100)
assert ch.interval_tag() == "5" and " · 5 · NSE" in ch.header.text()
quit_cleanly(w, 0, exit_process=False)
print("ROUND 32 OK")
os._exit(0)
