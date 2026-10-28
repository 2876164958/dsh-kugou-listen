"""DSH MCP server: 读酷狗正在播的歌和歌词。

stdio 传输。工具：
  kugou_now   读当前歌曲 + 屏幕上可见的歌词行
  kugou_lyric 只读歌词行
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time

from PIL import Image, ImageGrab
import winocr
from mcp.server.fastmcp import FastMCP

import wincap

user32 = ctypes.windll.user32
EnumWindows = user32.EnumWindows
EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

mcp = FastMCP("kugou-listen")


def kugou_window():
    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if '酷狗音乐' in title and '桌面歌词' not in title:
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append((hwnd, r.left, r.top, r.right, r.bottom, title))
            return False
        return True

    EnumWindows(EnumProc(cb), 0)
    return found[0] if found else None


def cjk_ratio(s):
    if not s:
        return 0.0
    cjk = sum(1 for ch in s if '\u4e00' <= ch <= '\u9fff')
    return cjk / max(len(s.replace(' ', '')), 1)


# ── 歌词区自适应 ────────────────────────────────────────────────────────
# 2026-10-28：以前是按窗口比例写死（宽 54%–94%、高 64%–90%）。
# 别人的分辨率、缩放、窗口比例一不一样，这一刀就切歪，读到空或者读到版权行。
# 现在改成：自己 OCR 找一片「像歌词」的行，聚成一簇，那个簇的包围盒就是歌词区；
# 找到之后按窗口尺寸缓存起来（每台机器各自一份），下次直接用。
CREDIT_WORDS = ('出品', '作词', '作曲', '编曲', '制作', '监制', '混音', '母带', '发行', '演唱',
                '和声', '录音', '吉他', '贝斯', '鼓', '钢琴', '笛子', '弦乐', '统筹', '企划',
                '策划', '厂牌', '版权', '词：', '曲：', '著作权', '翻唱', '翻录', '许可', '侵权',
                '转载', '声明', '未经', '授权', '保留所有权利')


def _history_file():
    return os.path.join(CFG_DIR, 'history.jsonl')


def _record_history(song, lines):
    """记一行听歌史。同一首歌、同一段词只记一次，避免每轮都写。

    行格式：{"t": "2026-10-29 02:05:11", "song": "…", "line": "…"}
    """
    # 一首歌只记一次。以前把歌词也算进 key，结果歌词一滚就写一行，日志会爆。
    key = song
    if _state.get('last_key') == key:
        return
    _state['last_key'] = key
    import datetime
    os.makedirs(CFG_DIR, exist_ok=True)
    rec = {'t': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'song': song,
           'line': (lines[0] if lines else '')}
    with open(_history_file(), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def _lyric_box_file():
    return os.path.join(CFG_DIR, 'lyric_box.json')


def _like_lyric(text):
    """判断一行字像不像歌词。宁可严一点，也别把版权行当歌词。"""
    if not text or len(text) < 2 or len(text) > 30:
        return False
    if cjk_ratio(text) < 0.6:
        return False
    if any(c in text for c in CREDIT_WORDS):
        return False
    # 时间、纯数字、英文名这类不算
    if TIME_RE.search(text) or any(ch.isdigit() for ch in text):
        return False
    return True


def _detect_lyric_box(shot):
    """整窗认一次字，找出一片像歌词的行，返回包围盒（窗口像素）。

    找不到就返回 None，调用方退回比例裁切。
    """
    from PIL import ImageOps

    w, h = shot.size
    img = ImageOps.autocontrast(shot.convert('L'))
    img = img.resize((w * 2, h * 2), Image.LANCZOS)
    try:
        res = winocr.recognize_pil_sync(img, 'zh-Hans')
    except Exception:
        return None

    cands = []
    for line in res.get('lines', []):
        text = ''.join(line.get('text', '').split())
        if not _like_lyric(text):
            continue
        xs, ys, xe, ye = [], [], [], []
        for word in line.get('words', []):
            r = word.get('bounding_rect', {})
            xs.append(r.get('x', 0) / 2)
            ys.append(r.get('y', 0) / 2)
            xe.append((r.get('x', 0) + r.get('width', 0)) / 2)
            ye.append((r.get('y', 0) + r.get('height', 0)) / 2)
        if not xs:
            continue
        cands.append((min(xs), min(ys), max(xe), max(ye)))

    if len(cands) < 2:
        return None

    # 按竖直方向聚簇：行距小于「平均行高 × 2.5」就算同一片
    cands.sort(key=lambda b: b[1])
    heights = [b[3] - b[1] for b in cands]
    avg_h = sum(heights) / len(heights)
    groups = [[cands[0]]]
    for b in cands[1:]:
        prev = groups[-1][-1]
        if b[1] - prev[3] <= max(avg_h * 2.5, 30):
            groups[-1].append(b)
        else:
            groups.append([b])

    # 歌词排版没个准：有的靠右一列，有的居中一块，字号用户还能自己调。
    # 所以别卡左右、也别卡绝对像素。改看三件和排版无关的事：
    #   ① 行数 2–8        —— 二十行那种是列表
    #   ② 行高占窗口 0.8%–12%  —— 和字号设置无关，只和窗口大小挂钩
    #   ③ 不贴着窗口顶部    —— 顶部是标签和按钮
    def ok_group(g):
        if not (2 <= len(g) <= 8):
            return False
        h_med = sorted((b[3] - b[1]) for b in g)[len(g) // 2]
        if h_med < h * 0.008 or h_med > h * 0.12:
            return False
        cy = sum((b[1] + b[3]) / 2 for b in g) / len(g)
        if cy < h * 0.22:
            return False
        # 横向：左侧栏那种窄条整个缩在左边一窄溜，排除掉；居中和靠右都放行
        gw = max(b[2] for b in g) - min(b[0] for b in g)
        if gw < w * 0.15:
            return False
        return True

    groups = [g for g in groups if ok_group(g)]
    if not groups:
        return None
    # 行多者优先；一样多时取字大的那片
    best = max(groups, key=lambda g: (len(g),
                                      sum(b[3] - b[1] for b in g) / len(g)))

    x0 = min(b[0] for b in best)
    y0 = min(b[1] for b in best)
    x1 = max(b[2] for b in best)
    y1 = max(b[3] for b in best)
    # 留一点余量，并夹在窗口内
    pad_x, pad_y = 12, 16
    return (max(0, int(x0 - pad_x)), max(0, int(y0 - pad_y)),
            min(w, int(x1 + pad_x)), min(h, int(y1 + pad_y)))


def lyric_box(shot):
    """拿到这块窗口的歌词区。优先用缓存，缓存不灵就重新认一次。"""
    w, h = shot.size
    key = '%dx%d' % (w, h)
    try:
        cache = _read_json(_lyric_box_file())
    except Exception:
        cache = {}
    box = cache.get(key)
    if box and len(box) == 4:
        return tuple(int(v) for v in box)

    box = _detect_lyric_box(shot)
    if box:
        cache[key] = list(box)
        try:
            os.makedirs(CFG_DIR, exist_ok=True)
            with open(_lyric_box_file(), 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return box

    # 兜底：老办法，比例裁切
    return (int(w * 0.54), int(h * 0.64), int(w * 0.94), int(h * 0.90))


def kugou_version():
    """读酷狗 exe 的版本号。读不到返回空串。

    只在 20.0.41 上完整验证过；20.1.51 是 Chromium 内核，控件树可能是空的。
    把版本报出来，别人一跑就知道自己属于哪种情况。
    """
    path = ''
    try:
        import ctypes.wintypes as wt
        wins = all_kugou_windows()
        if wins:
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(wins[0][0], ctypes.byref(pid))
            path = _exe_path_of_pid(pid.value)
    except Exception:
        path = ''
    if not path:
        # 窗口枚举拿不到就按默认安装位置找
        for p in (r'C:\Program Files (x86)\KuGou\KGMusic\KuGou.exe',
                  r'C:\Program Files\KuGou\KGMusic\KuGou.exe'):
            if os.path.exists(p):
                path = p
                break
    if not path or not os.path.exists(path):
        return ''
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ''
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
            return ''
        r = ctypes.c_void_p()
        ln = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buf, '\\', ctypes.byref(r), ctypes.byref(ln)):
            return ''
        fixed = ctypes.cast(r, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
        ms, ls = fixed[2], fixed[3]
        ver = '%d.%d.%d.%d' % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
        return '' if ver.startswith('0.0') else ver
    except Exception:
        return ''


def uia_tree_report():
    """控件树体检：每个酷狗窗口报了哪些控件、关键按钮在不在。

    给别人的机器用的。控件树空的时候，读歌和截图都正常，
    但「更多 / 一起听」一个都找不到——这种失败以前只能靠猜。
    """
    info = {'available': False, 'total': 0, 'windows': [], 'keys': {},
            'msg': ''}
    if not uia_available():
        info['msg'] = '没装 uiautomation，开房/退房只能靠认字'
        return info
    info['available'] = True
    try:
        pts = uia_points()
    except Exception as e:
        info['msg'] = '读控件树失败：%s' % e
        return info
    info['total'] = len(pts)
    by_hwnd = {}
    for nm, typ, x, y, c, h in pts:
        by_hwnd.setdefault(h, []).append(nm)
    for hwnd, l, t, r, b, title in all_kugou_windows()[:4]:
        names = by_hwnd.get(hwnd, [])
        info['windows'].append({'title': (title or '')[:30],
                                'size': '%dx%d' % (r - l, b - t),
                                'controls': len(names)})
    for key in ('更多', '一起听', '查看歌词写真', '收起歌曲详情页', '邀请', '退出'):
        info['keys'][key] = any(nm == key for nm in (p[0] for p in pts))
    if info['total'] <= 6:
        info['msg'] = ('控件树几乎是空的（只有 %d 项）——这个版本的酷狗没把控件暴露出来。'
                       '读歌、读歌词不受影响；开房/退房会走认字兜底，可能点不动' % info['total'])
    elif not info['keys'].get('更多'):
        info['msg'] = '控件树有 %d 项，但找不到「更多」按钮——界面可能不是默认皮肤' % info['total']
    else:
        info['msg'] = '控件树正常（%d 项），关键按钮都在' % info['total']
    return info


def selfcheck():
    """自检：这台机器上这条路走不走得通，走不通卡在哪一步。

    别人的机器上，多数问题都出在这五条里：不是 Windows、找不到窗口、
    抓到黑图、标题格式不对、歌词区切歪。所以逐条给结论，别只说一句"失败"。
    """
    out = {'steps': [], 'verdict': 'fail', 'advice': []}

    def step(name, ok, detail):
        out['steps'].append({'name': name, 'ok': ok, 'detail': detail})

    # ① 平台
    if sys.platform != 'win32':
        step('平台', False, '当前是 %s；这条只用 Windows 的 PrintWindow 和系统 OCR' % sys.platform)
        out['advice'].append('换 Windows 跑，或者自己实现本平台的窗口截图 + 文字识别')
        return out
    step('平台', True, 'Windows')

    # ② DPI（不是 100% 时坐标会偏，先报出来）
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        scale = dpi / 96.0
        step('DPI', True, '%d dpi（缩放 %d%%）%s' % (dpi, round(scale * 100),
                                                '' if abs(scale - 1) < 0.01 else ' —— 不是 100%，坐标可能偏'))
    except Exception as e:
        step('DPI', True, '读不到（%s），按 100%% 处理' % e)

    # ③ 找窗口
    win = kugou_window()
    if not win:
        step('找窗口', False, '没找到标题里带「酷狗音乐」的窗口')
        out['advice'].append('先把酷狗打开，别最小化，再点自检')
        return out
    hwnd, l, t, r, b, title = win
    ww, hh = r - l, b - t
    minimized = ww <= 200 or hh <= 60 or l <= -30000
    step('找窗口', True, '标题「%s」，%dx%d%s'
         % (title, ww, hh, '（最小化了！）' if minimized else ''))

    # ③b 酷狗版本（只在一两个版本上验过，报出来让人对号入座）
    ver = kugou_version()
    if ver:
        known = ver.startswith('20.0.')
        step('酷狗版本', True, '%s%s' % (ver, '' if known else '（未完整验证的版本，开房/退房可能不可用）'))
        if not known:
            out['advice'].append('当前酷狗 %s 没在验证范围内（验证过的是 20.0.x）；'
                                 '读歌读词不受影响，开房若失败属于已知情况' % ver)
    else:
        step('酷狗版本', True, '读不到版本号（不影响读歌）')

    # ③c 控件树：读歌走截图，和这一条无关；开房走控件名，全靠它
    # 最小化的时候控件树一定是空的，那是窗口状态问题，别误判成版本问题
    if minimized:
        step('控件树', True, '窗口最小化了，跳过（还原后再测）')
        out['advice'].append('酷狗最小化时读不到也点不到：先还原成正常窗口再自检')
    else:
        tree = uia_tree_report()
        step('控件树', tree.get('total', 0) > 6 if tree['available'] else False,
             tree.get('msg') or ('共 %d 项' % tree.get('total', 0)))
        if tree['available'] and tree.get('windows'):
            for w in tree['windows'][:3]:
                step('  窗口', True, '%s %s，%d 项' % (w['title'], w['size'], w['controls']))
        if tree['available'] and tree.get('total', 0) <= 6:
            out['advice'].append('控件树空着：开房/退房会退回认字，可能点不动，但听歌感想不受影响')

    # ④ 抓图
    shot = wincap.capture(hwnd)
    if shot is None:
        step('抓图', False, 'PrintWindow 返回空')
        out['advice'].append('有些显卡驱动不允许后台渲染，把酷狗切到前台再试')
        return out

    from PIL import ImageStat
    stat = ImageStat.Stat(shot.convert('L'))
    if stat.stddev[0] < 2:
        step('抓图', False, '抓到的是纯色图（标准差 %.2f），多半是最小化或被独占' % stat.stddev[0])
        out['advice'].append('把酷狗还原成正常窗口（不是最小化），再点自检')
        return out
    step('抓图', True, '%dx%d，灰度标准差 %.1f' % (shot.width, shot.height, stat.stddev[0]))

    # ⑤ 标题能不能解析出歌名
    song = title.replace('酷狗音乐', '').strip(' -')
    if not song:
        step('歌名', False, '标题里没解析出「歌手 - 歌名」')
        out['advice'].append('窗口标题格式不是预期的那种，歌名这条会空着，但歌词仍可能读到')
    else:
        step('歌名', True, song)

    # ⑥ 歌词能不能认出来（用自适应那块，和 read_now 同一套逻辑）
    box = lyric_box(shot)
    step('歌词区', True, '按 %s 认的' % ('缓存/自适应', )) if box else None
    img = shot.crop(box)
    from PIL import ImageOps
    img = ImageOps.autocontrast(img.convert('L'))
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    res = winocr.recognize_pil_sync(img, 'zh-Hans')

    CREDIT = ('出品', '作词', '作曲', '编曲', '制作', '监制', '混音', '母带', '发行', '演唱', '和声', '录音', '统筹', '企划', '厂牌', '版权',
              '著作权', '翻唱', '翻录', '许可', '侵权', '转载', '声明', '未经', '授权', '保留所有权利')
    lines = []
    for line in res.get('lines', []):
        text = ''.join(line.get('text', '').split())
        if not text or cjk_ratio(text) < 0.6:
            continue
        if any(c in text for c in CREDIT):
            continue
        lines.append(text)

    if lines:
        step('歌词', True, '认到 %d 行，例如「%s」' % (len(lines), lines[0]))
        out['verdict'] = 'ok'
    else:
        step('歌词', False, '那一块没认出中文行')
        out['advice'].append('多半是没停在歌词页，或者皮肤把歌词区挪了：先点开歌曲页的「歌词」标签再试')
        out['verdict'] = 'partial' if song else 'fail'

    if out['verdict'] == 'ok' and not song:
        out['verdict'] = 'partial'

    return out


@mcp.tool()
async def kugou_selfcheck() -> str:
    """自检：当前这台机器上能不能读到酷狗（找不到窗口/抓到黑图/歌词区切歪都会说明）。"""
    import asyncio
    return json.dumps(await asyncio.to_thread(selfcheck), ensure_ascii=False)


def _progress_text(hwnd, shot=None):
    """播放条上那个 00:32/05:11。用来判断到底在不在放。"""
    s = shot if shot is not None else wincap.capture(hwnd)
    if s is None:
        return ''
    w, h = s.size
    y0 = int(h * 0.80)
    strip = s.crop((0, y0, w, h))
    strip = strip.resize((strip.width * 2, strip.height * 2), Image.LANCZOS)
    res = winocr.recognize_pil_sync(strip, 'zh-Hans')
    for line in res.get('lines', []):
        t = line.get('text', '').replace(' ', '').replace('：', ':')
        m = TIME_RE.search(t)
        if m:
            return m.group(0)
    return ''


def _lyric_view_open(names):
    """歌词写真面板开没开。

    ⚠️ 2026-10-28 实测（酷狗 20.0.41）：「收起歌曲详情页」和「查看歌词写真」
       **会同时存在**，所以只看控件名永远判成"开着"，永远不去点。这是真错的。

    实测数据（同一首歌、同一个窗口）：
      关着 → 61 个控件，左侧导航（乐库/分类/为你推荐…）都在
      打开 → 148 个控件，导航整块消失，多出播放队列的一堆歌名

    所以判据改成：控件总数。粉丝页/歌词页比歌曲页多一个数量级。
    阈值取 90：低于它一定是歌曲页（实测 61~67），高于它一定是展开态（148+）。
    不同版本数字会有出入，所以先用「收起」这个名字做前置，再用总数确认。
    """
    if '收起歌曲详情页' not in names and '全屏写真' not in names:
        return False
    return len(names) >= 90


def ensure_lyric_view():
    """确保歌词写真面板是开着的。返回 (是否开着, 说明)。

    ⚠️ 2026-10-28：以前只靠定时任务那边「先点开」的提示词，代码里没做，
       所以把提示词一改就没人点了。这件事本来就该由代码保证。
    """
    if not uia_available():
        return False, '没装 uiautomation，点不了'
    try:
        names = [nm for nm, *_ in uia_points() if nm]
    except Exception as e:
        return False, '读控件失败：%s' % e

    if _lyric_view_open(names):
        return True, '已经开着（%d 项）' % len(names)

    if '查看歌词写真' not in names:
        return False, '找不到「查看歌词写真」这个按钮（可能不在歌曲页）'

    r = uia_click('查看歌词写真', gap=1.8, tries=2)
    if not r.get('ok'):
        return False, '点了但没反应'

    time.sleep(0.9)
    names = [nm for nm, *_ in uia_points() if nm]
    if _lyric_view_open(names):
        return True, '刚点开（%d 项）' % len(names)
    return False, '点开之后控件只有 %d 项，看着没展开' % len(names)


def read_now(front=False):
    win = kugou_window()
    if not win:
        return {'ok': False, 'error': '找不到酷狗窗口 —— 酷狗没开，或者被最小化了'}
    hwnd, l, t, r, b, title = win
    if front:
        user32.ShowWindow(hwnd, 9)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.6)

    # 先确保歌词写真面板开着（没开就点左下角那个封面）。
    # 点它要窗口在前台，所以先抢一次焦点；抢不到也继续读，只是可能读空。
    lyric_view = 'skipped'
    try:
        if uia_available():
            _uia_focus()
            ok_lv, why_lv = ensure_lyric_view()
            lyric_view = 'open' if ok_lv else why_lv
    except Exception as e:
        lyric_view = '检查失败：%s' % e

    # 先用 PrintWindow 直接渲染整窗（不怕被盖），再从里面裁歌词列
    shot = wincap.capture(hwnd)
    if shot is None:
        return {'ok': False, 'error': '抓不到酷狗画面（PrintWindow 失败）'}

    # ⭐ 窗口标题会停在上一首不动，进度那行 OCR 又常常读空。
    #    最稳的判据是「画面有没有在动」：放歌时进度条和歌词一直在变，暂停就静止。
    import numpy as np

    def _motion():
        a = wincap.capture(hwnd)
        time.sleep(2.0)
        b = wincap.capture(hwnd)
        if a is None or b is None:
            return None
        if a.size != b.size:
            return None
        w, h = a.size
        aa = np.asarray(a.convert('L'), dtype=np.int16)
        bb = np.asarray(b.convert('L'), dtype=np.int16)
        d = np.abs(aa - bb)

        # ⭐ 别用平均值：进度条一秒只走一两个像素，摊到整行上小得看不见。
        #    数「变了多少颗像素」才对。
        bar = d[int(h * 0.86):int(h * 0.94), :]
        lyr = d[int(h * 0.60):int(h * 0.90), int(w * 0.50):]
        bar_px = int((bar > 12).sum())
        lyr_px = int((lyr > 12).sum())
        return bar_px, lyr_px

    mv = _motion()
    if mv is None:
        playing = False
        motion = '抓不到第二帧'
    else:
        bar_px, lyr_px = mv
        playing = bar_px >= 2 or lyr_px >= 40
        motion = '进度条变了 %d 像素 / 歌词区变了 %d 像素' % (bar_px, lyr_px)

    w, h = shot.size
    box = lyric_box(shot)
    img = shot.crop(box)
    # 放大两倍再 OCR：酷狗这套字体会把「把」「你」拆成偏旁，小图认不出来
    from PIL import ImageOps
    img = ImageOps.autocontrast(img.convert('L'))
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    res = winocr.recognize_pil_sync(img, 'zh-Hans')

    CREDIT = ('出品', '作词', '作曲', '编曲', '制作', '监制', '混音', '母带', '发行', '演唱', '和声', '录音', '吉他', '贝斯', '鼓', '钢琴', '笛子', '弦乐', '统筹', '企划', '策划', '厂牌', '版权', '词：', '曲：',
              '著作权', '翻唱', '翻录', '许可', '侵权', '转载', '声明', '未经', '授权', '保留所有权利')
    lines = []
    for line in res.get('lines', []):
        text = ''.join(line.get('text', '').split())
        if not text or cjk_ratio(text) < 0.6:
            continue
        if any(c in text for c in CREDIT):
            continue
        lines.append(text)

    song = title.replace('酷狗音乐', '').strip(' -')

    # 顺手记一笔听歌史：以前只记「说了什么」，没记「在听什么」，
    # 想做个按天的听歌日记就没材料。这里只记变化点，同一首歌不重复写。
    try:
        if song and playing:
            _record_history(song, lines)
    except Exception:
        pass

    # 缓存切歪了会一直读空。连空两次就把这个尺寸的缓存删掉，下次重新认。
    if not lines:
        _state['empty'] = _state.get('empty', 0) + 1
        if _state['empty'] >= 2:
            _state['empty'] = 0
            try:
                p = _lyric_box_file()
                cache = _read_json(p)
                cache.pop('%dx%d' % (w, h), None)
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(cache, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
    else:
        _state['empty'] = 0

    return {'ok': True, 'song': song, 'playing': playing,
            'motion': motion,
            'lyric_view': lyric_view,
            'lines': lines[:12]}


@mcp.tool()
async def kugou_now(front: bool = False) -> str:
    """读酷狗当前在放的歌、在不在放、以及屏幕上看得见的歌词。默认不抢前台（抢前台在有些机器上会卡住）。"""
    import asyncio
    d = await asyncio.to_thread(read_now, front)
    return json.dumps(d, ensure_ascii=False)


@mcp.tool()
async def kugou_lyric() -> str:
    """只读当前屏幕上可见的歌词行（不切窗口）。"""
    import asyncio
    d = await asyncio.to_thread(read_now, False)
    return json.dumps({'ok': d.get('ok'), 'song': d.get('song', ''), 'lines': d.get('lines', [])},
                      ensure_ascii=False)


# 桥接这几个值都可以用环境变量改，默认值只是「按常见布局猜」：
#   KUGOU_BRIDGE_URL    默认 http://127.0.0.1:3100
#   KUGOU_BRIDGE_TOKEN  控制台令牌文件，默认 ~/qq-bridge/state/console-token
#   KUGOU_OWNER_QQ      默认收件人 QQ
BRIDGE = os.environ.get('KUGOU_BRIDGE_URL', 'http://127.0.0.1:3100')
TOKEN_FILE = os.environ.get(
    'KUGOU_BRIDGE_TOKEN',
    os.path.join(os.path.expanduser('~'), 'qq-bridge', 'state', 'console-token'),
)
OWNER = os.environ.get('KUGOU_OWNER_QQ', '')


def qq_send(text, user=''):
    """通过 qq-bridge 的私聊接口发一条。没装桥接就返回失败，不抛。"""
    import urllib.error
    import urllib.request

    try:
        with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
            token = f.read().strip()
    except Exception as e:
        return {'ok': False, 'error': '没有 qq-bridge（%s）' % e}

    # 收件人：参数 > 设置里的 owner_qq > 环境变量
    target = (user or load_cfg().get('owner_qq') or OWNER or '').strip()
    if not target:
        return {'ok': False, 'error': '没配收件人：设置里填 owner_qq，或者设环境变量 KUGOU_OWNER_QQ'}
    body = json.dumps({'userId': target, 'message': text}).encode('utf-8')

    def once():
        req = urllib.request.Request(BRIDGE + '/api/send/private', data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('x-console-token', token)
        req.add_header('User-Agent', 'kugou-listen/0.4')
        # 本机回环别走系统代理：宿主那套环境里可能有 HTTP_PROXY，
        # 一绕代理 Host 头就变了，桥接只认 127.0.0.1:<port>，会直接 403
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=20) as r:
            return {'ok': True, 'status': r.status, 'body': r.read().decode('utf-8', 'replace')[:200]}

    # 桥接刚重启那几秒会回 403/500，等一下再试一次就过去了
    last = ''
    for attempt in (0, 1, 2):
        try:
            return once()
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode('utf-8', 'replace')[:200]
            except Exception:
                detail = ''
            last = 'HTTP %s %s' % (e.code, detail)
            time.sleep(1.0 + attempt)
        except Exception as e:
            last = str(e)
            time.sleep(1.0 + attempt)

    return {'ok': False, 'error': last}


# ── 投递层：默认不依赖任何外部服务 ────────────────────────────────────────
# 别人装上这个插件时不会有 qq-bridge。所以默认只写本地日志，
# 想推到手机就填一个 webhook，桥接只是其中一种可选通道。

def log_path():
    p = os.path.join(os.path.expanduser('~'), '.kugou-listen')
    os.makedirs(p, exist_ok=True)
    return os.path.join(p, 'listening.log')


def deliver_log(text):
    from datetime import datetime
    try:
        with open(log_path(), 'a', encoding='utf-8') as f:
            f.write('[%s]\n%s\n\n' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), text))
        return {'ok': True, 'path': log_path()}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def deliver_webhook(text):
    import urllib.error
    import urllib.request
    url = (load_cfg().get('webhook_url') or '').strip()
    if not url:
        return {'ok': False, 'error': '没配 webhook_url'}
    body = json.dumps({'text': text, 'content': text, 'msg': text,
                       'title': '酷狗在放', 'body': text}).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return {'ok': True, 'status': r.status}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def deliver(text):
    """按配置把一段话投出去。默认只有 log，永远可用。"""
    chans = load_cfg().get('deliver') or ['log']
    if isinstance(chans, str):
        chans = [chans]
    out = {}
    if 'log' in chans:
        out['log'] = deliver_log(text)
    if 'webhook' in chans:
        out['webhook'] = deliver_webhook(text)
    if 'qqbridge' in chans:
        out['qqbridge'] = qq_send(text)
    if not out:
        out['log'] = deliver_log(text)
    return out


@mcp.tool()
async def kugou_say(text: str) -> str:
    """把一段话投给用户。按配置走通道：本地日志 / webhook / qq-bridge（默认日志，永远可用）。"""
    import asyncio
    r = await asyncio.to_thread(deliver, text)
    return json.dumps(r, ensure_ascii=False)


def _window_shot(hwnd):
    return wincap.capture(hwnd)


def _exe_path_of_pid(pid):
    """取进程可执行文件的全路径。读不到返回空串。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ''
    try:
        size = ctypes.wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ''
        return buf.value
    finally:
        k32.CloseHandle(h)


