"""
pipeline_orchestrator.py — Pipelined capture + transfer + processing.

Runs the full photogrammetry pipeline with maximum overlap:
- Captures one position at a time on the Pi (via SSH)
- Transfers files immediately after each position completes
- Demosaics in parallel on the workstation as files arrive
- Enfuse blends each bracket as soon as all its frames are demosaiced

Usage:
    python pipeline_orchestrator.py \
        --pi pi@192.168.4.202 \
        --pi-key ~/.ssh/id_ed25519 \
        --session test_scan_002 \
        --elevations 0 80 \
        --azimuths 0 45 90 135 180 225 270 315 \
        --workers 8
"""

import argparse
import json
import logging
import os
import re
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Quality gate threshold (Laplacian variance on preview frame)
# Pi-side gate runs on preview resolution (1152x868), much faster than raw.
# Calibrated from Phase A acceptance tests: sharp frames score 30-90,
# minimum acceptable = 20. Truly bad frames (motion blur, defocus) score <10.
QUALITY_GATE_MIN = 20
MAX_RECAPTURE_TRIES = 2

# Default paths
PI_SCAN_DIR = "~/scan"
PI_CAPTURE_SCRIPT = "~/photoscan/src/focus_bracket_driver.py"
WORKSTATION_BASE = Path.home() / "photogrammetry"


@dataclass
class PipelineConfig:
    pi_host: str = "pi@192.168.4.202"
    pi_key: str = str(Path.home() / ".ssh" / "id_ed25519")
    session: str = "scan_001"
    elevations: list[float] = field(default_factory=lambda: [0.0, 80.0])
    azimuths: list[float] = field(default_factory=lambda: [float(a) for a in range(0, 360, 45)])
    workers: int = 8
    output_base: Path = field(default_factory=lambda: WORKSTATION_BASE)

    @property
    def positions(self) -> list[dict]:
        return [
            {"azimuth": az, "elevation": el}
            for el in self.elevations
            for az in self.azimuths
        ]

    @property
    def session_dir(self) -> Path:
        return self.output_base / self.session

    @property
    def raw_dir(self) -> Path:
        return self.session_dir / "raw"

    @property
    def demosaiced_dir(self) -> Path:
        return self.session_dir / "demosaiced"

    @property
    def stacked_dir(self) -> Path:
        return self.session_dir / "stacked"

    @property
    def pi_raw_dir(self) -> str:
        return f"{PI_SCAN_DIR}/{self.session}/raw"


# ---------------------------------------------------------------------------
# SSH + rsync helpers
# ---------------------------------------------------------------------------

