"""Webcam hand tracker. Runs in its own Python (NOT in Maya).

Captures the webcam, finds one hand with MediaPipe, derives a cursor
position and a pinch state, and streams both -- plus a JPEG preview --
to the Maya panel over a local socket.

Kept out of Maya's interpreter on purpose: mediapipe/opencv drag in their
own numpy, which fights Maya's bundled one, and a blocking camera read
would stall Maya's UI thread.

Usage:
    python waldo_tracker.py [--port 5599] [--camera 0] [--no-preview]
"""

import argparse
import math
import os
import socket
import sys
import time

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python import vision

import waldo_protocol as proto

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "models", "hand_landmarker.task")
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker"
             "/hand_landmarker/float16/latest/hand_landmarker.task")

HAND_EDGES = [(c.start, c.end) for c in vision.HandLandmarksConnections.HAND_CONNECTIONS]

# MediaPipe hand landmark indices we care about.
WRIST = 0
THUMB_TIP = 4
INDEX_TIP = 8
MIDDLE_MCP = 9

# Pinch thresholds as a ratio of hand size, with hysteresis so the grab
# doesn't flicker when you hover right at the boundary.
PINCH_ON = 0.38
PINCH_OFF = 0.55


class OneEuroFilter:
    """Low-lag smoothing filter.

    A plain moving average forces a choice between jitter and lag. This
    adapts: it smooths hard when the hand is still (killing tracker
    jitter) and backs off when the hand moves fast (keeping response
    tight). See Casiez et al., "1e Filter", CHI 2012.
    """

    def __init__(self, min_cutoff=1.0, beta=0.02, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x_prev = None
        self._dx_prev = 0.0
        self._t_prev = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, t):
        if self._x_prev is None:
            self._x_prev, self._t_prev = x, t
            return x

        dt = t - self._t_prev
        if dt <= 0:
            return self._x_prev
        self._t_prev = t

        dx = (x - self._x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1.0 - a_d) * self._dx_prev
        self._dx_prev = dx_hat

        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1.0 - a) * self._x_prev
        self._x_prev = x_hat
        return x_hat


class HandReader:
    """Wraps MediaPipe and turns raw landmarks into cursor + pinch state."""

    def __init__(self):
        ensure_model()
        options = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=MODEL_PATH),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.6,
            min_tracking_confidence=0.5,
        )
        self._hands = vision.HandLandmarker.create_from_options(options)
        self._fx = OneEuroFilter()
        self._fy = OneEuroFilter()
        self._pinching = False
        self._last_ms = -1

    def close(self):
        self._hands.close()

    def read(self, bgr_frame, now):
        """Returns (state_dict, landmarks_or_None) for one frame."""
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        # VIDEO mode demands strictly increasing timestamps.
        stamp = max(int(now * 1000), self._last_ms + 1)
        self._last_ms = stamp
        result = self._hands.detect_for_video(image, stamp)

        if not result.hand_landmarks:
            self._pinching = False
            return {"found": False, "pinch": False}, None

        lm = result.hand_landmarks[0]

        # Normalise pinch distance by hand size so it works at any depth.
        span = math.dist(
            (lm[WRIST].x, lm[WRIST].y), (lm[MIDDLE_MCP].x, lm[MIDDLE_MCP].y)
        )
        gap = math.dist(
            (lm[THUMB_TIP].x, lm[THUMB_TIP].y), (lm[INDEX_TIP].x, lm[INDEX_TIP].y)
        )
        ratio = gap / span if span > 1e-6 else 999.0

        threshold = PINCH_OFF if self._pinching else PINCH_ON
        self._pinching = ratio < threshold

        # Track the pinch point (thumb/index midpoint) rather than the
        # fingertip -- it stays put as the fingers close, so grabbing
        # doesn't yank the object sideways.
        raw_x = (lm[THUMB_TIP].x + lm[INDEX_TIP].x) * 0.5
        raw_y = (lm[THUMB_TIP].y + lm[INDEX_TIP].y) * 0.5

        state = {
            "found": True,
            "pinch": self._pinching,
            "pinch_ratio": round(ratio, 3),
            # Flip x: the preview is mirrored, so move-right must read as right.
            "x": round(1.0 - self._fx(raw_x, now), 4),
            # Flip y: image space is top-down, viewports are bottom-up.
            "y": round(1.0 - self._fy(raw_y, now), 4),
        }
        return state, lm


