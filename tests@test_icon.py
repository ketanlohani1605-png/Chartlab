"""The Mac icon sits on Apple's icon grid: an 824 body in the middle of a 1024 square (so it is the same size as
every other app in the Dock), and that padded picture is also what the running app shows in the Dock."""
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PySide6 import QtGui, QtWidgets
app = QtWidgets.QApplication([])
from chartlab import data as datamod
from chartlab import mainwindow as mw

A = datamod.ROOT / "assets"


def body(path):
    """-> (left, top, right, bottom) of the solid part and the picture's side, alpha of the four corners."""
    im = QtGui.QImage(str(path)).convertToFormat(QtGui.QImage.Format.Format_ARGB32)
    n = im.width()
    assert n == im.height() and n >= 64, (path, n)   # Qt opens a small picture of the .icns
    mid = n // 2
    solid = lambda x, y: QtGui.qAlpha(im.pixel(x, y)) >= 250
    left = next(x for x in range(n) if solid(x, mid)); right = next(x for x in range(n - 1, -1, -1) if solid(x, mid))
    top = next(y for y in range(n) if solid(mid, y)); bottom = next(y for y in range(n - 1, -1, -1) if solid(mid, y))
    corners = [QtGui.qAlpha(im.pixel(x, y)) for x, y in ((0, 0), (n - 1, 0), (0, n - 1), (n - 1, n - 1))]
    return (left, top, right, bottom), n, corners


for name in ("icon_mac.png", "icon.icns"):
    p = A / name
    assert p.exists(), name
    if name.endswith(".icns") and b"icns" not in QtGui.QImageReader.supportedImageFormats():
        continue                                    # this Qt cannot read .icns: the PNG check covers the picture
    (l, t, r, b), n, corners = body(p)
    w, h = (r - l + 1) / n, (b - t + 1) / n
    assert 0.775 <= w <= 0.815 and 0.775 <= h <= 0.815, (name, w, h)        # Apple's grid: 824 / 1024 = 0.805
    assert abs(l - (n - 1 - r)) <= 2 and abs(t - (n - 1 - b)) <= 2, (name, l, t, r, b)   # in the middle
    assert corners == [0, 0, 0, 0], (name, corners)

# the plain logo (the chart's watermark, the Windows icon) still fills its square
(l, t, r, b), n, _c = body(A / "logo.png") if QtGui.QImage(str(A / "logo.png")).width() >= 256 else ((0, 0, 255, 255), 256, [])
assert (r - l + 1) / n > 0.95

# the running app: the padded picture on a Mac, the plain logo elsewhere
real = sys.platform
try:
    mw.sys.platform = "darwin"; assert mw.app_icon_path().name == "icon_mac.png"
    mw.sys.platform = "win32"; assert mw.app_icon_path().name == "logo.png"
    mw.sys.platform = "linux"; assert mw.app_icon_path().name == "logo.png"
finally:
    mw.sys.platform = real
print("ICON OK")