def _exe_of_pid(pid):
    """取进程的可执行文件名（小写）。不用 tasklist —— 它在有些环境里只回表头。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ''
    try:
        size = ctypes.wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ''
        return buf.value.replace('/', '\\').split('\\')[-1].lower()
    finally:
        k32.CloseHandle(h)


def all_kugou_windows():
    """酷狗所有可见的顶层窗口（合唱歌房/邀请弹窗有时候不在主窗口里）。"""
    out = []
    exe_cache = {}

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        p = pid.value
        if p not in exe_cache:
            exe_cache[p] = _exe_of_pid(p)
        # 酷狗的主进程和几个 helper 都算（kugou.exe / kugou*.exe）
        # 2026-10-28：以前用 startswith('kugou')，某些机器上 exe 名带前缀（比如改名的），
        # 整个窗口就被滤掉了。改成包含判断，宁可多认几个窗口。
        if 'kugou' not in exe_cache[p].lower():
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        # 邀请弹窗、菜单弹窗往往很小、也没有标题，别把它们漏掉
        # （阈值从 120×90 放宽到 60×40，2026-10-28）
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left >= 60 and r.bottom - r.top >= 40:
            out.append((hwnd, r.left, r.top, r.right, r.bottom, title))
        return True

    EnumWindows(EnumProc(cb), 0)
    return out


def uia_find_invite():
    """用控件名找一起听邀请弹窗（不截图）。找不到返回 None。

    邀请弹窗里有两个按钮：「接受」和「拒绝」（有时叫「同意」/「忽略」）。
    只要同时看到「接受」和邀请相关文字，就认为真有邀请 ——
    这样不会因为歌词里出现「接受」两个字而误会。
    """
    if not uia_available():
        return None
    try:
        items = uia_points()
    except Exception:
        return None

    accept = decline = None
    invite_text = ''
    for nm, t, x, y, c, h in items:
        if nm in ('接受', '同意') and accept is None:
            accept = (nm, t, x, y, c, h)
        elif nm in ('拒绝', '忽略', '稍后') and decline is None:
            decline = (nm, t, x, y, c, h)
        elif ('邀请' in nm and '一起听' in nm) or '邀请你' in nm:
            invite_text = nm

    if accept and (invite_text or decline):
        out = {'found': True, 'kind': 'accept', 'via': 'uia',
               'text': invite_text or accept[0],
               'x': accept[2], 'y': accept[3],
               'control': accept[4]}
        if decline:
            out['decline'] = {'x': decline[2], 'y': decline[3], 'control': decline[4]}
        return out
    return None


def _ocr_shot_enhanced(shot):
    """认一张窗口截图。先按原样认，认不到字再灰度+放大，还是不行就反相再认。

    邀请弹窗常常是「深色底 + 浅灰字」，普通 OCR 对那种对比度直接读空。
    返回格式跟 winocr 一致（都有 'lines'）。
    """
    from PIL import ImageOps

    res = winocr.recognize_pil_sync(shot, 'zh-Hans')
    if res.get('lines'):
        return res

    gray = ImageOps.autocontrast(shot.convert('L'))
    for variant in (gray, ImageOps.invert(gray)):
        big = variant.resize((variant.width * 2, variant.height * 2), Image.LANCZOS)
        r2 = winocr.recognize_pil_sync(big, 'zh-Hans')
        if r2.get('lines'):
            # 坐标是放大过的，缩回去，调用方按窗口像素算
            for line in r2['lines']:
                for w in line.get('words', []):
                    b = w.get('bounding_rect', {})
                    for k in ('x', 'y', 'width', 'height'):
                        if k in b:
                            b[k] = b[k] / 2
            return r2
    return res


def find_invite():
    """找一起听邀请：先走 UI 自动化（准），不行再退回截图认字。"""
    hit = uia_find_invite()
    if hit:
        return hit

    wins = all_kugou_windows()
    if not wins:
        return {'found': False, 'error': '酷狗没开'}

    for hwnd, left, top, right, bottom, title in wins:
        if '桌面歌词' in title:
            continue
        shot = _window_shot(hwnd)
        if shot is None:
            continue
        res = _ocr_shot_enhanced(shot)

        hits = []
        inviter = False
        for line in res.get('lines', []):
            t = line.get('text', '').replace(' ', '')
            if not t:
                continue
            # ⚠️ 歌词里也会出现「接受」（「她会接受我吗」），所以「邀请」的上下文必须先成立
            if '邀请你一起听' in t or ('邀请' in t and '一起听' in t):
                inviter = True
            if ('一起听' in t) or ('邀请' in t) or ('接受' in t) or ('同意' in t):
                for w in line.get('words', []):
                    r = w.get('bounding_rect', {})
                    hits.append((t, r.get('x', 0), r.get('y', 0), r.get('width', 0), r.get('height', 0)))

        for t, x, y, w, h in hits:
            if ('接受' in t or '同意' in t) and inviter:
                # PrintWindow 出来的坐标就是窗口像素；加上窗口左上角即屏幕坐标
                return {'found': True, 'kind': 'accept', 'text': t, 'window': title,
                        'x': int(left + x + w / 2), 'y': int(top + y + h / 2)}
        if inviter and hits:
            t, x, y, w, h = hits[0]
            return {'found': True, 'kind': 'notice', 'text': t,
                    'x': int(left + x + w / 2), 'y': int(top + y + h / 2)}

    # 兜底：邀请弹窗是附属窗口，EnumWindows 拿不到 —— 直接整屏认字
    return _find_invite_on_screen()


def _find_invite_on_screen():
    from PIL import ImageGrab
    shot = ImageGrab.grab()
    res = winocr.recognize_pil_sync(shot, 'zh-Hans')

    inviter = False
    accept = None
    for line in res.get('lines', []):
        t = line.get('text', '').replace(' ', '')
        if '邀请你一起听' in t or '邀请你' in t:
            inviter = True
        if '接受' in t or '同意' in t:
            for w in line.get('words', []):
                r = w.get('bounding_rect', {})
                accept = (t, r.get('x', 0) + r.get('width', 0) / 2,
                          r.get('y', 0) + r.get('height', 0) / 2)

    if accept and inviter:
        return {'found': True, 'kind': 'accept', 'text': accept[0], 'via': 'screen',
                'x': int(accept[1]), 'y': int(accept[2])}
    if inviter:
        return {'found': True, 'kind': 'notice', 'text': '邀请你一起听'}
    return {'found': False}


def click_at(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.2)
    user32.mouse_event(0x02, 0, 0, 0, 0)
    time.sleep(0.06)
    user32.mouse_event(0x04, 0, 0, 0, 0)


@mcp.tool()
async def kugou_invite_check() -> str:
    """看看酷狗有没有弹一起听邀请。只读，不点。"""
    import asyncio
    r = await asyncio.to_thread(find_invite)
    return json.dumps(r, ensure_ascii=False)


def _ocr_screen():
    from PIL import ImageGrab
    return winocr.recognize_pil_sync(ImageGrab.grab(), 'zh-Hans')


def _ocr_screen_enhanced():
    """整屏再认一遍，专治「深色弹窗 + 灰字」——普通 OCR 对那种对比度直接读空。

    做法：灰度 → 自动对比度 → 放大两倍；偏暗的图再反相一次，两种都试。
    返回的坐标已经是屏幕坐标（放大过，所以除以 2）。
    """
    from PIL import Image, ImageGrab, ImageOps

    shot = ImageGrab.grab()
    base = ImageOps.autocontrast(shot.convert('L'))

    variants = [base]
    try:
        import numpy as np
        if float(np.asarray(base).mean()) < 110:      # 整体偏暗 → 再来一份反相的
            variants.append(ImageOps.invert(base))
    except Exception:
        pass

    out = []
    seen = set()
    for img in variants:
        big = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        res = winocr.recognize_pil_sync(big, 'zh-Hans')
        for line in res.get('lines', []):
            t = _norm(line.get('text', ''))
            if not t or t in seen:
                continue
            seen.add(t)
            words = []
            for w in line.get('words', []):
                r = w.get('bounding_rect', {})
                words.append((r.get('x', 0) / 2, r.get('y', 0) / 2))
            if words:
                out.append((t, words))
    return out


def _find_text(needle, res, exclude=()):
    """在整屏 OCR 结果里找一个词，返回屏幕坐标中心。"""
    for line in res.get('lines', []):
        t = line.get('text', '').replace(' ', '')
        if needle in t and not any(x in t for x in exclude):
            for w in line.get('words', []):
                r = w.get('bounding_rect', {})
                return (int(r.get('x', 0) + r.get('width', 0) / 2),
                        int(r.get('y', 0) + r.get('height', 0) / 2), t)
    return None


# 2026-10-28：一起听的「退出」不做了。
# 房间面板是没有标题的独立窗口，「退出」还在面板左下角，点完还有一层确认框；
# 靠认字点击命中率太低，做出来只会时灵时不灵。用前必看里已经写明这条限制。
# 替代做法：房主是自己人时，让她把音乐暂停，房间就静下来了。

VK_MEDIA_PLAY_PAUSE = 0xB3


def pause_music():
    """按一下媒体键（播放/暂停），再回读状态确认它真的变了。"""
    before = read_now(False).get('playing')
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 2, 0)
    time.sleep(1.8)
    after = read_now(False).get('playing')
    return {'ok': before != after, 'before': before, 'after': after,
            'note': '变了就说明按到了；没变说明这台机器上媒体键没送到酷狗'}


@mcp.tool()
async def kugou_pause() -> str:
    """暂停（或继续）当前播放。用媒体键，不动鼠标也不截图。"""
    import asyncio
    return json.dumps(await asyncio.to_thread(pause_music), ensure_ascii=False)


VK_MEDIA_NEXT = 0xB0
VK_MEDIA_PREV = 0xB1


def _tap_media_key(vk):
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(vk, 0, 2, 0)


def skip_track(back=False):
    """切歌：按媒体键，再回读歌名确认真的换了。"""
    before = read_now(False).get('song')
    _tap_media_key(VK_MEDIA_PREV if back else VK_MEDIA_NEXT)
    time.sleep(2.0)
    after = read_now(False).get('song')
    return {'ok': before != after, 'before': before, 'after': after,
            'note': '歌名变了才算换成功；没变说明媒体键没送到酷狗'}


@mcp.tool()
async def kugou_next() -> str:
    """下一首。"""
    import asyncio
    return json.dumps(await asyncio.to_thread(skip_track, False), ensure_ascii=False)


@mcp.tool()
async def kugou_prev() -> str:
    """上一首。"""
    import asyncio
    return json.dumps(await asyncio.to_thread(skip_track, True), ensure_ascii=False)


@mcp.tool()
async def kugou_invite_accept() -> str:
    """接受一起听邀请（点那个「接受」）。没有邀请就什么都不点。"""
    import asyncio

    def do():
        r = find_invite()
        if not r.get('found'):
            return {'ok': False, 'reason': '没看到邀请', **r}
        if r.get('kind') != 'accept':
            return {'ok': False, 'reason': '看到的是提示不是接受按钮', **r}
        # 有控件就调它的默认动作，比按坐标点更准
        ctl = r.get('control')
        if ctl is not None:
            try:
                ctl.Click(simulateMove=False)
                return {'ok': True, 'via': 'uia', 'clicked': [r['x'], r['y']], 'text': r.get('text')}
            except Exception:
                pass
        click_at(r['x'], r['y'])
        return {'ok': True, 'clicked': [r['x'], r['y']], 'text': r.get('text')}

    return json.dumps(await asyncio.to_thread(do), ensure_ascii=False)


# ── 认字撒点：先找到字，再点它的邻居 ─────────────────────────────────────
# 硬编码坐标在这套自绘界面上永远差十几个像素，所以坐标一律从 OCR 结果推。

import re

# ⚠️ OCR 经常把「04:53」读成「0453」（冒号丢），也会把 / 认成全角。
#    所以两边都允许没有冒号，只强制要求中间有个斜杠。
TIME_RE = re.compile(r'\d{1,2}[:：]?\d{2}\s*[/／|]\s*\d{1,2}[:：]?\d{2}')


def _words(res):
    out = []
    for line in res.get('lines', []):
        lt = line.get('text', '')
        for w in line.get('words', []):
            r = w.get('bounding_rect', {})
            out.append({'text': w.get('text', ''), 'line': lt,
                        'x': r.get('x', 0) + r.get('width', 0) / 2,
                        'y': r.get('y', 0) + r.get('height', 0) / 2,
                        'w': r.get('width', 0), 'h': r.get('height', 0)})
    return out


def find_time_bar():
    """播放条上「00:32/05:11」那段时间的坐标 (屏幕坐标)。"""
    win = kugou_window()
    if not win:
        return None
    hwnd, left, top, right, bottom, title = win
    shot = wincap.capture(hwnd)
    if shot is None:
        return None
    w, h = shot.size
    y0 = int(h * 0.80)
    strip = shot.crop((0, y0, w, h))
    strip = strip.resize((strip.width * 2, strip.height * 2), Image.LANCZOS)
    res = winocr.recognize_pil_sync(strip, 'zh-Hans')

    for line in res.get('lines', []):
        lt = line.get('text', '').replace(' ', '').replace('：', ':')
        if not TIME_RE.search(lt):
            continue
        xs, ys = [], []
        for word in line.get('words', []):
            r = word.get('bounding_rect', {})
            xs.append(r.get('x', 0) / 2)
            ys.append(r.get('y', 0) / 2 + y0)
        if xs:
            return {'x': int(left + min(xs)), 'y': int(top + sum(ys) / len(ys)), 'text': lt}
    return None


def _norm(s):
    return str(s or '').replace(' ', '').replace('\u3000', '')


def _text_matches(needle, text):
    """容错匹配：OCR 常错一个字（「邀请好友」被认成「适好友一起听」）。

    三级：包含 → 掐头/去尾再找 → 相似度够高。
    另外限制长度差，避免在一整句无关的话里凑出来。
    """
    import difflib

    n, t = _norm(needle), _norm(text)
    if not n or not t:
        return False
    if n in t:
        return True
    if abs(len(n) - len(t)) > 2:          # 长度差太多，基本不是同一个东西
        return False
    if len(n) >= 3 and n[1:] in t:        # 首字认错
        return True
    if len(n) >= 3 and n[:-1] in t:       # 末字认错
        return True
    if len(n) >= 4 and n[1:-1] in t:      # 两头都错
        return True
    return difflib.SequenceMatcher(None, n, t).ratio() >= 0.75


def _ocr_region(bbox, enhance=False):
    """只在屏幕某块区域里认字，返回 [(归一化文本, [(x,y), ...]), ...]（坐标是屏幕坐标）。

    ⚠️ 2026-10-28 踩过：全屏认字会认到我自己的对话窗口（屏幕上写着「复制链接」这几个字），
    于是点歪。凡是要点酷狗界面的地方，都必须把范围收进酷狗窗口。
    """
    from PIL import Image, ImageGrab, ImageOps

    x0, y0, x1, y1 = [int(v) for v in bbox]
    shot = ImageGrab.grab(bbox=(x0, y0, x1, y1))

    variants = []
    if enhance:
        base = ImageOps.autocontrast(shot.convert('L'))
        variants.append(base)
        try:
            import numpy as np
            if float(np.asarray(base).mean()) < 110:
                variants.append(ImageOps.invert(base))
        except Exception:
            pass
        variants = [v.resize((v.width * 2, v.height * 2), Image.LANCZOS) for v in variants]
        scale = 2
    else:
        variants = [shot]
        scale = 1

    out = []
    seen = set()
    for img in variants:
        res = winocr.recognize_pil_sync(img, 'zh-Hans')
        for line in res.get('lines', []):
            t = _norm(line.get('text', ''))
            if not t or t in seen:
                continue
            seen.add(t)
            pts = []
            for w in line.get('words', []):
                r = w.get('bounding_rect', {})
                pts.append((x0 + r.get('x', 0) / scale + r.get('width', 0) / scale / 2,
                            y0 + r.get('y', 0) / scale + r.get('height', 0) / scale / 2))
            if pts:
                out.append((t, pts))
    return out


def click_text_in_screen(needle, tries=8, gap=0.6, bbox=None, max_len=None):
    """在屏幕（或指定区域）找一段字并点它。

    普通 OCR 读空时再走一遍增强 OCR（灰度 + 对比度 + 放大，必要时反相），
    专治酷狗那种「深色弹窗 + 灰字」。
    """
    if bbox is None:
        win = kugou_window()
        if win:
            bbox = (win[1], win[2], win[3], win[4])

    for attempt in range(tries):
        if bbox:
            lines = _ocr_region(bbox)
        else:
            res = _ocr_screen()
            lines = []
            for line in res.get('lines', []):
                pts = []
                for w in line.get('words', []):
                    r = w.get('bounding_rect', {})
                    pts.append((r.get('x', 0) + r.get('width', 0) / 2,
                                r.get('y', 0) + r.get('height', 0) / 2))
                if pts:
                    lines.append((_norm(line.get('text', '')), pts))

        for lt, pts in lines:
            if max_len and len(lt) > max_len:
                continue
            if _text_matches(needle, lt):
                x = sum(p[0] for p in pts) / len(pts)
                y = sum(p[1] for p in pts) / len(pts)
                click_at(x, y)
                return {'ok': True, 'text': lt, 'x': int(x), 'y': int(y)}

        # 普通认不到 → 增强再认一次（后几次才做，省时间）
        if attempt >= 1 and bbox:
            for lt, pts in _ocr_region(bbox, enhance=True):
                if not _text_matches(needle, lt):
                    continue
                x = sum(p[0] for p in pts) / len(pts)
                y = sum(p[1] for p in pts) / len(pts)
                click_at(x, y)
                return {'ok': True, 'text': lt, 'x': int(x), 'y': int(y), 'via': 'enhanced'}

        time.sleep(gap)
    return {'ok': False, 'reason': '没找到「%s」' % needle}


def click_first_in_screen(needles, tries=8, gap=0.6, max_len=None):
    """按优先级试几个候选词，谁先认出来点谁。OCR 错字时很有用。

    max_len：只接受这么长的行。用来甩掉提示气泡那种长句——
    实测「邀请好友一起听时，其他人将跟听你的音乐哦」会被当成按钮。
    """
    last = ''
    for needle in needles:
        r = click_text_in_screen(needle, tries=tries, gap=gap, max_len=max_len)
        if r.get('ok'):
            return r
        last = r.get('reason') or last
    return {'ok': False, 'reason': '这几个都没找到：%s（最后：%s）' % ('、'.join(needles), last)}


# ── UI 自动化那一路（不截图）─────────────────────────────────────────────
# 2026-10-28 发现：酷狗把控件暴露给了系统的 UI Automation，名字都是现成的。
# 点按钮和读歌名都走这里；截图只剩「歌词」还需要（歌词是画上去的，没有控件）。

def _uia():
    import uia_kugou
    return uia_kugou


def uia_available():
    try:
        return _uia().HAVE_UIA
    except Exception:
        return False


def uia_points():
    return _uia().named(all_kugou_windows())


def uia_read():
    """从控件里读歌名和时间（不截图）。读不到就返回 {}。"""
    if not uia_available():
        return {}
    try:
        items = uia_points()
    except Exception:
        return {}

    out = {}
    for nm, t, x, y, c, h in items:
        # 时间形如 02:14/04:53
        if t == 'TextControl' and TIME_RE.fullmatch(nm.replace(' ', '').replace('：', ':')):
            out['time'] = nm
            break
    # 歌名：播放条上那个唱歌名的地方是 Hyperlink；顶上是 TextControl
    songs = [nm for nm, t, x, y, c, h in items if t in ('HyperlinkControl',) and nm and len(nm) < 60]
    if songs:
        out['song'] = songs[0]
    return out


def uia_click(name, gap=0.9, tries=3, **kw):
    """按名字点控件；点不到返回 False。"""
    if not uia_available():
        return {'ok': False, 'reason': '没装 uiautomation（pip install uiautomation）'}
    for i in range(tries):
        try:
            items = uia_points()
        except Exception as e:
            return {'ok': False, 'reason': '读控件失败：%s' % e}
        r = _uia().click(items, name, **kw)
        if r and 'error' not in r:
            time.sleep(gap)
            return {'ok': True, 'clicked': r}
        time.sleep(0.4)
    return {'ok': False, 'reason': '没找到「%s」这个控件' % name}


def _uia_escape():
    """按一下 Esc 把弹出面板收起来（收不起来也不影响结果）。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        u.keybd_event(0x1B, 0, 0, 0)
        u.keybd_event(0x1B, 0, 2, 0)
        time.sleep(0.3)
    except Exception:
        pass


