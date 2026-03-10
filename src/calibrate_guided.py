"""
calibrate_guided.py — Operator-guided calibration session.

Walks you through 20 checkerboard positions chosen to maximise calibration
coverage. In manual mode, the script tells you where to place the board,
waits for your confirmation, captures one frame, then tells you whether to
keep it or adjust and retry.

Audio cues:
  - 1 beep  = move to next position
  - 2 beeps = capturing now
  - 2 short beeps after = GOOD, frame saved
  - 3 rapid beeps after = adjust and retry

Usage:
    python3 src/calibrate_guided.py
    python3 src/calibrate_guided.py --square-mm 24.0
    python3 src/calibrate_guided.py --auto
"""

import argparse
import json
import logging
import subprocess
import time
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_FIRMWARE_URL = "http://192.168.4.202:8000"
DEFAULT_API_VERSION  = "latest"
DEFAULT_CAMERA       = "arducam_64mp"
DEFAULT_SQUARE_MM    = 24.0
DEFAULT_COLS         = 9
DEFAULT_ROWS         = 6
DETECT_SCALE         = 0.25
PRE_CAPTURE_WAIT     = 2.0   # seconds to hold still before capture

# 20 positions chosen for good calibration coverage:
#   - 5 frame zones (centre + 4 corners)
#   - 4 tilt axes (left/right/up/down) at centre
#   - edges of frame
#   - near/far distances
#   - combined corner + tilt for cross-terms
POSITIONS = [
    {"instruction": "CENTRE — flat-on, medium distance (~300mm)", "zone": "center", "scale": "medium"},
    {"instruction": "TOP-LEFT corner — flat-on", "zone": "top-left", "scale": "medium"},
    {"instruction": "TOP-RIGHT corner — flat-on", "zone": "top-right", "scale": "medium"},
    {"instruction": "BOTTOM-LEFT corner — flat-on", "zone": "bottom-left", "scale": "medium"},
    {"instruction": "BOTTOM-RIGHT corner — flat-on", "zone": "bottom-right", "scale": "medium"},
    {"instruction": "CENTRE — tilt LEFT ~30°", "zone": "center", "scale": "medium"},
    {"instruction": "CENTRE — tilt RIGHT ~30°", "zone": "center", "scale": "medium"},
    {"instruction": "CENTRE — tilt UP ~30°", "zone": "center", "scale": "medium"},
    {"instruction": "CENTRE — tilt DOWN ~30°", "zone": "center", "scale": "medium"},
    {"instruction": "LEFT EDGE — flat-on", "zone": "left-edge", "scale": "medium"},
    {"instruction": "RIGHT EDGE — flat-on", "zone": "right-edge", "scale": "medium"},
    {"instruction": "TOP EDGE — flat-on", "zone": "top-edge", "scale": "medium"},
    {"instruction": "BOTTOM EDGE — flat-on", "zone": "bottom-edge", "scale": "medium"},
    {"instruction": "CENTRE — CLOSE (~150mm)", "zone": "center", "scale": "close"},
    {"instruction": "CENTRE — FAR (~450mm)", "zone": "center", "scale": "far"},
    {"instruction": "TOP-LEFT corner — tilt right + tilt down", "zone": "top-left", "scale": "medium"},
    {"instruction": "TOP-RIGHT corner — tilt left + tilt down", "zone": "top-right", "scale": "medium"},
    {"instruction": "BOTTOM-LEFT corner — tilt right + tilt up", "zone": "bottom-left", "scale": "medium"},
    {"instruction": "BOTTOM-RIGHT corner — tilt left + tilt up", "zone": "bottom-right", "scale": "medium"},
    {"instruction": "CENTRE — strong tilt ~45° (any direction)", "zone": "center", "scale": "medium"},
]


def beep(n: int, rapid: bool = False):
    """Send n terminal bell characters, optionally rapid."""
    gap = 0.12 if rapid else 0.35
    for i in range(n):
        print("\a", end="", flush=True)
        if i < n - 1:
            time.sleep(gap)
    # Also try paplay for systems where terminal bell is muted
    try:
        for _ in range(n):
            subprocess.run(
                ["paplay", "/usr/share/sounds/freedesktop/stereo/bell.oga"],
                timeout=1, capture_output=True
            )
            if n > 1:
                time.sleep(gap)
    except Exception:
        pass


def fetch_frame(firmware_url, api_version, camera) -> np.ndarray:
    url = f"{firmware_url}/{api_version}/cameras/{camera}/photo"
    resp = requests.get(url, params={"grayscale": "true"}, timeout=60)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content))
    return np.array(img)


