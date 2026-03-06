"""
pose_metadata.py  —  Phase A / A5
Pose metadata generation and validation for OpenScan capture pipeline.

Generates a sidecar .json file alongside each captured image containing
the full pose record (azimuth, elevation, focus info, sensor metadata).

Pi-side path: ~/photoscan/src/pose_metadata.py

Usage:
    from pose_metadata import PoseMetadata, write_sidecar, validate_sidecar

    meta = PoseMetadata(
        image_id="scan_001_az045.00_el020.00_f2",
        azimuth_deg=45.0,
        elevation_deg=20.0,
        radius_mm=185.0,
        focus_lens_position=8.684,
        focus_bracket_index=2,
        focus_bracket_total=5,
        sensor_resolution=(9152, 6944),
        file_path="raw/scan_001_az045.00_el020.00_f2.png",
    )
    write_sidecar(meta, Path("raw/scan_001_az045.00_el020.00_f2.json"))
    validate_sidecar(Path("raw/scan_001_az045.00_el020.00_f2.json"))
"""

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

METADATA_VERSION = "1.0"

# Required fields for a valid sidecar
REQUIRED_FIELDS = {
    "version", "image_id", "azimuth_deg", "elevation_deg",
    "focus_bracket_index", "focus_bracket_total",
    "timestamp_utc", "sensor_resolution", "file_path",
}


@dataclass
class PoseMetadata:
    """
    Full pose and capture metadata for a single bracketed frame.
    Travels with the image through every pipeline stage.
    """
    image_id:              str
    azimuth_deg:           float
    elevation_deg:         float
    focus_bracket_index:   int
    focus_bracket_total:   int
    sensor_resolution:     tuple[int, int]     # (width, height)
    file_path:             str                 # relative to session root

    # Optional — filled in when known
    radius_mm:             Optional[float] = None
    focus_lens_position:   Optional[float] = None  # LensPosition 0–15
    focus_distance_mm:     Optional[float] = None  # physical distance if known
    exposure_time_us:      Optional[int]   = None
    analogue_gain:         Optional[float] = None
    colour_gains:          Optional[tuple[float, float]] = None
    laplacian_variance:    Optional[float] = None  # filled by quality gate (A7)

    # Auto-filled on creation
    version:               str = METADATA_VERSION
    timestamp_utc:         str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def from_capture(
        cls,
        azimuth_deg: float,
        elevation_deg: float,
        focus_bracket_index: int,
        focus_bracket_total: int,
        sensor_resolution: tuple[int, int],
        session_name: str,
        file_path: str,
        focus_lens_position: Optional[float] = None,
        radius_mm: Optional[float] = None,
        camera_metadata: Optional[dict] = None,
    ) -> "PoseMetadata":
        """
        Construct from capture parameters.
        camera_metadata: dict from picamera2 capture_metadata().
        """
        image_id = (
            f"{session_name}_"
            f"az{azimuth_deg:06.2f}_"
            f"el{elevation_deg:06.2f}_"
            f"f{focus_bracket_index}"
        )

        exposure_time_us = None
        analogue_gain    = None
        colour_gains     = None

        if camera_metadata:
            exposure_time_us = camera_metadata.get("ExposureTime")
            analogue_gain    = camera_metadata.get("AnalogueGain")
            cg = camera_metadata.get("ColourGains")
            if cg is not None:
                colour_gains = tuple(float(x) for x in cg)

        return cls(
            image_id=image_id,
            azimuth_deg=round(azimuth_deg, 4),
            elevation_deg=round(elevation_deg, 4),
            focus_bracket_index=focus_bracket_index,
            focus_bracket_total=focus_bracket_total,
            sensor_resolution=sensor_resolution,
            file_path=file_path,
            radius_mm=radius_mm,
            focus_lens_position=focus_lens_position,
            exposure_time_us=exposure_time_us,
            analogue_gain=analogue_gain,
            colour_gains=colour_gains,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        # Convert tuples to lists for JSON serialisation
        if isinstance(d.get("sensor_resolution"), (tuple, list)):
            d["sensor_resolution"] = list(d["sensor_resolution"])
        if isinstance(d.get("colour_gains"), (tuple, list)):
            d["colour_gains"] = list(d["colour_gains"])
        return d


def write_sidecar(meta: PoseMetadata, path: Path) -> Path:
    """
    Write pose metadata as a JSON sidecar file.
    Uses atomic write (temp file + rename) to avoid partial writes.
    """
    path = Path(path)
    tmp = path.with_suffix(".json.tmp")
    data = meta.to_dict()
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)
    logger.debug("Sidecar written: %s", path)
    return path


