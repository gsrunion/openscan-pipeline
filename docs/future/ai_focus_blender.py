"""
ai_focus_blender.py

AI-guided focus blending for OpenScan photogrammetry brackets.

Implements a Pi-friendly blending path with:
- sharpness-map driven weighted blending
- seam smoothing via softened one-hot masks
- optional Enfuse comparison + winner selection
- fallback policy for runtime/memory/quality failures

Usage examples:
  python ai_focus_blender.py --stack f0.tif f1.tif ... f6.tif --output out.tif --metrics out.json
  python ai_focus_blender.py --batch-dir ./scan_inbox/raw --output-dir ./stacked --metrics-dir ./stacked/metrics
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from focus_stacker import focus_stack_enfuse

logger = logging.getLogger(__name__)

RUNTIME_BUDGET_S = 10.0
MEMORY_LIMIT_MB = 2500.0
HARD_MEMORY_LIMIT_MB = float(os.environ.get("AI_BLEND_HARD_MEMORY_MB", "5000"))
AI_SSIM_GOOD = 0.85
ENFUSE_SSIM_LOW = 0.80
SSIM_MIN_FAIL = 0.70
LAPLACIAN_TIE_PCT = 0.05
ARTIFACT_EDGE_DIFF_PCT = 0.15
ARTIFACT_LOCAL_SEAM_PCT = 0.10
ARTIFACT_COLOR_SHIFT_PCT = 0.05
ARTIFACT_NOISE_AMP_PCT = 0.20
LOWMEM_FOCUS_SCALE = 0.25


def rss_mb() -> float:
    """Return current process RSS in MB (Linux)."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = float(line.split()[1])
                    return kb / 1024.0
    except OSError:
        pass
    return 0.0


def ensure_memory_limit() -> None:
    current = rss_mb()
    if current > HARD_MEMORY_LIMIT_MB:
        raise MemoryError(
            f"Memory usage {current:.1f} MB exceeded hard limit {HARD_MEMORY_LIMIT_MB:.1f} MB"
        )
    if current > MEMORY_LIMIT_MB:
        logger.warning(
            "Memory usage %.1f MB is above target %.1f MB (hard limit %.1f MB)",
            current,
            MEMORY_LIMIT_MB,
            HARD_MEMORY_LIMIT_MB,
        )


def load_16bit_bgr(path: Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"Could not read image: {path}")

    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    if img.dtype != np.uint16:
        # Preserve dynamic range for 8-bit inputs if needed.
        if img.dtype == np.uint8:
            img = (img.astype(np.uint16) << 8)
        else:
            img = np.clip(img, 0, 65535).astype(np.uint16)
    return img


def to_gray_float(img16_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img16_bgr, cv2.COLOR_BGR2GRAY)
    return gray.astype(np.float32) / 65535.0


def laplacian_variance_gray(gray_norm: np.ndarray) -> float:
    lap = cv2.Laplacian(gray_norm, cv2.CV_32F, ksize=3)
    return float(np.var(lap))


def compute_sharpness_map(gray_norm: np.ndarray) -> np.ndarray:
    """
    Robust sharpness map:
    - Gaussian denoise (limits noise-as-sharpness)
    - abs(Laplacian) as focus cue
    - local blur of response to stabilize map
    """
    den = cv2.GaussianBlur(gray_norm, (0, 0), 0.8)
    lap = cv2.Laplacian(den, cv2.CV_32F, ksize=3)
    sharp = np.abs(lap)
    sharp = cv2.GaussianBlur(sharp, (0, 0), 1.1)
    return sharp


