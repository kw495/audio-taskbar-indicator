# -*- coding: utf-8 -*-
"""
音声インジケーター (Audio Taskbar Indicator) for Windows 11

音を出しているウィンドウのタスクバーボタンに目印を付ける常駐ツール。
  - アイコン右下のバッジ（色・形を設定可能）
  - タスクバーボタンのオレンジ点灯（Windows標準の「注意を引く」表示）
  - ウィンドウタイトル先頭への記号付加

必要なライブラリ:  pip install pycaw comtypes psutil pystray Pillow
"""

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections import defaultdict
from logging.handlers import RotatingFileHandler
from xml.sax.saxutils import escape

APP_NAME = "AudioTaskbarIndicator"
APP_TITLE = "音声インジケーター"
TASK_NAME = APP_NAME
MUTEX_NAME = "Local\\AudioTaskbarIndicator_SingleInstance"
APPDATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(APPDATA_DIR, "config.json")
LOG_PATH = os.path.join(APPDATA_DIR, "log.txt")
ICON_DIR = os.path.join(APPDATA_DIR, "icons")

os.makedirs(ICON_DIR, exist_ok=True)

log = logging.getLogger(APP_NAME)


def setup_logging():
    log.setLevel(logging.INFO)
    h = RotatingFileHandler(LOG_PATH, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(h)


# ---------------------------------------------------------------------------
# Win32 API（ctypes）
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

LRESULT = ctypes.c_ssize_t
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("hwnd", wt.HWND),
        ("dwFlags", wt.DWORD),
        ("uCount", wt.UINT),
        ("dwTimeout", wt.DWORD),
    ]


user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.EnumChildWindows.argtypes = [wt.HWND, WNDENUMPROC, wt.LPARAM]
user32.EnumChildWindows.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wt.LONG
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND
user32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
user32.FlashWindowEx.restype = wt.BOOL
user32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
                                       ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = LRESULT
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadImageW.restype = wt.HANDLE
user32.DestroyIcon.argtypes = [wt.HICON]
user32.DestroyIcon.restype = wt.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
user32.MessageBoxW.restype = ctypes.c_int
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
shell32.IsUserAnAdmin.argtypes = []
shell32.IsUserAnAdmin.restype = wt.BOOL
shell32.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = wt.HINSTANCE

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
GW_OWNER = 4
DWMWA_CLOAKED = 14
DWM_CLOAKED_APP = 0x1
DWM_CLOAKED_INHERITED = 0x4
FLASHW_STOP = 0
FLASHW_TRAY = 0x2
WM_SETTEXT = 0x000C
SMTO_ABORTIFHUNG = 0x2
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
SM_CXSMICON = 49
ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = 0x08000000


def msgbox(text, flags=0x40):
    user32.MessageBoxW(None, text, APP_TITLE, flags)


# サードパーティ製ライブラリ（pythonw では例外が見えないのでメッセージで知らせる）
try:
    import comtypes
    from comtypes import GUID, COMMETHOD, HRESULT, IUnknown, CLSCTX_ALL
    import psutil
    import pystray
    from PIL import Image, ImageDraw, ImageTk
    from pycaw.pycaw import (AudioUtilities, IAudioMeterInformation,
                             IAudioSessionControl2, IAudioSessionManager2)
except ImportError as _e:
    msgbox("必要なライブラリが見つかりません:\n%s\n\nコマンドプロンプトで次を実行してください:\n"
           "py -m pip install pycaw comtypes psutil pystray Pillow" % _e, 0x10)
    sys.exit(1)

try:
    from pycaw.pycaw import IMMDeviceEnumerator
except ImportError:  # 古い pycaw
    IMMDeviceEnumerator = None

import tkinter as tk
from tkinter import ttk, colorchooser, messagebox
from tkinter import font as tkfont


def is_admin():
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def hwnd_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def get_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def set_title(hwnd, text):
    buf = ctypes.create_unicode_buffer(text)
    res = ctypes.c_size_t()
    r = user32.SendMessageTimeoutW(hwnd, WM_SETTEXT, 0, ctypes.addressof(buf),
                                   SMTO_ABORTIFHUNG, 300, ctypes.byref(res))
    return bool(r)


def get_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def cloaked_flags(hwnd):
    val = ctypes.c_int(0)
    hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    return val.value if hr == 0 else 0


def flash_window(hwnd, flags, count):
    fi = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, flags, count, 0)
    user32.FlashWindowEx(ctypes.byref(fi))


def uwp_content_pid(frame_hwnd, frame_pid):
    """ApplicationFrameHost の枠ウィンドウから、中身のアプリのPIDを探す"""
    found = []

    def cb(child, _lp):
        try:
            p = hwnd_pid(child)
            if p and p != frame_pid:
                found.append(p)
        except Exception:
            pass
        return True

    user32.EnumChildWindows(frame_hwnd, WNDENUMPROC(cb), 0)
    return found[0] if found else None


def enum_taskbar_windows(own_pid):
    """タスクバーに出ているトップレベルウィンドウを (hwnd, pid, title) で返す"""
    result = []

    def cb(hwnd, _lp):
        try:
            if not hwnd or not user32.IsWindowVisible(hwnd):
                return True
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
            if ex & WS_EX_TOOLWINDOW:
                return True
            if user32.GetWindow(hwnd, GW_OWNER) and not (ex & WS_EX_APPWINDOW):
                return True
            if cloaked_flags(hwnd) & (DWM_CLOAKED_APP | DWM_CLOAKED_INHERITED):
                return True
            title = get_title(hwnd)
            if not title:
                return True
            pid = hwnd_pid(hwnd)
            if pid == own_pid:
                return True
            if get_class(hwnd) == "ApplicationFrameWindow":
                pid = uwp_content_pid(hwnd, pid) or pid
            result.append((int(hwnd), pid, title))
        except Exception:
            log.exception("ウィンドウ列挙中のエラー")
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return result


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
SHAPES = {"speaker": "スピーカー", "circle": "丸", "square": "四角"}

