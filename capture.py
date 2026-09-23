import ctypes
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import wave
import winreg
from ctypes import wintypes
from datetime import datetime
from tkinter import filedialog

import numpy as np
import pystray
import soundcard as sc
from PIL import Image, ImageDraw, ImageGrab, ImageTk

APP_NAME = "ScreenCapture"
APP_VERSION = "1.2.0"
GITHUB_REPO = "sovereignbrains/ScreenCapture"
FRAMERATE = "50"
ENCODE_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "app.log")
FFMPEG_LOG = os.path.join(CONFIG_DIR, "ffmpeg.log")

DEFAULTS = {
    "screenshot_hotkey": "print screen",
    "record_hotkey": "ctrl+print screen",
    "screenshot_dir": os.path.join(os.path.expanduser("~"), "Desktop", "Capture", "Screenshots"),
    "video_dir": os.path.join(os.path.expanduser("~"), "Desktop", "Capture", "Recordings"),
}

HOTKEY_LABELS = {"print screen": "PrtScn", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}

user32 = ctypes.WinDLL("user32", use_last_error=True)

FLASHW_STOP = 0
FLASHW_TRAY = 2
FLASHW_TIMER = 4


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


FFMPEG = os.path.join(app_dir(), "ffmpeg.exe")

cfg = dict(DEFAULTS)
state = {
    "proc": None,
    "backend": None,
    "err_files": [],
    "audio": None,
    "paths": None,
    "update": None,
}
toggle_lock = threading.Lock()
select_lock = threading.Lock()
icon = None


def log(msg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def notify(message):
    try:
        if icon is not None:
            icon.notify(message, APP_NAME)
    except Exception:
        pass


def load_config():
    global cfg
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        for key in DEFAULTS:
            value = saved.get(key)
            if isinstance(value, str) and value.strip():
                cfg[key] = value
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"config read failed: {e}")
    repaired = False
    for key in ("screenshot_hotkey", "record_hotkey"):
        if parse_hotkey(cfg[key]) is None:
            log(f"config hotkey '{cfg[key]}' is broken, back to default")
            cfg[key] = DEFAULTS[key]
            repaired = True
    if repaired:
        save_config()
    for key in ("screenshot_dir", "video_dir"):
        try:
            os.makedirs(cfg[key], exist_ok=True)
        except Exception as e:
            log(f"mkdir {cfg[key]} failed: {e}")


def save_config():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"config write failed: {e}")


def enable_dark_native_ui():
    """Включает тёмную тему для нативных win32-меню и заголовков окон (тот же механизм, что у Проводника)."""
    try:
        uxtheme = ctypes.WinDLL("uxtheme", use_last_error=True)
        set_preferred_app_mode = uxtheme[135]
        set_preferred_app_mode.argtypes = [ctypes.c_int]
        set_preferred_app_mode.restype = ctypes.c_int
        set_preferred_app_mode(2)  # ForceDark
        flush_menu_themes = uxtheme[136]
        flush_menu_themes.argtypes = []
        flush_menu_themes()
    except Exception as e:
        log(f"dark ui setup failed: {e}")


def set_dark_titlebar(hwnd):
    try:
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def make_icon_image(recording=False):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=(30, 30, 34, 255), outline=(235, 235, 235, 255), width=3)
    dot = (225, 45, 45, 255) if recording else (205, 205, 210, 255)
    draw.ellipse((20, 20, 44, 44), fill=dot)
    return img


MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
WM_HOTKEY = 0x0312
WM_APP_RELOAD = 0x8001
WM_APP_STOP = 0x8002
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

MOD_NAMES = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
             "win": MOD_WIN, "windows": MOD_WIN}

VK_NAMES = {
    "print screen": 0x2C, "printscreen": 0x2C, "prtscn": 0x2C, "prtsc": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "page up": 0x21, "page down": 0x22, "space": 0x20, "tab": 0x09,
    "enter": 0x0D, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "pause": 0x13, "scroll lock": 0x91, "caps lock": 0x14,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}
for _i in range(1, 25):
    VK_NAMES[f"f{_i}"] = 0x6F + _i