def ensure_model():
    """Fetch the hand landmarker model on first run."""
    if os.path.exists(MODEL_PATH):
        return
    import urllib.request

    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    print("Downloading hand landmarker model...", flush=True)
    urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)


def draw_overlay(frame, landmarks, state):
    """Annotate the preview so the user can see what's being tracked."""
    height, width = frame.shape[:2]

    if landmarks is not None:
        points = [(int(p.x * width), int(p.y * height)) for p in landmarks]
        for start, end in HAND_EDGES:
            cv2.line(frame, points[start], points[end], (80, 220, 80), 2)
        for point in points:
            cv2.circle(frame, point, 3, (240, 240, 240), -1)

    if state.get("found"):
        # state x is already mirrored for Maya; un-mirror it for the preview.
        cx = int((1.0 - state["x"]) * width)
        cy = int((1.0 - state["y"]) * height)
        colour = (0, 200, 255) if state["pinch"] else (180, 180, 180)
        cv2.circle(frame, (cx, cy), 14, colour, -1 if state["pinch"] else 2)

    label = "no hand"
    if state.get("found"):
        label = "PINCH" if state["pinch"] else "open"
    cv2.putText(
        frame, label, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2
    )
    return frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=proto.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=proto.DEFAULT_PORT)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument(
        "--preview-width",
        type=int,
        default=320,
        help="width of the JPEG sent to Maya; smaller keeps the socket cheap",
    )
    parser.add_argument(
        "--no-preview", action="store_true", help="send gesture state only"
    )
    parser.add_argument(
        "--show-window",
        action="store_true",
        help="also open a local OpenCV window (handy for debugging alone)",
    )
    parser.add_argument(
        "--solo",
        action="store_true",
        help="run without Maya: opens a local window only, connects to nothing",
    )
    args = parser.parse_args()
    if args.solo:
        args.show_window = True

    cam = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    if not cam.isOpened():
        sys.exit("Could not open camera %d" % args.camera)

    sock = None
    if not args.solo:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((args.host, args.port))
        except OSError as exc:
            cam.release()
            sys.exit(
                "Could not reach the Maya panel on %s:%d (%s).\n"
                "Open the panel in Maya first -- it listens, this connects."
                % (args.host, args.port, exc)
            )
        print("Connected to Maya panel. Ctrl+C to stop.", flush=True)
    else:
        print("Solo mode: local preview only. Esc to quit.", flush=True)
    reader = HandReader()

    try:
        while True:
            ok, frame = cam.read()
            if not ok:
                time.sleep(0.01)
                continue

            frame = cv2.flip(frame, 1)  # mirror, so the preview reads naturally
            state, landmarks = reader.read(frame, time.monotonic())

            jpeg = None
            if not args.no_preview:
                preview = draw_overlay(frame.copy(), landmarks, state)
                scale = args.preview_width / preview.shape[1]
                if scale < 1.0:
                    preview = cv2.resize(
                        preview, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
                    )
                ok, buf = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    jpeg = buf.tobytes()

                if args.show_window:
                    cv2.imshow("tracker", preview)
                    if cv2.waitKey(1) & 0xFF == 27:
                        break

            if sock is not None:
                try:
                    proto.send_message(sock, state, jpeg)
                except OSError:
                    print("Maya panel disconnected.", flush=True)
                    break
    except KeyboardInterrupt:
        pass
    finally:
        reader.close()
        cam.release()
        cv2.destroyAllWindows()
        if sock is not None:
            sock.close()


if __name__ == "__main__":
    main()