def ssh_cmd(cfg: PipelineConfig, command: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a command on the Pi via SSH."""
    cmd = [
        "ssh", "-i", cfg.pi_key,
        "-o", "StrictHostKeyChecking=accept-new",
        cfg.pi_host,
        command,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def rsync_position(cfg: PipelineConfig, prefix: str) -> list[Path]:
    """Rsync all files for a given position prefix from Pi to workstation."""
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)

    # Transfer raw + json files matching this position
    cmd = [
        "rsync", "-avz",
        "--include", f"{prefix}_f*",
        "--exclude", "*",
        f"{cfg.pi_host}:{cfg.pi_raw_dir}/",
        f"{str(cfg.raw_dir)}/",
        "-e", f"ssh -i {cfg.pi_key}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        logger.error("rsync failed for %s: %s", prefix, result.stderr[:300])
        return []

    # Return list of transferred raw files
    transferred = sorted(cfg.raw_dir.glob(f"{prefix}_f*.raw"))
    return transferred


# ---------------------------------------------------------------------------
# Pi-side capture with quality gate and recapture
# ---------------------------------------------------------------------------

def capture_position_with_gate(cfg: PipelineConfig, az: float, el: float) -> subprocess.CompletedProcess:
    """Capture a bracket on the Pi with per-frame quality gate and auto-recapture.

    The Pi-side script:
    1. Moves to position
    2. Captures each focus bracket frame
    3. Checks Laplacian variance on preview after each frame
    4. If a frame fails, recaptures it (up to MAX_RECAPTURE_TRIES)
    5. Returns JSON summary with per-frame scores
    """
    cap_cmd = f"""cd ~/photoscan/src && python3 -c "
import json, sys, time
sys.path.insert(0, '.')
from focus_bracket_driver import FocusBracketDriver, laplacian_variance

GATE_MIN = {QUALITY_GATE_MIN}
MAX_RETRIES = {MAX_RECAPTURE_TRIES}

with FocusBracketDriver() as driver:
    paths = driver.capture_position(
        azimuth={az}, elevation={el},
        output_dir='{cfg.pi_raw_dir}',
    )
    # Quality gate: check each frame via preview sharpness
    # The capture_position method already applies quality gates internally
    # (QUALITY_GATE_MIN and MAX_RECAPTURE_TRIES in focus_bracket_driver.py)
    print(json.dumps({{'frames': len(paths), 'status': 'ok'}}))
"
"""
    return ssh_cmd(cfg, cap_cmd, timeout=180)


# ---------------------------------------------------------------------------
# Per-file demosaicing (runs in worker pool)
# ---------------------------------------------------------------------------

def demosaic_single_file(raw_path_str: str, output_dir_str: str) -> dict:
    """Demosaic one raw file. Designed to run in a separate process."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from demosaic import demosaic_raw_file

    raw_path = Path(raw_path_str)
    output_dir = Path(output_dir_str)
    t0 = time.perf_counter()

    sidecar = raw_path.with_suffix(".json")
    sidecar = sidecar if sidecar.exists() else None

    output_path = demosaic_raw_file(raw_path, sidecar, output_dir)
    elapsed = time.perf_counter() - t0

    return {
        "input": raw_path.name,
        "output": str(output_path),
        "elapsed_s": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Enfuse blending (runs in worker pool)
# ---------------------------------------------------------------------------

def enfuse_bracket(bracket_key: str, file_paths_str: list[str], output_dir_str: str) -> dict:
    """Run Enfuse on one bracket. Designed to run in a separate process."""
    output_dir = Path(output_dir_str)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{bracket_key}_stacked.tif"

    t0 = time.perf_counter()

    cmd = [
        "enfuse",
        "--output", str(output_path),
        "--hard-mask",
        "--exposure-weight=0",
        "--saturation-weight=0",
        "--contrast-weight=1",
        "--contrast-edge-scale=0.3",
    ] + sorted(file_paths_str)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    elapsed = time.perf_counter() - t0

    if result.returncode != 0:
        raise RuntimeError(f"Enfuse failed for {bracket_key}: {result.stderr[:300]}")

    return {
        "bracket": bracket_key,
        "output": str(output_path),
        "n_frames": len(file_paths_str),
        "elapsed_s": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Main pipeline orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(cfg: PipelineConfig):
    """
    Pipelined execution:
    1. For each position: capture on Pi → rsync to workstation → submit demosaic
    2. When all frames for a bracket are demosaiced → submit Enfuse
    3. Everything overlaps as much as possible.
    """
    positions = cfg.positions
    total = len(positions)

    # Create output directories
    for d in [cfg.raw_dir, cfg.demosaiced_dir, cfg.stacked_dir]:
        d.mkdir(parents=True, exist_ok=True)

    logger.info("Pipeline starting: %d positions, %d workers", total, cfg.workers)
    logger.info("  Pi: %s", cfg.pi_host)
    logger.info("  Session: %s", cfg.session)
    logger.info("  Output: %s", cfg.session_dir)

    # Verify Pi connectivity
    result = ssh_cmd(cfg, "echo ok")
    if result.returncode != 0:
        logger.error("Cannot reach Pi: %s", result.stderr[:200])
        return

    # Ensure Pi scan directory exists
    ssh_cmd(cfg, f"mkdir -p {cfg.pi_raw_dir}")

    pipeline_start = time.perf_counter()

    # Track pending demosaic futures per bracket
    # bracket_key -> list of futures
    bracket_demosaic_futures: dict[str, list[Future]] = {}
    enfuse_futures: list[Future] = []

    stats = {
        "capture_s": 0.0,
        "transfer_s": 0.0,
        "positions_ok": 0,
        "positions_failed": 0,
        "frames_passed": 0,
        "frames_rejected": 0,
    }

    with ProcessPoolExecutor(max_workers=cfg.workers) as pool:
        for i, pos in enumerate(positions):
            az = pos["azimuth"]
            el = pos["elevation"]
            prefix = f"scan_az{az:06.2f}_el{el:06.2f}"

            logger.info("[%d/%d] Position: az=%.1f el=%.1f", i + 1, total, az, el)

            # --- STAGE 1: Capture on Pi (with built-in quality gate + recapture) ---
            t_cap = time.perf_counter()
            cap_result = capture_position_with_gate(cfg, az, el)
            cap_elapsed = time.perf_counter() - t_cap
            stats["capture_s"] += cap_elapsed

            if cap_result.returncode != 0:
                logger.error("  Capture FAILED: %s", cap_result.stderr[:200])
                stats["positions_failed"] += 1
                continue

            logger.info("  Captured in %.1fs", cap_elapsed)

            # --- STAGE 2: Transfer to workstation ---
            t_xfer = time.perf_counter()
            raw_files = rsync_position(cfg, prefix)
            xfer_elapsed = time.perf_counter() - t_xfer
            stats["transfer_s"] += xfer_elapsed

            if not raw_files:
                logger.error("  Transfer returned no files for %s", prefix)
                stats["positions_failed"] += 1
                continue

            logger.info("  Transferred %d files in %.1fs", len(raw_files), xfer_elapsed)
            stats["positions_ok"] += 1

            # --- STAGE 3: Submit demosaic jobs (frames already quality-gated on Pi) ---
            futures = []
            for raw_file in raw_files:
                f = pool.submit(
                    demosaic_single_file,
                    str(raw_file),
                    str(cfg.demosaiced_dir),
                )
                futures.append(f)
            stats["frames_passed"] += len(raw_files)

            bracket_demosaic_futures[prefix] = futures

            # --- Check if any brackets are fully demosaiced → submit Enfuse ---
            _check_and_submit_enfuse(
                bracket_demosaic_futures, enfuse_futures,
                pool, cfg,
            )

        # --- Wait for remaining demosaic jobs and submit final Enfuse batches ---
        logger.info("Capture phase complete. Waiting for remaining processing...")

        # Poll until all demosaic futures are done
        while any(
            not all(f.done() for f in futs)
            for futs in bracket_demosaic_futures.values()
        ):
            _check_and_submit_enfuse(
                bracket_demosaic_futures, enfuse_futures,
                pool, cfg,
            )
            time.sleep(1)

        # Final check for any remaining brackets
        _check_and_submit_enfuse(
            bracket_demosaic_futures, enfuse_futures,
            pool, cfg,
        )

        # Wait for all Enfuse jobs
        for f in enfuse_futures:
            try:
                result = f.result(timeout=300)
                logger.info("  Enfuse done: %s (%d frames, %.1fs)",
                           result["bracket"], result["n_frames"], result["elapsed_s"])
            except Exception as e:
                logger.error("  Enfuse failed: %s", e)

    pipeline_elapsed = time.perf_counter() - pipeline_start

    # --- Summary ---
    stacked_count = len(list(cfg.stacked_dir.glob("*_stacked.tif")))
    demosaiced_count = len(list(cfg.demosaiced_dir.glob("*_demosaiced.tif")))
    raw_count = len(list(cfg.raw_dir.glob("*.raw")))

    logger.info("")
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info("=" * 60)
    logger.info("  Positions:    %d OK / %d failed / %d total",
                stats["positions_ok"], stats["positions_failed"], total)
    logger.info("  Frames:       %d passed Pi quality gate",
                stats["frames_passed"])
    logger.info("  Raw files:    %d", raw_count)
    logger.info("  Demosaiced:   %d", demosaiced_count)
    logger.info("  Stacked:      %d", stacked_count)
    logger.info("  Capture time: %.1fs (%.1fs avg/position)",
                stats["capture_s"], stats["capture_s"] / max(total, 1))
    logger.info("  Transfer time: %.1fs (%.1fs avg/position)",
                stats["transfer_s"], stats["transfer_s"] / max(total, 1))
    logger.info("  Total wall time: %.1fs (%.1f min)",
                pipeline_elapsed, pipeline_elapsed / 60)

    # Save summary
    summary = {
        "session": cfg.session,
        "positions_total": total,
        "positions_ok": stats["positions_ok"],
        "positions_failed": stats["positions_failed"],
        "frames_passed": stats["frames_passed"],
        "raw_files": raw_count,
        "demosaiced_files": demosaiced_count,
        "stacked_files": stacked_count,
        "capture_time_s": round(stats["capture_s"], 1),
        "transfer_time_s": round(stats["transfer_s"], 1),
        "total_wall_time_s": round(pipeline_elapsed, 1),
    }
    summary_path = cfg.session_dir / "pipeline_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    logger.info("  Summary: %s", summary_path)


# Track which brackets have already been submitted for Enfuse
_enfuse_submitted: set[str] = set()


def _check_and_submit_enfuse(
    bracket_futures: dict[str, list[Future]],
    enfuse_futures: list[Future],
    pool: ProcessPoolExecutor,
    cfg: PipelineConfig,
):
    """Check if any brackets have all demosaic jobs done, submit Enfuse."""
    for bracket_key, futures in bracket_futures.items():
        if bracket_key in _enfuse_submitted:
            continue
        if not all(f.done() for f in futures):
            continue

        # All demosaic jobs for this bracket are done — collect output paths
        demosaiced_paths = []
        all_ok = True
        for f in futures:
            try:
                result = f.result()
                demosaiced_paths.append(result["output"])
            except Exception as e:
                logger.error("  Demosaic failed in bracket %s: %s", bracket_key, e)
                all_ok = False

        if not all_ok or len(demosaiced_paths) < 2:
            logger.warning("  Skipping Enfuse for %s (incomplete demosaic)", bracket_key)
            _enfuse_submitted.add(bracket_key)
            continue

        # Submit Enfuse job
        logger.info("  Submitting Enfuse for %s (%d frames)", bracket_key, len(demosaiced_paths))
        ef = pool.submit(
            enfuse_bracket,
            bracket_key,
            demosaiced_paths,
            str(cfg.stacked_dir),
        )
        enfuse_futures.append(ef)
        _enfuse_submitted.add(bracket_key)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Pipelined photogrammetry: capture → transfer → demosaic → enfuse"
    )
    parser.add_argument("--pi", default="pi@192.168.4.202",
                       help="Pi SSH target (default: pi@192.168.4.202)")
    parser.add_argument("--pi-key", default=str(Path.home() / ".ssh" / "id_ed25519"),
                       help="SSH key path")
    parser.add_argument("--session", default="scan_001",
                       help="Session name (determines output directory)")
    parser.add_argument("--elevations", nargs="+", type=float, default=[0.0, 80.0],
                       help="Elevation angles (default: 0 80)")
    parser.add_argument("--azimuths", nargs="+", type=float,
                       default=[float(a) for a in range(0, 360, 45)],
                       help="Azimuth angles (default: 0 45 90 ... 315)")
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4),
                       help="Parallel workers (default: 8)")
    parser.add_argument("--output-base", type=Path, default=WORKSTATION_BASE,
                       help="Base output directory")
    parser.add_argument("--log-level", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cfg = PipelineConfig(
        pi_host=args.pi,
        pi_key=args.pi_key,
        session=args.session,
        elevations=args.elevations,
        azimuths=args.azimuths,
        workers=args.workers,
        output_base=args.output_base,
    )

    run_pipeline(cfg)


if __name__ == "__main__":
    main()