def detect_corners(gray: np.ndarray, cols: int, rows: int):
    h, w = gray.shape
    small = cv2.resize(gray, (int(w * DETECT_SCALE), int(h * DETECT_SCALE)),
                       interpolation=cv2.INTER_AREA)
    found, corners_small = cv2.findChessboardCorners(
        small, (cols, rows),
        flags=cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE
    )
    if not found:
        return False, None
    corners_full = corners_small / DETECT_SCALE
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    corners_refined = cv2.cornerSubPix(gray, corners_full, (11, 11), (-1, -1), criteria)
    return True, corners_refined


def corner_metrics(gray: np.ndarray, corners: np.ndarray) -> dict:
    """Summarise checkerboard placement in the frame."""
    h, w = gray.shape
    xs = corners[:, 0, 0]
    ys = corners[:, 0, 1]
    min_x, max_x = float(xs.min()), float(xs.max())
    min_y, max_y = float(ys.min()), float(ys.max())
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    bbox_area = max(max_x - min_x, 1.0) * max(max_y - min_y, 1.0)
    return {
        "center_x_norm": center_x / w,
        "center_y_norm": center_y / h,
        "area_frac": bbox_area / (w * h),
    }


def _zone_ok(zone: str, x: float, y: float) -> bool:
    if zone == "center":
        return 0.35 <= x <= 0.65 and 0.35 <= y <= 0.65
    if zone == "top-left":
        return x <= 0.35 and y <= 0.35
    if zone == "top-right":
        return x >= 0.65 and y <= 0.35
    if zone == "bottom-left":
        return x <= 0.35 and y >= 0.65
    if zone == "bottom-right":
        return x >= 0.65 and y >= 0.65
    if zone == "left-edge":
        return x <= 0.25 and 0.25 <= y <= 0.75
    if zone == "right-edge":
        return x >= 0.75 and 0.25 <= y <= 0.75
    if zone == "top-edge":
        return y <= 0.25 and 0.25 <= x <= 0.75
    if zone == "bottom-edge":
        return y >= 0.75 and 0.25 <= x <= 0.75
    return True


def assess_capture(position: dict, metrics: dict) -> tuple[bool, list[str]]:
    """Return (good_enough, suggestions) for the current guided position."""
    x = metrics["center_x_norm"]
    y = metrics["center_y_norm"]
    area = metrics["area_frac"]
    suggestions = []

    if not _zone_ok(position["zone"], x, y):
        if x < 0.25:
            suggestions.append("move the board right in the frame")
        elif x > 0.75:
            suggestions.append("move the board left in the frame")
        if y < 0.25:
            suggestions.append("move the board down in the frame")
        elif y > 0.75:
            suggestions.append("move the board up in the frame")

    scale = position["scale"]
    if scale == "close":
        if area < 0.18:
            suggestions.append("move the board closer")
        elif area > 0.45:
            suggestions.append("move the board slightly farther away")
    elif scale == "far":
        if area > 0.10:
            suggestions.append("move the board farther away")
        elif area < 0.02:
            suggestions.append("move the board a bit closer")
    else:
        if area < 0.06:
            suggestions.append("move the board closer")
        elif area > 0.28:
            suggestions.append("move the board farther away")

    return len(suggestions) == 0, suggestions


def prompt_ready() -> str:
    return input("         Press Enter when in position, 's' to skip, 'q' to quit: ").strip().lower()


def run_calibration(image_dir: Path, cols: int, rows: int, square_mm: float) -> dict:
    image_paths = sorted(image_dir.glob("*.png"))
    objp = np.zeros((rows * cols, 3), np.float32)
    objp[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    objp *= square_mm

    obj_points, img_points = [], []
    image_size = None

    for path in image_paths:
        gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            continue
        if image_size is None:
            image_size = (gray.shape[1], gray.shape[0])
        found, corners = detect_corners(gray, cols, rows)
        if found:
            obj_points.append(objp)
            img_points.append(corners)

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, image_size, None, None
    )
    return {
        "rms_reprojection_error_px": round(float(rms), 4),
        "image_size": list(image_size),
        "camera_matrix": K.tolist(),
        "dist_coeffs": dist.tolist(),
        "checkerboard": {"cols": cols, "rows": rows, "square_size_mm": square_mm},
        "n_images_used": len(obj_points),
        "n_images_total": len(image_paths),
    }


