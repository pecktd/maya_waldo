"""Turns cursor + pinch state into Maya scene edits. Maya-side only.

Everything here must run on Maya's main thread -- the socket reader lives
in a worker thread and hands state over via a queued Qt signal.
"""

import maya.api.OpenMaya as om
import maya.api.OpenMayaUI as omui
import maya.cmds as cmds

# Re-picking on every frame is wasteful and makes the highlight flicker.
# Only re-select once the cursor has travelled this far (viewport fraction).
REPICK_DISTANCE = 0.012


def _active_view():
    try:
        return omui.M3dView.active3dView()
    except RuntimeError:
        return None


def _view_direction(view):
    """World-space forward vector of the viewport camera."""
    camera_path = view.getCamera()
    return om.MFnCamera(camera_path).viewDirection(om.MSpace.kWorld)


def to_port_pixels(view, x_norm, y_norm):
    """Normalised (0-1, origin bottom-left) -> viewport pixels."""
    return int(x_norm * view.portWidth()), int(y_norm * view.portHeight())


def pick_at(x_norm, y_norm):
    """Select whatever sits under the cursor. Returns the transform, or None.

    Maya's own selection highlight doubles as the hover feedback, so
    there's nothing extra to draw.
    """
    view = _active_view()
    if view is None:
        return None

    px, py = to_port_pixels(view, x_norm, y_norm)
    try:
        om.MGlobal.selectFromScreen(px, py, om.MGlobal.kReplaceList)
    except RuntimeError:
        return None

    chosen = cmds.ls(selection=True, long=True, transforms=True)
    if not chosen:
        # selectFromScreen can land on the shape; walk up to its transform.
        shapes = cmds.ls(selection=True, long=True)
        if not shapes:
            return None
        parents = cmds.listRelatives(shapes[0], parent=True, fullPath=True)
        if not parents:
            return None
        chosen = parents
        cmds.select(chosen[0], replace=True)
    return chosen[0]


def plane_hit(view, x_norm, y_norm, plane_point, plane_normal):
    """Intersect the cursor ray with a world plane. Returns MPoint or None.

    The plane is the camera-facing one through the grabbed object, which
    is what makes dragging feel like sliding the object across the screen
    -- correct for both perspective and orthographic cameras.
    """
    px, py = to_port_pixels(view, x_norm, y_norm)
    origin, direction = view.viewToWorld(px, py)

    denominator = plane_normal * direction
    if abs(denominator) < 1e-9:  # ray parallel to the plane
        return None

    distance = (plane_normal * (om.MVector(plane_point) - om.MVector(origin))) / denominator
    if distance < 0:  # plane is behind the camera
        return None
    return om.MPoint(om.MVector(origin) + direction * distance)


class GestureManipulator:
    """Drives selection and dragging from a stream of gesture states."""

    def __init__(self):
        self.target = None          # transform being dragged
        self._last_hit = None       # plane intersection from the previous frame
        self._plane_point = None
        self._plane_normal = None
        self._last_pick = None      # cursor pos at the last re-pick
        self._undo_open = False
        self.status = "idle"

    # -- grab lifecycle ---------------------------------------------------

    def _begin_grab(self, x, y):
        chosen = cmds.ls(selection=True, long=True, transforms=True)
        if not chosen:
            self.status = "nothing under cursor"
            return

        view = _active_view()
        if view is None:
            return

        self.target = chosen[0]
        pivot = cmds.xform(self.target, query=True, worldSpace=True, rotatePivot=True)
        self._plane_point = om.MPoint(pivot[0], pivot[1], pivot[2])
        self._plane_normal = _view_direction(view)
        self._last_hit = plane_hit(view, x, y, self._plane_point, self._plane_normal)

        if self._last_hit is None:
            self.target = None
            self.status = "bad grab angle"
            return

        # One grab-to-release should collapse into a single undo step.
        cmds.undoInfo(openChunk=True, chunkName="Gesture drag")
        self._undo_open = True
        self.status = "grabbed %s" % self.target.rsplit("|", 1)[-1]

    def _drag(self, x, y):
        view = _active_view()
        if view is None or self._last_hit is None:
            return

        hit = plane_hit(view, x, y, self._plane_point, self._plane_normal)
        if hit is None:
            return

        delta = om.MVector(hit) - om.MVector(self._last_hit)
        if delta.length() < 1e-6:
            return

        # Relative world move: survives hierarchy, pivots and frozen transforms
        # in a way that setting an absolute translate does not.
        try:
            cmds.move(delta.x, delta.y, delta.z, self.target,
                      relative=True, worldSpace=True)
        except RuntimeError as exc:
            self.status = "cannot move: %s" % exc
            self._release()
            return
        self._last_hit = hit

    def _release(self):
        if self._undo_open:
            cmds.undoInfo(closeChunk=True)
            self._undo_open = False
        self.target = None
        self._last_hit = None
        self.status = "idle"

    # -- entry point ------------------------------------------------------

    def update(self, state):
        """Consume one gesture state from the tracker."""
        if not state.get("found"):
            if self.target:
                self._release()
                self.status = "lost hand - dropped"
            return

        x, y = state["x"], state["y"]
        pinching = state["pinch"]

        if pinching and self.target is None:
            self._begin_grab(x, y)
        elif pinching and self.target is not None:
            self._drag(x, y)
        elif not pinching and self.target is not None:
            self._release()
        else:
            # Open hand: hover-pick, throttled so we don't thrash selection.
            if (self._last_pick is None
                    or abs(x - self._last_pick[0]) > REPICK_DISTANCE
                    or abs(y - self._last_pick[1]) > REPICK_DISTANCE):
                self._last_pick = (x, y)
                found = pick_at(x, y)
                self.status = ("hover %s" % found.rsplit("|", 1)[-1]) if found else "idle"

    def cleanup(self):
        """Always call on shutdown -- an unclosed undo chunk corrupts the queue."""
        if self._undo_open:
            cmds.undoInfo(closeChunk=True)
            self._undo_open = False