DEFAULTS = {
    "badge_enabled": True,
    "badge_color": "#FFC800",
    "badge_shape": "speaker",
    "flash_enabled": True,
    "flash_count": 2,
    "title_enabled": False,
    "title_prefix": "🔊 ",
    "threshold": 0.01,      # 0〜1（ピーク値）
    "hold_seconds": 1.5,
    "interval_ms": 250,
    "exclude": [],
    "exclude_devices": [],   # 監視しない出力デバイスのID
    "device_names": {},      # 除外中デバイスの表示名（未接続時の表示用）
    "paused": False,
}


class ConfigStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.data = dict(DEFAULTS)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for k in DEFAULTS:
                if k in loaded:
                    self.data[k] = loaded[k]
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("設定ファイルの読み込みに失敗。既定値を使います")

    def get(self):
        with self.lock:
            return dict(self.data), self.version

    def update(self, **kw):
        with self.lock:
            self.data.update(kw)
            self.version += 1
            data = dict(self.data)
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            log.exception("設定ファイルの保存に失敗")


# ---------------------------------------------------------------------------
# アイコン画像
# ---------------------------------------------------------------------------
def hex_to_rgb(h):
    h = h.lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return (255, 200, 0)


def make_badge_image(color, shape, size=256):
    rgb = hex_to_rgb(color)
    lum = (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255
    fg = (30, 30, 30, 255) if lum > 0.6 else (255, 255, 255, 255)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    pad = s * 0.04
    ow = max(1, int(s * 0.06))
    box = [pad, pad, s - pad, s - pad]
    if shape == "square":
        d.rounded_rectangle(box, radius=s * 0.2, fill=rgb + (255,), outline=(30, 30, 30, 255), width=ow)
    else:
        d.ellipse(box, fill=rgb + (255,), outline=(30, 30, 30, 255), width=ow)
    if shape == "speaker":
        d.rectangle([s * 0.22, s * 0.40, s * 0.36, s * 0.60], fill=fg)
        d.polygon([(s * 0.34, s * 0.40), (s * 0.52, s * 0.25), (s * 0.52, s * 0.75), (s * 0.34, s * 0.60)], fill=fg)
        w = max(1, int(s * 0.065))
        d.arc([s * 0.40, s * 0.34, s * 0.66, s * 0.66], -55, 55, fill=fg, width=w)
        d.arc([s * 0.38, s * 0.22, s * 0.80, s * 0.78], -50, 50, fill=fg, width=w)
    return img


def load_badge_hicon(cfg):
    color = cfg["badge_color"]
    shape = cfg["badge_shape"] if cfg["badge_shape"] in SHAPES else "speaker"
    path = os.path.join(ICON_DIR, "badge_%s_%s.ico" % (shape, color.lstrip("#").upper()))
    if not os.path.exists(path):
        make_badge_image(color, shape).save(
            path, format="ICO", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48)])
    size = user32.GetSystemMetrics(SM_CXSMICON) or 16
    h = user32.LoadImageW(None, path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
    if not h:
        raise OSError("LoadImageW 失敗 (%d)" % ctypes.get_last_error())
    return h


def make_tray_image(cfg):
    color = "#9E9E9E" if cfg["paused"] else cfg["badge_color"]
    return make_badge_image(color, "speaker", 64)


# ---------------------------------------------------------------------------
# ITaskbarList3（オーバーレイアイコン用）
# ---------------------------------------------------------------------------
class ITaskbarList(IUnknown):
    _iid_ = GUID("{56FDF342-FD6D-11d0-958A-006097C9A090}")
    _methods_ = [
        COMMETHOD([], HRESULT, "HrInit"),
        COMMETHOD([], HRESULT, "AddTab", (["in"], wt.HWND, "hwnd")),
        COMMETHOD([], HRESULT, "DeleteTab", (["in"], wt.HWND, "hwnd")),
        COMMETHOD([], HRESULT, "ActivateTab", (["in"], wt.HWND, "hwnd")),
        COMMETHOD([], HRESULT, "SetActiveAlt", (["in"], wt.HWND, "hwnd")),
    ]


class ITaskbarList2(ITaskbarList):
    _iid_ = GUID("{602D4995-B13A-429b-A66E-1935E44F4317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "MarkFullscreenWindow",
                  (["in"], wt.HWND, "hwnd"), (["in"], wt.BOOL, "fFullscreen")),
    ]