def ssim_gray(a: np.ndarray, b: np.ndarray) -> float:
    """Global SSIM on grayscale float images in [0,1]."""
    if a.shape != b.shape:
        raise ValueError("SSIM requires same shape")

    c1 = (0.01 ** 2)
    c2 = (0.03 ** 2)

    mu_a = cv2.GaussianBlur(a, (11, 11), 1.5)
    mu_b = cv2.GaussianBlur(b, (11, 11), 1.5)

    mu_a2 = mu_a * mu_a
    mu_b2 = mu_b * mu_b
    mu_ab = mu_a * mu_b

    sigma_a2 = cv2.GaussianBlur(a * a, (11, 11), 1.5) - mu_a2
    sigma_b2 = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mu_b2
    sigma_ab = cv2.GaussianBlur(a * b, (11, 11), 1.5) - mu_ab

    numerator = (2.0 * mu_ab + c1) * (2.0 * sigma_ab + c2)
    denominator = (mu_a2 + mu_b2 + c1) * (sigma_a2 + sigma_b2 + c2)

    denom = np.maximum(denominator, 1e-12)
    score_map = numerator / denom
    return float(np.mean(score_map))


def detect_artifacts(ai_bgr16: np.ndarray, enfuse_bgr16: np.ndarray) -> dict[str, Any]:
    ai = ai_bgr16.astype(np.float32) / 65535.0
    en = enfuse_bgr16.astype(np.float32) / 65535.0

    diff = np.abs(ai - en)
    diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)

    # Halo proxy: edge-heavy difference response
    edges = cv2.Canny((diff_gray * 255).astype(np.uint8), 40, 120)
    edge_ratio = float(np.count_nonzero(edges) / edges.size)

    # Seam proxy: local Laplacian variance jump in difference map
    lap = cv2.Laplacian(diff_gray, cv2.CV_32F, ksize=3)
    local_seam = float(np.std(lap))

    # Color shift proxy: channel variance drift
    var_ai = np.var(ai.reshape(-1, 3), axis=0)
    var_en = np.var(en.reshape(-1, 3), axis=0)
    color_shift = float(np.max(np.abs(var_ai - var_en) / np.maximum(var_en, 1e-9)))

    # Noise amplification proxy: high-pass energy difference
    ai_lp = cv2.GaussianBlur(cv2.cvtColor(ai, cv2.COLOR_BGR2GRAY), (0, 0), 1.0)
    en_lp = cv2.GaussianBlur(cv2.cvtColor(en, cv2.COLOR_BGR2GRAY), (0, 0), 1.0)
    ai_hp = cv2.cvtColor(ai, cv2.COLOR_BGR2GRAY) - ai_lp
    en_hp = cv2.cvtColor(en, cv2.COLOR_BGR2GRAY) - en_lp
    noise_amp = float((np.var(ai_hp) - np.var(en_hp)) / max(np.var(en_hp), 1e-9))

    flags = {
        "haloing": edge_ratio > ARTIFACT_EDGE_DIFF_PCT,
        "seams": local_seam > ARTIFACT_LOCAL_SEAM_PCT,
        "color_shift": color_shift > ARTIFACT_COLOR_SHIFT_PCT,
        "noise_amplification": noise_amp > ARTIFACT_NOISE_AMP_PCT,
    }
    flags["any"] = bool(any(flags.values()))

    return {
        "edge_ratio": edge_ratio,
        "local_seam_std": local_seam,
        "color_shift": color_shift,
        "noise_amplification": noise_amp,
        "flags": flags,
    }


