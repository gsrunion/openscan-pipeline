# openscan-pipeline

Workstation-side processing pipeline for [OpenScan](https://github.com/OpenScan-org/OpenScan3) photogrammetry captures.

Handles everything after images leave the Pi: transfer, demosaicing, focus stacking, and reconstruction preparation.

See [STATUS.md](STATUS.md) for current project status and phase tracking.

## Pipeline stages

1. **Transfer** — rsync raw frames from Pi to workstation
2. **Demosaic** — Unpack SBGGR10_CSI2P raw Bayer → 16-bit linear RGB TIFF
3. **Focus stack** — Enfuse blending of focus brackets into all-in-focus images
4. **Reconstruction** — COLMAP SfM + OpenMVS dense reconstruction (planned)

## Setup

### System dependencies

```bash
# Ubuntu/Pop!_OS/Debian
sudo apt install enfuse rsync openssh-client

# For reconstruction (Phase D)
sudo apt install colmap
# OpenMVS: build from source or use snap/flatpak
```

### Python dependencies

Requires Python 3.10+.

```bash
pip install numpy>=1.24 opencv-python>=4.9
```

Or install from the project:

```bash
pip install .
```

### Pi connectivity

The pipeline expects SSH access to the Raspberry Pi running OpenScan3:

```bash
# Ensure passwordless SSH works
ssh -i ~/.ssh/id_ed25519 pi@<PI_IP> "echo ok"
```

## Usage

### Batch processing (post-capture)

```bash
# Parallel demosaicing (raw Bayer → 16-bit TIFF)
python src/batch_process.py demosaic ~/photogrammetry/scan_001/raw \
    --output-dir ~/photogrammetry/scan_001/demosaiced --workers 8

# Parallel Enfuse blending (focus brackets → all-in-focus)
python src/batch_process.py enfuse ~/photogrammetry/scan_001/demosaiced \
    --output-dir ~/photogrammetry/scan_001/stacked --workers 8
```

### Single file demosaicing

```bash
python src/demosaic.py ~/photogrammetry/scan_001/raw/scan_az0.0_el0.0_f0.raw \
    --output-dir ~/photogrammetry/scan_001/demosaiced
```

### Full pipelined orchestrator

Overlaps capture, transfer, and processing for maximum throughput:

```bash
python src/pipeline_orchestrator.py \
    --session scan_001 \
    --elevations 0 20 40 60 80 \
    --azimuths 0 15 30 45 60 75 90 105 120 135 150 165 180 195 210 225 240 255 270 285 300 315 330 345 \
    --workers 3
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