def uia_room_exit():
    """退出一键听房间：开面板 → 退出 → 确认退出。

    ⚠️ 判断「在不在房间」看播放条：「正在一起听」有人，「等待好友加入」没人。
    ⚠️ 但「在房间」不等于「面板开着」——播放条上那行字一直都在，
       面板却是收起的。2026-10-28 别人的机器上就栽在这：
       看到「待好友加入」就以为面板开着，跳过展开，然后找不到「退出」。
       所以这里**无条件先展开一次**，重复展开是幂等的。
    """
    steps = []
    if not uia_available():
        return {'ok': False, 'reason': '没装 uiautomation'}

    steps.append('前台:%s' % _uia_focus())
    state = uia_room_state()
    steps.append('状态:%s' % state)

    # 不管判成什么，先把面板展开（收起的就点开，开着的再点也没事）
    _uia_ensure_playing()
    opened = _uia_open_room_panel(steps)
    steps.append('展开面板:%s' % opened)
    time.sleep(0.6)

    if not opened and state != 'in_room':
        return {'ok': True, 'already_out': True,
                'note': '不在房间里，面板也打不开', 'steps': steps}

    r = uia_click('退出', x_min=0, gap=0.6)
    steps.append('退出:%s' % r.get('ok'))
    if not r.get('ok'):
        # 面板可能又收起了：再展开一次，然后重试
        _uia_open_room_panel(steps)
        time.sleep(0.5)
        r = uia_click('退出', x_min=0, gap=0.6)
        steps.append('退出(重试):%s' % r.get('ok'))
    if not r.get('ok'):
        return {'ok': False, 'reason': '面板展开了但没找到退出按钮',
                'steps': steps, 'state': uia_room_state()}

    # 确认框：靠它自己的提示文字找到所在窗口，再点那个窗口里的按钮。
    # 用坐标猜不准 —— 面板的「退出」和确认框的按钮可能落在同一坐标上。
    # ⚠️ 确认框里的按钮叫「确定退出」，旁边是「再等一会」（2026-10-28 实测）。
    time.sleep(0.9)
    pts = uia_points()
    dlg_hwnds = {h for nm, t, x, y, c, h in pts if '退出后将回到' in nm}
    clicked = False
    if dlg_hwnds:
        for want in ('确定退出', '退出'):
            hit = [(c, x, y) for nm, t, x, y, c, h in pts if nm == want and h in dlg_hwnds]
            if not hit:
                continue
            c, x, y = hit[0]
            try:
                c.Click(simulateMove=False)
                clicked = True
                steps.append('确认退出:点了「%s」(%d,%d)' % (want, x, y))
            except Exception:
                clicked = False
            break
    if not clicked:
        r = uia_click('退出', gap=0.6, tries=1)
        clicked = bool(r.get('ok'))
        steps.append('确认退出:%s' % ('退一步也点到了' if clicked else '没弹框'))

    gone = uia_room_state() != 'in_room'
    return {'ok': gone, 'steps': steps, 'still_in_room': not gone,
            'note': '' if gone else '还显示在房间里，可能要再点一次'}


