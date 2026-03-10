"""
pipeline_orchestrator.py — Firmware-API scan pipeline.

Drives the OpenScan turntable via the OpenScan3 firmware REST API, capturing
grayscale JPEG images for COLMAP reconstruction. No raw files, no demosaic,
no Enfuse — just fast, lean captures over HTTP.

Requires the OpenScan3 firmware (with PR #67 quality gate) running on the Pi.

Pipeline per position:
    1. PUT /motors/{turntable}/angle  — move turntable (blocks until done)
    2. PUT /motors/{rotor}/angle      — move rotor arm (blocks until done)
    3. GET /cameras/{camera}/photo    — capture JPEG (quality gate in firmware)
    4. Convert to grayscale + downscale on workstation
    5. Save JPEG + pose sidecar JSON

Usage:
    python pipeline_orchestrator.py \\
        --firmware-url http://192.168.4.202:8000 \\
        --session my_scan \\
        --elevations 0 45 80 \\
        --azimuths $(seq 0 45 315)
"""

import argparse
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Request timeouts (seconds)
MOTOR_TIMEOUT = 120   # motor moves block until done; allow generous timeout
CAPTURE_TIMEOUT = 60  # photo capture


@dataclass
class PipelineConfig:
    firmware_url: str = "http://192.168.4.202:8000"
    api_version: str = "latest"
    turntable_motor: str = "turntable"
    rotor_motor: str = "rotor"
    camera_name: str = "arducam_64mp"
    session: str = "scan_001"
    elevations: list[float] = field(default_factory=lambda: [0.0, 45.0, 80.0])
    azimuths: list[float] = field(default_factory=lambda: [float(a) for a in range(0, 360, 45)])
    output_base: Path = field(default_factory=lambda: Path.home() / "photogrammetry")

    @property
    def base_url(self) -> str:
        return f"{self.firmware_url}/{self.api_version}"

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
    def images_dir(self) -> Path:
        return self.session_dir / "images"


# ---------------------------------------------------------------------------
# Firmware API calls
# ---------------------------------------------------------------------------