VK_TO_NAME = {
    0x2C: "print screen", 0x2D: "insert", 0x2E: "delete", 0x24: "home", 0x23: "end",
    0x21: "page up", 0x22: "page down", 0x20: "space", 0x09: "tab", 0x0D: "enter",
    0x1B: "esc", 0x08: "backspace", 0x13: "pause", 0x91: "scroll lock", 0x14: "caps lock",
    0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right",
}
for _i in range(1, 25):
    VK_TO_NAME[0x6F + _i] = f"f{_i}"

MODIFIER_VKS = {0x10: "shift", 0x11: "ctrl", 0x12: "alt", 0x5B: "win", 0x5C: "win",
                0xA0: "shift", 0xA1: "shift", 0xA2: "ctrl", 0xA3: "ctrl", 0xA4: "alt", 0xA5: "alt"}


def parse_hotkey(text):
    """'ctrl+print screen' -> (модификаторы, virtual key). None, если строку не разобрать."""
    mods = 0
    vk = None
    for part in str(text).split("+"):
        name = part.strip().lower()
        if not name:
            continue
        if name in MOD_NAMES:
            mods |= MOD_NAMES[name]
        elif name in VK_NAMES:
            vk = VK_NAMES[name]
        elif len(name) == 1 and (name.isdigit() or "a" <= name <= "z"):
            vk = ord(name.upper())
        else:
            return None
    return (mods, vk) if vk is not None else None


def vk_to_name(vk):
    if vk in VK_TO_NAME:
        return VK_TO_NAME[vk]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    return None


def fmt_hotkey(hotkey):
    parts = []
    for part in str(hotkey).split("+"):
        clean = part.strip().lower()
        parts.append(HOTKEY_LABELS.get(clean, clean.upper()))
    return "+".join(parts)


class HotkeyManager:
    """Системные хоткеи через RegisterHotKey: Windows сам разбирает модификаторы и перехватывает PrtScn."""

    def __init__(self):
        self.thread_id = 0
        self.ready = threading.Event()
        self.bindings = []
        self.actions = {}
        self.paused = False

    def start(self, bindings):
        self.bindings = bindings
        threading.Thread(target=self._loop, daemon=True).start()
        self.ready.wait(timeout=5)

    def configure(self, bindings):
        self.bindings = bindings
        self._post(WM_APP_RELOAD)

    def pause(self):
        self.paused = True
        self._post(WM_APP_RELOAD)
        time.sleep(0.15)

    def resume(self):
        self.paused = False
        self._post(WM_APP_RELOAD)

    def stop(self):
        self._post(WM_APP_STOP)

    def _post(self, message):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, message, 0, 0)

    def _unregister_all(self):
        for hotkey_id in list(self.actions):
            user32.UnregisterHotKey(None, hotkey_id)
        self.actions = {}

    def _register_all(self):
        self._unregister_all()
        if self.paused:
            return
        for hotkey_id, (text, action) in enumerate(self.bindings, start=1):
            combo = parse_hotkey(text)
            if combo is None:
                log(f"hotkey '{text}' not recognized")
                notify(f"Не понял хоткей {text}")
                continue
            mods, vk = combo
            if user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
                self.actions[hotkey_id] = action
                continue
            code = ctypes.get_last_error()
            log(f"RegisterHotKey '{text}' failed with {code}")
            if code == ERROR_HOTKEY_ALREADY_REGISTERED:
                notify(f"Хоткей {fmt_hotkey(text)} занят другой программой")
            else:
                notify(f"Не удалось назначить {fmt_hotkey(text)}")

    def _loop(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._register_all()
        self.ready.set()
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result in (0, -1):
                break
            if msg.message == WM_HOTKEY:
                action = self.actions.get(msg.wParam)
                if action is not None:
                    action()
            elif msg.message == WM_APP_RELOAD:
                self._register_all()
            elif msg.message == WM_APP_STOP:
                break
        self._unregister_all()


hotkeys = HotkeyManager()


def apply_hotkeys():
    hotkeys.configure([
        (cfg["screenshot_hotkey"], take_screenshot),
        (cfg["record_hotkey"], toggle_recording),
    ])


def refresh_menu():
    if icon is None:
        return
    icon.icon = make_icon_image(state["proc"] is not None)
    icon.menu = build_menu()
    icon.update_menu()


def virtual_screen():
    return (
        user32.GetSystemMetrics(76),
        user32.GetSystemMetrics(77),
        user32.GetSystemMetrics(78),
        user32.GetSystemMetrics(79),
    )


def copy_image_to_clipboard(image):
    try:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "BMP")
        data = buffer.getvalue()[14:]  # CF_DIB — это BMP без 14-байтового файлового заголовка
        buffer.close()
    except Exception as e:
        log(f"clipboard encode failed: {e}")
        return False

    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        log("clipboard busy")
        return False

    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(8, handle)  # CF_DIB
        return True
    except Exception as e:
        log(f"clipboard copy failed: {e}")
        return False
    finally:
        user32.CloseClipboard()


