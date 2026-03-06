"""
demosaic.py — Workstation-side raw Bayer unpacking and demosaicing.

Processes packed SBGGR10_CSI2P raw files from the Pi and produces 16-bit
demosaiced TIFF images for the rest of the photogrammetry pipeline.

This completes the Phase B "raw-on-Pi architecture" change: Pi captures
and ships packed raw bytes (~80MB), workstation unpacks + demosaics.

Usage:
    from demosaic import demosaic_raw_file

    output_tiff = demosaic_raw_file(
        raw_path="scan_az045_el030_f0.raw",
        sidecar_json="scan_az045_el030_f0.json",
        output_dir=Path("./corrected/")
    )
"""

import json
import logging
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Expected sensor resolution (from camera spec)
SENSOR_RESOLUTION = (9152, 6944)


def _unpack_raw10csi2p(raw: np.ndarray, sensor_width: int = SENSOR_RESOLUTION[0]) -> np.ndarray:
    """Unpack RAW10_CSI2P packed Bayer buffer to uint16, scaled to full 16-bit range.

    CSI-2 packed 10-bit: 4 pixels in 5 bytes.
    Byte layout: [P0[9:2], P1[9:2], P2[9:2], P3[9:2],
                  P3[1:0]<<6 | P2[1:0]<<4 | P1[1:0]<<2 | P0[1:0]]

    Works for SBGGR10_CSI2P, SRGGB10_CSI2P, etc. — packing format is identical.
    The row stride in the buffer may include padding bytes; sensor_width is used
    to compute the valid pixel region and discard any padding.

    Returns uint16 array of shape (height, sensor_width), values scaled 10→16 bit.
    """
    if raw.dtype != np.uint8:
        # Some formats already unpack to uint16
        return (raw[:, :sensor_width].astype(np.uint16) << 6)

    h = raw.shape[0]
    n_groups = sensor_width // 4          # groups of 4 pixels that fit in sensor_width
    packed_bytes = n_groups * 5           # valid packed bytes per row (no padding)

    # Slice off any stride-alignment padding before reshaping
    packed = raw[:, :packed_bytes].reshape(h, n_groups, 5)
    p4 = packed[:, :, 4].astype(np.uint16)

    out = np.empty((h, n_groups * 4), dtype=np.uint16)
    out[:, 0::4] = (packed[:, :, 0].astype(np.uint16) << 2) | (p4 & 0x03)
    out[:, 1::4] = (packed[:, :, 1].astype(np.uint16) << 2) | ((p4 >> 2) & 0x03)
    out[:, 2::4] = (packed[:, :, 2].astype(np.uint16) << 2) | ((p4 >> 4) & 0x03)
    out[:, 3::4] = (packed[:, :, 3].astype(np.uint16) << 2) | ((p4 >> 6) & 0x03)

    # Scale 10-bit → full 16-bit range
    return (out << 6)


def demosaic_raw_file(
    raw_path: Path,
    sidecar_json: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    camera_mounted_inverted: bool = True,
) -> Path:
    """
    Unpack and demosaic a raw file from the Pi.

    Args:
        raw_path: Path to .raw file (SBGGR10_CSI2P packed bytes from Pi)
        sidecar_json: Path to accompanying .json metadata (optional, for format validation)
        output_dir: Directory to save demosaiced TIFF (defaults to raw_path parent)
        camera_mounted_inverted: Whether to apply 180° rotation (Pi camera mounted inverted)

    Returns:
        Path to output 16-bit TIFF file
    """
    raw_path = Path(raw_path)

    if output_dir is None:
        output_dir = raw_path.parent / "corrected"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load raw bytes from file
    raw_bytes = np.fromfile(raw_path, dtype=np.uint8)

    # Load sidecar if provided (for validation and format info)
    raw_format_info = {}
    if sidecar_json:
        sidecar_json = Path(sidecar_json)
        if sidecar_json.exists():
            metadata = json.loads(sidecar_json.read_text())
            raw_format_info = metadata.get("raw_format", {})
            logger.debug(f"Loaded raw format from sidecar: {raw_format_info}")

    # Reshape to image dimensions
    # Raw buffer from Pi has shape (height, packed_bytes_per_row)
    h = SENSOR_RESOLUTION[1]  # 6944
    expected_bytes_per_row = (SENSOR_RESOLUTION[0] // 4) * 5  # 9152/4 * 5 = 11440 bytes

    # Handle possible stride padding
    actual_bytes_per_row = raw_bytes.size // h
    if actual_bytes_per_row >= expected_bytes_per_row:
        raw = raw_bytes.reshape(h, actual_bytes_per_row)
    else:
        raise ValueError(
            f"Raw buffer size {raw_bytes.size} does not match expected "
            f"height={h}, bytes_per_row={expected_bytes_per_row}"
        )

    logger.info(f"Loaded raw buffer: shape={raw.shape}, dtype={raw.dtype}")

    # Unpack 10-bit CSI2P → uint16
    bayer_16 = _unpack_raw10csi2p(raw, sensor_width=SENSOR_RESOLUTION[0])
    logger.info(f"Unpacked Bayer: shape={bayer_16.shape}, dtype={bayer_16.dtype}, "
                f"range=[{bayer_16.min()}, {bayer_16.max()}]")

    # Demosaic (SBGGR → BGR)
    rgb_16 = cv2.cvtColor(bayer_16, cv2.COLOR_BayerBG2BGR)
    logger.debug(f"Demosaiced: shape={rgb_16.shape}")

    # Apply 180° rotation if camera mounted inverted
    if camera_mounted_inverted:
        rgb_16 = rgb_16[::-1, ::-1]
        logger.debug("Applied 180° rotation for inverted camera mount")

    # Save as 16-bit TIFF
    output_path = output_dir / (raw_path.stem + "_demosaiced.tif")
    cv2.imwrite(str(output_path), rgb_16)
    logger.info(f"Wrote demosaiced 16-bit TIFF: {output_path} ({output_path.stat().st_size / 1e6:.1f} MB)")

    return output_path


def batch_demosaic(raw_dir: Path, output_dir: Optional[Path] = None) -> list[Path]:
    """
    Demosaic all .raw files in a directory.

    Args:
        raw_dir: Directory containing .raw and .json sidecar pairs
        output_dir: Output directory (defaults to raw_dir/corrected)

    Returns:
        List of output TIFF paths
    """
    raw_dir = Path(raw_dir)
    raw_files = sorted(raw_dir.glob("*.raw"))

    if not raw_files:
        logger.warning(f"No .raw files found in {raw_dir}")
        return []

    outputs = []
    for raw_path in raw_files:
        sidecar = raw_path.with_suffix(".json")
        try:
            output = demosaic_raw_file(raw_path, sidecar, output_dir)
            outputs.append(output)
        except Exception as e:
            logger.error(f"Failed to demosaic {raw_path}: {e}")

    logger.info(f"Demosaiced {len(outputs)}/{len(raw_files)} files")
    return outputs


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Demosaic raw files from Pi capture")
    parser.add_argument("input", type=Path, help="Raw file or directory")
    parser.add_argument("--output-dir", type=Path, help="Output directory (default: input parent/corrected)")
    args = parser.parse_args()

    if args.input.is_file():
        output = demosaic_raw_file(args.input, args.input.with_suffix(".json"), args.output_dir)
        print(f"Output: {output}")
    else:
        outputs = batch_demosaic(args.input, args.output_dir)
        for o in outputs:
            print(f"  {o}")