class ITaskbarList3(ITaskbarList2):
    _iid_ = GUID("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}")
    _methods_ = [
        COMMETHOD([], HRESULT, "SetProgressValue", (["in"], wt.HWND, "hwnd"),
                  (["in"], ctypes.c_ulonglong, "done"), (["in"], ctypes.c_ulonglong, "total")),
        COMMETHOD([], HRESULT, "SetProgressState", (["in"], wt.HWND, "hwnd"), (["in"], ctypes.c_int, "flags")),
        COMMETHOD([], HRESULT, "RegisterTab", (["in"], wt.HWND, "tab"), (["in"], wt.HWND, "mdi")),
        COMMETHOD([], HRESULT, "UnregisterTab", (["in"], wt.HWND, "tab")),
        COMMETHOD([], HRESULT, "SetTabOrder", (["in"], wt.HWND, "tab"), (["in"], wt.HWND, "before")),
        COMMETHOD([], HRESULT, "SetTabActive", (["in"], wt.HWND, "tab"), (["in"], wt.HWND, "mdi"),
                  (["in"], wt.DWORD, "reserved")),
        COMMETHOD([], HRESULT, "ThumbBarAddButtons", (["in"], wt.HWND, "hwnd"), (["in"], wt.UINT, "c"),
                  (["in"], ctypes.c_void_p, "buttons")),
        COMMETHOD([], HRESULT, "ThumbBarUpdateButtons", (["in"], wt.HWND, "hwnd"), (["in"], wt.UINT, "c"),
                  (["in"], ctypes.c_void_p, "buttons")),
        COMMETHOD([], HRESULT, "ThumbBarSetImageList", (["in"], wt.HWND, "hwnd"), (["in"], ctypes.c_void_p, "himl")),
        COMMETHOD([], HRESULT, "SetOverlayIcon", (["in"], wt.HWND, "hwnd"), (["in"], wt.HICON, "hIcon"),
                  (["in"], wt.LPCWSTR, "desc")),
        COMMETHOD([], HRESULT, "SetThumbnailTooltip", (["in"], wt.HWND, "hwnd"), (["in"], wt.LPCWSTR, "tip")),
        COMMETHOD([], HRESULT, "SetThumbnailClip", (["in"], wt.HWND, "hwnd"), (["in"], ctypes.c_void_p, "clip")),
    ]


CLSID_TaskbarList = GUID("{56FDF344-FD6D-11d0-958A-006097C9A090}")
CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")


# ---------------------------------------------------------------------------
# 音声セッションの取得
# ---------------------------------------------------------------------------
DEFAULT_DEVICE_ID = "default"


class AudioScanner:
    """全ての再生デバイスのセッションから、(PID, デバイス)ごとのピーク値を取る"""

    REFRESH_SEC = 5.0

    def __init__(self):
        self.enumerator = None
        self.managers = []   # [(device_id, name, IAudioSessionManager2)]
        self.devices = []    # [(device_id, name)]
        self.last_refresh = 0.0
        if IMMDeviceEnumerator is not None:
            try:
                self.enumerator = comtypes.CoCreateInstance(
                    CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator, clsctx=CLSCTX_ALL)
            except Exception:
                log.exception("デバイス列挙の初期化に失敗。既定デバイスのみ監視します")

    def _refresh(self):
        managers, devices = [], []
        coll = self.enumerator.EnumAudioEndpoints(0, 1)  # eRender, DEVICE_STATE_ACTIVE
        for i in range(coll.GetCount()):
            try:
                dev = coll.Item(i)
                dev_id = dev.GetId()
                name = dev_id
                try:
                    name = AudioUtilities.CreateDevice(dev).FriendlyName or dev_id
                except Exception:
                    pass
                unk = dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
                managers.append((dev_id, name, unk.QueryInterface(IAudioSessionManager2)))
                devices.append((dev_id, name))
            except Exception:
                log.exception("デバイスのセッション管理の取得に失敗")
        self.managers = managers
        self.devices = devices
        self.last_refresh = time.monotonic()

    def sessions(self):
        """{(pid, device_id): peak} を返す"""
        result = {}

        def put(pid, dev_id, peak):
            key = (pid, dev_id)
            result[key] = max(result.get(key, 0.0), peak)

        if self.enumerator is not None:
            try:
                if time.monotonic() - self.last_refresh > self.REFRESH_SEC or not self.managers:
                    self._refresh()
                for dev_id, _name, mgr in self.managers:
                    se = mgr.GetSessionEnumerator()
                    for i in range(se.GetCount()):
                        try:
                            ctl = se.GetSession(i)
                            pid = ctl.QueryInterface(IAudioSessionControl2).GetProcessId()
                            if not pid:
                                continue  # システム音
                            put(pid, dev_id, ctl.QueryInterface(IAudioMeterInformation).GetPeakValue())
                        except Exception:
                            pass
                return result
            except Exception:
                log.exception("セッション取得に失敗。次回デバイスを再取得します")
                self.managers = []
                return result
        # フォールバック: 既定デバイスのみ（出力先の区別はできない）
        self.devices = [(DEFAULT_DEVICE_ID, "既定の出力デバイス")]
        for s in AudioUtilities.GetAllSessions():
            try:
                if not s.ProcessId:
                    continue
                put(s.ProcessId, DEFAULT_DEVICE_ID,
                    s._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue())
            except Exception:
                pass
        return result


# ---------------------------------------------------------------------------
# プロセス情報（親をたどってウィンドウを探すため）
# ---------------------------------------------------------------------------
STOP_PARENTS = {
    "explorer.exe", "svchost.exe", "services.exe", "wininit.exe", "winlogon.exe", "sihost.exe",
    "runtimebroker.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
    "openconsole.exe", "conhost.exe", "taskhostw.exe", "dllhost.exe",
}


class ProcCache:
    TTL = 30.0

    def __init__(self):
        self.cache = {}

    def get(self, pid):
        now = time.monotonic()
        e = self.cache.get(pid)
        if e and now - e[0] < self.TTL:
            return e[1]
        try:
            p = psutil.Process(pid)
            info = (p.name().lower(), p.ppid(), p.create_time())
        except Exception:
            info = None
        self.cache[pid] = (now, info)
        if len(self.cache) > 2000:
            self.cache = {k: v for k, v in self.cache.items() if now - v[0] < self.TTL}
        return info

    def name(self, pid):
        info = self.get(pid)
        return info[0] if info else "?"