def _uia_focus():
    """把酷狗提到前台再点。

    ⚠️ 2026-10-28 踩过：UIA 的 Click 是按屏幕坐标发鼠标事件的。
    如果有别的窗口（比如 DSH 自己）压在上面，点下去就打在那个窗口上，
    表现是「控件找到了、点了、但没反应」。

    ⚠️ 也有人报过 SetForegroundWindow 直接返回失败（前台锁）。
       那就最小化再还原一次，再挂 AttachThreadInput 强行抢。
    """
    try:
        w = kugou_window()
        if not w:
            return False
        h = w[0]
        u = ctypes.windll.user32
        for attempt in (1, 2, 3):
            u.ShowWindow(h, 9)          # 先从最小化还原
            u.SetForegroundWindow(h)
            u.BringWindowToTop(h)
            time.sleep(0.35)
            if u.GetForegroundWindow() == h:
                return True
            if attempt == 2:
                # 最小化再还原：绕过前台锁的常用手法
                u.ShowWindow(h, 6)
                time.sleep(0.25)
                u.ShowWindow(h, 9)
                time.sleep(0.45)
                fg = u.GetForegroundWindow()
                k32 = ctypes.windll.kernel32
                fgt = u.GetWindowThreadProcessId(fg, 0)
                cur = k32.GetCurrentThreadId()
                u.AttachThreadInput(fgt, cur, True)
                u.SetForegroundWindow(h)
                u.BringWindowToTop(h)
                time.sleep(0.25)
                u.AttachThreadInput(fgt, cur, False)
        return u.GetForegroundWindow() == h
    except Exception:
        return False