def read_sidecar(path: Path) -> PoseMetadata:
    """Load a sidecar JSON and return a PoseMetadata instance."""
    path = Path(path)
    data = json.loads(path.read_text())
    sr = data.get("sensor_resolution")
    if sr:
        data["sensor_resolution"] = tuple(sr)
    cg = data.get("colour_gains")
    if cg:
        data["colour_gains"] = tuple(cg)
    # Remove unknown fields to handle version differences gracefully
    known = {f.name for f in PoseMetadata.__dataclass_fields__.values()}
    data = {k: v for k, v in data.items() if k in known}
    return PoseMetadata(**data)


def validate_sidecar(path: Path) -> bool:
    """
    Validate a sidecar JSON file.
    Returns True if valid, raises ValueError if not.
    """
    path = Path(path)
    if not path.exists():
        raise ValueError(f"Sidecar not found: {path}")

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {path}: {e}")

    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        raise ValueError(f"Sidecar {path} missing fields: {missing}")

    if data.get("version") != METADATA_VERSION:
        raise ValueError(f"Unexpected version {data.get('version')} in {path}")

    sr = data.get("sensor_resolution")
    if not (isinstance(sr, list) and len(sr) == 2 and all(isinstance(x, int) for x in sr)):
        raise ValueError(f"Invalid sensor_resolution in {path}: {sr}")

    az = data.get("azimuth_deg")
    el = data.get("elevation_deg")
    if not (isinstance(az, (int, float)) and 0 <= az <= 360):
        raise ValueError(f"Invalid azimuth_deg {az}")
    if not (isinstance(el, (int, float)) and 0 <= el <= 140):
        raise ValueError(f"Invalid elevation_deg {el}")

    logger.debug("Sidecar valid: %s", path)
    return True


def sidecar_path_for(image_path: Path) -> Path:
    """Return the expected sidecar path for a given image file."""
    return image_path.with_suffix(".json")


# ---------------------------------------------------------------------------
# CLI — acceptance test
# ---------------------------------------------------------------------------

def _run_acceptance_test(output_dir: Path):
    """A5 acceptance test: write and validate 50 test sidecars."""
    import random

    output_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    paths = []

    for i in range(50):
        az = random.uniform(0, 360)
        el = random.uniform(0, 140)
        bi = i % 5
        bt = 5

        meta = PoseMetadata.from_capture(
            azimuth_deg=az,
            elevation_deg=el,
            focus_bracket_index=bi,
            focus_bracket_total=bt,
            sensor_resolution=(9152, 6944),
            session_name="test_session",
            file_path=f"raw/test_{i:03d}.png",
            focus_lens_position=8.684,
            radius_mm=185.0,
            camera_metadata={
                "ExposureTime": 10000,
                "AnalogueGain": 2.0,
                "ColourGains": (1.8, 1.5),
            },
        )

        sidecar = output_dir / f"test_{i:03d}.json"
        write_sidecar(meta, sidecar)
        paths.append(sidecar)

        try:
            validate_sidecar(sidecar)
            # Round-trip check
            meta2 = read_sidecar(sidecar)
            assert abs(meta2.azimuth_deg - round(az, 4)) < 1e-6
            assert abs(meta2.elevation_deg - round(el, 4)) < 1e-6
            assert meta2.image_id == meta.image_id
        except Exception as e:
            errors.append(f"Frame {i}: {e}")

    passed = 50 - len(errors)
    print(f"\nA5 Acceptance: {passed}/50 sidecars valid")
    if errors:
        for e in errors:
            print(f"  ERROR: {e}")
    print(f"Result: {'PASS' if not errors else 'FAIL'}")

    # Clean up test files
    for p in paths:
        p.unlink(missing_ok=True)

    return not errors


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Pose Metadata Generator")
    parser.add_argument("--acceptance-test", action="store_true")
    parser.add_argument("--output-dir", type=Path,
                        default=Path.home() / "scan/inbox/raw")
    args = parser.parse_args()

    if args.acceptance_test:
        ok = _run_acceptance_test(args.output_dir)
        raise SystemExit(0 if ok else 1)
    else:
        parser.print_help()
