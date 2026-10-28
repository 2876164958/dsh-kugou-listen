"""用 UI Automation 驱动酷狗，不截图、不认字。

为什么要有这个文件
------------------
酷狗是自绘界面，我们一开始只能截图 + OCR + 按坐标点。问题是：
  · PrintWindow 偶发失败
  · OCR 会把「邀请好友」认成「适好友一起听」
  · 窗口一挪、按钮一换位置，算出来的坐标全废

后来发现它其实把控件暴露给了系统的 UI Automation（IAccessible）。
名字都是现成的：「更多」「一起听」「邀请好友一起听」「复制链接」
「退出」「再听一会」「上一首」「播放」……

于是：读状态和点按钮都走这里，截图只留给歌词（歌词是画上去的，
没有控件）。

注意
----
· uiautomation 是可选依赖：没装也不影响读歌读词，只是点不了按钮。
· 点之前一律先找「恰好叫这个名字」的控件，找不到就报错，不猜坐标。
"""
from __future__ import annotations

import time

# 依赖说明：uiautomation（Apache 2.0）+ 它带的 comtypes（MIT），装法就一行：
#     pip install uiautomation
# 没装也能读歌读词，只是点不了按钮（开房/退出会退回截图认字）。
# 试过把这两个包直接塞进插件里（2 MB），实测在别的环境里 UIA 树是空的，
# 所以不内置 —— 让目标机器自己安装靠得住。
try:
    import uiautomation as auto
    HAVE_UIA = True
except Exception:                                     # pragma: no cover
    auto = None
    HAVE_UIA = False


def init_thread():
    """在后台线程里用 UIA 之前必须调一次。

    ⚠️ 2026-10-28 踩过：后台扫描线程里直接读控件会静默失败（COM 没在这个线程初始化），
    表现就是「明明开着扫描却什么都不发生」，日志里连一行都没有。
    """
    if not HAVE_UIA:
        return False
    try:
        auto.InitializeUIAutomationInCurrentThread()
        return True
    except Exception:
        try:
            import comtypes
            comtypes.CoInitialize()
            return True
        except Exception:
            return False


def _windows(all_windows):
    """把 [(hwnd, l, t, r, b, title)] 包成 UIA 控件。"""
    for hwnd, *_rest in all_windows:
        try:
            yield hwnd, auto.ControlFromHandle(hwnd)
        except Exception:
            continue


def _safe_int(v):
    """控件坐标转 int。

    ⚠️ 2026-10-28：有些控件的 BoundingRectangle 是「未定义」的巨大值（远大于屏幕），
    直接 int() 会抛 OverflowError，整轮扫描就废了。这里统一夹到合理范围。
    """
    try:
        f = float(v)
    except Exception:
        return 0
    if f != f or f > 1e6 or f < -1e6:      # NaN 或离谱值
        return 0
    return int(f)


def named(all_windows, max_depth=8):
    """枚举所有有名字的控件：[(name, type, x, y, control, hwnd)]。"""
    if not HAVE_UIA:
        return []
    out = []
    for hwnd, ctl in _windows(all_windows):
        stack = [(ctl, 0)]
        while stack:
            c, d = stack.pop()
            if d > max_depth:
                continue
            try:
                nm = (c.Name or "").strip()
                if nm:
                    r = c.BoundingRectangle
                    out.append((nm, c.ControlTypeName, _safe_int(r.left), _safe_int(r.top), c, hwnd))
                stack.extend((ch, d + 1) for ch in c.GetChildren())
            except Exception:
                continue
    return out


# 真能点的控件类型。同名的弹窗（WindowControl）点它只会把菜单点掉，
# 2026-10-28 就栽在这上面：叫「更多」的控件里混着一个同名窗口，点错了。
CLICKABLE = ('ButtonControl', 'CheckBoxControl', 'RadioButtonControl', 'MenuItemControl',
             'HyperlinkControl', 'TextControl', 'ListItemControl', 'ImageControl', 'TabItemControl')


def find(items, name, *, x_min=None, y_min=None, hwnd=None, prefer=True):
    """找「恰好叫 name」的控件，可以再按屏幕位置或窗口筛。

    prefer=True 时会优先返回真正能点的控件类型（按钮/复选/菜单项…），
    而不是同名的窗口或面板 —— 后者点下去往往只是把界面点没了。
    """
    hits = []
    for nm, t, x, y, c, h in items:
        if nm != name:
            continue
        if x_min is not None and x < x_min:
            continue
        if y_min is not None and y < y_min:
            continue
        if hwnd is not None and h != hwnd:
            continue
        hits.append((nm, t, x, y, c, h))
    if not hits:
        return None
    if prefer:
        clickable = [x for x in hits if x[1] in CLICKABLE]
        if clickable:
            return clickable[0]
    return hits[0]


def click(items, name, **kw):
    """点到就算成功；返回点了哪个（含坐标），点不到返回 None。"""
    hit = find(items, name, **kw)
    if not hit:
        return None
    nm, t, x, y, c, h = hit
    try:
        c.Click(simulateMove=False)
    except Exception as e:
        return {'name': nm, 'error': str(e)}
    return {'name': nm, 'type': t, 'x': x, 'y': y}


def click_sequence(all_windows, steps, gap=0.9):
    """依次点一串名字。每步都重新枚举控件（界面会变）。

    steps 允许写 ('退出', {'x_min': 1000}) 这种带筛选的。
    返回每一步的结果，方便出错时看清卡在哪。
    """
    done = []
    for step in steps:
        name, kw = (step, {}) if isinstance(step, str) else step
        items = named(all_windows())
        r = click(items, name, **kw)
        done.append({'step': name, 'ok': bool(r and 'error' not in r), 'detail': r})
        if not r or 'error' in r:
            return {'ok': False, 'steps': done, 'reason': '没点到「%s」' % name}
        time.sleep(gap)
    return {'ok': True, 'steps': done}