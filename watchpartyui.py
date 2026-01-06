# watchparty_windows.py
#
# Windows Watch Party app (in-app video, nice UI, ready for .exe)
# - Embedded libmpv video inside the app (QOpenGLWidget)
# - Cloudflare Worker backend for rooms (WebSocket)
# - Everyone has the same local file
# - Overlay controls + chat + presence
# - .env support
#
# Install:
#   pip install PySide6 qasync websockets python-dotenv
#
# You also need mpv runtime on dev machine (for mpv-1.dll).
# For bundling: ship mpv-1.dll (and its dependent dlls) next to the exe.
#
# .env example:
#   WS_BASE_URL=wss://watchparty.zainmagdon.workers.dev
#   DEFAULT_ROOM=movie-night
#   DEFAULT_NAME=Zain
#   SEEK_STEP_SECONDS=10
#   HEARTBEAT_SECONDS=25
#   # Optional explicit path to mpv-1.dll:
#   # LIBMPV_PATH=C:\path\to\mpv-1.dll
#
# Run:
#   python watchparty_windows.py

import asyncio
import ctypes
import ctypes.util
import json
import os
import platform
import random
import string
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv
from PySide6 import QtCore, QtGui, QtWidgets, QtOpenGLWidgets
from qasync import QEventLoop, asyncSlot
import websockets

load_dotenv(override=False)

# ----------------------------
# Utilities
# ----------------------------


def ms() -> int:
    return int(time.time() * 1000)


