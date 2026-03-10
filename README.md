# openscan-pipeline

Workstation-side photogrammetry pipeline for [OpenScan](https://github.com/OpenScan-org/OpenScan3).

## Archive Status

This repository is an experimental prototype for photogrammetry workflows around OpenScan hardware.

It is no longer the intended long-term home for this work. Future development and upstreamable ideas are being tracked in the OpenScan3 project:

- https://github.com/OpenScan-org/OpenScan3/issues/69
- https://github.com/OpenScan-org/OpenScan3/issues/70
- https://github.com/OpenScan-org/OpenScan3/issues/71
- https://github.com/OpenScan-org/OpenScan3/issues/72
- https://github.com/OpenScan-org/OpenScan3/issues/73
- https://github.com/OpenScan-org/OpenScan3/issues/74
- https://github.com/OpenScan-org/OpenScan3/issues/75

This repo remains available as a reference implementation and experiment log.

The active workflow uses the OpenScan3 firmware REST API to:
- calibrate the camera from checkerboard captures,
- drive the turntable and rotor from the workstation,
- capture grayscale JPEG images with pose sidecars,
- run COLMAP sparse reconstruction with turntable pose priors.

Legacy raw-transfer, demosaic, and Enfuse utilities are still present for the earlier pipeline.

See [STATUS.md](STATUS.md) for current project status and phase tracking.

## Pipeline stages

1. **Calibration** — capture checkerboard images and solve intrinsics with OpenCV
2. **Capture** — move motors via firmware API and save grayscale JPEGs + pose JSON
3. **Sparse reconstruction** — run COLMAP with injected turntable pose priors
4. **Dense reconstruction** — optional OpenMVS export and meshing

## Setup

### System dependencies

```bash
# Ubuntu/Pop!_OS/Debian
sudo apt install colmap

# Optional legacy pipeline support
sudo apt install enfuse rsync openssh-client

# Optional dense reconstruction
# sudo apt install openmvs
```

### Python dependencies

Requires Python 3.10+.

```bash
pip install numpy>=1.24 opencv-python>=4.9 requests pillow
```

Or install from the project:

```bash
pip install .
```

### Firmware connectivity

The active pipeline expects the OpenScan3 firmware API to be reachable on the Pi:

```bash
curl http://<PI_IP>:8000/latest/
```

## Usage

### Recommended workflow

```bash
# 1. Guided calibration capture
python src/calibrate_guided.py --square-mm 24.0

# 2. Capture a session through the firmware API
python src/pipeline_orchestrator.py \
    --firmware-url http://<PI_IP>:8000 \
    --session scan_001 \
    --elevations 0 20 40 60 80 \
    --azimuths 0 15 30 45 60 75 90 105 120 135 150 165 180 195 210 225 240 255 270 285 300 315 330 345

# 3. Reconstruct with pose priors
python src/colmap_reconstruct.py \
    --images ~/photogrammetry/scan_001/images \
    --output ~/photogrammetry/scan_001/colmap \
    --calibration data/calibration/calibration.json \
    --sparse-only
```

Current status: a first calibration solve exists at `data/calibration/calibration.json`, but its RMS reprojection error is `3.6627 px`, so treat it as a validation calibration rather than a final one.

### Calibration options

```bash
# Capture checkerboard frames every 2 seconds and auto-calibrate
python src/calibrate.py

# Re-run calibration from an existing image set
python src/calibrate.py --calibrate-only --image-dir data/calibration/images

# Resume a partially completed guided session
python src/calibrate_guided.py --square-mm 24.0 --resume
```

### Legacy raw-processing workflow

The older raw Bayer pipeline is still available for comparison and recovery:

```bash
python src/batch_process.py demosaic ~/photogrammetry/scan_001/raw \
    --output-dir ~/photogrammetry/scan_001/demosaiced --workers 8

python src/batch_process.py enfuse ~/photogrammetry/scan_001/demosaiced \
    --output-dir ~/photogrammetry/scan_001/stacked --workers 8
```

## Hardware context

Designed for the OpenScan Classic turntable with Arducam Hawkeye 64MP camera:
- **Sensor:** 9152×6944, 1/1.7", SBGGR10_CSI2P raw format
- **Raw frame size:** ~76MB packed (46.5% smaller than demosaiced TIFF)
- **Capture platform:** Raspberry Pi 4/5 running OpenScan3 firmware
- **Processing platform:** Workstation (tested on i7-10700F, 62GB RAM, GTX 1660 SUPER)

The companion firmware PR ([OpenScan3#67](https://github.com/OpenScan-org/OpenScan3/pull/67)) adds an optional Laplacian variance quality gate to reject blurry captures at the source.

## Performance

Benchmarked on i7-10700F with 8 workers:

| Operation | Serial | Parallel (8 workers) | Speedup |
|-----------|--------|---------------------|---------|
| Demosaic (96 files) | ~6.5 min | ~1 min | 6.3x |
| Enfuse (16 brackets) | ~8 min | ~1.1 min | 7.1x |

## License

MIT