def uia_room_state():
    """当前是什么状态。只有两种，别猜：

      in_room   房间里（不管有没有人进来）
      out       没开房

    判据：面板里出现「待好友加入」或「正在一起听」就是在房间里。
    面板没开的时候，看播放条上有没有房间相关字样。
    """
    if not uia_available():
        return 'unknown'
    names = [nm for nm, *_ in uia_points()]
    # ⚠️ 实际字样是「等待好友加入」，不是「待好友加入」（我认错一次，害得状态一直是 out）
    # 这里只认「在不在房间」，不认「面板开没开」——播放条上那行字面板收起时也在
    ROOM_MARKS = ('等待好友加入', '待好友加入', '正在一起听')
    if any(n in ROOM_MARKS for n in names):
        return 'in_room'
    return 'out'


def _uia_ensure_playing():
    """暂停时「更多」菜单里没有「一起听」，所以点之前先确保在放。"""
    d = read_now(False)
    if d.get('playing'):
        return True
    _tap_media_key(VK_MEDIA_PLAY_PAUSE)
    time.sleep(1.8)
    return bool(read_now(False).get('playing'))


def _menu_box(more_hit):
    """按「更多」按钮的位置，估出它弹出的菜单范围。

    菜单在按钮上方（酷狗是这么弹的），给一块够宽的矩形。
    只在这块里认字，不会点到别的地方。
    """
    x = more_hit.get('x') or 0
    y = more_hit.get('y') or 0
    try:
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
    except Exception:
        sw, sh = 1920, 1080
    left = max(0, x - int(sw * 0.28))
    top = max(0, y - 460)
    right = min(sw, x + 140)
    bottom = min(sh, y + 40)
    return (left, top, right, bottom)