def select_region(hint):
    """Затемняет экран и даёт выделить область мышью. Возвращает (region, кадр) или None."""
    if not select_lock.acquire(blocking=False):
        return None
    try:
        vx, vy, vw, vh = virtual_screen()
        shot = ImageGrab.grab(all_screens=True, include_layered_windows=True).convert("RGB")
        dimmed = shot.point(lambda p: p * 45 // 100)

        root = tk.Tk()
        root.overrideredirect(True)
        root.geometry(f"{vw}x{vh}+{vx}+{vy}")
        root.attributes("-topmost", True)
        root.config(cursor="crosshair")

        canvas = tk.Canvas(root, width=vw, height=vh, highlightthickness=0, bd=0, bg="black")
        canvas.pack()
        photo = ImageTk.PhotoImage(dimmed)
        canvas.create_image(0, 0, image=photo, anchor="nw")
        canvas.create_text(
            vw // 2, 40, text=hint + "   (Esc — отмена)", fill="#ffffff", font=("Segoe UI", 16)
        )

        start = {}
        result = {}
        shapes = {"rect": None, "label": None}

        def on_press(event):
            start["x"], start["y"] = event.x, event.y

        def on_drag(event):
            if "x" not in start:
                return
            x0, y0 = start["x"], start["y"]
            x1, y1 = event.x, event.y
            if shapes["rect"] is None:
                shapes["rect"] = canvas.create_rectangle(x0, y0, x1, y1, outline="#ff3b3b", width=2)
                shapes["label"] = canvas.create_text(0, 0, text="", fill="#ffffff", font=("Segoe UI", 11))
            else:
                canvas.coords(shapes["rect"], x0, y0, x1, y1)
            canvas.itemconfig(shapes["label"], text=f"{abs(x1 - x0)} x {abs(y1 - y0)}")
            canvas.coords(shapes["label"], min(x0, x1) + 40, min(y0, y1) - 12)

        def on_release(event):
            if "x" not in start:
                root.destroy()
                return
            x0, y0 = start["x"], start["y"]
            x1, y1 = event.x, event.y
            x, y = min(x0, x1), min(y0, y1)
            w, h = abs(x1 - x0), abs(y1 - y0)
            w -= w % 2
            h -= h % 2
            if w >= 16 and h >= 16:
                result["region"] = (x + vx, y + vy, w, h)
                result["crop"] = (x, y, x + w, y + h)
            root.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        root.bind("<Escape>", lambda e: root.destroy())
        root.bind("<Button-3>", lambda e: root.destroy())
        root.focus_force()
        root.mainloop()

        if "region" not in result:
            return None
        return {"region": result["region"], "image": shot.crop(result["crop"])}
    except Exception as e:
        log(f"region select failed: {e}")
        return None
    finally:
        select_lock.release()


def take_screenshot(icon_=None, item=None):
    def worker():
        selection = select_region("Выдели область для скриншота")
        if selection is None:
            return
        try:
            os.makedirs(cfg["screenshot_dir"], exist_ok=True)
            path = os.path.join(
                cfg["screenshot_dir"], f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
            )
            selection["image"].save(path)
            copied = copy_image_to_clipboard(selection["image"])
            width, height = selection["image"].size
            log(f"screenshot {width}x{height}{' +clipboard' if copied else ''}: {path}")
            notify(
                f"Скриншот {width}x{height}{' скопирован в буфер' if copied else ''}:\n"
                f"{os.path.basename(path)}"
            )
        except Exception as e:
            log(f"screenshot failed: {e}")
            notify(f"Ошибка скриншота: {e}")

    threading.Thread(target=worker, daemon=True).start()


def _ddagrab_cmd(path, region):
    x, y, w, h = region
    return [
        FFMPEG, "-y",
        "-f", "lavfi",
        "-i",
        f"ddagrab=framerate={FRAMERATE}:draw_mouse=1:video_size={w}x{h}:offset_x={x}:offset_y={y}"
        ",hwdownload,format=bgra",
        *ENCODE_ARGS,
        path,
    ]


def _gdigrab_cmd(path, region):
    x, y, w, h = region
    return [
        FFMPEG, "-y",
        "-f", "gdigrab",
        "-framerate", FRAMERATE,
        "-draw_mouse", "1",
        "-offset_x", str(x),
        "-offset_y", str(y),
        "-video_size", f"{w}x{h}",
        "-i", "desktop",
        *ENCODE_ARGS,
        path,
    ]


def _spawn(cmd):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    err_file = open(FFMPEG_LOG, "a", encoding="utf-8", errors="replace")
    state["err_files"].append(err_file)
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=err_file,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def warm_up_ffmpeg():
    """Холодный запуск 222-мегабайтного ffmpeg.exe стоит несколько секунд (антивирус + чтение с диска).
    Прогреваем его при старте, чтобы запись начиналась сразу по хоткею."""
    def worker():
        started = time.time()
        try:
            subprocess.run(
                [FFMPEG, "-hide_banner", "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=60,
            )
            log(f"ffmpeg warmed up in {time.time() - started:.1f}s")
        except Exception as e:
            log(f"ffmpeg warm-up failed: {e}")

    threading.Thread(target=worker, daemon=True).start()


FFMPEG_READY_MARK = "Press [q] to stop"


def _wait_until_capturing(proc, log_offset, timeout=4.0):
    """Ждём, пока ffmpeg сообщит о готовности (~0.35 с), вместо фиксированной паузы.
    Размер выходного файла для этого не годится: заголовок mkv висит в буфере минутами."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(FFMPEG_LOG, encoding="utf-8", errors="replace") as f:
                f.seek(log_offset)
                if FFMPEG_READY_MARK in f.read():
                    return True
        except OSError:
            pass
        if proc.poll() is not None:
            return False
        time.sleep(0.03)
    return proc.poll() is None


def _close_err_files():
    for err_file in state["err_files"]:
        try:
            err_file.close()
        except Exception:
            pass
    state["err_files"] = []


AUDIO_SAMPLERATE = 44100
AUDIO_CHANNELS = 2


class AudioRecorder:
    def __init__(self, path):
        self.path = path
        self._stop = threading.Event()
        self._thread = None
        self.ok = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        except Exception as e:
            log(f"audio: no loopback device ({e})")
            return
        try:
            with wave.open(self.path, "wb") as wf:
                wf.setnchannels(AUDIO_CHANNELS)
                wf.setsampwidth(2)
                wf.setframerate(AUDIO_SAMPLERATE)
                with mic.recorder(samplerate=AUDIO_SAMPLERATE, channels=AUDIO_CHANNELS) as rec:
                    self.ok = True
                    chunk = AUDIO_SAMPLERATE // 20
                    while not self._stop.is_set():
                        data = rec.record(numframes=chunk)
                        pcm = np.clip(data, -1.0, 1.0)
                        pcm16 = (pcm * 32767.0).astype(np.int16)
                        wf.writeframes(pcm16.tobytes())
        except Exception as e:
            log(f"audio recording failed: {e}")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


class RecIndicator:
    """Индикатор активной записи: минимизированное окно в панели задач,
    которое моргает (флешится) через FlashWindowEx, пока идёт запись."""

    def __init__(self):
        self._root = None
        self._stop_evt = threading.Event()
        self._thread = None
        self._ready = threading.Event()

    def start(self):
        self._stop_evt.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2)

    def _run(self):
        root = tk.Tk()
        self._root = root
        root.title("Идёт запись экрана")
        try:
            root.iconbitmap(os.path.join(app_dir(), "icon.ico"))
        except Exception:
            pass
        root.geometry("320x90")
        tk.Label(root, text="Идёт запись экрана...", pady=18, font=("Segoe UI", 12)).pack()
        root.protocol("WM_DELETE_WINDOW", root.iconify)
        root.update_idletasks()
        hwnd = root.winfo_id()
        root.iconify()

        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_TRAY | FLASHW_TIMER, 0, 500)
        user32.FlashWindowEx(ctypes.byref(info))
        self._ready.set()

        def check_stop():
            if self._stop_evt.is_set():
                stop_info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_STOP, 0, 0)
                user32.FlashWindowEx(ctypes.byref(stop_info))
                root.destroy()
                return
            root.after(200, check_stop)

        root.after(0, check_stop)
        root.mainloop()

    def stop(self):
        self._stop_evt.set()


rec_indicator = RecIndicator()


def _mux(video_path, audio_path, final_path):
    cmd = [
        FFMPEG, "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        final_path,
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW
    )
    return result.returncode == 0


def _remux(video_path, final_path):
    cmd = [
        FFMPEG, "-y",
        "-i", video_path,
        "-c:v", "copy",
        "-movflags", "+faststart",
        final_path,
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW
    )
    return result.returncode == 0


def _start_recording(region):
    started = time.time()
    os.makedirs(cfg["video_dir"], exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    video_tmp = os.path.join(cfg["video_dir"], f".tmp_video_{stamp}.mkv")
    audio_tmp = os.path.join(cfg["video_dir"], f".tmp_audio_{stamp}.wav")
    final_path = os.path.join(cfg["video_dir"], f"record_{stamp}.mp4")
    try:
        open(FFMPEG_LOG, "w").close()
    except Exception:
        pass

    proc = _spawn(_ddagrab_cmd(video_tmp, region))
    backend = "D3D11"
    if not _wait_until_capturing(proc, 0):
        fallback_offset = os.path.getsize(FFMPEG_LOG)
        proc = _spawn(_gdigrab_cmd(video_tmp, region))
        backend = "GDI"
        if not _wait_until_capturing(proc, fallback_offset):
            _close_err_files()
            tail = ""
            try:
                with open(FFMPEG_LOG, encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-800:]
            except Exception:
                pass
            log(f"record failed: {tail}")
            notify("Не удалось начать запись, смотри app.log")
            return

    audio = AudioRecorder(audio_tmp)
    audio.start()
    rec_indicator.start()

    state["proc"] = proc
    state["backend"] = backend
    state["audio"] = audio
    state["paths"] = (video_tmp, audio_tmp, final_path)
    log(f"record started ({backend}) in {time.time() - started:.2f}s region={region}: {final_path}")
    notify(f"Запись пошла: {region[2]}x{region[3]}, 50 fps ({backend})")
    refresh_menu()


def _stop_recording():
    proc = state["proc"]
    audio = state.get("audio")
    video_tmp, audio_tmp, final_path = state["paths"]

    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    _close_err_files()

    if audio is not None:
        audio.stop()
    rec_indicator.stop()

    state["proc"] = None
    state["backend"] = None
    state["audio"] = None
    log("record stopped, muxing...")
    refresh_menu()

    def finalize():
        has_audio = False
        ok = False
        if audio is not None and audio.ok and os.path.exists(audio_tmp) and os.path.getsize(audio_tmp) > 44:
            ok = _mux(video_tmp, audio_tmp, final_path)
            has_audio = ok
        if not ok:
            ok = _remux(video_tmp, final_path)
        if not ok:
            try:
                os.replace(video_tmp, final_path)
            except Exception as e:
                log(f"finalize replace failed: {e}")
        for tmp in (video_tmp, audio_tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        log(f"record finalized{'(with audio)' if has_audio else '(video only)'}: {final_path}")
        notify(f"Запись сохранена{' со звуком' if has_audio else ' (без звука)'}:\n{os.path.basename(final_path)}")

    threading.Thread(target=finalize, daemon=True).start()


def toggle_recording(icon_=None, item=None):
    def worker():
        if not toggle_lock.acquire(blocking=False):
            return
        try:
            if state["proc"] is None:
                selection = select_region("Выдели область для записи")
                if selection is None:
                    return
                time.sleep(0.25)
                _start_recording(selection["region"])
            else:
                _stop_recording()
        except Exception as e:
            log(f"toggle failed: {e}")
            notify(f"Ошибка записи: {e}")
        finally:
            toggle_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def choose_dir(cfg_key, title):
    def worker():
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(title=title, initialdir=cfg[cfg_key], parent=root)
            root.destroy()
        except Exception as e:
            log(f"dir dialog failed: {e}")
            return
        if path:
            cfg[cfg_key] = os.path.normpath(path)
            try:
                os.makedirs(cfg[cfg_key], exist_ok=True)
            except Exception as e:
                log(f"mkdir failed: {e}")
            save_config()
            notify(f"Папка изменена:\n{cfg[cfg_key]}")
            refresh_menu()

    threading.Thread(target=worker, daemon=True).start()


def rebind_hotkey(cfg_key, title):
    def worker():
        result = {}
        pressed_mods = []
        hotkeys.pause()
        try:
            root = tk.Tk()
            root.title(title)
            root.geometry("400x150")
            root.resizable(False, False)
            root.attributes("-topmost", True)
            root.protocol("WM_DELETE_WINDOW", lambda: None)
            root.configure(bg="#202020")
            root.update_idletasks()
            set_dark_titlebar(root.winfo_id())
            tk.Label(
                root, text="Нажми новое сочетание клавиш", font=("Segoe UI", 12), bg="#202020", fg="#f0f0f0"
            ).pack(pady=(34, 8))
            tk.Label(root, text="Esc — отмена", font=("Segoe UI", 9), bg="#202020", fg="#9a9a9a").pack()

            def finish(vk):
                name = vk_to_name(vk)
                if name is None:
                    return
                if name != "esc":
                    result["hotkey"] = "+".join(pressed_mods + [name])
                root.destroy()

            def on_press(event):
                mod = MODIFIER_VKS.get(event.keycode)
                if mod is not None:
                    if mod not in pressed_mods:
                        pressed_mods.append(mod)
                    return
                finish(event.keycode)

            def on_release(event):
                # PrtScn Windows присылает только на отпускании, поэтому ловим и это событие
                mod = MODIFIER_VKS.get(event.keycode)
                if mod is not None:
                    return
                finish(event.keycode)

            root.bind("<KeyPress>", on_press)
            root.bind("<KeyRelease>", on_release)
            root.focus_force()
            root.grab_set()
            root.mainloop()
        except Exception as e:
            log(f"rebind dialog failed: {e}")

        hotkey = result.get("hotkey")
        if hotkey and parse_hotkey(hotkey) is not None:
            cfg[cfg_key] = hotkey
            save_config()
            notify(f"Новый хоткей: {fmt_hotkey(hotkey)}")
        hotkeys.resume()
        apply_hotkeys()
        refresh_menu()

    threading.Thread(target=worker, daemon=True).start()


def version_tuple(text):
    parts = []
    for chunk in str(text).strip().lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    parts += [0, 0, 0]
    return tuple(parts[:3])


def _github_request(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}", "Accept": "application/vnd.github+json"}
    )
    return urllib.request.urlopen(request, timeout=20)


def check_updates(manual=False):
    def worker():
        try:
            with _github_request(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest") as response:
                release = json.load(response)
        except Exception as e:
            log(f"update check failed: {e}")
            if manual:
                notify("Не удалось проверить обновления")
            return

        tag = str(release.get("tag_name", ""))
        if version_tuple(tag) <= version_tuple(APP_VERSION):
            state["update"] = None
            refresh_menu()
            if manual:
                notify(f"Установлена последняя версия ({APP_VERSION})")
            return

        asset = next(
            (a for a in release.get("assets", []) if str(a.get("name", "")).lower().endswith(".exe")), None
        )
        if asset is None:
            log(f"release {tag} has no installer")
            if manual:
                notify(f"Версия {tag} вышла, но установщик не выложен")
            return

        state["update"] = {
            "version": tag.lstrip("vV"),
            "url": asset["browser_download_url"],
            "name": re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(str(asset["name"]))),
        }
        log(f"update available: {tag}")
        refresh_menu()
        if manual:
            install_update()
        else:
            notify(f"Вышла версия {state['update']['version']} — обновить можно из меню")

    threading.Thread(target=worker, daemon=True).start()


def install_update(icon_=None, item=None):
    info = state.get("update")
    if not info:
        check_updates(manual=True)
        return

    def worker():
        notify(f"Качаю версию {info['version']}...")
        target = os.path.join(tempfile.gettempdir(), info["name"])
        try:
            with _github_request(info["url"]) as response, open(target, "wb") as out:
                shutil.copyfileobj(response, out)
        except Exception as e:
            log(f"update download failed: {e}")
            notify("Не удалось скачать обновление")
            return

        runner = os.path.join(tempfile.gettempdir(), f"{APP_NAME}-update.bat")
        try:
            with open(runner, "w", encoding="ascii") as f:
                # Приложение поднимаем отсюда, дождавшись конца установки. Из [Run] установщика оно
                # стартует в его контексте и загрузчик PyInstaller падает с ошибкой про Python DLL.
                f.write(
                    "@echo off\r\n"
                    'cd /d "%TEMP%"\r\n'
                    "timeout /t 3 /nobreak >nul\r\n"
                    f'"{target}" /SILENT /NORESTART\r\n'
                    "timeout /t 2 /nobreak >nul\r\n"
                    f'start "" "{get_exe_path()}"\r\n'
                    f'del "{target}"\r\n'
                    'del "%~f0"\r\n'
                )
            subprocess.Popen(
                ["cmd", "/c", runner],
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                close_fds=True,
            )
        except Exception as e:
            log(f"update launch failed: {e}")
            notify("Не удалось запустить установщик")
            return

        log(f"installing update {info['version']}")
        notify("Ставлю обновление, приложение перезапустится")
        quit_app()

    threading.Thread(target=worker, daemon=True).start()


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def get_exe_path():
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(__file__)


def is_autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
        return os.path.normcase(value.strip('"')) == os.path.normcase(get_exe_path())
    except Exception:
        return False


def toggle_autostart(icon_=None, item=None):
    try:
        enable = not is_autostart_enabled()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{get_exe_path()}"')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        log(f"autostart set to {enable}")
    except Exception as e:
        log(f"autostart toggle failed: {e}")
    refresh_menu()


def open_dir(path):
    def action(icon_=None, item=None):
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except Exception as e:
            log(f"open dir failed: {e}")

    return action


def quit_app(icon_=None, item=None):
    if state["proc"] is not None:
        try:
            _stop_recording()
        except Exception:
            pass
    hotkeys.stop()
    if icon is not None:
        icon.stop()


def build_menu():
    recording = state["proc"] is not None
    update = state.get("update")
    record_text = (
        f"Остановить запись   [{fmt_hotkey(cfg['record_hotkey'])}]"
        if recording
        else f"Записать область   [{fmt_hotkey(cfg['record_hotkey'])}]"
    )
    update_text = f"Обновить до {update['version']}" if update else f"Проверить обновления   ({APP_VERSION})"
    return pystray.Menu(
        pystray.MenuItem(
            f"Скриншот области   [{fmt_hotkey(cfg['screenshot_hotkey'])}]", take_screenshot, default=True
        ),
        pystray.MenuItem(record_text, toggle_recording),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Открыть папку с записями", open_dir(cfg["video_dir"])),
        pystray.MenuItem("Открыть папку со скриншотами", open_dir(cfg["screenshot_dir"])),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Настройки",
            pystray.Menu(
                pystray.MenuItem(
                    f"Папка скриншотов:  {short_path(cfg['screenshot_dir'])}",
                    lambda i=None, it=None: choose_dir("screenshot_dir", "Куда сохранять скриншоты"),
                ),
                pystray.MenuItem(
                    f"Папка видео:  {short_path(cfg['video_dir'])}",
                    lambda i=None, it=None: choose_dir("video_dir", "Куда сохранять видео"),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    f"Хоткей скриншота:  {fmt_hotkey(cfg['screenshot_hotkey'])}",
                    lambda i=None, it=None: rebind_hotkey("screenshot_hotkey", "Хоткей скриншота"),
                ),
                pystray.MenuItem(
                    f"Хоткей записи:  {fmt_hotkey(cfg['record_hotkey'])}",
                    lambda i=None, it=None: rebind_hotkey("record_hotkey", "Хоткей записи"),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Запускать при входе в Windows",
                    toggle_autostart,
                    checked=lambda item: is_autostart_enabled(),
                ),
            ),
        ),
        pystray.MenuItem(update_text, install_update),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", quit_app),
    )


def short_path(path, limit=52):
    return path if len(path) <= limit else "..." + path[-(limit - 3):]


def main():
    kernel32.CreateMutexW(None, False, "Local\\ScreenCaptureTrayApp")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    enable_dark_native_ui()

    global icon
    load_config()
    icon = pystray.Icon(APP_NAME, make_icon_image(False), APP_NAME, build_menu())
    hotkeys.start([
        (cfg["screenshot_hotkey"], take_screenshot),
        (cfg["record_hotkey"], toggle_recording),
    ])
    log(f"app started (v{APP_VERSION})")
    warm_up_ffmpeg()
    threading.Timer(20.0, lambda: check_updates(manual=False)).start()
    icon.run()


if __name__ == "__main__":
    main()
