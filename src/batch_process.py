"""
batch_process.py — Parallel demosaicing and Enfuse focus blending.

Workstation-side batch processor for the photogrammetry pipeline.
Uses multiprocessing to parallelize independent operations across CPU cores.

Usage:
    # Parallel demosaicing
    python batch_process.py demosaic ~/photogrammetry/test_scan_001/raw --output-dir ~/photogrammetry/test_scan_001/demosaiced --workers 8

    # Parallel Enfuse blending
    python batch_process.py enfuse ~/photogrammetry/test_scan_001/demosaiced --output-dir ~/photogrammetry/test_scan_001/stacked --workers 8
"""

import argparse
import logging
import os
import re
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Demosaicing (parallel wrapper around demosaic.py)
# ---------------------------------------------------------------------------

def demosaic_single(raw_path: Path, output_dir: Path) -> dict:
    """Demosaic a single raw file. Returns timing info."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from demosaic import demosaic_raw_file

    t0 = time.perf_counter()
    sidecar = raw_path.with_suffix(".json")
    sidecar = sidecar if sidecar.exists() else None

    output_path = demosaic_raw_file(raw_path, sidecar, output_dir)
    elapsed = time.perf_counter() - t0

    return {
        "input": str(raw_path.name),
        "output": str(output_path.name),
        "elapsed_s": round(elapsed, 2),
    }


def run_parallel_demosaic(raw_dir: Path, output_dir: Path, workers: int) -> list[dict]:
    """Demosaic all raw files in parallel."""
    raw_files = sorted(raw_dir.glob("*.raw"))
    if not raw_files:
        logger.error("No .raw files found in %s", raw_dir)
        return []

    output_dir.mkdir(parents=True, exist_ok=True)

    # Skip already-processed files
    existing = {p.stem.replace("_demosaiced", "") for p in output_dir.glob("*_demosaiced.tif")}
    to_process = [f for f in raw_files if f.stem not in existing]

    if not to_process:
        logger.info("All %d files already demosaiced, nothing to do", len(raw_files))
        return []

    logger.info("Demosaicing %d files (%d skipped) with %d workers...",
                len(to_process), len(existing), workers)

    results = []
    t_start = time.perf_counter()

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(demosaic_single, f, output_dir): f
            for f in to_process
        }
        for i, future in enumerate(as_completed(futures), 1):
            raw_file = futures[future]
            try:
                result = future.result()
                results.append(result)
                logger.info("[%d/%d] %s done in %.1fs",
                           i, len(to_process), result["input"], result["elapsed_s"])
            except Exception as e:
                logger.error("[%d/%d] %s FAILED: %s", i, len(to_process), raw_file.name, e)

    total = time.perf_counter() - t_start
    logger.info("Demosaicing complete: %d files in %.1fs (%.1fs avg, %.1fx speedup over serial)",
                len(results), total,
                total / max(len(results), 1),
                sum(r["elapsed_s"] for r in results) / max(total, 0.01))
    return results


# ---------------------------------------------------------------------------
# Enfuse blending (parallel across brackets)
# ---------------------------------------------------------------------------

def group_brackets(image_dir: Path) -> dict[str, list[Path]]:
    """Group demosaiced files into focus brackets by position prefix."""
    groups: dict[str, list[Path]] = {}
    pattern = re.compile(
        r"^(scan_az[\d.]+_el[\d.]+)_f\d+(?:_demosaiced)?\.(?:tif|tiff|png)$",
        re.IGNORECASE
    )
    for p in sorted(image_dir.glob("*")):
        if not p.is_file():
            continue
        m = pattern.match(p.name)
        if m:
            groups.setdefault(m.group(1), []).append(p)
    return groups


def enfuse_single(bracket_key: str, bracket_paths: list[Path], output_dir: Path) -> dict:
    """Run Enfuse on a single bracket. Returns timing info."""
    t0 = time.perf_counter()

    output_path = output_dir / f"{bracket_key}_stacked.tif"

    cmd = [
        "enfuse",
        "--output", str(output_path),
        "--hard-mask",
        "--exposure-weight=0",
        "--saturation-weight=0",
        "--contrast-weight=1",
        "--contrast-edge-scale=0.3",
    ] + [str(p) for p in sorted(bracket_paths)]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    elapsed = time.perf_counter() - t0

    if result.returncode != 0:
        raise RuntimeError(f"Enfuse failed: {result.stderr[:500]}")

    return {
        "bracket": bracket_key,
        "n_frames": len(bracket_paths),
        "output": output_path.name,
        "elapsed_s": round(elapsed, 2),
    }


def run_parallel_enfuse(image_dir: Path, output_dir: Path, workers: int) -> list[dict]:
    """Run Enfuse on all brackets in parallel."""
    groups = group_brackets(image_dir)
    if not groups:
        logger.error("No bracket groups found in %s", image_dir)
        return []

    output_dir.mkdir(parents=True, exist_ok=True)

    # Skip already-processed brackets
    existing = {p.stem.replace("_stacked", "") for p in output_dir.glob("*_stacked.tif")}
    to_process = {k: v for k, v in groups.items() if k not in existing}

    if not to_process:
        logger.info("All %d brackets already stacked, nothing to do", len(groups))
        return []

    logger.info("Enfuse blending %d brackets (%d skipped) with %d workers...",
                len(to_process), len(existing), workers)

    results = []
    t_start = time.perf_counter()

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(enfuse_single, key, paths, output_dir): key
            for key, paths in to_process.items()
        }
        for i, future in enumerate(as_completed(futures), 1):
            key = futures[future]
            try:
                result = future.result()
                results.append(result)
                logger.info("[%d/%d] %s (%d frames) done in %.1fs",
                           i, len(to_process), result["bracket"],
                           result["n_frames"], result["elapsed_s"])
            except Exception as e:
                logger.error("[%d/%d] %s FAILED: %s", i, len(to_process), key, e)

    total = time.perf_counter() - t_start
    logger.info("Enfuse complete: %d brackets in %.1fs (%.1fs avg, %.1fx speedup over serial)",
                len(results), total,
                total / max(len(results), 1),
                sum(r["elapsed_s"] for r in results) / max(total, 0.01))
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Parallel batch processing for photogrammetry pipeline")
    parser.add_argument("command", choices=["demosaic", "enfuse"], help="Operation to run")
    parser.add_argument("input_dir", type=Path, help="Input directory")
    parser.add_argument("--output-dir", type=Path, help="Output directory")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4,
                       help="Number of parallel workers (default: CPU count)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.command == "demosaic":
        output = args.output_dir or args.input_dir / "demosaiced"
        run_parallel_demosaic(args.input_dir, output, args.workers)

    elif args.command == "enfuse":
        output = args.output_dir or args.input_dir.parent / "stacked"
        run_parallel_enfuse(args.input_dir, output, args.workers)


if __name__ == "__main__":
    main()