def main():
    parser = argparse.ArgumentParser(description="Guided calibration session")
    parser.add_argument("--firmware-url", default=DEFAULT_FIRMWARE_URL)
    parser.add_argument("--api-version",  default=DEFAULT_API_VERSION)
    parser.add_argument("--camera",       default=DEFAULT_CAMERA)
    parser.add_argument("--square-mm",    type=float, default=DEFAULT_SQUARE_MM)
    parser.add_argument("--cols",         type=int, default=DEFAULT_COLS)
    parser.add_argument("--rows",         type=int, default=DEFAULT_ROWS)
    parser.add_argument("--image-dir",    type=Path,
                        default=Path("data/calibration/images"))
    parser.add_argument("--output",       type=Path,
                        default=Path("data/calibration/calibration.json"))
    parser.add_argument("--auto", action="store_true",
                        help="Use the old timed mode instead of waiting for operator confirmation")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from existing saved frames in image-dir")
    parser.add_argument("--move-time",    type=float, default=6.0,
                        help="Seconds to move into position in --auto mode")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    args.image_dir.mkdir(parents=True, exist_ok=True)

    existing_images = sorted(args.image_dir.glob("cal_*.png"))
    if args.resume:
        saved = len(existing_images)
    else:
        for f in existing_images:
            f.unlink()
        saved = 0

    total = len(POSITIONS)
    print(f"\n{'='*55}")
    print(f"  GUIDED CALIBRATION  ({total} positions)")
    print(f"  Square size: {args.square_mm}mm  |  Board: {args.cols}x{args.rows}")
    print(f"{'='*55}")
    print(f"  Mode: {'AUTO countdown' if args.auto else 'MANUAL operator-confirmed'}")
    print(f"\n  BEEP LEGEND:")
    print(f"    1 beep          = move to next position")
    print(f"    2 slow beeps    = HOLD STILL — capturing")
    print(f"    2 short beeps   = frame accepted")
    print(f"    3 rapid beeps   = adjust and retry")
    print(f"\n  Starting in 5 seconds — get the checkerboard ready...\n")
    time.sleep(5)

    pos_idx = saved

    while saved < total and pos_idx < total:
        position = POSITIONS[pos_idx]
        print(f"\n[{saved+1}/{total}]  MOVE TO: {position['instruction']}")
        beep(1)
        if args.auto:
            print(f"         ({args.move_time:.0f}s to get into position...)")
            time.sleep(args.move_time)
        else:
            action = prompt_ready()
            if action == "q":
                break
            if action == "s":
                print("         Skipping this planned position")
                pos_idx += 1
                continue

        # Capture attempt loop for this position
        attempts = 0
        while True:
            attempts += 1
            print(f"         Capturing... (hold still!)", end="", flush=True)
            beep(2)
            time.sleep(PRE_CAPTURE_WAIT)

            try:
                gray = fetch_frame(args.firmware_url, args.api_version, args.camera)
            except Exception as e:
                print(f"\n         ERROR fetching frame: {e}")
                beep(3, rapid=True)
                time.sleep(3)
                continue

            found, corners = detect_corners(gray, args.cols, args.rows)

            if found:
                metrics = corner_metrics(gray, corners)
                good_enough, suggestions = assess_capture(position, metrics)
                if good_enough:
                    out_path = args.image_dir / f"cal_{saved:03d}.png"
                    cv2.imwrite(str(out_path), gray)
                    saved += 1
                    print(
                        f"  ACCEPTED ({out_path.name})  "
                        f"center=({metrics['center_x_norm']:.2f}, {metrics['center_y_norm']:.2f})  "
                        f"coverage={metrics['area_frac']:.3f}"
                    )
                    beep(2, rapid=True)
                    time.sleep(0.5)
                    break

                print(f"  detected, but adjust first (attempt {attempts})")
                for suggestion in suggestions:
                    print(f"         - {suggestion}")
                beep(3, rapid=True)
                if args.auto:
                    print("         Retrying in 3s...")
                    time.sleep(3)
                else:
                    action = prompt_ready()
                    if action == "q":
                        pos_idx = total
                        break
                    if action == "s":
                        print("         Skipping this planned position")
                        break
            else:
                print(f"  no board detected (attempt {attempts})")
                beep(3, rapid=True)
                print("         Keep the whole checkerboard visible and hold still")
                if args.auto:
                    print("         Stay in position — retrying in 3s...")
                    time.sleep(3)
                else:
                    action = prompt_ready()
                    if action == "q":
                        pos_idx = total
                        break
                    if action == "s":
                        print("         Skipping this planned position")
                        break

        pos_idx += 1

    print(f"\n{'='*55}")
    print(f"  Capture complete: {saved} frames")
    print(f"  Running calibration...")
    print(f"{'='*55}\n")

    if saved < 6:
        print("  Not enough accepted frames to calibrate. Need at least 6.")
        return

    cal = run_calibration(args.image_dir, args.cols, args.rows, args.square_mm)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(cal, indent=2))

    rms = cal["rms_reprojection_error_px"]
    status = "PASS" if rms < 0.5 else "WARN — recapture with more angular variety"
    print(f"  RMS reprojection error: {rms} px")
    print(f"  Target: < 0.5 px  —  {status}")
    print(f"  Images used: {cal['n_images_used']} / {cal['n_images_total']}")
    print(f"  Saved: {args.output}")
    print(f"\n{'='*55}\n")

    beep(1 if rms < 0.5 else 3, rapid=(rms >= 0.5))


if __name__ == "__main__":
    main()