def blend_ai_guided(frames_bgr16: list[np.ndarray]) -> tuple[np.ndarray, dict[str, Any]]:
    if len(frames_bgr16) < 2:
        raise ValueError("Need at least 2 frames for blending")

    h, w = frames_bgr16[0].shape[:2]
    for f in frames_bgr16[1:]:
        if f.shape[:2] != (h, w):
            raise ValueError("All frames must have the same resolution")

    ensure_memory_limit()

    gray_list = [to_gray_float(f) for f in frames_bgr16]
    sharp_maps = [compute_sharpness_map(g) for g in gray_list]
    sharp_stack = np.stack(sharp_maps, axis=0)  # [n,h,w]

    # Stable winner map then softened masks for seam handling.
    winner_idx = np.argmax(sharp_stack, axis=0)  # [h,w]

    one_hot = np.stack([(winner_idx == i).astype(np.float32) for i in range(len(frames_bgr16))], axis=0)
    soft = np.empty_like(one_hot)
    for i in range(one_hot.shape[0]):
        soft[i] = cv2.GaussianBlur(one_hot[i], (0, 0), 1.2)

    norm = np.sum(soft, axis=0, keepdims=False)
    norm = np.maximum(norm, 1e-8)
    soft = soft / norm[None, :, :]

    # Weighted blend in float32 then clip back to uint16.
    blend = np.zeros_like(frames_bgr16[0], dtype=np.float32)
    for i, frame in enumerate(frames_bgr16):
        w_i = soft[i][:, :, None]
        blend += frame.astype(np.float32) * w_i

    blend16 = np.clip(np.rint(blend), 0, 65535).astype(np.uint16)

    # Pixel quality proxy from normalized peak sharpness.
    peak = np.max(sharp_stack, axis=0)
    p99 = float(np.percentile(peak, 99.0))
    if p99 > 0:
        peak_norm = np.clip(peak / p99, 0.0, 1.0)
        pixel_quality = float(np.mean(peak_norm > 0.7) * 100.0)
    else:
        pixel_quality = 0.0

    info = {
        "pixel_quality": pixel_quality,
        "sharpness_peak_p99": p99,
        "n_frames": len(frames_bgr16),
        "blend_mode": "full_memory_softmask",
    }
    return blend16, info


def blend_ai_guided_lowmem(bracket_paths: list[Path], focus_scale: float = LOWMEM_FOCUS_SCALE) -> tuple[np.ndarray, dict[str, Any]]:
    """
    Low-memory focus blend:
    1) Build sharpness winner map at reduced resolution.
    2) Upsample winner map to full resolution.
    3) Compose full-resolution output by selecting pixels from each frame.
    """
    if len(bracket_paths) < 2:
        raise ValueError("Need at least 2 frames for blending")

    first = load_16bit_bgr(bracket_paths[0])
    h, w = first.shape[:2]
    hs = max(1, int(h * focus_scale))
    ws = max(1, int(w * focus_scale))

    sharp_small = []
    for p in bracket_paths:
        frame = load_16bit_bgr(p)
        gray = to_gray_float(frame)
        gray_small = cv2.resize(gray, (ws, hs), interpolation=cv2.INTER_AREA)
        sharp_small.append(compute_sharpness_map(gray_small))
        ensure_memory_limit()

    sharp_stack = np.stack(sharp_small, axis=0)  # [n,hs,ws]
    winner_small = np.argmax(sharp_stack, axis=0).astype(np.uint8)
    winner_full = cv2.resize(winner_small, (w, h), interpolation=cv2.INTER_NEAREST)

    # Light smoothing of the winner labels to reduce tiny speckled seams.
    winner_full = cv2.medianBlur(winner_full, 3)

    out = np.zeros((h, w, 3), dtype=np.uint16)
    for i, p in enumerate(bracket_paths):
        frame = load_16bit_bgr(p)
        mask = winner_full == i
        out[mask] = frame[mask]
        ensure_memory_limit()

    peak = np.max(sharp_stack, axis=0)
    p99 = float(np.percentile(peak, 99.0))
    if p99 > 0:
        peak_norm = np.clip(peak / p99, 0.0, 1.0)
        pixel_quality = float(np.mean(peak_norm > 0.7) * 100.0)
    else:
        pixel_quality = 0.0

    info = {
        "pixel_quality": pixel_quality,
        "sharpness_peak_p99": p99,
        "n_frames": len(bracket_paths),
        "blend_mode": "low_memory_hardmask",
        "focus_scale": focus_scale,
    }
    return out, info


