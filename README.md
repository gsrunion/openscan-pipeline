# openscan-pipeline

Workstation-side processing pipeline for [OpenScan](https://github.com/OpenScan-org/OpenScan3) photogrammetry captures.

Handles everything after images leave the Pi: transfer, demosaicing, focus stacking, and reconstruction preparation.

## Pipeline stages

1. **Transfer** — rsync raw frames from Pi to workstation
2. **Demosaic** — Unpack SBGGR10_CSI2P raw Bayer → 16-bit linear RGB TIFF
3. **Focus stack** — Enfuse blending of focus brackets into all-in-focus images
4. **Reconstruction** — COLMAP SfM + OpenMVS dense reconstruction (planned)

## Usage

```bash
# Parallel demosaicing
python src/batch_process.py demosaic ~/photogrammetry/scan_001/raw \
    --output-dir ~/photogrammetry/scan_001/demosaiced --workers 8

# Parallel Enfuse blending
python src/batch_process.py enfuse ~/photogrammetry/scan_001/demosaiced \
    --output-dir ~/photogrammetry/scan_001/stacked --workers 8

# Full pipelined orchestrator (capture + transfer + process)
python src/pipeline_orchestrator.py --session scan_001 \
    --elevations 0 20 40 60 80 --azimuths 0 15 30 ... --workers 3
```

## Requirements

- Python 3.10+
- OpenCV 4.9+
- NumPy
- Enfuse 4.2+ (system package: `sudo apt install enfuse`)
- rsync + SSH access to Pi

## Hardware context

Designed for the OpenScan Classic turntable with Arducam Hawkeye 64MP camera (9152×6944, SBGGR10_CSI2P raw format). The companion firmware PR ([OpenScan3#67](https://github.com/OpenScan-org/OpenScan3/pull/67)) adds an optional quality gate to reject blurry captures at the source.

## Status

- [x] Demosaicing (parallel, ~6x speedup)
- [x] Focus stacking via Enfuse (parallel, ~7x speedup)
- [x] Pipelined orchestrator (overlapped capture/transfer/process)
- [ ] Camera calibration
- [ ] Pose-prior SfM (COLMAP with known turntable geometry)
- [ ] Dense reconstruction (OpenMVS)