def resolve_windows(pid, by_pid, procs):
    """音を出しているPIDに対応するウィンドウを探す（なければ親プロセスをたどる）"""
    if pid in by_pid:
        return by_pid[pid]
    cur = pid
    for _ in range(4):
        info = procs.get(cur)
        if not info:
            break
        ppid = info[1]
        if not ppid or ppid == cur:
            break
        pinfo = procs.get(ppid)
        if not pinfo or pinfo[0] in STOP_PARENTS or pinfo[2] > info[2]:
            break
        if ppid in by_pid:
            return by_pid[ppid]
        cur = ppid
    return []


# ---------------------------------------------------------------------------
# 表示（バッジ・点灯・タイトル）
# ---------------------------------------------------------------------------
class Indicators:
    BADGE_REFRESH = 3.0  # アプリ側に上書きされても戻るよう定期的に付け直す

    def __init__(self):
        self.tbl = None
        try:
            self.tbl = comtypes.CoCreateInstance(CLSID_TaskbarList, interface=ITaskbarList3,
                                                 clsctx=comtypes.CLSCTX_INPROC_SERVER)
            self.tbl.HrInit()
        except Exception:
            log.exception("ITaskbarList3 の初期化に失敗（バッジは表示されません）")
            self.tbl = None
        self.hicon = None
        self.state = {}
        self.badge_errors = 0

    def configure(self, cfg):
        self._destroy_icon()
        if cfg["badge_enabled"]:
            try:
                self.hicon = load_badge_hicon(cfg)
            except Exception:
                log.exception("バッジアイコンの作成に失敗")

    def _destroy_icon(self):
        if self.hicon:
            user32.DestroyIcon(self.hicon)
            self.hicon = None

    def apply(self, active, cfg):
        now = time.monotonic()
        fg = int(user32.GetForegroundWindow() or 0)
        for hwnd in list(self.state):
            if hwnd not in active or not user32.IsWindow(hwnd):
                self._remove(hwnd)
        for hwnd in active:
            if not user32.IsWindow(hwnd):
                continue
            st = self.state.setdefault(hwnd, {"badge_t": -1e9, "badge": False, "flashed": False, "prefix": None})

            if cfg["badge_enabled"] and self.hicon and self.tbl and now - st["badge_t"] >= self.BADGE_REFRESH:
                st["badge_t"] = now
                try:
                    self.tbl.SetOverlayIcon(hwnd, self.hicon, "再生中")
                    st["badge"] = True
                except Exception as e:
                    self.badge_errors += 1
                    if self.badge_errors <= 5:
                        log.warning("SetOverlayIcon 失敗 hwnd=%s: %s", hwnd, e)

            if cfg["flash_enabled"]:
                if hwnd == fg:
                    st["flashed"] = False  # 前面にするとWindowsが点灯を消す
                elif not st["flashed"]:
                    flash_window(hwnd, FLASHW_TRAY, max(1, int(cfg["flash_count"])))
                    st["flashed"] = True

            prefix = cfg["title_prefix"] if cfg["title_enabled"] else ""
            if prefix:
                t = get_title(hwnd)
                if t and not t.startswith(prefix):
                    if set_title(hwnd, prefix + t):
                        st["prefix"] = prefix

    def _remove(self, hwnd):
        st = self.state.pop(hwnd, None)
        if not st or not user32.IsWindow(hwnd):
            return
        if st["badge"] and self.tbl:
            try:
                self.tbl.SetOverlayIcon(hwnd, None, None)
            except Exception:
                pass
        if st["flashed"]:
            flash_window(hwnd, FLASHW_STOP, 0)
        if st["prefix"]:
            t = get_title(hwnd)
            if t.startswith(st["prefix"]):
                set_title(hwnd, t[len(st["prefix"]):])

    def clear_all(self):
        for hwnd in list(self.state):
            try:
                self._remove(hwnd)
            except Exception:
                log.exception("表示の解除に失敗")

    def close(self):
        self.clear_all()
        self._destroy_icon()


