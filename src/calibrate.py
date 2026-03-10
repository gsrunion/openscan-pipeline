"""
calibrate.py — Camera calibration via the OpenScan3 firmware API.

Captures checkerboard images through the firmware photo endpoint and runs
OpenCV calibration on the workstation. The firmware owns the camera; this
script just calls GET /cameras/{name}/photo.

Workflow:
    1. Mount the printed checkerboard (9x6 inner corners, 30mm squares)
       on a flat rigid backing.
    2. Run this script.
    3. Hold the checkerboard in front of the camera at varied angles,
       distances, and positions across the frame.
    4. The script captures every 2 seconds and prints whether the
       checkerboard was detected. Aim for 20+ valid frames.
    5. Calibration runs automatically when enough frames are collected.

Usage:
    python src/calibrate.py

    # Custom checkerboard (if you measured your printed squares):
    python src/calibrate.py --square-mm 28.5

    # Re-run calibration from existing images:
    python src/calibrate.py --calibrate-only --image-dir data/calibration/images

    # Capture only, skip auto-calibration:
    python src/calibrate.py --capture-only --target 30

Output:
    data/calibration/images/        — saved checkerboard frames
    data/calibration/calibration.json — intrinsics + distortion coefficients
"""

import argparse
import json
import logging
import time
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image

logger = logging.getLogger(__name__)

# Checkerboard spec — must match the printed target
DEFAULT_COLS = 9        # inner corners along width
DEFAULT_ROWS = 6        # inner corners along height
DEFAULT_SQUARE_MM = 30.0

# Capture settings
DEFAULT_TARGET = 20     # number of valid frames to collect
CAPTURE_INTERVAL_S = 2  # seconds between capture attempts

# Firmware
DEFAULT_FIRMWARE_URL = "http://192.168.4.202:8000"
DEFAULT_API_VERSION = "latest"
DEFAULT_CAMERA = "arducam_64mp"

# Detection scale — run corner detection at 1/4 resolution for speed
DETECT_SCALE = 0.25


def fetch_frame(firmware_url: str, api_version: str, camera: str) -> np.ndarray:
    """Fetch a grayscale frame from the firmware and return as numpy array."""
    url = f"{firmware_url}/{api_version}/cameras/{camera}/photo"
    resp = requests.get(url, params={"grayscale": "true"}, timeout=60)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content))
    return np.array(img)


def detect_corners(gray: np.ndarray, cols: int, rows: int) -> tuple[bool, np.ndarray | None]:
    """
    Detect checkerboard corners in a grayscale image.

    Runs detection at DETECT_SCALE for speed, then refines at full resolution.
    Returns (found, corners_full_res) where corners are in full-res coordinates.
    """
    h, w = gray.shape

    # Downscale for fast detection
    small = cv2.resize(gray, (int(w * DETECT_SCALE), int(h * DETECT_SCALE)),
                       interpolation=cv2.INTER_AREA)
    found, corners_small = cv2.findChessboardCorners(
        small, (cols, rows),
        flags=cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE
    )

    if not found:
        return False, None

    # Scale corners back to full resolution
    corners_full = corners_small / DETECT_SCALE

    # Sub-pixel refinement at full resolution
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    corners_refined = cv2.cornerSubPix(gray, corners_full, (11, 11), (-1, -1), criteria)

    return True, corners_refined


def run_calibration(image_dir: Path, cols: int, rows: int,
                    square_mm: float) -> dict:
    """Run OpenCV calibration from saved images. Returns calibration dict."""
    image_paths = sorted(image_dir.glob("*.png"))
    if not image_paths:
        raise RuntimeError(f"No .png images in {image_dir}")

    objp = np.zeros((rows * cols, 3), np.float32)
    objp[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    objp *= square_mm

    obj_points = []
    img_points = []
    valid = []
    image_size = None

    logger.info("Detecting corners in %d images...", len(image_paths))
    for path in image_paths:
        gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            logger.warning("Could not read %s", path.name)
            continue
        if image_size is None:
            image_size = (gray.shape[1], gray.shape[0])

        found, corners = detect_corners(gray, cols, rows)
        if found:
            obj_points.append(objp)
            img_points.append(corners)
            valid.append(path)
            logger.info("  ✓ %s", path.name)
        else:
            logger.warning("  ✗ %s — no checkerboard", path.name)

    if len(obj_points) < 6:
        raise RuntimeError(
            f"Only {len(obj_points)} valid images — need at least 6. "
            "Capture more frames with the checkerboard fully visible."
        )

    logger.info("Running calibrateCamera with %d/%d valid frames...",
                len(obj_points), len(image_paths))

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, image_size, None, None
    )

    # Per-image reprojection errors
    per_image = []
    for i in range(len(obj_points)):
        proj, _ = cv2.projectPoints(obj_points[i], rvecs[i], tvecs[i], K, dist)
        err = cv2.norm(img_points[i], proj, cv2.NORM_L2) / len(proj)
        per_image.append(round(float(err), 4))

    result = {
        "rms_reprojection_error_px": round(float(rms), 4),
        "image_size":       list(image_size),
        "camera_matrix":    K.tolist(),
        "dist_coeffs":      dist.tolist(),
        "checkerboard": {
            "cols":         cols,
            "rows":         rows,
            "square_size_mm": square_mm,
        },
        "n_images_used":    len(obj_points),
        "n_images_total":   len(image_paths),
        "valid_images":     [p.name for p in valid],
        "per_image_errors": per_image,
    }

    status = "PASS ✓" if rms < 0.5 else "WARN — above 0.5px target"
    logger.info("Calibration: RMS=%.4f px  %s", rms, status)
    return result