def _put(url: str, timeout: int = MOTOR_TIMEOUT, **kwargs) -> requests.Response:
    resp = requests.put(url, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp


def _get(url: str, timeout: int = CAPTURE_TIMEOUT, **kwargs) -> requests.Response:
    resp = requests.get(url, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp


def check_firmware(cfg: PipelineConfig) -> dict:
    """Verify firmware is reachable. Returns firmware info dict."""
    resp = _get(f"{cfg.base_url}/", timeout=10)
    return resp.json()


def move_motor(cfg: PipelineConfig, motor_name: str, degrees: float):
    """Move a motor to an absolute angle. Blocks until the move completes."""
    url = f"{cfg.base_url}/motors/{motor_name}/angle"
    _put(url, params={"degrees": degrees}, timeout=MOTOR_TIMEOUT)
    logger.debug("Motor %s → %.1f°", motor_name, degrees)


def capture_photo(cfg: PipelineConfig) -> bytes:
    """Capture a grayscale JPEG from the firmware camera endpoint.

    Grayscale conversion happens on the Pi before transfer, minimising
    network payload (≈3× smaller than colour JPEG at the same resolution).
    """
    url = f"{cfg.base_url}/cameras/{cfg.camera_name}/photo"
    resp = _get(url, params={"grayscale": "true"}, timeout=CAPTURE_TIMEOUT)
    return resp.content


def move_to_position(cfg: PipelineConfig, azimuth: float, elevation: float):
    """Move both motors to the target position."""
    # Move in parallel via threads would be cleaner, but sequential is simpler
    # and the firmware handles each move as a blocking call anyway.
    move_motor(cfg, cfg.turntable_motor, azimuth)
    move_motor(cfg, cfg.rotor_motor, elevation)


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------



def save_image(path: Path, jpeg_bytes: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg_bytes)


def save_sidecar(path: Path, azimuth: float, elevation: float, extra: dict = None):
    """Save a minimal pose sidecar JSON alongside the image."""
    data = {
        "azimuth_deg": azimuth,
        "elevation_deg": elevation,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if extra:
        data.update(extra)
    path.with_suffix(".json").write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(cfg: PipelineConfig):
    cfg.images_dir.mkdir(parents=True, exist_ok=True)

    positions = cfg.positions
    total = len(positions)

    # Verify firmware
    logger.info("Connecting to firmware at %s ...", cfg.base_url)
    try:
        info = check_firmware(cfg)
        logger.info("Firmware: %s (model: %s)", info.get("firmware_version"), info.get("model"))
    except Exception as e:
        logger.error("Cannot reach firmware: %s", e)
        logger.error("Is the OpenScan3 firmware running on the Pi?")
        return

    logger.info("Scan: %d positions, session '%s'", total, cfg.session)
    logger.info("Output: %s", cfg.images_dir)

    stats = {"ok": 0, "failed": 0, "capture_s": 0.0, "move_s": 0.0}
    t_pipeline = time.perf_counter()

    for i, pos in enumerate(positions):
        az = pos["azimuth"]
        el = pos["elevation"]
        prefix = f"scan_az{az:06.2f}_el{el:06.2f}"
        image_path = cfg.images_dir / f"{prefix}.jpg"

        logger.info("[%d/%d] az=%.1f° el=%.1f°", i + 1, total, az, el)

        # Move motors
        t0 = time.perf_counter()
        try:
            move_to_position(cfg, az, el)
        except Exception as e:
            logger.error("  Motor move failed: %s", e)
            stats["failed"] += 1
            continue
        move_elapsed = time.perf_counter() - t0
        stats["move_s"] += move_elapsed
        logger.info("  Moved in %.1fs", move_elapsed)

        # Capture
        t0 = time.perf_counter()
        try:
            raw_jpeg = capture_photo(cfg)
        except Exception as e:
            logger.error("  Capture failed: %s", e)
            stats["failed"] += 1
            continue
        cap_elapsed = time.perf_counter() - t0
        stats["capture_s"] += cap_elapsed
        logger.info("  Captured in %.1fs (%d KB)", cap_elapsed, len(raw_jpeg) // 1024)

        # Save
        try:
            save_image(image_path, raw_jpeg)
            save_sidecar(image_path, az, el)
        except Exception as e:
            logger.error("  Save failed: %s", e)
            stats["failed"] += 1
            continue

        stats["ok"] += 1
        logger.info("  Saved: %s (%d KB)", image_path.name, len(raw_jpeg) // 1024)

    total_elapsed = time.perf_counter() - t_pipeline
    image_count = len(list(cfg.images_dir.glob("*.jpg")))

    logger.info("")
    logger.info("=" * 55)
    logger.info("SCAN COMPLETE")
    logger.info("=" * 55)
    logger.info("  Positions:    %d OK / %d failed / %d total",
                stats["ok"], stats["failed"], total)
    logger.info("  Images saved: %d  (%s)",
                image_count, cfg.images_dir)
    logger.info("  Move time:    %.1fs total (%.1fs avg)",
                stats["move_s"], stats["move_s"] / max(total, 1))
    logger.info("  Capture time: %.1fs total (%.1fs avg)",
                stats["capture_s"], stats["capture_s"] / max(total, 1))
    logger.info("  Wall time:    %.1fs (%.1f min)",
                total_elapsed, total_elapsed / 60)

    summary = {
        "session": cfg.session,
        "positions_total": total,
        "positions_ok": stats["ok"],
        "positions_failed": stats["failed"],
        "images": image_count,
        "move_time_s": round(stats["move_s"], 1),
        "capture_time_s": round(stats["capture_s"], 1),
        "total_wall_time_s": round(total_elapsed, 1),
    }
    (cfg.session_dir / "scan_summary.json").write_text(json.dumps(summary, indent=2))

    if stats["ok"] > 0:
        logger.info("")
        logger.info("Next step — run reconstruction:")
        logger.info("  python src/colmap_reconstruct.py \\")
        logger.info("    --images %s \\", cfg.images_dir)
        logger.info("    --output %s/colmap \\", cfg.session_dir)
        logger.info("    --calibration data/calibration/calibration.json")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="OpenScan firmware-API scan pipeline → grayscale JPEG → COLMAP"
    )
    parser.add_argument("--firmware-url", default="http://192.168.4.202:8000",
                        help="OpenScan3 firmware base URL")
    parser.add_argument("--api-version", default="latest",
                        help="API version prefix (default: latest)")
    parser.add_argument("--turntable-motor", default="turntable",
                        help="Turntable motor name in firmware config")
    parser.add_argument("--rotor-motor", default="rotor",
                        help="Rotor/tilt arm motor name in firmware config")
    parser.add_argument("--camera", default="arducam_64mp",
                        help="Camera name in firmware config")
    parser.add_argument("--session", default="scan_001",
                        help="Session name (output directory)")
    parser.add_argument("--elevations", nargs="+", type=float, default=[0.0, 45.0, 80.0],
                        help="Elevation angles in degrees")
    parser.add_argument("--azimuths", nargs="+", type=float,
                        default=[float(a) for a in range(0, 360, 45)],
                        help="Azimuth angles in degrees")
    parser.add_argument("--output-base", type=Path,
                        default=Path.home() / "photogrammetry",
                        help="Base output directory")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cfg = PipelineConfig(
        firmware_url=args.firmware_url,
        api_version=args.api_version,
        turntable_motor=args.turntable_motor,
        rotor_motor=args.rotor_motor,
        camera_name=args.camera,
        session=args.session,
        elevations=args.elevations,
        azimuths=args.azimuths,
        output_base=args.output_base,
    )

    run_pipeline(cfg)


if __name__ == "__main__":
    main()
