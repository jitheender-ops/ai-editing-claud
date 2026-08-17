"""
Camera and digital motion, by affine estimation.

Between two sampled frames, track a few hundred corners with sparse optical flow
and fit a partial affine transform to the correspondences. That fit decomposes
directly into the two things an editing style cares about: a scale factor (a zoom
or punch-in) and a translation (a pan or reframe).

Partial affine — translation, rotation, uniform scale — rather than a full
homography on purpose. Four degrees of freedom is what a camera move or a digital
reframe actually has, and the extra freedom of a homography mostly buys the
ability to fit noise convincingly.

Two things this does not distinguish, and the DNA says so rather than pretending:
a physical camera push from a digital punch-in (they are the same pixels), and a
zoom from a dolly. Both read as scale, because from a rendered video that is all
there is to read.

Runs on the 480p proxy at a few frames per second. Corner tracking is the most
expensive thing in the whole analysis pass, and full resolution buys nothing —
a 5% zoom is a 5% zoom at any resolution.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

ANALYZER_VERSION = "1.0"

#: Scale change per second below this is tracking noise, not a move.
ZOOM_EPSILON = 0.012
#: Translation per second, as a fraction of frame width, below which nothing moved.
PAN_EPSILON = 0.008

_MIN_TRACKED_POINTS = 12


@dataclass
class MotionSample:
    at_s: float
    scale: float  # 1.0 = no zoom; >1 pushing in
    dx: float  # fraction of frame width
    dy: float


@dataclass
class MotionProfile:
    samples: list[MotionSample] = field(default_factory=list)
    #: Fraction of sampled intervals containing a real zoom.
    zoom_frequency: float = 0.0
    pan_frequency: float = 0.0
    #: Typical magnitude of a zoom that is happening, as a per-second rate.
    zoom_magnitude: float = 0.0
    static_ratio: float = 1.0
    #: 0..1 — how much of the footage this was actually derived from.
    coverage: float = 0.0

    @property
    def measured(self) -> bool:
        """Fewer than a handful of usable samples is not a measurement."""
        return len(self.samples) >= 4 and self.coverage > 0.25


def analyse_motion(
    path: Path | str, *, sample_fps: float = 4.0, max_samples: int = 400
) -> MotionProfile:
    import cv2
    import numpy as np

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return MotionProfile()

    source_fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    step = max(1, int(round(source_fps / sample_fps)))
    if total <= step:
        capture.release()
        return MotionProfile()

    # Cap the work on long files: sampling every Nth frame of a 40-minute video
    # is still tens of thousands of frames, and the statistics converge long
    # before that.
    planned = min(max_samples, total // step)
    stride = max(step, (total // planned) if planned else step)

    samples: list[MotionSample] = []
    attempted = 0
    previous_grey = None
    previous_index = 0

    for index in range(0, total, stride):
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()
        if not ok:
            continue
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if previous_grey is not None:
            attempted += 1
            gap_s = (index - previous_index) / source_fps
            estimate = _estimate(previous_grey, grey, cv2, np)
            if estimate and gap_s > 0:
                scale, dx, dy = estimate
                samples.append(
                    MotionSample(
                        at_s=index / source_fps,
                        # Normalise to per-second so sampling rate does not change
                        # the numbers.
                        scale=1.0 + (scale - 1.0) / gap_s,
                        dx=dx / gap_s,
                        dy=dy / gap_s,
                    )
                )
        previous_grey, previous_index = grey, index
        if len(samples) >= max_samples:
            break

    capture.release()

    if not samples:
        return MotionProfile(coverage=0.0)

    zooming = [s for s in samples if abs(s.scale - 1.0) > ZOOM_EPSILON]
    panning = [s for s in samples if max(abs(s.dx), abs(s.dy)) > PAN_EPSILON]
    moving = {id(s) for s in zooming} | {id(s) for s in panning}

    return MotionProfile(
        samples=samples,
        zoom_frequency=round(len(zooming) / len(samples), 3),
        pan_frequency=round(len(panning) / len(samples), 3),
        zoom_magnitude=(
            round(statistics.fmean(abs(s.scale - 1.0) for s in zooming), 4) if zooming else 0.0
        ),
        static_ratio=round(1.0 - len(moving) / len(samples), 3),
        coverage=round(len(samples) / attempted, 3) if attempted else 0.0,
    )


def _estimate(previous, current, cv2, np) -> tuple[float, float, float] | None:
    """Fit a partial affine between two frames. None when the fit is untrustworthy."""
    corners = cv2.goodFeaturesToTrack(
        previous, maxCorners=300, qualityLevel=0.01, minDistance=8, blockSize=7
    )
    if corners is None or len(corners) < _MIN_TRACKED_POINTS:
        # A flat or featureless frame — a slide, a solid background — genuinely
        # has nothing to track. Reporting "no motion" would be a guess.
        return None

    tracked, status, _ = cv2.calcOpticalFlowPyrLK(previous, current, corners, None)
    if tracked is None or status is None:
        return None

    keep = status.ravel() == 1
    source, destination = corners[keep], tracked[keep]
    if len(source) < _MIN_TRACKED_POINTS:
        return None

    matrix, inliers = cv2.estimateAffinePartial2D(
        source, destination, method=cv2.RANSAC, ransacReprojThreshold=3
    )
    if matrix is None or inliers is None or int(inliers.sum()) < _MIN_TRACKED_POINTS:
        return None

    # For a partial affine [[s·cosθ, -s·sinθ, tx], [s·sinθ, s·cosθ, ty]],
    # the uniform scale is the norm of the first column.
    scale = float(np.hypot(matrix[0, 0], matrix[1, 0]))

    height, width = previous.shape[:2]
    width = width or 1

    # A zoom anchored at the frame centre — which is what a punch-in is — puts a
    # translation of (1-s)·centre into the affine even though nothing panned.
    # Subtracting it is what stops every zoom being counted as a pan too, which
    # would otherwise inflate pan_frequency to 1.0 on any pushed-in footage.
    centre_x, centre_y = width / 2.0, height / 2.0
    induced_x = (1.0 - scale) * centre_x
    induced_y = (1.0 - scale) * centre_y

    residual_x = (float(matrix[0, 2]) - induced_x) / width
    residual_y = (float(matrix[1, 2]) - induced_y) / width
    return scale, residual_x, residual_y