def capture_session(firmware_url: str, api_version: str, camera: str,
                    image_dir: Path, cols: int, rows: int,
                    target: int, interval: float):
    """
    Interactive capture loop. Captures every `interval` seconds until
    `target` valid checkerboard frames are saved.
    """
    image_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nCalibration capture — target: {target} valid frames")
    print(f"Checkerboard: {cols}x{rows} inner corners, held in front of camera")
    print(f"Capturing every {interval}s. Move the board to a new position each time.\n")
    print("Tips for good coverage:")
    print("  - Fill different parts of the frame (corners, edges, centre)")
    print("  - Vary tilt: flat-on, tilted left/right, tilted up/down")
    print("  - Vary distance: close (~200mm), mid, far (~500mm)")
    print("  - Avoid motion blur — hold still during capture\n")

    captured = 0
    attempt = 0

    while captured < target:
        time.sleep(interval)
        attempt += 1

        try:
            gray = fetch_frame(firmware_url, api_version, camera)
        except Exception as e:
            logger.error("Capture failed: %s", e)
            continue

        found, corners = detect_corners(gray, cols, rows)

        if found:
            out_path = image_dir / f"cal_{captured:03d}.png"
            cv2.imwrite(str(out_path), gray)
            captured += 1
            print(f"  [{captured:2d}/{target}] ✓  Saved {out_path.name}")
        else:
            print(f"  [attempt {attempt:3d}] ✗  No checkerboard — adjust position")

    print(f"\nCapture complete: {captured} frames saved to {image_dir}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Camera calibration via OpenScan3 firmware API"
    )
    parser.add_argument("--firmware-url", default=DEFAULT_FIRMWARE_URL)
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--camera", default=DEFAULT_CAMERA)
    parser.add_argument("--cols", type=int, default=DEFAULT_COLS,
                        help="Checkerboard inner corner columns (default: 9)")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS,
                        help="Checkerboard inner corner rows (default: 6)")
    parser.add_argument("--square-mm", type=float, default=DEFAULT_SQUARE_MM,
                        help="Measured square size in mm (default: 30.0)")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET,
                        help="Number of valid frames to collect (default: 20)")
    parser.add_argument("--interval", type=float, default=CAPTURE_INTERVAL_S,
                        help="Seconds between capture attempts (default: 2)")
    parser.add_argument("--image-dir", type=Path,
                        default=Path("data/calibration/images"),
                        help="Directory for calibration images")
    parser.add_argument("--output", type=Path,
                        default=Path("data/calibration/calibration.json"),
                        help="Output calibration JSON")
    parser.add_argument("--capture-only", action="store_true",
                        help="Capture images but skip calibration")
    parser.add_argument("--calibrate-only", action="store_true",
                        help="Run calibration on existing images, skip capture")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not args.calibrate_only:
        capture_session(
            firmware_url=args.firmware_url,
            api_version=args.api_version,
            camera=args.camera,
            image_dir=args.image_dir,
            cols=args.cols,
            rows=args.rows,
            target=args.target,
            interval=args.interval,
        )

    if not args.capture_only:
        cal = run_calibration(args.image_dir, args.cols, args.rows, args.square_mm)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(cal, indent=2))

        print("=" * 50)
        print(f"RMS reprojection error: {cal['rms_reprojection_error_px']} px")
        print(f"Target: < 0.5 px  —  "
              f"{'PASS ✓' if cal['rms_reprojection_error_px'] < 0.5 else 'WARN — recapture with more varied angles'}")
        print(f"Images used: {cal['n_images_used']} / {cal['n_images_total']}")
        print(f"Saved: {args.output}")
        print("=" * 50)


if __name__ == "__main__":
    main()
