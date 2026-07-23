"""Waldo's dockable Maya panel: shows the webcam feed, drives the manipulator.

Maya listens, the tracker connects. That way the panel owns the lifecycle
-- you can restart the tracker without touching Maya.
"""

import os
import subprocess
import sys

from PySide2 import QtCore, QtGui, QtWidgets
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
import maya.cmds as cmds

import waldo_protocol as proto
from waldo_manip import GestureManipulator

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOW_OBJECT = "waldoPanel"
LOG_PATH = os.path.join(HERE, "waldo.log")

# Capture resolutions offered in the panel. Lower is lighter: tracking cost
# scales with pixel count, so drop this when the feed feels laggy. Ordered
# fastest-first; the default (index 0) favours responsiveness over detail.
RESOLUTIONS = [
    ("640 x 480 (fastest)", 640, 480),
    ("960 x 540", 960, 540),
    ("1280 x 720 (HD)", 1280, 720),
    ("1920 x 1080 (Full HD)", 1920, 1080),
]


def tracker_python():
    """Path to the interpreter that has mediapipe -- the project venv."""
    venv = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    return venv if os.path.exists(venv) else "python"


class BridgeThread(QtCore.QThread):
    """Accepts the tracker's connection and pumps its messages to the UI."""

    received = QtCore.Signal(object, object)  # (state dict, jpeg bytes or None)
    notice = QtCore.Signal(str)

    def __init__(self, port, parent=None):
        super(BridgeThread, self).__init__(parent)
        self.port = port
        self._server = None
        self._client = None
        self._stop = False

    def run(self):
        try:
            self._server = proto.make_server(port=self.port)
        except OSError as exc:
            self.notice.emit("Port %d unavailable: %s" % (self.port, exc))
            return

        # Outer loop: keep serving, so Stop camera -> Start camera reconnects
        # instead of leaving a dead listener behind.
        while not self._stop:
            self.notice.emit("Waiting for tracker on port %d..." % self.port)
            try:
                self._client, _ = self._server.accept()
            except OSError:
                return  # socket closed by stop()

            self.notice.emit("Tracker connected.")
            try:
                while not self._stop:
                    message = proto.recv_message(self._client)
                    if message is None:
                        break
                    state, jpeg = message
                    self.received.emit(state, jpeg)
            except OSError:
                pass
            finally:
                try:
                    self._client.close()
                except OSError:
                    pass
                self._client = None
            if not self._stop:
                self.notice.emit("Tracker disconnected.")

    def stop(self):
        self._stop = True
        # Closing the sockets is what unblocks accept()/recv().
        for sock in (self._client, self._server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self.wait(2000)


class LiveManipulatorPanel(MayaQWidgetDockableMixin, QtWidgets.QWidget):

    def __init__(self, port=proto.DEFAULT_PORT, parent=None):
        super(LiveManipulatorPanel, self).__init__(parent=parent)
        self.setObjectName(WINDOW_OBJECT)
        self.setWindowTitle("Waldo")
        self.setMinimumWidth(300)

        self.port = port
        self.manipulator = GestureManipulator()
        self.bridge = None
        self.process = None
        self._log = None
        self._enabled = False

        self._build_ui()
        self._start_bridge()

    # -- ui ---------------------------------------------------------------

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self.video = QtWidgets.QLabel("no signal")
        self.video.setAlignment(QtCore.Qt.AlignCenter)
        self.video.setMinimumHeight(220)
        # Ignored policy: otherwise each scaled pixmap enlarges the label's
        # size hint and the panel creeps wider every frame.
        self.video.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                 QtWidgets.QSizePolicy.Ignored)
        self.video.setStyleSheet(
            "background:#1e1e1e; color:#777; border:1px solid #3a3a3a;")
        layout.addWidget(self.video, 1)

        self.gesture_label = QtWidgets.QLabel("hand: -")
        self.gesture_label.setStyleSheet("font-weight:bold;")
        layout.addWidget(self.gesture_label)

        self.status_label = QtWidgets.QLabel("starting...")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color:#9a9a9a;")
        layout.addWidget(self.status_label)

        self.enable_box = QtWidgets.QCheckBox("Control Maya with gestures")
        self.enable_box.toggled.connect(self._on_enable_toggled)
        layout.addWidget(self.enable_box)

        res_row = QtWidgets.QHBoxLayout()
        res_row.addWidget(QtWidgets.QLabel("Camera res:"))
        self.res_combo = QtWidgets.QComboBox()
        for label, _w, _h in RESOLUTIONS:
            self.res_combo.addItem(label)
        self.res_combo.setToolTip(
            "Resolution the tracker captures at. Takes effect next time you "
            "start the camera; restart it to apply a change.")
        res_row.addWidget(self.res_combo, 1)
        layout.addLayout(res_row)

        buttons = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start camera")
        self.start_button.clicked.connect(self.start_tracker)
        buttons.addWidget(self.start_button)

        self.stop_button = QtWidgets.QPushButton("Stop camera")
        self.stop_button.clicked.connect(self.stop_tracker)
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.stop_button)
        layout.addLayout(buttons)

        hint = QtWidgets.QLabel(
            "Open hand to hover • pinch thumb+index to grab • "
            "move to drag • release to drop")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#6f6f6f; font-size:10px;")
        layout.addWidget(hint)

    # -- wiring -----------------------------------------------------------

    def _start_bridge(self):
        self.bridge = BridgeThread(self.port, parent=self)
        # Queued by default across threads, so the slots run on Maya's
        # main thread -- mandatory for any cmds call.
        self.bridge.received.connect(self._on_state)
        self.bridge.notice.connect(self.status_label.setText)
        self.bridge.start()

    def start_tracker(self):
        if self.process and self.process.poll() is None:
            return

        _label, width, height = RESOLUTIONS[self.res_combo.currentIndex()]
        command = [tracker_python(), os.path.join(HERE, "waldo_tracker.py"),
                   "--port", str(self.port),
                   "--width", str(width), "--height", str(height)]
        creation = 0
        if sys.platform == "win32":  # keep the console window out of the way
            creation = subprocess.CREATE_NO_WINDOW

        # The console is hidden, so send output to a log -- otherwise a
        # busy camera or missing dependency fails completely silently.
        try:
            self._log = open(LOG_PATH, "w")
            self.process = subprocess.Popen(
                command, cwd=HERE, creationflags=creation,
                stdout=self._log, stderr=subprocess.STDOUT)
        except OSError as exc:
            self.status_label.setText("Could not launch tracker: %s" % exc)
            return

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        # Resolution is fixed at launch, so lock it while the camera runs.
        self.res_combo.setEnabled(False)
        self.status_label.setText("Tracker starting (camera warm-up ~2s)...")
        # Catch an early crash and surface the reason instead of hanging
        # on "Waiting for tracker...".
        QtCore.QTimer.singleShot(4000, self._check_tracker_alive)

    def _check_tracker_alive(self):
        if self.process is None or self.process.poll() is None:
            return
        reason = ""
        try:
            with open(LOG_PATH) as handle:
                lines = [ln.strip() for ln in handle if ln.strip()]
            if lines:
                reason = lines[-1]
        except OSError:
            pass
        self.status_label.setText(
            "Tracker exited. %s\n(full log: %s)" % (reason, LOG_PATH))
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.res_combo.setEnabled(True)

    def stop_tracker(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.process = None
        if self._log:
            self._log.close()
            self._log = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.res_combo.setEnabled(True)
        self.video.setText("no signal")
        self.video.setPixmap(QtGui.QPixmap())
        self.status_label.setText("Camera stopped.")

    def _on_enable_toggled(self, checked):
        self._enabled = checked
        if not checked:
            self.manipulator.cleanup()
            self.manipulator = GestureManipulator()

    def _on_state(self, state, jpeg):
        if jpeg:
            image = QtGui.QImage.fromData(jpeg, "JPG")
            if not image.isNull():
                self.video.setPixmap(QtGui.QPixmap.fromImage(image).scaled(
                    self.video.size(), QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation))

        if not state.get("found"):
            self.gesture_label.setText("hand: none")
        else:
            self.gesture_label.setText(
                "hand: %s   (pinch %.2f)"
                % ("PINCH" if state["pinch"] else "open", state["pinch_ratio"]))

        if self._enabled:
            try:
                self.manipulator.update(state)
            except Exception as exc:  # never let one bad frame kill the stream
                self.status_label.setText("error: %s" % exc)
            else:
                self.status_label.setText(self.manipulator.status)

    # -- teardown ---------------------------------------------------------

    def closeEvent(self, event):
        self.stop_tracker()
        if self.bridge:
            self.bridge.stop()
        self.manipulator.cleanup()
        super(LiveManipulatorPanel, self).closeEvent(event)


_panel = None


def show(port=proto.DEFAULT_PORT):
    """Open the panel, replacing any existing one."""
    global _panel

    if _panel is not None:
        try:
            _panel.close()
            _panel.deleteLater()
        except RuntimeError:
            pass
        _panel = None

    # A stale workspace control keeps the old, dead widget alive.
    control = WINDOW_OBJECT + "WorkspaceControl"
    if cmds.workspaceControl(control, exists=True):
        cmds.deleteUI(control)

    _panel = LiveManipulatorPanel(port=port)
    _panel.show(dockable=True)
    return _panel