def _ocr_click_in_menu(text, more_hit, steps):
    """UIA 看不到菜单项时的兜底：只在菜单矩形里认字并点它。

    ⚠️ 不是「截图盲点」：范围被夹在菜单里，而且点完要由调用方验状态。
    """
    box = _menu_box(more_hit)
    try:
        r = click_text_in_screen(text, tries=2, gap=0.8, bbox=box, max_len=6)
    except Exception as e:
        steps.append('菜单认字:异常(%s)' % e)
        return False
    steps.append('菜单认字:%s@%s' % ('ok' if r.get('ok') else 'miss', box))
    return bool(r.get('ok'))


def _uia_open_room_panel(steps):
    """把「一起听」面板弄出来。已经在房间里且面板开着就直接返回。"""
    # 面板开没开，只看面板里独有的东西。
    # ⚠️ 「等待好友加入」不算：那是播放条上的常驻文字，面板收起时也在（2026-10-28 踩过）
    names = [nm for nm, *_ in uia_points()]
    # ⚠️「邀请好友一起听」是**还没建房**时那个面板上的按钮，不能当「已进房」的判据
    PANEL_MARKS = ('正在一起听', '退出')
    if any(n in names for n in PANEL_MARKS):
        steps.append('面板:已开着')
        return True

    for attempt in (1, 2):
        if attempt == 2:
            _uia_ensure_playing()          # 第二轮回先让它放起来

        # 「更多」可能不止一个（同名窗口/复选框混在一起）。以前只点第一个，
        # 点到同名的别的控件就白点了。现在逐个试：点完看菜单里有没有「一起听」。
        cands = [x for x in uia_points() if x[0] == '更多']
        if not cands:
            steps.append('更多:一个都没有')
            continue
        steps.append('更多候选:%d个' % len(cands))

        more_hit = {}
        opened = False
        for idx, (nm, typ, mx, my, ctrl, hwnd) in enumerate(cands[:4]):
            try:
                ctrl.Click(simulateMove=False)
            except Exception:
                continue
            time.sleep(0.9)
            more_hit = {'x': mx, 'y': my}
            if any(n == '一起听' for n, *_ in uia_points()):
                steps.append('更多:第%d个弹出了菜单(%d,%d)' % (idx + 1, mx, my))
                opened = True
                break
            steps.append('更多:第%d个没菜单(%d,%d)' % (idx + 1, mx, my))
            _uia_escape()
            time.sleep(0.3)

        if not opened:
            # 菜单没弹出来也要给认字兜底一次机会（有的版本菜单项不在 UIA 里）
            more_hit = more_hit or {'x': cands[0][2], 'y': cands[0][3]}

        hit = uia_click('一起听', gap=1.5, tries=2)
        if hit.get('ok'):
            steps.append('一起听:ok(第%d次)' % attempt)
            time.sleep(0.8)
            return True
        steps.append('一起听:失败(第%d次, %s)' % (attempt, (hit.get('reason') or '')[:46]))

        # UIA 里根本没有这一项（别人的机器上遇到过）：只在那块菜单里认字
        if _ocr_click_in_menu('一起听', more_hit, steps):
            time.sleep(1.4)
            if uia_room_state() == 'in_room' or any(
                    n in [nm for nm, *_ in uia_points()] for n in ('等待好友加入', '待好友加入', '邀请好友一起听')):
                steps.append('一起听:认字点到了(第%d次)' % attempt)
                return True
            steps.append('一起听:认字点了但没开(%d)' % attempt)

        _uia_escape()
        time.sleep(0.5)
    return False


def uia_create_invite():
    """开房并拿到邀请链接。

    两种状态分开走：
      out       → 更多 → 一起听（会建房，面板出现「待好友加入」）→ 邀请 → 复制链接
      in_room   → 面板没开就打开 → 邀请 → 复制链接
    """
    steps = []
    if not uia_available():
        return {'ok': False, 'reason': '没装 uiautomation'}

    steps.append('前台:%s' % _uia_focus())
    state = uia_room_state()
    steps.append('状态:%s' % state)

    if not _uia_open_room_panel(steps):
        return {'ok': False, 'reason': '没能打开「一起听」面板', 'steps': steps}

    # 面板底部的按钮：没建房那一刻可能叫「邀请好友一起听」，房间已开叫「邀请」
    hit = ''
    for cand in ('邀请好友一起听', '邀请'):
        if uia_click(cand, gap=1.2, tries=2).get('ok'):
            hit = cand
            break
    steps.append('点邀请:%s' % (hit or '失败'))
    if not hit:
        return {'ok': False, 'reason': '面板里找不到邀请按钮', 'steps': steps}

    before = read_clipboard()
    r = uia_click('复制链接', gap=0.8, tries=4)
    steps.append('复制链接:%s' % r.get('ok'))
    if not r.get('ok'):
        return {'ok': False, 'reason': '没点到复制链接', 'steps': steps}

    clip = ''
    for _ in range(10):
        time.sleep(0.3)
        clip = read_clipboard()
        if clip and clip != before and 'kugou' in clip:
            break

    return {'ok': bool(clip and 'kugou' in clip), 'steps': steps,
            'state_before': state, 'clipboard': clip}


def create_invite():
    """开房间并拿到邀请链接。

    装了 uiautomation 就只走控件那条路，**不再退回截图认字**：
    2026-10-28 实测，UIA 偶尔一次不成（面板刚好自动关了），
    退回认字那条路反而会把界面点乱、越修越坏。所以：
      · 有 UIA：重试两次，失败就如实报错
      · 没 UIA：才用截图认字
    """
    if uia_available():
        last = None
        for i in range(2):
            r = uia_create_invite()
            if r.get('ok'):
                r['attempt'] = i + 1
                return r
            last = r
            time.sleep(1.2)
        last['note'] = 'UIA 两次都没成；没有退回截图，免得把界面点乱'
        return last
    return _create_invite_ocr(hint='没装 uiautomation，只能用截图认字')


def _create_invite_ocr(hint=''):
    """老办法：竖点 → 一起听 → 邀请好友 → 复制链接（截图认字版）。

    ⚠️ 竖点没法直接认（它不是文字），所以按时长左边试几个偏移，
       哪个偏移能弹出带「一起听」的菜单就用哪个。
    """
    steps = []

    t = find_time_bar()
    if not t:
        return {'ok': False, 'reason': '没看到播放条上的时长，酷狗可能不在前台'}
    steps.append('time@%d,%d' % (t['x'], t['y']))

    menu_hit = None
    for dx in (38, 30, 46, 54, 62, 70):
        click_at(t['x'] - dx, t['y'])
        time.sleep(0.9)
        r = click_first_in_screen(['一起听'], tries=2, gap=0.5)
        if r.get('ok'):
            menu_hit = r
            steps.append('menu@-%d:%s' % (dx, r.get('text')))
            break
        steps.append('menu@-%d:miss' % dx)

    if not menu_hit:
        return {'ok': False, 'reason': '菜单没弹出来（试了六个偏移）', 'steps': steps}
    time.sleep(1.4)

    # 「邀请好友一起听」整句常被认错（实测认成「适好友一起听」），所以退到「好友」
    r = click_first_in_screen(['邀请好友一起听', '邀请好友', '邀请'], tries=10, gap=0.9, max_len=10)
    steps.append('invite:' + str(r.get('text') or r.get('reason')))
    if not r.get('ok'):
        return {'ok': False, 'reason': '没点到邀请好友', 'steps': steps}
    time.sleep(1.4)

    r = click_first_in_screen(['复制链接', '复制'], tries=10, gap=0.9, max_len=8)
    steps.append('copy:' + str(r.get('text') or r.get('reason')))
    if not r.get('ok'):
        return {'ok': False, 'reason': '没点到复制链接', 'steps': steps}

    time.sleep(0.8)
    return {'ok': True, 'steps': steps, 'clipboard': read_clipboard()}


