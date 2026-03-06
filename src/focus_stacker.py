"""
focus_stacker.py  —  Phase A / A9
Enfuse-based focus stacking for the workstation side of the pipeline.

Takes a bracket of focus-shifted PNGs (or TIFFs) and produces a single
all-in-focus image using Enfuse's focus/exposure fusion.

Workstation path: ~/photogrammetry/focus_stacker.py

Prerequisites:
    sudo apt install enfuse

Usage:
    from focus_stacker import focus_stack_enfuse

    stacked = focus_stack_enfuse(
        bracket_paths=[Path("f0.png"), Path("f1.png"), Path("f2.png")],
        output_path=Path("stacked/position_00.png"),
    )
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


def laplacian_variance(image_bgr: np.ndarray) -> float:
    grey = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(grey, cv2.CV_64F).var())


def focus_stack_enfuse(
    bracket_paths: list[Path],
    output_path: Path,
    hard_mask: bool = True,
    contrast_weight: float = 1.0,
    exposure_weight: float = 0.0,
    saturation_weight: float = 0.2,
) -> Path:
    """
    Run Enfuse focus stacking on a bracket of images.

    Args:
        bracket_paths:     List of input image paths (focus-shifted bracket)
        output_path:       Where to write the stacked result
        hard_mask:         Use hard masks for sharper transitions (recommended)
        contrast_weight:   Weight for sharpness criterion (default: 1.0)
        exposure_weight:   Weight for exposure criterion (0 = pure focus stack)
        saturation_weight: Weight for saturation criterion

    Returns:
        Path to the stacked output image.

    Raises:
        FileNotFoundError: if enfuse binary is not installed
        subprocess.CalledProcessError: if enfuse fails
    """
    enfuse_bin = shutil.which("enfuse")
    if enfuse_bin is None:
        raise FileNotFoundError(
            "enfuse not found. Install with: sudo apt install enfuse"
        )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        enfuse_bin,
        "--output", str(output_path),
        f"--contrast-weight={contrast_weight}",
        f"--exposure-weight={exposure_weight}",
        f"--saturation-weight={saturation_weight}",
    ]
    if hard_mask:
        cmd.append("--hard-mask")

    cmd += [str(p) for p in bracket_paths]

    logger.info("Running enfuse: %d input frames → %s", len(bracket_paths), output_path)
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )

    logger.info("Enfuse complete: %s", output_path)
    return output_path


def stack_quality_score(stacked_path: Path, bracket_paths: list[Path]) -> dict:
    """
    Compare stacked image quality against the best single bracket frame.
    Returns a quality report dict.
    """
    stacked = cv2.imread(str(stacked_path))
    if stacked is None:
        raise ValueError(f"Could not read stacked image: {stacked_path}")

    stacked_score = laplacian_variance(stacked)

    bracket_scores = []
    for p in bracket_paths:
        img = cv2.imread(str(p))
        if img is not None:
            bracket_scores.append(laplacian_variance(img))

    best_single = max(bracket_scores) if bracket_scores else 0.0

    return {
        "stacked_sharpness":    round(stacked_score, 2),
        "best_single_sharpness": round(best_single, 2),
        "improvement_ratio":    round(stacked_score / best_single, 3) if best_single > 0 else None,
        "stacked_path":         str(stacked_path),
        "n_input_frames":       len(bracket_paths),
    }


# ---------------------------------------------------------------------------
# CLI — acceptance test
# ---------------------------------------------------------------------------

def _run_acceptance_test(scan_inbox: Path, output_dir: Path):
    """
    A9 acceptance test: stack all brackets found in scan_inbox/raw/.
    Requires at least one complete bracket to be present.
    """
    raw_dir = scan_inbox / "raw"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Group PNGs by position prefix (everything before _f{N})
    from collections import defaultdict
    import re

    groups: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(raw_dir.glob("scan_az*.tif")):
        m = re.match(r"(scan_az[\d.]+_el[\d.]+)_f\d+\.tif", p.name)
        if m:
            groups[m.group(1)].append(p)

    if not groups:
        print("No bracket TIFFs found in", raw_dir)
        print("Run a capture first: python focus_bracket_driver.py --capture")
        return False

    results = []
    all_passed = True

    for prefix, frames in sorted(groups.items()):
        frames = sorted(frames)
        output_path = output_dir / f"{prefix}_stacked.tif"
        try:
            focus_stack_enfuse(frames, output_path)
            quality = stack_quality_score(output_path, frames)
            passed = quality["stacked_sharpness"] > 0
            results.append({**quality, "prefix": prefix, "passed": passed})
            status = "✓" if passed else "✗"
            print(f"  {status} {prefix}: stacked={quality['stacked_sharpness']:.1f} "
                  f"best_single={quality['best_single_sharpness']:.1f} "
                  f"ratio={quality['improvement_ratio']}")
            if not passed:
                all_passed = False
        except Exception as e:
            print(f"  ✗ {prefix}: FAILED — {e}")
            all_passed = False

    pass_count = sum(1 for r in results if r.get("passed"))
    total = len(results)
    print(f"\nA9 Acceptance: {pass_count}/{total} brackets stacked successfully")
    print(f"Result: {'PASS' if all_passed else 'FAIL'}")

    (output_dir / "a9_acceptance_results.json").write_text(
        json.dumps(results, indent=2)
    )
    return all_passed


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Enfuse Focus Stacker")
    parser.add_argument("--acceptance-test", action="store_true")
    parser.add_argument("--stack", nargs="+", type=Path, metavar="IMAGE",
                        help="Stack a specific bracket")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--scan-inbox", type=Path,
                        default=Path.home() / "photogrammetry/scan_inbox")
    parser.add_argument("--output-dir", type=Path,
                        default=Path.home() / "photogrammetry/stacked")
    args = parser.parse_args()

    if args.acceptance_test:
        ok = _run_acceptance_test(args.scan_inbox, args.output_dir)
        raise SystemExit(0 if ok else 1)
    elif args.stack:
        out = args.output or Path(args.stack[0]).parent / "stacked_output.png"
        focus_stack_enfuse(args.stack, out)
        quality = stack_quality_score(out, args.stack)
        print(json.dumps(quality, indent=2))
    else:
        parser.print_help()