# ---------------------------------------------------------------------------
# 監視スレッド
# ---------------------------------------------------------------------------
class Monitor(threading.Thread):
    def __init__(self, store):
        super().__init__(daemon=True, name="monitor")
        self.store = store
        self.stop_evt = threading.Event()
        self.snapshot = []  # 設定画面の「検出状況」用
        self.devices = []   # 設定画面の「出力デバイス」一覧用 [(id, name)]
        self.own_pid = os.getpid()

    def run(self):
        comtypes.CoInitialize()
        ind = None
        try:
            scanner = AudioScanner()
            procs = ProcCache()
            ind = Indicators()
            last_sound = {}
            version = -1
            self._cleanup_stale_prefix()
            log.info("監視開始（管理者権限: %s）", is_admin())
            while not self.stop_evt.is_set():
                cfg, ver = self.store.get()
                if ver != version:
                    ind.clear_all()
                    ind.configure(cfg)
                    last_sound.clear()
                    version = ver
                if cfg["paused"]:
                    self.snapshot = []
                    self.stop_evt.wait(0.5)
                    continue
                try:
                    self._tick(cfg, scanner, procs, ind, last_sound)
                except Exception:
                    log.exception("監視処理のエラー")
                self.stop_evt.wait(max(100, int(cfg["interval_ms"])) / 1000.0)
        except Exception:
            log.exception("監視スレッドが異常終了")
        finally:
            if ind:
                ind.close()
            comtypes.CoUninitialize()
            log.info("監視終了")

    def _cleanup_stale_prefix(self):
        """前回の異常終了でタイトルに記号が残っていれば外す"""
        cfg, _ = self.store.get()
        prefix = cfg["title_prefix"]
        if not prefix:
            return
        for hwnd, _pid, title in enum_taskbar_windows(self.own_pid):
            if title.startswith(prefix):
                set_title(hwnd, title[len(prefix):])

    def _tick(self, cfg, scanner, procs, ind, last_sound):
        now = time.monotonic()
        threshold = float(cfg["threshold"])
        hold = float(cfg["hold_seconds"])
        exclude = {x.lower() for x in cfg["exclude"]}
        exclude_dev = set(cfg.get("exclude_devices", []))

        recs = scanner.sessions()
        self.devices = list(scanner.devices)
        dev_names = dict(self.devices)
        windows = enum_taskbar_windows(self.own_pid)
        by_pid = defaultdict(list)
        title_of = {}
        for hwnd, pid, title in windows:
            by_pid[pid].append(hwnd)
            title_of[hwnd] = title

        # 除外していない出力先だけでPIDごとの最大レベルを出す
        pid_peak = defaultdict(float)
        for (pid, dev), peak in recs.items():
            if dev not in exclude_dev:
                pid_peak[pid] = max(pid_peak[pid], peak)

        pid_windows = {}
        for pid, peak in pid_peak.items():
            if peak >= threshold and procs.name(pid) not in exclude:
                hwnds = [h for h in resolve_windows(pid, by_pid, procs)
                         if procs.name(hwnd_pid(h)) not in exclude]
                pid_windows[pid] = hwnds
                for h in hwnds:
                    last_sound[h] = now

        snap = []
        for (pid, dev), peak in recs.items():
            exe = procs.name(pid)
            dev_ex = dev in exclude_dev
            sounding = peak >= threshold and not dev_ex and exe not in exclude
            hwnds = pid_windows.get(pid, []) if sounding else []
            snap.append({"exe": exe, "pid": pid, "dev": dev, "dev_name": dev_names.get(dev, dev),
                         "peak": peak, "sounding": sounding,
                         "excluded": exe in exclude, "dev_excluded": dev_ex,
                         "titles": [title_of.get(h, "") for h in hwnds]})

        for h in list(last_sound):
            if now - last_sound[h] > hold or not user32.IsWindow(h):
                del last_sound[h]
        ind.apply(set(last_sound), cfg)
        snap.sort(key=lambda r: -r["peak"])
        self.snapshot = snap

    def stop(self):
        self.stop_evt.set()


# ---------------------------------------------------------------------------
# 自動起動（タスクスケジューラ）
# ---------------------------------------------------------------------------
def launch_command(extra_args=""):
    """(実行ファイル, 引数) を返す。exe化していればexe自身"""
    if getattr(sys, "frozen", False):
        return sys.executable, extra_args
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    script = os.path.abspath(__file__)
    return exe, ('"%s" %s' % (script, extra_args)).strip()


def _run_schtasks(args):
    p = subprocess.run(["schtasks"] + args, capture_output=True, creationflags=CREATE_NO_WINDOW)
    out = (p.stdout + p.stderr).decode("mbcs", errors="replace").strip()
    return p.returncode == 0, out


def task_registered():
    try:
        ok, _ = _run_schtasks(["/Query", "/TN", TASK_NAME])
        return ok
    except Exception:
        return False