def read_clipboard():
    """读剪贴板里的文本。

    ⚠️ 2026-10-28：之前直接 wstring_at(GlobalLock(h)) 会 access violation ——
    默认 restype 是 32 位 int，64 位句柄被截断，指针就是错的。
    所以这里显式声明 argtypes/restype，并在拿不到时干净地返回 ''。
    """
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.GetClipboardData.argtypes = [wintypes.UINT]
        u.GetClipboardData.restype = wintypes.HANDLE
        u.CloseClipboard.restype = wintypes.BOOL
        k.GlobalLock.argtypes = [wintypes.HANDLE]
        k.GlobalLock.restype = wintypes.LPVOID
        k.GlobalUnlock.argtypes = [wintypes.HANDLE]
        k.GlobalUnlock.restype = wintypes.BOOL

        if not u.OpenClipboard(None):
            return 'ERR 剪贴板被别的程序占着，打不开'
        try:
            h = u.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ''
            p = k.GlobalLock(h)
            if not p:
                return 'ERR GlobalLock 失败'
            try:
                return ctypes.wstring_at(p)
            finally:
                k.GlobalUnlock(h)
        finally:
            u.CloseClipboard()
    except Exception as e:
        return 'ERR %s' % e


@mcp.tool()
async def kugou_room_exit() -> str:
    """退出一键听房间（UI 自动化，点退出再确认）。"""
    import asyncio
    try:
        return json.dumps(await asyncio.to_thread(uia_room_exit), ensure_ascii=False)
    except Exception as e:
        # 永远回结构化结果：调用方拿 [object Object] 等于没有信息（2026-10-28 踩过）
        return json.dumps({'ok': False,
                           'reason': '退出时内部出错：%s: %s' % (type(e).__name__, e)},
                          ensure_ascii=False)


@mcp.tool()
async def kugou_invite_create() -> str:
    """开一个一起听房间，把邀请链接拿回来（认字撒点，不靠硬编码坐标）。"""
    import asyncio
    try:
        return json.dumps(await asyncio.to_thread(create_invite), ensure_ascii=False)
    except Exception as e:
        return json.dumps({'ok': False,
                           'reason': '开房时内部出错：%s: %s' % (type(e).__name__, e)},
                          ensure_ascii=False)


# ── 后台盯邀请 ───────────────────────────────────────────────────────────
# 我不在回合里的时候没法"决定"，所以只有两条路：按策略办，或者推消息把人叫醒。
# 策略：auto=看到就接 / ask=推消息问 / off=不管

import os
import threading

CFG_DIR = os.path.join(os.path.expanduser('~'), '.kugou-listen')
CFG_PATH = os.path.join(CFG_DIR, 'config.json')
UI_PATH = os.path.join(CFG_DIR, 'settings.json')
DEFAULT_CFG = {'invite': 'ask', 'watch_on': True, 'poll_seconds': 15, 'notify': True,
               'push_lyric': False, 'push_interval': 90,
               'deliver': ['log'], 'webhook_url': '', 'owner_qq': '',
               # 听力：设置页填的那三项 + 开关。少了这几个键，load_cfg 会把它们丢掉
               'hearing': False, 'audio_api_key': '', 'audio_api_url': '', 'audio_model': '',
               'think_on': True, 'think_every_min': 10,
}

_state = {'last_sig': '', 'last_at': 0.0, 'last_line': '', 'last_push': 0.0, 'last_song': ''}


def _read_json(path):
    try:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception:
        return {}


def _usable(v):
    """只认能用的值。设置页那边可能塞进来 {} 这种 loader 占位对象，一律丢掉。"""
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        return v != ''
    if isinstance(v, list):
        return all(isinstance(x, str) for x in v) and len(v) > 0
    return False


def load_cfg():
    out = dict(DEFAULT_CFG)
    # 手改的 config.json 优先；设置页写的 settings.json 覆盖它
    for path in (CFG_PATH, UI_PATH):
        for k, v in _read_json(path).items():
            if k in DEFAULT_CFG and _usable(v):
                out[k] = v
    return out


