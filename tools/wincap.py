"""抓指定窗口的画面（PrintWindow），被别的窗口盖住也能抓到。

不依赖前台，适合后台读歌词。

⚠️ 2026-10-28：PrintWindow 会**偶发**返回 false（同一台机器上时好时坏，
    实测连续三次失败、下一分钟又好了）。所以这里：
      1) 重试三次
      2) 还不行就从屏幕直接 BitBlt（窗口没被盖住时有效）
    调用方只管拿图，不用管走的是哪条路。
"""
import ctypes
import ctypes.wintypes as wt
import time

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020

# ⚠️ 2026-10-28 必须显式声明签名：
#   这些 GDI/USER 函数返回的是 64 位句柄，ctypes 默认按 c_int（32 位）处理，
#   一旦句柄数值超过 2^31 就被截断 —— 于是 PrintWindow「偶发失败」、
#   CreateCompatibleDC 直接抛 OverflowError。后台线程里几乎每轮都撞上。
user32.GetWindowDC.argtypes = [wt.HWND]
user32.GetWindowDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.PrintWindow.restype = wt.BOOL
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL

gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.BitBlt.restype = wt.BOOL


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG),
        ('biPlanes', wt.WORD), ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
        ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', wt.LONG), ('biYPelsPerMeter', wt.LONG),
        ('biClrUsed', wt.DWORD), ('biClrImportant', wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wt.DWORD * 3)]


def _to_image(mdc, bmp, w, h):
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0

    buf = ctypes.create_string_buffer(w * h * 4)
    if not gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0):
        return None
    return Image.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1).convert('RGB')


def _grab(hwnd, w, h, mode):
    """mode='print' 用 PrintWindow；mode='screen' 从屏幕 BitBlt。"""
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    try:
        if mode == 'print':
            if not user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT):
                return None
        else:
            if not gdi32.BitBlt(mdc, 0, 0, w, h, hdc, 0, 0, SRCCOPY):
                return None
        return _to_image(mdc, bmp, w, h)
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)


def capture(hwnd):
    """返回 PIL.Image；真的抓不到才返回 None。"""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    # ① PrintWindow 重试三次
    for i in range(3):
        img = _grab(hwnd, w, h, 'print')
        if img is not None:
            return img
        time.sleep(0.25)

    # ② 兜底：从屏幕直接抄（窗口被别的窗口盖住时会抓到盖住它的东西，
    #    但总比什么都没有强；调用方会用自己的判据决定要不要信这张图）
    img = _grab(hwnd, w, h, 'screen')
    if img is not None:
        return img

    return None