def pick_winner(
    ai_img: np.ndarray,
    ai_metrics: dict[str, Any],
    enfuse_img: np.ndarray | None,
    enfuse_metrics: dict[str, Any] | None,
    ai_elapsed_s: float,
    fallback_reason: str | None,
) -> tuple[str, str, dict[str, Any]]:
    """Return (winner, reason, merged_metrics)."""
    merged = dict(ai_metrics)
    merged["processing_time_s"] = ai_elapsed_s

    if fallback_reason is not None:
        merged["winner_reason"] = "fallback"
        return "enfuse" if enfuse_img is not None else "ai_guided", "fallback", merged

    if enfuse_img is None or enfuse_metrics is None:
        merged["winner_reason"] = "ai_only"
        return "ai_guided", "ai_only", merged

    ssim_ai = float(ai_metrics.get("ssim_vs_enfuse", 0.0))
    ssim_en = float(enfuse_metrics.get("ssim_self", 1.0))  # placeholder, always 1.0
    lap_ai = float(ai_metrics.get("laplacian_variance", 0.0))
    lap_en = float(enfuse_metrics.get("laplacian_variance", 0.0))

    if ssim_ai > AI_SSIM_GOOD and ssim_en <= ENFUSE_SSIM_LOW:
        merged["winner_reason"] = "higher_ssim"
        return "ai_guided", "higher_ssim", merged

    if ssim_en > AI_SSIM_GOOD and ssim_ai <= ENFUSE_SSIM_LOW:
        merged["winner_reason"] = "higher_ssim"
        return "enfuse", "higher_ssim", merged

    # Artifact-aware decision when close.
    artifacts = ai_metrics.get("artifact_metrics", {}).get("flags", {})
    if artifacts.get("any", False):
        merged["winner_reason"] = "artifact_penalty"
        return "enfuse", "artifact_penalty", merged

    if lap_ai > lap_en * (1.0 + LAPLACIAN_TIE_PCT):
        merged["winner_reason"] = "higher_sharpness"
        return "ai_guided", "higher_sharpness", merged

    if lap_en > lap_ai * (1.0 + LAPLACIAN_TIE_PCT):
        merged["winner_reason"] = "higher_sharpness"
        return "enfuse", "higher_sharpness", merged

    merged["winner_reason"] = "enfuse_tiebreak"
    return "enfuse", "enfuse_tiebreak", merged