def save_cfg(cfg):
    os.makedirs(CFG_DIR, exist_ok=True)
    with open(CFG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


def _watch_log(msg):
    """把每一轮扫描记一行，方便回答「到底有没有在扫」。

    文件超过 200 KB 就删掉重来，避免无限长。
    """
    try:
        p = os.path.join(CFG_DIR, 'watch.log')
        try:
            if os.path.getsize(p) > 200 * 1024:
                os.remove(p)
        except Exception:
            pass
        from datetime import datetime
        with open(p, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (datetime.now().strftime('%H:%M:%S'), msg))
    except Exception:
        pass


def watch_loop():
    # 后台线程里用 UI 自动化，必须先在这个线程初始化 COM，否则静默失败
    try:
        import uia_kugou
        uia_kugou.init_thread()
    except Exception as e:
        _watch_log('UIA 线程初始化失败：%s（会退回截图认字）' % e)

    while True:
        try:
            cfg = load_cfg()
            if cfg.get('watch_on', True) and cfg.get('invite') != 'off':
                r = find_invite()
                sig = (r.get('kind'), r.get('text'))
                _watch_log('scan: %s' % ('找到邀请 %s [%s]' % (r.get('text'), r.get('via', 'ocr'))
                                        if r.get('found') else '没有邀请'))
                if r.get('found') and sig != _state['last_sig']:
                    _state['last_sig'] = sig
                    _state['last_at'] = time.time()
                    if cfg.get('invite') == 'auto' and r.get('kind') == 'accept':
                        ctl = r.get('control')
                        done = False
                        if ctl is not None:
                            try:
                                ctl.Click(simulateMove=False)
                                done = True
                            except Exception:
                                done = False
                        if not done:
                            click_at(r['x'], r['y'])
                        _watch_log('已接受邀请（%s）' % ('控件' if done else '坐标'))
                        if cfg.get('notify'):
                            qq_send('一起听的邀请我接了。')
                    elif cfg.get('notify'):
                        qq_send('酷狗弹了个一起听的邀请，看到「%s」。回我一句「接」我就点。' % (r.get('text') or ''))
                elif not r.get('found'):
                    _state['last_sig'] = ''
            if cfg.get('push_lyric'):
                d = read_now(False)
                if d.get('ok'):
                    song = d.get('song') or ''
                    lines = d.get('lines') or []
                    line = lines[-1] if lines else ''
                    now = time.time()
                    gap = max(30, int(cfg.get('push_interval') or 90))
                    changed = line and line != _state['last_line']
                    song_changed = song != _state['last_song']
                    if changed and (now - _state['last_push'] >= gap or song_changed):
                        _state['last_line'] = line
                        _state['last_song'] = song
                        _state['last_push'] = now
                        qq_send('%s\n\n「%s」' % (song, line))
                    elif song_changed and song:
                        _state['last_song'] = song
                        _state['last_line'] = ''
        except Exception as e:
            # 以前这里是 pass —— 结果线程挂了也看不出来。现在至少记一行
            _watch_log('出错：%s: %s' % (type(e).__name__, str(e)[:120]))
        time.sleep(max(5, int(load_cfg().get('poll_seconds') or 15)))


@mcp.tool()
async def kugou_invite_policy(mode: str = '') -> str:
    """查看或设置邀请策略。mode: auto=看到就接 / ask=推消息问 / off=不管（留空只查看）。"""
    import asyncio

    def do():
        cfg = load_cfg()
        if mode in ('auto', 'ask', 'off'):
            cfg['invite'] = mode
            save_cfg(cfg)
        return cfg

    return json.dumps(await asyncio.to_thread(do), ensure_ascii=False)


def _hear(seconds=15):
    """真的听一段：录环回音频 → 交给配置里的多模态模型 → 返回文字描述。"""
    cfg = load_cfg()
    if not cfg.get('hearing'):
        return {'ok': False, 'reason': '听力没开（设置里勾「开启真正的听力能力」）'}
    key = (cfg.get('audio_api_key') or '').strip()
    url = (cfg.get('audio_api_url') or '').strip()
    model = (cfg.get('audio_model') or '').strip()
    if not key or not url or not model:
        return {'ok': False, 'reason': 'API Key / URL / 模型 没填全'}

    import tempfile
    import rec

    out = os.path.join(tempfile.gettempdir(), 'kugou_hear.wav')

    # ⚠️ 2026-10-28 实测：没有声音时 WASAPI 的 stream.read 会阻塞很久（112 秒），
    #    光在里面加截止时间没用。所以录音放进独立线程，超时就放弃这轮。
    box = {}

    def _do_record():
        try:
            box['info'] = rec.record(seconds, out)
        except Exception as e:
            box['err'] = str(e)

    th = threading.Thread(target=_do_record, daemon=True)
    th.start()
    th.join(seconds * 2 + 8)

    if th.is_alive():
        return {'ok': False, 'reason': '录音卡住了（设备没响应，可能正在暂停或没有输出）'}
    if 'err' in box:
        return {'ok': False, 'reason': '录音失败：%s' % box['err']}
    info = box.get('info') or {}

    # 静音就别浪费一次接口调用：暂停时录到的就是静音，交给模型它也听不出东西
    if float(info.get('rms') or 0) < 0.004:
        return {'ok': False, 'reason': '录到的是静音（音乐可能在暂停或音量太小）',
                'rms': info.get('rms')}

    import base64
    import urllib.error
    import urllib.request

    with open(out, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()

    # ⚠️ 2026-10-28：以前这里是一组四个固定问题（人声收着吗 / 有没有喘 / 鼓还是贝斯靠前 / 紧还是松），
#    结果不管放什么歌，回来的都是同一句「人声收着…鼓更靠前…整段紧」—— 模板是我自己给的。
#    现在换成开放问法，听不出就让它说听不出。
    prompt = ('你在听一段 %d 秒的音乐，可能是纯音乐。'
              '用两三句说你实际听到的：速度快慢、都用了什么乐器、有没有人声、情绪是紧还是松。'
              '听不清楚就直说听不清楚，不要猜，也不要说好听不好听。' % seconds)

    if 'multimodal-generation' in url:
        body = {'model': model,
                'input': {'messages': [{'role': 'user', 'content': [
                    {'audio': 'data:audio/wav;base64,' + b64}, {'text': prompt}]}]}}
    else:
        body = {'model': model,
                'messages': [{'role': 'user', 'content': [
                    {'type': 'input_audio', 'input_audio': {'data': b64, 'format': 'wav'}},
                    {'type': 'text', 'text': prompt}]}]}

    req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', 'Bearer ' + key)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode('utf-8', 'replace')
    except Exception as e:
        return {'ok': False, 'reason': '调用失败：%s' % e, 'rms': info.get('rms')}

    text = ''
    try:
        d = json.loads(raw)
        text = (d['output']['choices'][0]['message']['content'][0].get('text') or '')
    except Exception:
        try:
            text = d['choices'][0]['message']['content']  # type: ignore[name-defined]
        except Exception:
            text = raw[:400]

    return {'ok': True, 'seconds': seconds, 'rms': info.get('rms'), 'text': text.strip()}


def build_report():
    """一份人能看懂、能直接贴出来的诊断报告。

    换台机器跑不通时，让对方跑 `--report` 把输出贴过来，
    基本就不用再来回问「你什么系统」「窗口多大」这种问题了。
    """
    import platform
    from datetime import datetime

    lines = []
    w = lines.append
    w('=== 酷狗一起听 诊断报告 ===')
    w('时间：%s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    w('系统：%s' % platform.platform())
    w('Python：%s（%s）' % (sys.version.split()[0], sys.executable))
    w('脚本：%s' % os.path.abspath(__file__))
    w('')

    # 依赖
    try:
        d = deps_check()
        w('--- 依赖 ---')
        w('结论：%s' % ('齐全' if d.get('ok') else '缺东西'))
        for k, v in (d.get('versions') or {}).items():
            w('  %-14s %s' % (k, v))
        if d.get('missing'):
            w('  缺：%s' % '、'.join(d['missing']))
        w('  uiautomation：%s' % ('可用' if d.get('uia_ok') else '不可用'))
        w('  系统 OCR 语言：%s' % (', '.join(d.get('ocr_languages') or []) or '查不到'))
        for a in (d.get('advice') or []):
            w('  建议：%s' % a)
    except Exception as e:
        w('依赖体检失败：%s' % e)
    w('')

    # 屏幕与窗口
    try:
        import ctypes
        u = ctypes.windll.user32
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        w('--- 屏幕与窗口 ---')
        w('  主屏：%dx%d' % (u.GetSystemMetrics(0), u.GetSystemMetrics(1)))
        try:
            dpi = u.GetDpiForSystem()
            w('  DPI：%d（缩放 %d%%）' % (dpi, round(dpi / 96 * 100)))
        except Exception:
            w('  DPI：读不到')
        wins = all_kugou_windows()
        w('  酷狗窗口数：%d' % len(wins))
        for hwnd, l, t, r, b, title in wins[:4]:
            w('    %dx%d @(%d,%d) 标题=%r' % (r - l, b - t, l, t, title[:40]))
        if not wins:
            w('    （一个都没找到，酷狗是不是没开？）')
    except Exception as e:
        w('窗口检查失败：%s' % e)
    w('')

    # 读一次
    try:
        w('--- 读一次 ---')
        r = read_now(False)
        w('  ok=%s song=%r playing=%s' % (r.get('ok'), r.get('song'), r.get('playing')))
        w('  歌词行数：%d' % len(r.get('lines') or []))
        if r.get('error'):
            w('  错误：%s' % r.get('error'))
    except Exception as e:
        w('读一次失败：%s' % e)
    w('')

    # 设置与日志
    try:
        cfg = load_cfg()
        w('--- 设置 ---')
        w('  think_on=%s think_every_min=%s hearing=%s deliver=%s'
          % (cfg.get('think_on'), cfg.get('think_every_min'), cfg.get('hearing'), cfg.get('deliver')))
        w('  owner_qq=%s' % ('已填' if cfg.get('owner_qq') else '空'))
        w('  配置文件：%s / %s' % (CFG_PATH, UI_PATH))
    except Exception as e:
        w('读设置失败：%s' % e)

    try:
        p = os.path.join(CFG_DIR, 'watch.log')
        if os.path.exists(p):
            w('--- 最近扫描（watch.log 尾 5 行）---')
            with open(p, encoding='utf-8', errors='replace') as f:
                tail = f.readlines()[-5:]
            for ln in tail:
                w('  ' + ln.rstrip())
    except Exception:
        pass

    w('')
    w('把上面整段贴回去就行。')
    return '\n'.join(lines)


def deps_check():
    """体检：这台机器能不能跑这套东西。缺什么、什么版本、怎么补，都说清楚。"""
    import platform
    from importlib.metadata import version, PackageNotFoundError

    out = {'python': sys.version.split()[0], 'platform': platform.platform(),
           'ok': True, 'missing': [], 'versions': {}, 'advice': []}

    if sys.version_info < (3, 9):
        out['ok'] = False
        out['advice'].append('Python 太旧，至少要 3.9（你现在 %s）' % out['python'])

    need = ['pillow', 'numpy', 'pyaudiowpatch', 'winocr', 'mcp', 'uiautomation']
    for pkg in need:
        try:
            v = version(pkg)
            out['versions'][pkg] = v
        except PackageNotFoundError:
            out['missing'].append(pkg)

    # uiautomation 缺了不致命（读歌读词照旧），但点按钮那几个功能会用不了，
    # 所以要单独标出来，让设置页能显示「去下载」。
    out['uia_ok'] = 'uiautomation' not in out['missing']
    if not out['uia_ok']:
        out['advice'].append(
            '缺 uiautomation（开房 / 邀请 / 退出房间要用它）：'
            'pip install uiautomation　或打开 https://pypi.org/project/uiautomation/')

    # ⭐ 系统 OCR 的中文语言包：没装的话 winocr 认不出中文，读词这条直接废
    #    （很多精简版 Windows / 英文系统上是缺的，比缺 pip 包更常见）
    try:
        from winrt.windows.media.ocr import OcrEngine
        tags = [l.language_tag.lower() for l in OcrEngine.available_recognizer_languages]
        out['ocr_languages'] = tags
        has_zh = any(t.startswith('zh') for t in tags)
        out['ocr_chinese'] = has_zh
        if not has_zh:
            out['ok'] = False
            out['advice'].append(
                '系统里没有中文 OCR 语言包（现有：%s）。这条只能用系统 OCR 认中文，没有就只读得到歌名。'
                '装法：设置 → 时间和语言 → 语言 → 添加「中文(简体)」并勾上「可选功能 → 光学字符识别」'
                % (', '.join(tags) or '无'))
    except Exception as e:
        out['ocr_chinese'] = None
        out['advice'].append('查不了系统 OCR 语言包（%s），装完先跑 --selfcheck 看歌词那步过不过' % e)

    if 'pyaudiowpatch' in out['missing']:
        out['advice'].append('缺 pyaudiowpatch：pip install pyaudiowpatch（只有录音需要它，不装也能读屏）')
    if 'winocr' in out['missing'] or 'pillow' in out['missing']:
        out['advice'].append('缺 winocr / pillow：pip install winocr pillow（读屏必需）')
    if 'numpy' in out['missing']:
        out['advice'].append('缺 numpy：pip install numpy')

    # mcp 必须 1.x：2.x 把 FastMCP 改名了，这个脚本会起不来
    mv = out['versions'].get('mcp', '')
    if not mv:
        out['advice'].append('缺 mcp：pip install "mcp<2"（必须是 1.x）')
    elif not mv.startswith('1.'):
        out['ok'] = False
        out['advice'].append('mcp 是 %s，必须 1.x：pip install "mcp<2"' % mv)

    # uiautomation 是可选（只是点不了按钮），不能让整份体检判成「不 ok」
    hard_missing = [m for m in out['missing'] if m != 'uiautomation']
    if hard_missing or not out['ok']:
        out['ok'] = False
        out['advice'].append('一次装齐：pip install pillow numpy pyaudiowpatch winocr uiautomation "mcp<2"')

    if sys.platform != 'win32':
        out['ok'] = False
        out['advice'].append('这套只用 Windows 的接口（PrintWindow / 系统 OCR / WASAPI），换平台跑不了')

    return out


@mcp.tool()
async def kugou_hear(seconds: int = 15) -> str:
    """录一小段正在放的声音，交给设置里配的多模态模型，返回它听到的描述。"""
    import asyncio
    return json.dumps(await asyncio.to_thread(_hear, seconds), ensure_ascii=False)


if __name__ == '__main__':
    # 下面这几个开关是给「命令行直接用」准备的：
    # MCP 那条路偶尔会卡（客户端夹在两个 server 之间），命令行永远通。
    # --say "文本"：投递一段话
    if '--say' in sys.argv:
        i = sys.argv.index('--say')
        text = sys.argv[i + 1] if len(sys.argv) > i + 1 else ''
        print(json.dumps(deliver(text), ensure_ascii=False))
        sys.exit(0)
    # --hear 15：录一小段交给多模态模型
    if '--hear' in sys.argv:
        i = sys.argv.index('--hear')
        secs = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 15
        print(json.dumps(_hear(secs), ensure_ascii=False))
        sys.exit(0)
    # --exit：退出一键听房间（UI 自动化，不截图）
    if '--exit' in sys.argv:
        print(json.dumps(uia_room_exit(), ensure_ascii=False))
        sys.exit(0)
    # --report：出一份诊断包，让别人一次就能把环境说清楚
    #           跑不通时把它贴回来，我不用猜
    if '--report' in sys.argv:
        print(build_report())
        sys.exit(0)
    # --invite：开房并把邀请链接放到剪贴板
    if '--invite' in sys.argv:
        print(json.dumps(create_invite(), ensure_ascii=False))
        sys.exit(0)
    # --tick：定时轮开头用。它替定时任务判断「这一轮该不该说话」，
    #        把开关真正接上（以前 think_on 只在设置里躺着，没任何地方读）
    if '--tick' in sys.argv:
        cfg = load_cfg()
        d = read_now(False)
        speak = True
        why = ''
        if not cfg.get('think_on', True):
            speak = False
            why = '设置里关掉了听歌感想'
        elif not d.get('ok'):
            speak = False
            why = '读不到酷狗'
        elif not d.get('playing'):
            speak = False
            why = '没在放歌'
        elif not (d.get('song') or '').strip():
            speak = False
            why = '读不到歌名'
        d['speak'] = speak
        d['reason'] = why
        d['think_every_min'] = cfg.get('think_every_min')
        d['hearing'] = bool(cfg.get('hearing'))
        print(json.dumps(d, ensure_ascii=False))
        sys.exit(0)
    # --deps：装之前先体检，缺什么一次说清
    if '--deps' in sys.argv:
        print(json.dumps(deps_check(), ensure_ascii=False, indent=1))
        sys.exit(0)
    # --pause / --next / --prev：媒体键，不动鼠标也不截屏
    if '--pause' in sys.argv:
        print(json.dumps(pause_music(), ensure_ascii=False))
        sys.exit(0)
    if '--next' in sys.argv or '--prev' in sys.argv:
        print(json.dumps(skip_track('--prev' in sys.argv), ensure_ascii=False))
        sys.exit(0)
    # --once：给设置页那个「试一下」按钮用，读一次就退出，不起 MCP 也不起后台
    if '--once' in sys.argv:
        print(json.dumps(read_now(False), ensure_ascii=False))
        sys.exit(0)
    # --selfcheck：给设置页那个「自检」按钮用
    if '--selfcheck' in sys.argv:
        print(json.dumps(selfcheck(), ensure_ascii=False))
        sys.exit(0)
    threading.Thread(target=watch_loop, daemon=True).start()
    mcp.run()
