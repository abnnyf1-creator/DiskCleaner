import ctypes
from ctypes import wintypes
from PIL import Image, ImageGrab

u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32

hwnd = u32.FindWindowW(None, "磁盘清理工具 DiskCleaner")
if not hwnd:
    print("WINDOW_NOT_FOUND")
    raise SystemExit(1)

print("HWND", hwnd, "visible=", bool(u32.IsWindowVisible(hwnd)))

r = wintypes.RECT()
u32.GetWindowRect(hwnd, ctypes.byref(r))
w, h = r.right - r.left, r.bottom - r.top
print("RECT", w, h, "at", r.left, r.top)

hdc = u32.GetDC(0)
memdc = g32.CreateCompatibleDC(hdc)
hbmp = g32.CreateCompatibleBitmap(hdc, w, h)
g32.SelectObject(memdc, hbmp)
u32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_uint]
u32.PrintWindow.restype = ctypes.c_int
res = u32.PrintWindow(hwnd, memdc, 2)
print("PrintWindow =", res)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


bmi = BITMAPINFOHEADER()
bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
bmi.biWidth = w
bmi.biHeight = -h
bmi.biPlanes = 1
bmi.biBitCount = 32
buf = ctypes.create_string_buffer(w * h * 4)
g32.GetDIBits(memdc, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)

# PrintWindow 对部分渲染技术(如 Qt)可能截到全黑/全透明, 回退为屏幕区域抓取
colors = img.convert("L").getcolors(4096) or []
is_blank = (not colors) or (len(colors) <= 2 and img.getbbox() is not None and
                            sum(1 for _c, px in colors if px < 8) == len(colors))
if is_blank or img.getbbox() is None:
    print("PRINTWINDOW_BLANK -> fallback to screen grab")
    u32.SetForegroundWindow(hwnd)
    import time
    time.sleep(0.8)
    img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))

img.save(r"D:\MyCode\DiskCleaner\_ui_preview.png")
print("SHOT_SAVED", img.size)

g32.DeleteObject(hbmp)
g32.DeleteDC(memdc)
u32.ReleaseDC(0, hdc)