def run_ai_focus_blend(
    bracket_paths: list[Path],
    output_path: Path,
    metrics_path: Path,
    run_enfuse_baseline: bool = True,
) -> dict[str, Any]:
    start = time.perf_counter()
    fallback_reason = None

    bracket_paths = [Path(p) for p in bracket_paths]
    if len(bracket_paths) < 2:
        raise ValueError("Need at least 2 images in bracket")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)

    # Kick off Enfuse early so it can run in parallel with AI path.
    enfuse_path = output_path.with_name(output_path.stem + "_enfuse" + output_path.suffix)
    enfuse_img = None
    enfuse_metrics: dict[str, Any] | None = None
    enfuse_exc: Exception | None = None

    do_enfuse = run_enfuse_baseline and shutil.which("enfuse") is not None
    enfuse_proc = None
    if do_enfuse:
        try:
            # Use subprocess path via focus_stacker helper (synchronous wrapper).
            # We'll run it in a child process to overlap with AI compute.
            cmd = [
                "python3",
                "-c",
                (
                    "from pathlib import Path; "
                    "from focus_stacker import focus_stack_enfuse; "
                    f"focus_stack_enfuse({[str(p) for p in bracket_paths]!r}, Path({str(enfuse_path)!r}))"
                ),
            ]
            enfuse_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception as e:  # pragma: no cover - defensive path
            enfuse_exc = e
            enfuse_proc = None

    ai_img = None
    ai_metrics: dict[str, Any] = {"method": "ai_guided"}

    try:
        first = load_16bit_bgr(bracket_paths[0])
        h, w = first.shape[:2]
        est_frame_mb = (h * w * 3 * 2) / (1024 * 1024)
        est_all_frames_mb = est_frame_mb * len(bracket_paths)
        use_lowmem = est_all_frames_mb > 1200.0

        if use_lowmem:
            ai_img, blend_info = blend_ai_guided_lowmem(bracket_paths, focus_scale=LOWMEM_FOCUS_SCALE)
        else:
            frames = [load_16bit_bgr(p) for p in bracket_paths]
            ensure_memory_limit()
            ai_img, blend_info = blend_ai_guided(frames)

        ai_gray = to_gray_float(ai_img)
        lap_ai = laplacian_variance_gray(ai_gray)

        ai_metrics.update(blend_info)
        ai_metrics["laplacian_variance"] = lap_ai

        elapsed = time.perf_counter() - start
        if elapsed > RUNTIME_BUDGET_S:
            fallback_reason = "timeout"

        ensure_memory_limit()

    except MemoryError:
        fallback_reason = "memory_limit"
    except Exception as e:
        fallback_reason = f"ai_exception: {type(e).__name__}: {e}"

    # Join Enfuse path.
    if enfuse_proc is not None:
        out, err = enfuse_proc.communicate()
        if enfuse_proc.returncode != 0:
            enfuse_exc = RuntimeError(f"enfuse failed rc={enfuse_proc.returncode}: {err.strip() or out.strip()}")

    if do_enfuse and enfuse_exc is None and enfuse_path.exists():
        try:
            enfuse_img = load_16bit_bgr(enfuse_path)
            en_gray = to_gray_float(enfuse_img)
            enfuse_metrics = {
                "laplacian_variance": laplacian_variance_gray(en_gray),
                "ssim_self": 1.0,
            }

            if ai_img is not None:
                ai_gray = to_gray_float(ai_img)
                ai_metrics["ssim_vs_enfuse"] = ssim_gray(ai_gray, en_gray)
                try:
                    ai_metrics["artifact_metrics"] = detect_artifacts(ai_img, enfuse_img)
                except Exception as artifact_err:
                    logger.warning("Artifact analysis failed: %s", artifact_err)
                    ai_metrics["artifact_metrics"] = None

                # Prompt policy: fail quality if too far from baseline.
                if ai_metrics["ssim_vs_enfuse"] < SSIM_MIN_FAIL:
                    fallback_reason = fallback_reason or "quality_fail_ssim"

        except Exception as e:
            enfuse_exc = e

    if enfuse_exc is not None:
        logger.warning("Enfuse baseline unavailable: %s", enfuse_exc)

    elapsed = time.perf_counter() - start

    winner, reason, merged = pick_winner(
        ai_img if ai_img is not None else np.zeros((1, 1, 3), dtype=np.uint16),
        ai_metrics,
        enfuse_img,
        enfuse_metrics,
        elapsed,
        fallback_reason,
    )

    # Decide output payload.
    if winner == "ai_guided" and ai_img is not None:
        final_img = ai_img
    elif enfuse_img is not None:
        final_img = enfuse_img
    elif ai_img is not None:
        final_img = ai_img
        winner = "ai_guided"
        reason = "ai_only"
    else:
        # Never stall the pipeline: fall back to best single frame.
        best_img = None
        best_score = -1.0
        best_path = None
        for p in bracket_paths:
            try:
                img = load_16bit_bgr(p)
                score = laplacian_variance_gray(to_gray_float(img))
                if score > best_score:
                    best_score = score
                    best_img = img
                    best_path = p
            except Exception:
                continue
        if best_img is None:
            raise RuntimeError("Both AI/Enfuse failed and could not load any fallback frame")
        final_img = best_img
        winner = "single_frame_fallback"
        reason = "fallback_best_single"
        fallback_reason = fallback_reason or "blend_failed_used_best_single"
        ai_metrics["best_single_path"] = str(best_path)
        ai_metrics["best_single_laplacian"] = float(best_score)

    cv2.imwrite(str(output_path), final_img)

    report = {
        "method": "ai_guided",
        "winner": winner,
        "winner_reason": reason,
        "processing_time_s": round(elapsed, 3),
        "pixel_quality": round(float(ai_metrics.get("pixel_quality", 0.0)), 3),
        "sharpness_score": round(float(ai_metrics.get("laplacian_variance", 0.0)), 6),
        "ssim_ai": round(float(ai_metrics.get("ssim_vs_enfuse", 0.0)), 6),
        "laplacian_ai": round(float(ai_metrics.get("laplacian_variance", 0.0)), 6),
        "laplacian_enfuse": round(float((enfuse_metrics or {}).get("laplacian_variance", 0.0)), 6),
        "fallback_reason": fallback_reason,
        "memory_rss_mb": round(rss_mb(), 2),
        "inputs": [str(p) for p in bracket_paths],
        "output": str(output_path),
        "enfuse_output": str(enfuse_path) if enfuse_path.exists() else None,
        "artifact_metrics": ai_metrics.get("artifact_metrics"),
        "decision_log": merged,
    }

    metrics_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def group_brackets(batch_dir: Path) -> dict[str, list[Path]]:
    """
    Group files like scan_az000.00_el000.00_f0.tif/png into brackets by prefix.
    """
    groups: dict[str, list[Path]] = {}
    pattern = re.compile(r"^(scan_az[\d.]+_el[\d.]+)_f\d+\.(?:tif|tiff|png)$", re.IGNORECASE)
    for p in sorted(batch_dir.glob("*")):
        if not p.is_file():
            continue
        m = pattern.match(p.name)
        if not m:
            continue
        key = m.group(1)
        groups.setdefault(key, []).append(p)
    return groups