def rand_id(n=12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def fmt_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(max(0, seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def resource_path(rel: str) -> str:
    """
    Supports PyInstaller:
    - When frozen: uses sys._MEIPASS
    - When dev: uses this file's directory
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return str(Path(sys._MEIPASS) / rel)
    return str(Path(__file__).parent / rel)


def _ensure_dll_search_paths():
    """
    On Windows (Python 3.8+), DLL search is restricted.
    If we bundle DLLs next to the exe (or in a folder), add it explicitly.
    """
    if platform.system().lower() != "windows":
        return

    base = (
        Path(sys.executable).parent
        if getattr(sys, "frozen", False)
        else Path(__file__).parent
    )
    # Typical places you might store mpv dlls:
    candidates = [
        base,
        base / "mpv",
        Path(resource_path(".")),
        Path(resource_path("mpv")),
    ]
    for d in candidates:
        try:
            if d.exists():
                os.add_dll_directory(str(d))
        except Exception:
            pass


# ----------------------------
# libmpv ctypes (minimal)
# ----------------------------

MPV_FORMAT_FLAG = 3
MPV_FORMAT_DOUBLE = 5

MPV_RENDER_API_TYPE_OPENGL = b"opengl"
MPV_RENDER_PARAM_API_TYPE = 1
MPV_RENDER_PARAM_OPENGL_INIT_PARAMS = 2
MPV_RENDER_PARAM_OPENGL_FBO = 3
MPV_RENDER_PARAM_FLIP_Y = 4

GET_PROC = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)


class mpv_opengl_init_params(ctypes.Structure):
    _fields_ = [
        ("get_proc_address", GET_PROC),
        ("get_proc_address_ctx", ctypes.c_void_p),
        ("extra_exts", ctypes.c_char_p),
    ]


class mpv_opengl_fbo(ctypes.Structure):
    _fields_ = [
        ("fbo", ctypes.c_int),
        ("w", ctypes.c_int),
        ("h", ctypes.c_int),
        ("internal_format", ctypes.c_int),
    ]


class mpv_render_param(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("data", ctypes.c_void_p)]


UPDATE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


def _load_mpv_library() -> ctypes.CDLL:
    """
    Windows-first loader.
    Looks for:
    - LIBMPV_PATH env
    - bundled mpv-1.dll next to exe or in ./mpv/
    - system lookup
    """
    _ensure_dll_search_paths()

    env = os.getenv("LIBMPV_PATH")
    if env and Path(env).exists():
        return ctypes.CDLL(env)

    # Try bundled locations
    local_candidates = [
        Path(resource_path("mpv-1.dll")),
        Path(resource_path("mpv")) / "mpv-1.dll",
        (
            Path(sys.executable).parent
            if getattr(sys, "frozen", False)
            else Path(__file__).parent
        )
        / "mpv-1.dll",
        (
            Path(sys.executable).parent
            if getattr(sys, "frozen", False)
            else Path(__file__).parent
        )
        / "mpv"
        / "mpv-1.dll",
    ]
    for p in local_candidates:
        if p.exists():
            return ctypes.CDLL(str(p))

    # Try plain name (PATH / added dll dirs)
    try:
        return ctypes.CDLL("mpv-1.dll")
    except Exception:
        pass

    found = ctypes.util.find_library("mpv-1") or ctypes.util.find_library("mpv")
    if found:
        return ctypes.CDLL(found)

    raise RuntimeError(
        "Could not find mpv-1.dll (libmpv). On Windows, place mpv-1.dll next to the script/exe "
        "or set LIBMPV_PATH in .env."
    )


_mpv = _load_mpv_library()

# mpv core
_mpv.mpv_create.restype = ctypes.c_void_p
_mpv.mpv_initialize.argtypes = [ctypes.c_void_p]
_mpv.mpv_initialize.restype = ctypes.c_int
_mpv.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]

_mpv.mpv_set_option_string.argtypes = [
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
]
_mpv.mpv_set_option_string.restype = ctypes.c_int

_mpv.mpv_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
_mpv.mpv_command.restype = ctypes.c_int

_mpv.mpv_get_property.argtypes = [
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_void_p,
]
_mpv.mpv_get_property.restype = ctypes.c_int
_mpv.mpv_set_property.argtypes = [
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_void_p,
]
_mpv.mpv_set_property.restype = ctypes.c_int

# mpv render
_mpv.mpv_render_context_create.argtypes = [
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_void_p,
    ctypes.POINTER(mpv_render_param),
]
_mpv.mpv_render_context_create.restype = ctypes.c_int
_mpv.mpv_render_context_free.argtypes = [ctypes.c_void_p]
_mpv.mpv_render_context_render.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(mpv_render_param),
]
_mpv.mpv_render_context_set_update_callback.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
]

# ----------------------------
# Embedded MPV player
# ----------------------------


class EmbeddedMPV(QtCore.QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        h = _mpv.mpv_create()
        if not h:
            raise RuntimeError("mpv_create failed")
        self.handle = ctypes.c_void_p(h)

        # options
        self._opt("terminal", "no")
        self._opt("osc", "no")
        self._opt("input-default-bindings", "no")
        self._opt("keep-open", "yes")
        self._opt("idle", "yes")
        self._opt("hwdec", "auto")
        self._opt("vo", "gpu")
        # Windows GPU context for OpenGL
        self._opt("gpu-context", "win")

        rc = _mpv.mpv_initialize(self.handle)
        if rc < 0:
            raise RuntimeError(f"mpv_initialize failed: {rc}")

    def _opt(self, k: str, v: str):
        _mpv.mpv_set_option_string(
            self.handle, k.encode("utf-8"), v.encode("utf-8")
        )

    def terminate(self):
        if self.handle:
            _mpv.mpv_terminate_destroy(self.handle)
            self.handle = None

    def command(self, *args: str):
        arr = (ctypes.c_char_p * (len(args) + 1))()
        for i, a in enumerate(args):
            arr[i] = a.encode("utf-8")
        arr[len(args)] = None
        rc = _mpv.mpv_command(self.handle, arr)
        if rc < 0:
            raise RuntimeError(f"mpv_command failed: {rc} args={args}")

    def load_file(self, path: str):
        self.command("loadfile", path, "replace")

    def get_double(self, prop: str) -> Optional[float]:
        out = ctypes.c_double()
        rc = _mpv.mpv_get_property(
            self.handle,
            prop.encode("utf-8"),
            MPV_FORMAT_DOUBLE,
            ctypes.byref(out),
        )
        if rc < 0:
            return None
        return float(out.value)

    def get_flag(self, prop: str) -> Optional[bool]:
        out = ctypes.c_int()
        rc = _mpv.mpv_get_property(
            self.handle,
            prop.encode("utf-8"),
            MPV_FORMAT_FLAG,
            ctypes.byref(out),
        )
        if rc < 0:
            return None
        return bool(out.value)

    def set_flag(self, prop: str, value: bool):
        v = ctypes.c_int(1 if value else 0)
        rc = _mpv.mpv_set_property(
            self.handle, prop.encode("utf-8"), MPV_FORMAT_FLAG, ctypes.byref(v)
        )
        if rc < 0:
            raise RuntimeError(f"mpv_set_property failed: {rc} {prop}")

    def set_double(self, prop: str, value: float):
        v = ctypes.c_double(float(value))
        rc = _mpv.mpv_set_property(
            self.handle,
            prop.encode("utf-8"),
            MPV_FORMAT_DOUBLE,
            ctypes.byref(v),
        )
        if rc < 0:
            raise RuntimeError(f"mpv_set_property failed: {rc} {prop}")

    # actions
    def play(self):
        self.set_flag("pause", False)

    def pause(self):
        self.set_flag("pause", True)

    def toggle_pause(self):
        p = self.get_flag("pause")
        if p is None:
            return
        self.set_flag("pause", not p)

    def seek_abs(self, seconds: float):
        self.command("seek", str(float(seconds)), "absolute")

    def seek_rel(self, seconds: float):
        self.command("seek", str(float(seconds)), "relative")

    def set_volume(self, vol: int):
        self.set_double("volume", float(clamp(vol, 0, 100)))

    def set_speed(self, speed: float):
        self.set_double("speed", float(speed))


# ----------------------------
# MPV OpenGL widget
# ----------------------------


class MPVGLWidget(QtOpenGLWidgets.QOpenGLWidget):
    def __init__(self, player: EmbeddedMPV, parent=None):
        super().__init__(parent)
        self.player = player
        self.mpv_render_ctx = ctypes.c_void_p(None)
        self._update_cb = None
        self._get_proc_address = None
        self._init_params = None
        self.setMinimumSize(640, 360)

    def initializeGL(self):
        ctx = self.context()
        if ctx is None:
            raise RuntimeError("No OpenGL context")

        @GET_PROC
        def _get_proc_address(_ctx, name):
            try:
                func_name = ctypes.cast(name, ctypes.c_char_p).value.decode(
                    "utf-8"
                )
            except Exception:
                return None
            ptr = ctx.getProcAddress(func_name)
            if not ptr:
                return None
            # IMPORTANT: return int address, not a ctypes pointer object
            return int(ptr)

        self._get_proc_address = _get_proc_address
        self._init_params = mpv_opengl_init_params(
            get_proc_address=self._get_proc_address,
            get_proc_address_ctx=None,
            extra_exts=None,
        )

        api_type = ctypes.c_char_p(MPV_RENDER_API_TYPE_OPENGL)

        params = (mpv_render_param * 3)()
        params[0] = mpv_render_param(
            MPV_RENDER_PARAM_API_TYPE,
            ctypes.cast(api_type, ctypes.c_void_p),
        )
        params[1] = mpv_render_param(
            MPV_RENDER_PARAM_OPENGL_INIT_PARAMS,
            ctypes.cast(ctypes.pointer(self._init_params), ctypes.c_void_p),
        )
        params[2] = mpv_render_param(0, None)

        out_ctx = ctypes.c_void_p()
        rc = _mpv.mpv_render_context_create(
            ctypes.byref(out_ctx), self.player.handle, params
        )
        if rc < 0:
            raise RuntimeError(f"mpv_render_context_create failed: {rc}")
        self.mpv_render_ctx = out_ctx

        @UPDATE_CB
        def on_update(_userdata):
            QtCore.QMetaObject.invokeMethod(
                self, "update", QtCore.Qt.ConnectionType.QueuedConnection
            )

        self._update_cb = on_update
        _mpv.mpv_render_context_set_update_callback(
            self.mpv_render_ctx, self._update_cb, None
        )

    def paintGL(self):
        if not self.mpv_render_ctx:
            return

        fbo_id = int(self.defaultFramebufferObject())
        w = int(self.width() * self.devicePixelRatio())
        h = int(self.height() * self.devicePixelRatio())

        fbo = mpv_opengl_fbo(fbo=fbo_id, w=w, h=h, internal_format=0)
        flip_y = ctypes.c_int(1)

        params = (mpv_render_param * 3)()
        params[0] = mpv_render_param(
            MPV_RENDER_PARAM_OPENGL_FBO,
            ctypes.cast(ctypes.pointer(fbo), ctypes.c_void_p),
        )
        params[1] = mpv_render_param(
            MPV_RENDER_PARAM_FLIP_Y,
            ctypes.cast(ctypes.pointer(flip_y), ctypes.c_void_p),
        )
        params[2] = mpv_render_param(0, None)

        _mpv.mpv_render_context_render(self.mpv_render_ctx, params)

    def closeEvent(self, event: QtGui.QCloseEvent):
        try:
            if self.mpv_render_ctx:
                _mpv.mpv_render_context_free(self.mpv_render_ctx)
                self.mpv_render_ctx = None
        except Exception:
            pass
        super().closeEvent(event)


# ----------------------------
# Room state
# ----------------------------


@dataclass
class RoomState:
    isPlaying: bool = False
    positionMs: int = 0
    updatedAt: int = 0
    updatedBy: str = ""
    version: int = 0


# ----------------------------
# UI controls overlay
# ----------------------------


class ControlOverlay(QtWidgets.QFrame):
    playClicked = QtCore.Signal()
    pauseClicked = QtCore.Signal()
    toggleClicked = QtCore.Signal()
    backClicked = QtCore.Signal()
    fwdClicked = QtCore.Signal()
    seekRequested = QtCore.Signal(float)  # 0..1
    volumeChanged = QtCore.Signal(int)
    fullscreenClicked = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("overlay")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)

        def btn(text: str) -> QtWidgets.QToolButton:
            b = QtWidgets.QToolButton()
            b.setText(text)
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            return b

        self.backBtn = btn("↺10")
        self.playBtn = btn("▶")
        self.pauseBtn = btn("❚❚")
        self.toggleBtn = btn("⏯")
        self.fwdBtn = btn("10↻")
        self.fullBtn = btn("⛶")

        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setTracking(False)

        self.timeLbl = QtWidgets.QLabel("00:00 / 00:00")
        self.timeLbl.setStyleSheet("font-weight:800;")

        self.volIcon = QtWidgets.QLabel("🔊")
        self.volSlider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.volSlider.setRange(0, 100)
        self.volSlider.setValue(80)
        self.volSlider.setFixedWidth(120)

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        row.addWidget(self.backBtn)
        row.addWidget(self.playBtn)
        row.addWidget(self.pauseBtn)
        row.addWidget(self.toggleBtn)
        row.addWidget(self.fwdBtn)
        row.addWidget(self.slider, 1)
        row.addWidget(self.timeLbl)
        row.addWidget(self.volIcon)
        row.addWidget(self.volSlider)
        row.addWidget(self.fullBtn)

        self.playBtn.clicked.connect(self.playClicked.emit)
        self.pauseBtn.clicked.connect(self.pauseClicked.emit)
        self.toggleBtn.clicked.connect(self.toggleClicked.emit)
        self.backBtn.clicked.connect(self.backClicked.emit)
        self.fwdBtn.clicked.connect(self.fwdClicked.emit)
        self.fullBtn.clicked.connect(self.fullscreenClicked.emit)
        self.slider.sliderReleased.connect(self._emit_seek)
        self.volSlider.valueChanged.connect(self.volumeChanged.emit)

    def _emit_seek(self):
        self.seekRequested.emit(self.slider.value() / 1000.0)


class VideoContainer(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)

        self._stack = QtWidgets.QStackedLayout(self)
        self._stack.setStackingMode(
            QtWidgets.QStackedLayout.StackingMode.StackAll
        )

        self._videoWidget = QtWidgets.QLabel("Select a file and Connect")
        self._videoWidget.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._videoWidget.setObjectName("videoSurface")
        self._stack.addWidget(self._videoWidget)

        self.overlay = ControlOverlay()
        self.overlay.setFixedHeight(64)

        overlayWrap = QtWidgets.QWidget()
        overlayLayout = QtWidgets.QVBoxLayout(overlayWrap)
        overlayLayout.setContentsMargins(14, 14, 14, 14)
        overlayLayout.addStretch(1)
        overlayLayout.addWidget(self.overlay)
        self._stack.addWidget(overlayWrap)

        self._overlayVisible = True
        self._hideTimer = QtCore.QTimer(self)
        self._hideTimer.setInterval(1800)
        self._hideTimer.timeout.connect(self._auto_hide)
        self._hideTimer.start()

    def set_video_widget(self, w: QtWidgets.QWidget):
        if w is self._videoWidget:
            return
        w.setObjectName("videoSurface")
        w.setMouseTracking(True)
        old = self._videoWidget
        self._videoWidget = w
        self._stack.insertWidget(0, w)
        self._stack.setCurrentIndex(0)
        old.setParent(None)
        old.deleteLater()

    def _auto_hide(self):
        if self._overlayVisible:
            self.overlay.hide()
            self._overlayVisible = False

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        if not self._overlayVisible:
            self.overlay.show()
            self._overlayVisible = True
        self._hideTimer.start()
        super().mouseMoveEvent(event)


# ----------------------------
# Main window
# ----------------------------


class WatchPartyWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WatchParty (Windows)")
        self.resize(1200, 720)

        # config
        self.ws_base_url = os.getenv("WS_BASE_URL", "").rstrip("/")
        if not self.ws_base_url:
            self.ws_base_url = "wss://watchparty.zainmagdon.workers.dev"
        self.heartbeat_seconds = int(os.getenv("HEARTBEAT_SECONDS", "25"))
        self.seek_step = float(os.getenv("SEEK_STEP_SECONDS", "10"))

        # runtime
        self.user_id = rand_id()
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.server_offset_ms = 0
        self.last_version = 0
        self.state = RoomState()

        self.player: Optional[EmbeddedMPV] = None
        self.gl: Optional[MPVGLWidget] = None

        # qasync re-entry guards
        self._connecting = False
        self._ui_tick_running = False

        self._build_ui()
        self._apply_style()
        self._bind_shortcuts()

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        # top bar
        self.roomEdit = QtWidgets.QLineEdit(
            os.getenv("DEFAULT_ROOM", "movie-night")
        )
        self.nameEdit = QtWidgets.QLineEdit(os.getenv("DEFAULT_NAME", "anon"))
        self.fileEdit = QtWidgets.QLineEdit()
        self.fileEdit.setPlaceholderText(
            "Select a local video file (everyone should pick the same file)"
        )
        self.fileBtn = QtWidgets.QPushButton("Browse…")
        self.connectBtn = QtWidgets.QPushButton("Connect")
        self.connectBtn.setCheckable(True)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self._labeled("Room", self.roomEdit), 2)
        top.addWidget(self._labeled("Name", self.nameEdit), 2)
        top.addWidget(self.fileEdit, 7)
        top.addWidget(self.fileBtn, 1)
        top.addWidget(self.connectBtn, 2)

        self.fileBtn.clicked.connect(self._pick_file)
        self.connectBtn.clicked.connect(self._toggle_connect)

        # video left
        self.videoContainer = VideoContainer()

        # right: presence + chat
        self.presenceList = QtWidgets.QListWidget()
        self.presenceList.setMinimumWidth(260)

        self.chatView = QtWidgets.QTextEdit()
        self.chatView.setReadOnly(True)
        self.chatInput = QtWidgets.QLineEdit()
        self.chatInput.setPlaceholderText("Message… (Enter to send)")
        self.chatSendBtn = QtWidgets.QPushButton("Send")
        self.chatInput.returnPressed.connect(self._send_chat_clicked)
        self.chatSendBtn.clicked.connect(self._send_chat_clicked)

        rightLayout = QtWidgets.QVBoxLayout()
        rightLayout.addWidget(self._card("People", self.presenceList), 2)
        rightLayout.addWidget(self._card("Chat", self.chatView), 6)
        chatBottom = QtWidgets.QHBoxLayout()
        chatBottom.addWidget(self.chatInput, 1)
        chatBottom.addWidget(self.chatSendBtn)
        rightLayout.addLayout(chatBottom)

        rightPane = QtWidgets.QWidget()
        rightPane.setLayout(rightLayout)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self.videoContainer)
        splitter.addWidget(rightPane)
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)

        self.statusLabel = QtWidgets.QLabel("Disconnected")
        self.statusLabel.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )

        root = QtWidgets.QVBoxLayout(central)
        root.addLayout(top)
        root.addWidget(splitter, 1)
        root.addWidget(self.statusLabel)

        # overlay wiring
        ov = self.videoContainer.overlay
        ov.playClicked.connect(
            lambda: asyncio.create_task(self._send_control("PLAY"))
        )
        ov.pauseClicked.connect(
            lambda: asyncio.create_task(self._send_control("PAUSE"))
        )
        ov.toggleClicked.connect(
            lambda: asyncio.create_task(self._toggle_play_pause())
        )
        ov.backClicked.connect(
            lambda: asyncio.create_task(self._seek_rel(-self.seek_step))
        )
        ov.fwdClicked.connect(
            lambda: asyncio.create_task(self._seek_rel(self.seek_step))
        )
        ov.seekRequested.connect(self._seek_fraction)
        ov.volumeChanged.connect(
            lambda v: asyncio.create_task(self._set_volume(v))
        )
        ov.fullscreenClicked.connect(self._toggle_fullscreen)

        self._set_controls_enabled(False)

    def _apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow { background: #0b1220; }
            QLabel, QListWidget, QTextEdit, QLineEdit { color: #e5e7eb; font-size: 13px; }
            QLineEdit, QTextEdit, QListWidget {
                background: #0f1a2e;
                border: 1px solid #22314d;
                border-radius: 12px;
                padding: 10px;
            }
            QPushButton {
                background: #2563eb;
                color: white;
                border: none;
                border-radius: 12px;
                padding: 10px 14px;
                font-weight: 800;
            }
            QPushButton:disabled { background: #1f2a44; color: #93a4c7; }
            QPushButton:checked { background: #16a34a; }

            QGroupBox {
                border: 1px solid #22314d;
                border-radius: 14px;
                margin-top: 10px;
                padding: 10px;
            }
            QGroupBox:title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #c7d2fe; font-weight: 900; }

            #videoSurface {
                background: #000;
                border-radius: 16px;
                border: 1px solid #22314d;
            }

            QFrame#overlay {
                background: rgba(15, 26, 46, 190);
                border: 1px solid rgba(34, 49, 77, 220);
                border-radius: 16px;
            }

            QToolButton {
                color: white;
                background: rgba(37, 99, 235, 220);
                border: none;
                border-radius: 12px;
                padding: 8px 12px;
                font-weight: 900;
                min-width: 44px;
            }
            QToolButton:hover { background: rgba(59, 130, 246, 240); }

            QSlider::groove:horizontal {
                height: 8px;
                background: rgba(31, 42, 68, 230);
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                width: 16px;
                margin: -6px 0;
                border-radius: 8px;
                background: rgba(96, 165, 250, 255);
            }
        """
        )

    def _bind_shortcuts(self):
        QtGui.QShortcut(
            QtGui.QKeySequence("Space"),
            self,
            activated=lambda: asyncio.create_task(self._toggle_play_pause()),
        )
        QtGui.QShortcut(
            QtGui.QKeySequence("Left"),
            self,
            activated=lambda: asyncio.create_task(
                self._seek_rel(-self.seek_step)
            ),
        )
        QtGui.QShortcut(
            QtGui.QKeySequence("Right"),
            self,
            activated=lambda: asyncio.create_task(
                self._seek_rel(self.seek_step)
            ),
        )
        QtGui.QShortcut(
            QtGui.QKeySequence("F"), self, activated=self._toggle_fullscreen
        )

    def _labeled(
        self, label: str, widget: QtWidgets.QWidget
    ) -> QtWidgets.QWidget:
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        lbl = QtWidgets.QLabel(label)
        lbl.setStyleSheet("color:#c7d2fe; font-weight:900;")
        layout.addWidget(lbl)
        layout.addWidget(widget)
        return box

    def _card(
        self, title: str, widget: QtWidgets.QWidget
    ) -> QtWidgets.QGroupBox:
        g = QtWidgets.QGroupBox(title)
        l = QtWidgets.QVBoxLayout(g)
        l.addWidget(widget)
        return g

    def _set_controls_enabled(self, enabled: bool):
        self.videoContainer.overlay.setEnabled(enabled)
        self.chatInput.setEnabled(enabled)
        self.chatSendBtn.setEnabled(enabled)

    def _set_status(self, text: str):
        self.statusLabel.setText(text)

    def _append_chat(self, html: str):
        self.chatView.append(html)
        self.chatView.verticalScrollBar().setValue(
            self.chatView.verticalScrollBar().maximum()
        )

    def _pick_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select video file",
            "",
            "Video Files (*.mkv *.mp4 *.avi *.mov *.webm);;All Files (*)",
        )
        if path:
            self.fileEdit.setText(path)

    def _send_chat_clicked(self):
        msg = self.chatInput.text().strip()
        if not msg:
            return
        self.chatInput.clear()
        asyncio.create_task(self._send_chat(msg))

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    # ---------- connect/disconnect ----------

    @asyncSlot()
    async def _toggle_connect(self):
        if self._connecting:
            return
        self._connecting = True
        try:
            if self.connectBtn.isChecked():
                await self._connect()
            else:
                await self._disconnect()
        finally:
            self._connecting = False

    async def _connect(self):
        room = self.roomEdit.text().strip()
        name = self.nameEdit.text().strip() or "anon"
        file_path = self.fileEdit.text().strip()

        if not room:
            self._set_status("Enter a room id.")
            self.connectBtn.setChecked(False)
            return

        if not file_path or not Path(file_path).exists():
            self._set_status("Select an existing local file.")
            self.connectBtn.setChecked(False)
            return

        # init mpv + gl once
        try:
            if self.player is None:
                self.player = EmbeddedMPV()
            if self.gl is None:
                self.gl = MPVGLWidget(self.player)
                self.videoContainer.set_video_widget(self.gl)
            self.player.load_file(file_path)
        except Exception as e:
            self._set_status(f"Failed to init libmpv: {e}")
            self.connectBtn.setChecked(False)
            return

        # connect websocket
        ws_url = f"{self.ws_base_url}/room/{room}"
        try:
            self.ws = await websockets.connect(ws_url, ping_interval=None)
        except Exception as e:
            self._set_status(f"WebSocket connect failed: {e}")
            self.connectBtn.setChecked(False)
            return

        self.last_version = 0
        self._set_controls_enabled(True)
        self._set_status(f"Connected: {ws_url}")

        await self._send({"type": "join", "userId": self.user_id, "name": name})
        asyncio.create_task(self._recv_loop())
        asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(
            self._set_volume(self.videoContainer.overlay.volSlider.value())
        )

    async def _disconnect(self):
        self._set_controls_enabled(False)
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass
        self.ws = None
        self._set_status("Disconnected")

    # ---------- WS ----------

    async def _send(self, obj: Dict[str, Any]):
        if not self.ws:
            return
        await self.ws.send(json.dumps(obj))

    async def _heartbeat_loop(self):
        while self.ws:
            try:
                await self._send({"type": "ping", "t": ms()})
            except Exception:
                return
            await asyncio.sleep(self.heartbeat_seconds)

    async def _recv_loop(self):
        assert self.ws is not None
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                t = msg.get("type")
                if t == "pong":
                    t0 = int(msg.get("t", 0))
                    server_time = int(msg.get("serverTimeMs", 0))
                    t1 = ms()
                    rtt = max(1, t1 - t0)
                    self.server_offset_ms = int(server_time - (t0 + rtt / 2))
                    continue

                if t == "presence":
                    self._update_presence(msg.get("users", []))
                    continue

                if t == "chat":
                    frm = msg.get("from", {})
                    name = frm.get("name", "?")
                    text = (
                        (msg.get("text", "") or "")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                    )
                    self._append_chat(
                        f"<b style='color:#c7d2fe'>{name}</b>: {text}"
                    )
                    continue

                if t == "state":
                    await self._apply_state(msg.get("state", {}) or {})
                    continue

        except Exception as e:
            self._set_status(f"Disconnected (recv error): {e}")
        finally:
            self.connectBtn.setChecked(False)
            await self._disconnect()

    def _update_presence(self, users: List[Dict[str, Any]]):
        self.presenceList.clear()
        for u in users:
            self.presenceList.addItem(u.get("name", "?"))

    # ---------- Controls ----------

    async def _send_control(
        self, action: str, position_ms: Optional[int] = None
    ):
        payload: Dict[str, Any] = {"type": "control", "action": action}
        if position_ms is not None:
            payload["positionMs"] = int(position_ms)
        await self._send(payload)

    async def _send_chat(self, text: str):
        await self._send({"type": "chat", "text": text[:500]})

    async def _toggle_play_pause(self):
        if not self.ws or not self.player:
            return
        try:
            self.player.toggle_pause()
            paused = self.player.get_flag("pause")
            await self._send_control("PAUSE" if paused else "PLAY")
        except Exception as e:
            self._set_status(str(e))

    async def _seek_rel(self, delta_seconds: float):
        if not self.ws or not self.player:
            return
        try:
            cur = self.player.get_double("time-pos") or 0.0
            target = max(0.0, float(cur) + float(delta_seconds))
            await self._send_control("SEEK", position_ms=int(target * 1000))
        except Exception as e:
            self._set_status(str(e))

    def _seek_fraction(self, frac: float):
        asyncio.create_task(self._seek_fraction_async(frac))

    async def _seek_fraction_async(self, frac: float):
        if not self.ws or not self.player:
            return
        try:
            dur = self.player.get_double("duration")
            if not dur or dur <= 0:
                return
            target = float(dur) * float(frac)
            await self._send_control("SEEK", position_ms=int(target * 1000))
        except Exception as e:
            self._set_status(str(e))

    async def _set_volume(self, vol: int):
        if not self.player:
            return
        try:
            self.player.set_volume(vol)
        except Exception as e:
            self._set_status(str(e))

    # ---------- Apply server state ----------

    async def _apply_state(self, state: Dict[str, Any]):
        version = int(state.get("version", 0))
        if version <= self.last_version:
            return
        self.last_version = version

        self.state = RoomState(
            isPlaying=bool(state.get("isPlaying", False)),
            positionMs=int(state.get("positionMs", 0)),
            updatedAt=int(state.get("updatedAt", 0)),
            updatedBy=str(state.get("updatedBy", "")),
            version=version,
        )

        if not self.player:
            return

        server_now = ms() + self.server_offset_ms
        if self.state.isPlaying:
            target_ms = self.state.positionMs + max(
                0, server_now - self.state.updatedAt
            )
        else:
            target_ms = self.state.positionMs
        target_s = max(0.0, target_ms / 1000.0)

        cur_s = self.player.get_double("time-pos")
        paused = self.player.get_flag("pause")
        if cur_s is None or paused is None:
            return

        drift = target_s - float(cur_s)

        # play/pause
        if self.state.isPlaying and paused:
            self.player.play()
        elif (not self.state.isPlaying) and (not paused):
            self.player.pause()

        # drift correction
        try:
            if abs(drift) < 0.25:
                self.player.set_speed(1.0)
            elif abs(drift) < 1.5:
                speed = 1.0 + clamp(drift * 0.10, -0.10, 0.10)
                self.player.set_speed(speed)
            else:
                self.player.set_speed(1.0)
                self.player.seek_abs(target_s)
        except Exception:
            pass

        self._set_status(
            f"Synced v{version} (by {self.state.updatedBy}) drift {drift:+.2f}s"
        )

    # ---------- UI tick (guarded) ----------

    def _on_tick(self):
        if self._connecting or self._ui_tick_running:
            return
        self._ui_tick_running = True
        asyncio.create_task(self._update_player_ui_guarded())

    async def _update_player_ui_guarded(self):
        try:
            await self._update_player_ui()
        finally:
            self._ui_tick_running = False

    async def _update_player_ui(self):
        if not self.player:
            self.videoContainer.overlay.timeLbl.setText("--:-- / --:--")
            return

        cur = self.player.get_double("time-pos")
        dur = self.player.get_double("duration")

        if cur is None or dur is None or dur <= 0:
            self.videoContainer.overlay.timeLbl.setText("--:-- / --:--")
            return

        self.videoContainer.overlay.timeLbl.setText(
            f"{fmt_time(cur)} / {fmt_time(dur)}"
        )

        if not self.videoContainer.overlay.slider.isSliderDown():
            self.videoContainer.overlay.slider.setValue(
                int(clamp((cur / dur) * 1000.0, 0, 1000))
            )


# ----------------------------
# Main
# ----------------------------


def main():
    app = QtWidgets.QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    w = WatchPartyWindow()
    w.show()

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