def register_task():
    exe, args = launch_command()
    user = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
    workdir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    xml = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{desc}</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId><Delay>PT10S</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{cmd}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{wd}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""".format(desc=escape(APP_TITLE + "（音を出しているウィンドウをタスクバーに表示）"),
           user=escape(user), cmd=escape(exe), args=escape(args), wd=escape(workdir))
    path = os.path.join(APPDATA_DIR, "task.xml")
    with open(path, "w", encoding="utf-16") as f:
        f.write(xml)
    return _run_schtasks(["/Create", "/TN", TASK_NAME, "/XML", path, "/F"])


def unregister_task():
    return _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])


# ---------------------------------------------------------------------------
# 二重起動防止
# ---------------------------------------------------------------------------
def acquire_mutex(wait_seconds):
    deadline = time.time() + wait_seconds
    while True:
        h = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        err = ctypes.get_last_error()
        if h and err != ERROR_ALREADY_EXISTS:
            return h
        if h:
            kernel32.CloseHandle(h)
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# 設定画面
# ---------------------------------------------------------------------------
class SettingsWindow:
    def __init__(self, app):
        self.app = app
        cfg, _ = app.store.get()
        self.color = cfg["badge_color"]
        self.preview_img = None

        w = self.win = tk.Toplevel(app.root)
        w.title(APP_TITLE + " - 設定")
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.minsize(560, 0)

        frm = ttk.Frame(w, padding=10)
        frm.pack(fill="both", expand=True)
        nb = ttk.Notebook(frm)
        nb.pack(fill="both", expand=True)
        pages = {}
        for key, label in [("disp", "表示方法"), ("detect", "検出と出力デバイス"),
                           ("status", "検出状況"), ("admin", "権限と自動起動")]:
            pages[key] = ttk.Frame(nb, padding=8)
            nb.add(pages[key], text=label)

        # --- 表示方法 ---
        lf = ttk.LabelFrame(pages["disp"], text="表示方法", padding=8)
        lf.pack(fill="x")
        self.v_badge = tk.BooleanVar(value=cfg["badge_enabled"])
        ttk.Checkbutton(lf, text="タスクバーのアイコン右下にバッジを付ける", variable=self.v_badge,
                        command=self.update_preview).grid(row=0, column=0, columnspan=5, sticky="w")
        ttk.Label(lf, text="色").grid(row=1, column=0, sticky="e", padx=(24, 4))
        self.color_btn = tk.Button(lf, width=4, bg=self.color, activebackground=self.color,
                                   relief="groove", command=self.pick_color)
        self.color_btn.grid(row=1, column=1, sticky="w")
        ttk.Label(lf, text="形").grid(row=1, column=2, sticky="e", padx=(16, 4))
        self.cb_shape = ttk.Combobox(lf, values=list(SHAPES.values()), state="readonly", width=10)
        self.cb_shape.set(SHAPES.get(cfg["badge_shape"], "スピーカー"))
        self.cb_shape.bind("<<ComboboxSelected>>", lambda e: self.update_preview())
        self.cb_shape.grid(row=1, column=3, sticky="w")
        self.preview = ttk.Label(lf)
        self.preview.grid(row=1, column=4, padx=12)

        self.v_flash = tk.BooleanVar(value=cfg["flash_enabled"])
        ttk.Checkbutton(lf, text="タスクバーボタンをオレンジに点灯する（色はWindowsの仕様で固定）",
                        variable=self.v_flash).grid(row=2, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Label(lf, text="点滅回数").grid(row=3, column=0, sticky="e", padx=(24, 4))
        self.v_count = tk.StringVar(value=str(cfg["flash_count"]))
        ttk.Spinbox(lf, from_=1, to=10, increment=1, textvariable=self.v_count, width=5).grid(
            row=3, column=1, sticky="w")
        ttk.Label(lf, text="回点滅したあと点灯したままになります", foreground="gray").grid(
            row=3, column=2, columnspan=3, sticky="w", padx=8)

        self.v_title = tk.BooleanVar(value=cfg["title_enabled"])
        ttk.Checkbutton(lf, text="ウィンドウタイトルの先頭に記号を付ける（ラベル表示時に見えます）",
                        variable=self.v_title).grid(row=4, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Label(lf, text="記号").grid(row=5, column=0, sticky="e", padx=(24, 4))
        self.v_prefix = tk.StringVar(value=cfg["title_prefix"])
        ttk.Entry(lf, textvariable=self.v_prefix, width=8).grid(row=5, column=1, sticky="w")

        # --- 検出 ---
        lf2 = ttk.LabelFrame(pages["detect"], text="検出", padding=8)
        lf2.pack(fill="x", pady=(8, 0))
        self.v_thr = tk.StringVar(value="%g" % (float(cfg["threshold"]) * 100))
        self.v_hold = tk.StringVar(value="%g" % float(cfg["hold_seconds"]))
        self.v_interval = tk.StringVar(value=str(cfg["interval_ms"]))
        rows = [("鳴っていると判定する音量（%）", self.v_thr, 0.1, 50, 0.1),
                ("音が止まってから表示を消すまで（秒）", self.v_hold, 0, 30, 0.5),
                ("確認間隔（ミリ秒）", self.v_interval, 100, 2000, 50)]
        for i, (label, var, lo, hi, step) in enumerate(rows):
            ttk.Label(lf2, text=label).grid(row=i, column=0, sticky="w")
            ttk.Spinbox(lf2, from_=lo, to=hi, increment=step, textvariable=var, width=8).grid(
                row=i, column=1, sticky="w", padx=8, pady=1)
        ttk.Label(lf2, text="除外するアプリ（exe名を1行に1つ。例: discord.exe）").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.t_exclude = tk.Text(lf2, height=4, width=40)
        self.t_exclude.insert("1.0", "\n".join(cfg["exclude"]))
        self.t_exclude.grid(row=4, column=0, columnspan=2, sticky="we")

        # --- 出力デバイス ---
        lfd = ttk.LabelFrame(pages["detect"], text="監視する出力デバイス", padding=8)
        lfd.pack(fill="x", pady=(8, 0))
        ttk.Label(lfd, text="チェックを外した出力先（仮想ケーブルなど聞いていないデバイス）に出ている音は無視します。",
                  foreground="gray",
                  wraplength=tkfont.nametofont("TkDefaultFont").measure("0") * 80).pack(anchor="w")
        self.dev_frame = ttk.Frame(lfd)
        self.dev_frame.pack(fill="x", pady=(4, 0))
        self.dev_vars = {}   # id -> BooleanVar（True = 監視する）
        self.dev_labels = {}  # id -> 表示名
        ttk.Button(lfd, text="一覧を更新", command=self.build_device_list).pack(anchor="e")
        self.build_device_list()

        # --- 検出状況 ---
        lf3 = ttk.LabelFrame(pages["status"], text="現在の検出状況（音声セッションのあるアプリ）", padding=8)
        lf3.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("exe", "pid", "device", "level", "windows")
        self.tree = ttk.Treeview(lf3, columns=cols, show="headings", height=10)
        cw = tkfont.nametofont("TkDefaultFont").measure("0")  # 文字幅に合わせて列幅を決める
        for c, text, chars, anchor, stretch in [("exe", "アプリ", 24, "w", False),
                                                ("pid", "PID", 7, "e", False),
                                                ("device", "出力先", 26, "w", False),
                                                ("level", "レベル", 8, "e", False),
                                                ("windows", "対応するウィンドウ", 36, "w", True)]:
            self.tree.heading(c, text=text)
            self.tree.column(c, width=cw * chars, minwidth=cw * min(chars, 6), anchor=anchor, stretch=stretch)
        self.tree.tag_configure("on", background="#FFF2B3")
        self.tree.tag_configure("ex", foreground="gray")
        self.tree.pack(fill="both", expand=True)
        ttk.Button(lf3, text="選択したアプリを除外に追加", command=self.add_exclude).pack(anchor="e", pady=(4, 0))

        # --- 権限・自動起動 ---
        lf4 = ttk.LabelFrame(pages["admin"], text="権限と自動起動", padding=8)
        lf4.pack(fill="x", pady=(8, 0))
        admin = is_admin()
        ttk.Label(lf4, text="現在の実行権限: " + ("管理者" if admin else
                  "通常（管理者権限で動くアプリには表示できません）")).grid(row=0, column=0, columnspan=3, sticky="w")
        b = ttk.Button(lf4, text="管理者として再起動", command=self.app.restart_as_admin)
        b.grid(row=1, column=0, sticky="w", pady=4)
        if admin:
            b.state(["disabled"])
        self.btn_reg = ttk.Button(lf4, text="ログオン時に管理者権限で自動起動する", command=self.do_register)
        self.btn_reg.grid(row=2, column=0, sticky="w")
        self.btn_unreg = ttk.Button(lf4, text="自動起動を解除", command=self.do_unregister)
        self.btn_unreg.grid(row=2, column=1, sticky="w", padx=8)
        if not admin:
            self.btn_reg.state(["disabled"])
            self.btn_unreg.state(["disabled"])
        self.lbl_task = ttk.Label(lf4, text="")
        self.lbl_task.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        if not admin:
            ttk.Label(lf4, text="自動起動の登録・解除は、管理者として再起動してから行えます。",
                      foreground="gray").grid(row=4, column=0, columnspan=3, sticky="w")
        self.refresh_task_label()

        # --- 下部 ---
        bottom = ttk.Frame(frm)
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Button(bottom, text="ログのフォルダを開く", command=lambda: os.startfile(APPDATA_DIR)).pack(side="left")
        ttk.Button(bottom, text="閉じる", command=self.close).pack(side="right")
        ttk.Button(bottom, text="保存", command=self.save).pack(side="right", padx=6)
        self.lbl_status = ttk.Label(bottom, text="", foreground="green")
        self.lbl_status.pack(side="right", padx=8)

        self.update_preview()
        self.refresh_tree()
        w.lift()
        w.focus_force()

    def pick_color(self):
        res = colorchooser.askcolor(color=self.color, parent=self.win, title="バッジの色")
        if res and res[1]:
            self.color = res[1].upper()
            self.color_btn.configure(bg=self.color, activebackground=self.color)
            self.update_preview()

    def shape_key(self):
        label = self.cb_shape.get()
        for k, v in SHAPES.items():
            if v == label:
                return k
        return "speaker"

    def update_preview(self):
        img = make_badge_image(self.color, self.shape_key()).resize((32, 32), Image.LANCZOS)
        if not self.v_badge.get():
            img = img.convert("LA").convert("RGBA")
        self.preview_img = ImageTk.PhotoImage(img)
        self.preview.configure(image=self.preview_img)

    def collect(self):
        try:
            thr = min(50.0, max(0.1, float(self.v_thr.get()))) / 100.0
            hold = min(30.0, max(0.0, float(self.v_hold.get())))
            interval = min(2000, max(100, int(float(self.v_interval.get()))))
            count = min(10, max(1, int(float(self.v_count.get()))))
        except ValueError:
            messagebox.showerror(APP_TITLE, "数値の入力が正しくありません。", parent=self.win)
            return None
        exclude = [ln.strip().lower() for ln in self.t_exclude.get("1.0", "end").splitlines() if ln.strip()]
        ex_dev = [i for i, v in self.dev_vars.items() if not v.get()]
        return {
            "exclude_devices": ex_dev,
            "device_names": {i: self.dev_labels.get(i, i) for i in ex_dev},
            "badge_enabled": self.v_badge.get(), "badge_color": self.color, "badge_shape": self.shape_key(),
            "flash_enabled": self.v_flash.get(), "flash_count": count,
            "title_enabled": self.v_title.get(), "title_prefix": self.v_prefix.get(),
            "threshold": thr, "hold_seconds": hold, "interval_ms": interval, "exclude": exclude,
        }

    def save(self):
        d = self.collect()
        if d is None:
            return
        self.app.store.update(**d)
        self.app.refresh_tray()
        self.lbl_status.configure(text="保存しました")
        self.win.after(2500, lambda: self.lbl_status.winfo_exists() and self.lbl_status.configure(text=""))

    def add_exclude(self):
        cur = [ln.strip().lower() for ln in self.t_exclude.get("1.0", "end").splitlines() if ln.strip()]
        for item in self.tree.selection():
            exe = self.tree.set(item, "exe")
            if exe and exe != "?" and exe not in cur:
                cur.append(exe)
        self.t_exclude.delete("1.0", "end")
        self.t_exclude.insert("1.0", "\n".join(cur))
        self.lbl_status.configure(text="「保存」で反映されます")

    def refresh_tree(self):
        if not self.win.winfo_exists():
            return
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for r in self.app.monitor.snapshot[:60]:
            if r["sounding"]:
                wins = " / ".join(r["titles"]) if r["titles"] else "（対応するウィンドウが見つかりません）"
                tags = ("on",)
            elif r["excluded"]:
                wins, tags = "（アプリを除外中）", ("ex",)
            elif r["dev_excluded"]:
                wins, tags = "（出力先を除外中）", ("ex",)
            else:
                wins, tags = "", ()
            iid = "%s|%s" % (r["pid"], r["dev"])
            if self.tree.exists(iid):
                continue
            self.tree.insert("", "end", iid=iid, tags=tags,
                             values=(r["exe"], r["pid"], r["dev_name"], "%.1f%%" % (r["peak"] * 100), wins))
            if iid in selected:
                self.tree.selection_add(iid)
        if self.app.store.get()[0]["paused"]:
            self.tree.insert("", "end", values=("（一時停止中）", "", "", "", ""))
        self.win.after(500, self.refresh_tree)

    def build_device_list(self):
        """監視する出力デバイスのチェック一覧を作り直す（チェック状態は引き継ぐ）"""
        cfg, _ = self.app.store.get()
        excluded = set(cfg.get("exclude_devices", []))
        saved_names = cfg.get("device_names", {})
        states = {k: v.get() for k, v in self.dev_vars.items()}
        for child in self.dev_frame.winfo_children():
            child.destroy()
        self.dev_vars.clear()
        self.dev_labels.clear()

        items = [(i, n, True) for i, n in self.app.monitor.devices]
        present = {i for i, _n, _p in items}
        for dev_id in excluded | set(states):
            if dev_id not in present:
                items.append((dev_id, saved_names.get(dev_id, dev_id), False))
        if not items:
            ttk.Label(self.dev_frame, text="（取得中…数秒後に「一覧を更新」を押してください）",
                      foreground="gray").pack(anchor="w")
            return
        for dev_id, name, connected in items:
            v = tk.BooleanVar(value=states.get(dev_id, dev_id not in excluded))
            self.dev_vars[dev_id] = v
            self.dev_labels[dev_id] = name
            ttk.Checkbutton(self.dev_frame, variable=v,
                            text=name + ("" if connected else "  （未接続）")).pack(anchor="w")

    def refresh_task_label(self):
        self.lbl_task.configure(text="自動起動: " + ("登録済み" if task_registered() else "未登録"))

    def do_register(self):
        ok, out = register_task()
        self.refresh_task_label()
        (messagebox.showinfo if ok else messagebox.showerror)(
            APP_TITLE, ("登録しました。次回ログオン時から管理者権限で自動起動します。" if ok else "登録に失敗しました。")
            + ("\n\n" + out if out and not ok else ""), parent=self.win)

    def do_unregister(self):
        ok, out = unregister_task()
        self.refresh_task_label()
        (messagebox.showinfo if ok else messagebox.showerror)(
            APP_TITLE, "解除しました。" if ok else "解除に失敗しました。\n\n" + out, parent=self.win)

    def close(self):
        self.win.destroy()
        self.app.settings = None


# ---------------------------------------------------------------------------
# アプリ本体（トレイ＋Tk）
# ---------------------------------------------------------------------------
class App:
    def __init__(self, mutex):
        self.mutex = mutex
        self.store = ConfigStore()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_TITLE)
        self._setup_style()
        self.settings = None
        self.queue = queue.Queue()
        self.monitor = Monitor(self.store)

        cfg, _ = self.store.get()
        self.tray = pystray.Icon(
            APP_NAME, make_tray_image(cfg), APP_TITLE,
            menu=pystray.Menu(
                pystray.MenuItem("設定...", lambda icon, item: self.queue.put("settings"), default=True),
                pystray.MenuItem("一時停止", self.toggle_pause,
                                 checked=lambda item: self.store.get()[0]["paused"]),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("終了", lambda icon, item: self.queue.put("quit")),
            ))

    def _setup_style(self):
        """高DPIで表の文字が見切れないよう、行の高さを文字の大きさに合わせる"""
        style = ttk.Style(self.root)
        try:
            body = tkfont.nametofont("TkDefaultFont")
            head = tkfont.nametofont("TkHeadingFont")
            style.configure("Treeview", rowheight=int(body.metrics("linespace") * 1.45))
            pad = max(2, head.metrics("linespace") // 5)
            style.configure("Treeview.Heading", padding=(4, pad))
        except Exception:
            log.exception("スタイル設定に失敗")

    def toggle_pause(self, icon=None, item=None):
        paused = not self.store.get()[0]["paused"]
        self.store.update(paused=paused)
        self.refresh_tray()

    def refresh_tray(self):
        cfg, _ = self.store.get()
        try:
            self.tray.icon = make_tray_image(cfg)
            self.tray.title = APP_TITLE + ("（一時停止中）" if cfg["paused"] else "")
            self.tray.update_menu()
        except Exception:
            log.exception("トレイアイコンの更新に失敗")

    def poll(self):
        try:
            while True:
                cmd = self.queue.get_nowait()
                if cmd == "settings":
                    self.open_settings()
                elif cmd == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    def open_settings(self):
        if self.settings and self.settings.win.winfo_exists():
            self.settings.win.deiconify()
            self.settings.win.lift()
            self.settings.win.focus_force()
            return
        self.settings = SettingsWindow(self)

    def restart_as_admin(self):
        exe, args = launch_command("--after-restart")
        if self.mutex:
            kernel32.CloseHandle(self.mutex)
            self.mutex = None
        r = shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
        if int(r or 0) <= 32:
            # UAC がキャンセルされた等
            self.mutex = acquire_mutex(0)
            return
        self.quit()

    def quit(self):
        log.info("終了します")
        self.monitor.stop()
        self.monitor.join(timeout=3)
        try:
            self.tray.stop()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.monitor.start()
        threading.Thread(target=self.tray.run, daemon=True, name="tray").start()
        self.root.after(150, self.poll)
        self.root.mainloop()


def set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    setup_logging()
    set_dpi_awareness()
    wait = 15 if "--after-restart" in sys.argv else 0
    mutex = acquire_mutex(wait)
    if not mutex:
        msgbox("%sはすでに起動しています。\nタスクトレイのアイコンから設定を開けます。" % APP_TITLE)
        return
    app = None
    try:
        app = App(mutex)
        app.run()
    except Exception:
        log.exception("致命的なエラー")
        msgbox("エラーで終了しました。詳細はログを確認してください:\n" + LOG_PATH, 0x10)
    finally:
        h = app.mutex if app else mutex
        if h:
            kernel32.CloseHandle(h)


if __name__ == "__main__":
    main()
