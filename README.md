# Waldo

Grab and move objects in the Maya viewport with your hand, via webcam.

Open hand → hover/select · pinch thumb+index → grab · move → drag · release → drop.

Named after the Heinlein story — a *waldo* is a remote manipulator you drive by
moving your own hand, which is exactly the trick here.

## How it's wired

```
  waldo_tracker.py (venv 3.12)            waldo_panel.py (Maya 2024, py3.10)
  ┌─────────────────────────┐             ┌──────────────────────────────┐
  │ OpenCV webcam capture   │   TCP       │ BridgeThread (worker)        │
  │ MediaPipe HandLandmarker│  127.0.0.1  │   ↓ queued Qt signal         │
  │ pinch + One Euro filter │ ──5599────► │ Panel (Maya main thread)     │
  │ JPEG preview encode     │  JSON+JPEG  │   ↓                          │
  └─────────────────────────┘             │ GestureManipulator → cmds.*  │
                                          └──────────────────────────────┘
```

Two processes on purpose:

- **Dependency isolation.** MediaPipe and OpenCV bring their own numpy, which
  conflicts with the numpy Maya bundles. Installing them into Maya risks
  breaking Maya itself.
- **No UI stalls.** A blocking camera read on Maya's main thread would freeze
  the viewport. Here the camera never touches Maya's thread.

Maya *listens* and the tracker *connects*, so you can stop and restart the
camera without restarting Maya.

## Setup

Already done in this repo, but to recreate it:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install mediapipe opencv-python
```

The hand model (`models/hand_landmarker.task`, ~7.8 MB) downloads automatically
on first run.

## Use

1. In Maya, open the Script Editor (Python tab), paste in the contents of
   `launch_in_maya.py`, and run it. The panel appears — dock it anywhere.
2. Click **Start camera**. Wait ~2s for warm-up; you'll see yourself.
3. Create something to grab: `Poly Cube`.
4. Tick **Control Maya with gestures**.
5. Open hand to move the cursor and highlight objects; pinch to grab; move to
   drag; release to drop.

Keep the checkbox off while you just want to watch the tracking — nothing
touches the scene until it's ticked.

## Notes

- **Dragging is view-plane.** The object slides along the plane facing the
  camera that passes through its pivot. Tumble the viewport to move along a
  different axis.
- **One grab = one undo.** Each grab-to-release collapses into a single undo
  step named "Gesture drag".
- **Selection is the highlight.** Hovering uses Maya's own selection, so the
  usual green highlight tells you what you're about to grab.

## Tuning

| What | Where | Default |
|---|---|---|
| Pinch sensitivity | `waldo_tracker.py` `PINCH_ON` / `PINCH_OFF` | 0.38 / 0.55 |
| Cursor smoothing vs lag | `waldo_tracker.py` `OneEuroFilter(min_cutoff, beta)` | 1.0, 0.02 |
| Hover re-pick threshold | `waldo_manip.py` `REPICK_DISTANCE` | 0.012 |
| Camera index | tracker `--camera N` | 0 |

`PINCH_ON` and `PINCH_OFF` differ deliberately — the gap is hysteresis, so a
grab holds steady instead of flickering when your fingers hover at the
threshold. Raise `PINCH_ON` if grabbing feels hard; lower it if it grabs
by accident.

## Troubleshooting

- **"Waiting for tracker..." forever** — check `waldo.log` in this folder.
  Usual cause is the camera being held by another app (Teams, Zoom, browser).
- **Test the camera without Maya:**
  `.venv/Scripts/python.exe waldo_tracker.py --solo` opens a local preview
  window. Esc to quit.
- **Cursor drifts / too jittery** — tune the One Euro filter: raise `beta` to
  cut lag on fast moves, lower `min_cutoff` to smooth more when still.
- **Nothing gets grabbed** — the object must be selectable and under the
  cursor when you pinch. Watch the status line; it says "nothing under cursor".

## Files

| File | Runs in | Purpose |
|---|---|---|
| `waldo_tracker.py` | venv 3.12 | Camera, hand tracking, gesture detection |
| `waldo_protocol.py` | both | Wire format (stdlib only, so both can import it) |
| `waldo_panel.py` | Maya | Dockable UI, bridge thread, tracker process |
| `waldo_manip.py` | Maya | Viewport math, selection, dragging, undo |
| `launch_in_maya.py` | Maya | Paste-and-run launcher |

Modules keep the `waldo_` prefix because they sit flat on Maya's `sys.path`,
where a bare `tracker` or `protocol` would be an easy collision.

## Possible next steps

Rotate/scale via two-hand gestures, a fist-to-tumble camera gesture, or
snapping the drag to a single world axis.
