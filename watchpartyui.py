import asyncio
import json
import os
import platform
import random
import string
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv
from PySide6 import QtCore, QtGui, QtWidgets
from qasync import QEventLoop, asyncSlot
import websockets

load_dotenv()


def ms() -> int:
    return int(time.time() * 1000)


def rand_id(n=10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ----------------------------
# MPV controller via JSON IPC
# ----------------------------


class MPV:
    def __init__(self, file_path: str, mpv_path: str = "mpv"):
        self.file_path = file_path
        self.mpv_path = mpv_path
        self.proc: Optional[subprocess.Popen] = None
        self.ipc_path = self._make_ipc_path()

    def _make_ipc_path(self) -> str:
        if platform.system().lower().startswith("win"):
            suffix = "".join(
                random.choice(string.ascii_lowercase) for _ in range(8)
            )
            return rf"\\.\pipe\watchparty-mpv-{suffix}"
        suffix = "".join(
            random.choice(string.ascii_lowercase) for _ in range(8)
        )
        return f"/tmp/watchparty-mpv-{suffix}.sock"

    def start(self):
        args = [
            self.mpv_path,
            self.file_path,
            "--force-window=yes",
            "--idle=yes",
            f"--input-ipc-server={self.ipc_path}",
            "--term-playing-msg=",
            "--keep-open=yes",
        ]
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    async def _connect_ipc(self, timeout_s=6.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if platform.system().lower().startswith("win"):
                    return await asyncio.open_connection(self.ipc_path)
                return await asyncio.open_unix_connection(self.ipc_path)
            except Exception:
                await asyncio.sleep(0.08)
        raise RuntimeError("Could not connect to mpv IPC")

    async def command(self, cmd):
        reader, writer = await self._connect_ipc()
        writer.write(json.dumps({"command": cmd}).encode("utf-8") + b"\n")
        await writer.drain()
        line = await reader.readline()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8", errors="ignore"))
        except Exception:
            return None

    async def get_property(self, prop: str):
        resp = await self.command(["get_property", prop])
        if resp and resp.get("error") == "success":
            return resp.get("data")
        return None

    async def set_property(self, prop: str, value):
        await self.command(["set_property", prop, value])

    async def play(self):
        await self.set_property("pause", False)

    async def pause(self):
        await self.set_property("pause", True)

    async def seek_to(self, seconds: float):
        await self.command(["set_property", "time-pos", float(seconds)])


# ----------------------------
# App state
# ----------------------------


@dataclass
class RoomState:
    isPlaying: bool = False
    positionMs: int = 0  # position at updatedAt
    updatedAt: int = 0
    updatedBy: str = ""
    version: int = 0
    mediaKey: str = ""


# ----------------------------
# UI
# ----------------------------


class WatchPartyWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WatchParty (Local File Sync)")
        self.resize(980, 640)

        # Config
        self.ws_base_url = os.getenv(
            "WS_BASE_URL", "wss://watchparty.zainmagdon.workers.dev"
        ).rstrip("/")
        self.heartbeat_seconds = int(os.getenv("HEARTBEAT_SECONDS", "25"))
        self.mpv_path = os.getenv("MPV_PATH", "mpv")

        # Runtime
        self.user_id = rand_id()
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.mpv: Optional[MPV] = None
        self.server_offset_ms = 0
        self.last_version = 0
        self.state = RoomState()

        self._build_ui()
        self._apply_style()

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        # Top: connection bar
        self.roomEdit = QtWidgets.QLineEdit(
            os.getenv("DEFAULT_ROOM", "movie-night")
        )
        self.nameEdit = QtWidgets.QLineEdit(os.getenv("DEFAULT_NAME", "anon"))
        self.fileEdit = QtWidgets.QLineEdit()
        self.fileEdit.setPlaceholderText(
            "Select a local video file (.mkv, .mp4, ...)"
        )
        self.fileBtn = QtWidgets.QPushButton("Browse…")
        self.connectBtn = QtWidgets.QPushButton("Connect")
        self.connectBtn.setCheckable(True)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self._labeled("Room", self.roomEdit), 2)
        top.addWidget(self._labeled("Name", self.nameEdit), 2)
        top.addWidget(self.fileEdit, 6)
        top.addWidget(self.fileBtn, 1)
        top.addWidget(self.connectBtn, 2)

        self.fileBtn.clicked.connect(self._pick_file)
        self.connectBtn.clicked.connect(self._toggle_connect)

        # Player controls
        self.playBtn = QtWidgets.QPushButton("Play")
        self.pauseBtn = QtWidgets.QPushButton("Pause")
        self.seekSlider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.seekSlider.setRange(0, 1000)
        self.seekSlider.setSingleStep(1)
        self.seekSlider.setTracking(False)
        self.timeLabel = QtWidgets.QLabel("00:00 / 00:00")
        self.statusLabel = QtWidgets.QLabel("Disconnected")
        self.statusLabel.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(self.playBtn)
        controls.addWidget(self.pauseBtn)
        controls.addWidget(self.seekSlider, 1)
        controls.addWidget(self.timeLabel)
        controls.addWidget(self.statusLabel)

        self.playBtn.clicked.connect(
            lambda: asyncio.create_task(self._send_control("PLAY"))
        )
        self.pauseBtn.clicked.connect(
            lambda: asyncio.create_task(self._send_control("PAUSE"))
        )
        self.seekSlider.sliderReleased.connect(self._slider_seek)

        # Left: presence
        self.presenceList = QtWidgets.QListWidget()
        self.presenceList.setMinimumWidth(220)

        # Right: chat
        self.chatView = QtWidgets.QTextEdit()
        self.chatView.setReadOnly(True)
        self.chatInput = QtWidgets.QLineEdit()
        self.chatInput.setPlaceholderText("Type a message and press Enter…")
        self.chatSendBtn = QtWidgets.QPushButton("Send")
        self.chatInput.returnPressed.connect(self._send_chat_clicked)
        self.chatSendBtn.clicked.connect(self._send_chat_clicked)

        chatBottom = QtWidgets.QHBoxLayout()
        chatBottom.addWidget(self.chatInput, 1)
        chatBottom.addWidget(self.chatSendBtn)

        chatLayout = QtWidgets.QVBoxLayout()
        chatLayout.addWidget(self.chatView, 1)
        chatLayout.addLayout(chatBottom)

        # Main split
        split = QtWidgets.QHBoxLayout()
        split.addWidget(self._card("People", self.presenceList), 2)
        split.addLayout(chatLayout, 5)

        # Overall layout
        root = QtWidgets.QVBoxLayout(central)
        root.addLayout(top)
        root.addLayout(controls)
        root.addLayout(split, 1)

        # Disable controls until connected
        self._set_controls_enabled(False)

    def _apply_style(self):
        # Simple “nice” theme using Qt stylesheets
        self.setStyleSheet(
            """
            QMainWindow { background: #0b1220; }
            QLabel, QListWidget, QTextEdit, QLineEdit { color: #e5e7eb; font-size: 13px; }
            QLineEdit, QTextEdit, QListWidget {
                background: #0f1a2e;
                border: 1px solid #22314d;
                border-radius: 10px;
                padding: 10px;
            }
            QPushButton {
                background: #2563eb;
                color: white;
                border: none;
                border-radius: 10px;
                padding: 10px 14px;
                font-weight: 600;
            }
            QPushButton:disabled { background: #1f2a44; color: #93a4c7; }
            QPushButton:checked { background: #16a34a; }
            QSlider::groove:horizontal {
                height: 8px;
                background: #1f2a44;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                width: 16px;
                margin: -6px 0;
                border-radius: 8px;
                background: #60a5fa;
            }
            QGroupBox {
                border: 1px solid #22314d;
                border-radius: 12px;
                margin-top: 12px;
                padding: 10px;
            }
            QGroupBox:title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #c7d2fe; }
        """
        )

    def _labeled(
        self, label: str, widget: QtWidgets.QWidget
    ) -> QtWidgets.QWidget:
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        lbl = QtWidgets.QLabel(label)
        lbl.setStyleSheet("color:#c7d2fe; font-weight:600;")
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
        self.playBtn.setEnabled(enabled)
        self.pauseBtn.setEnabled(enabled)
        self.seekSlider.setEnabled(enabled)
        self.chatInput.setEnabled(enabled)
        self.chatSendBtn.setEnabled(enabled)

    def _append_chat(self, text: str):
        self.chatView.append(text)
        self.chatView.verticalScrollBar().setValue(
            self.chatView.verticalScrollBar().maximum()
        )

    def _set_status(self, text: str):
        self.statusLabel.setText(text)

    def _pick_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select video file",
            "",
            "Video Files (*.mkv *.mp4 *.avi *.mov *.webm);;All Files (*)",
        )
        if path:
            self.fileEdit.setText(path)

    def _slider_seek(self):
        # Convert slider (0..1000) to seconds via duration
        asyncio.create_task(self._slider_seek_async())

    async def _slider_seek_async(self):
        if not self.ws or not self.mpv:
            return
        duration = await self.mpv.get_property("duration")
        if not duration or duration <= 0:
            return
        value = self.seekSlider.value() / 1000.0
        target_s = float(duration) * value
        await self._send_control("SEEK", position_ms=int(target_s * 1000))

    def _send_chat_clicked(self):
        msg = self.chatInput.text().strip()
        if not msg:
            return
        self.chatInput.clear()
        asyncio.create_task(self._send_chat(msg))

    @asyncSlot()
    async def _toggle_connect(self):
        if self.connectBtn.isChecked():
            await self._connect()
        else:
            await self._disconnect()

    async def _connect(self):
        room = self.roomEdit.text().strip()
        name = self.nameEdit.text().strip() or "anon"
        file_path = self.fileEdit.text().strip()

        if not room:
            self._set_status("Please enter a room.")
            self.connectBtn.setChecked(False)
            return
        if not file_path or not Path(file_path).exists():
            self._set_status("Please select an existing local file.")
            self.connectBtn.setChecked(False)
            return

        # Start mpv
        try:
            self.mpv = MPV(file_path, mpv_path=self.mpv_path)
            self.mpv.start()
        except Exception as e:
            self._set_status(f"Failed to start mpv: {e}")
            self.connectBtn.setChecked(False)
            return

        ws_url = f"{self.ws_base_url}/room/{room}"
        try:
            self.ws = await websockets.connect(ws_url, ping_interval=None)
        except Exception as e:
            self._set_status(f"WebSocket connect failed: {e}")
            self.connectBtn.setChecked(False)
            return

        self._set_status(f"Connected: {ws_url}")
        self._set_controls_enabled(True)

        # Send join
        await self._send(
            {
                "type": "join",
                "userId": self.user_id,
                "name": name,
            }
        )

        # Start tasks
        asyncio.create_task(self._recv_loop())
        asyncio.create_task(self._heartbeat_loop())

    async def _disconnect(self):
        self._set_controls_enabled(False)
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass
        self.ws = None
        self._set_status("Disconnected")

    async def _send(self, obj: Dict[str, Any]):
        if not self.ws:
            return
        await self.ws.send(json.dumps(obj))

    async def _send_control(
        self, action: str, position_ms: Optional[int] = None
    ):
        payload: Dict[str, Any] = {"type": "control", "action": action}
        if position_ms is not None:
            payload["positionMs"] = int(position_ms)
        await self._send(payload)

    async def _send_chat(self, text: str):
        await self._send({"type": "chat", "text": text[:500]})

    async def _heartbeat_loop(self):
        while self.ws:
            t0 = ms()
            try:
                await self._send({"type": "ping", "t": t0})
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
                    # offset ≈ server - (client_send + rtt/2)
                    self.server_offset_ms = int(server_time - (t0 + rtt / 2))
                elif t == "presence":
                    self._update_presence(msg.get("users", []))
                elif t == "chat":
                    frm = msg.get("from", {})
                    name = frm.get("name", "?")
                    text = msg.get("text", "")
                    self._append_chat(
                        f"<b>{QtGui.QGuiApplication.translate('', name)}</b>: {text}"
                    )
                elif t == "state":
                    state = msg.get("state", {})
                    await self._apply_state(state)
                elif t == "error":
                    self._append_chat(
                        f"<span style='color:#fca5a5'>Error: {msg.get('code')} {msg.get('message')}</span>"
                    )
                elif t == "welcome":
                    # ignore
                    pass
        except Exception as e:
            self._set_status(f"Disconnected (recv error): {e}")
        finally:
            # ensure UI reflects disconnected state
            self.connectBtn.setChecked(False)
            await self._disconnect()

    def _update_presence(self, users: List[Dict[str, Any]]):
        self.presenceList.clear()
        for u in users:
            name = u.get("name", "?")
            self.presenceList.addItem(name)

    async def _apply_state(self, state: Dict[str, Any]):
        # ignore old versions
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
            mediaKey=str(state.get("mediaKey", "")),
        )

        if not self.mpv:
            return

        # Compute target time now
        server_now = ms() + self.server_offset_ms
        if self.state.isPlaying:
            target_ms = self.state.positionMs + max(
                0, server_now - self.state.updatedAt
            )
        else:
            target_ms = self.state.positionMs
        target_s = max(0.0, target_ms / 1000.0)

        cur_s = await self.mpv.get_property("time-pos")
        paused = await self.mpv.get_property("pause")
        if cur_s is None:
            return
        cur_s = float(cur_s)
        paused = bool(paused) if paused is not None else False

        # Apply play/pause
        if self.state.isPlaying and paused:
            await self.mpv.play()
        elif (not self.state.isPlaying) and (not paused):
            await self.mpv.pause()

        # Drift correction
        drift = target_s - cur_s
        if abs(drift) > 2.0:
            await self.mpv.seek_to(target_s)
        elif abs(drift) > 0.6:
            await self.mpv.seek_to(target_s)
        # else: ignore small drift to avoid jitter

        self._set_status(
            f"Synced v{version} (by {self.state.updatedBy}) drift {drift:+.2f}s"
        )

    def _format_time(self, seconds: float) -> str:
        seconds = int(max(0, seconds))
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    def _on_tick(self):
        # Update slider + time label from local mpv (no network requests)
        asyncio.create_task(self._update_player_ui())

    async def _update_player_ui(self):
        if not self.mpv:
            return
        cur = await self.mpv.get_property("time-pos")
        dur = await self.mpv.get_property("duration")
        if cur is None or dur is None or dur <= 0:
            return
        cur = float(cur)
        dur = float(dur)
        self.timeLabel.setText(
            f"{self._format_time(cur)} / {self._format_time(dur)}"
        )

        # avoid fighting user while they drag; tracking is off but still be safe
        if not self.seekSlider.isSliderDown():
            value = int(clamp((cur / dur) * 1000.0, 0, 1000))
            self.seekSlider.setValue(value)


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