def run_batch(batch_dir: Path, output_dir: Path, metrics_dir: Path, run_enfuse_baseline: bool) -> list[dict[str, Any]]:
    groups = group_brackets(batch_dir)
    if not groups:
        raise ValueError(f"No bracket image groups found in {batch_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for key, paths in sorted(groups.items()):
        output_path = output_dir / f"{key}_ai_blended.tif"
        metrics_path = metrics_dir / f"{key}_metrics.json"
        logger.info("Blending bracket %s with %d frames", key, len(paths))
        report = run_ai_focus_blend(paths, output_path, metrics_path, run_enfuse_baseline)
        results.append(report)

    summary_path = metrics_dir / "ai_focus_blend_summary.json"
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    logger.info("Batch complete: %d brackets", len(results))
    return results


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI-guided focus blending for OpenScan brackets")
    p.add_argument("--stack", nargs="+", type=Path, help="Explicit list of image paths for a single bracket")
    p.add_argument("--output", type=Path, help="Output path for single bracket blend")
    p.add_argument("--metrics", type=Path, help="Metrics JSON path for single bracket")
    p.add_argument("--batch-dir", type=Path, help="Directory containing scan_az*_el*_f*.tif/png brackets")
    p.add_argument("--output-dir", type=Path, default=Path.home() / "photogrammetry" / "stacked")
    p.add_argument("--metrics-dir", type=Path, default=Path.home() / "photogrammetry" / "stacked" / "metrics")
    p.add_argument("--no-enfuse", action="store_true", help="Skip Enfuse baseline comparison")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s [%(levelname)s] %(message)s")

    if args.stack:
        if not args.output or not args.metrics:
            raise SystemExit("For --stack mode, provide --output and --metrics")
        report = run_ai_focus_blend(args.stack, args.output, args.metrics, run_enfuse_baseline=not args.no_enfuse)
        print(json.dumps(report, indent=2))
        return 0

    if args.batch_dir:
        results = run_batch(args.batch_dir, args.output_dir, args.metrics_dir, run_enfuse_baseline=not args.no_enfuse)
        print(json.dumps({"processed": len(results)}, indent=2))
        return 0

    raise SystemExit("Provide either --stack ... or --batch-dir ...")


if __name__ == "__main__":
    raise SystemExit(main())
