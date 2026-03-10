# Project Status

**Last updated:** 2026-03-10

## Current Phase: D — Dense Reconstruction (calibration + validation in progress)

### Phase A: Foundation ✅
- [x] SSH & passwordless auth to Pi
- [x] rsync transfer (checksummed, ~4MB/s WiFi)
- [x] Motor controller (24-position sweep, 0.04° max error)
- [x] Focus bracket driver (LensPosition 0–15 scale)
- [x] Pose metadata JSON sidecars
- [x] 16-bit TIFF capture (raw Bayer unpack + demosaic)
- [x] On-Pi quality gate (Laplacian variance, min=20)
- [x] Enfuse focus stacking (6/6 brackets, 1.04–1.06 sharpness ratio)
- [x] End-to-end integration test (5/5 PASS)
- [ ] Camera calibration (requires physical checkerboard + working rotor)

### Phase B: Raw-on-Pi Architecture ✅
- [x] Save packed raw bytes on Pi (~76MB/frame SBGGR10_CSI2P)
- [x] Workstation-side demosaic module
- [x] 46.5% bandwidth reduction (165MB → 76MB/frame)

### Phase C: Focus Blending ✅
- [x] Enfuse-based focus stacking (computational, deterministic)
- [x] Parallel batch processing (6.3x demosaic, 7.1x Enfuse speedup)
- [x] Pipelined orchestrator (overlapped capture/transfer/process)

### Phase D: Dense Reconstruction 🔄
- [x] 16-position test scan (capture → demosaic → blend) ✅
- [x] COLMAP feature extraction (9.4–10.8k features/image) ✅
- [x] Feature matching (120 matches found) ✅
- [x] **Rotor motor repair** ✅ — elevation gearbox repaired
- [ ] **Camera calibration** — unblocked; collect checkerboard set and solve intrinsics
- [ ] **Pose-prior SfM** — firmware capture + pose-prior COLMAP scripts drafted locally; needs end-to-end validation
- [ ] Full 120-position scan (5 elevations × 24 azimuths)
- [ ] OpenMVS dense reconstruction + mesh

### Phase E: Post-Processing (planned)
- [ ] Mesh smoothing, hole filling, noise reduction
- [ ] Segmentation and measurement extraction

---

## Blockers

| Blocker | Status | Notes |
|---------|--------|-------|
| Rotor gear | Cleared | Elevation gearbox repaired |
| Camera calibration | Active next step | Needs checkerboard capture at varied distances/tilts |
| Pose-prior SfM | Pending validation | New local scripts exist but are not documented/committed yet |

## Immediate Next Steps

1. Improve the checkerboard set until `data/calibration/calibration.json` reaches an acceptable RMS.
2. Capture a new multi-elevation grayscale JPEG session via the firmware API pipeline.
3. Run pose-prior COLMAP sparse reconstruction on that session with `--calibration`.
4. Validate sparse registration quality, then proceed to dense reconstruction.

## Upstream Contributions

| PR | Repo | Status | Description |
|----|------|--------|-------------|
| [#67](https://github.com/OpenScan-org/OpenScan3/pull/67) | OpenScan3 | Open | Laplacian variance quality gate for captures |

## Test Scan Results (16-position, 2026-03-06)

| Stage | Input | Output | Time |
|-------|-------|--------|------|
| Capture (Pi) | — | 96 raw (6.6GB) | 7 min |
| Transfer | 96 raw | local copy | 15 min |
| Demosaic | 96 raw | 96 TIFF-16 (15GB) | 6 min |
| Focus blend | 96 TIFF-16 | 16 blended | 7 min |
| COLMAP feat | 16 images | 9.4–10.8k feat/img | 20s |
| COLMAP match | features | 120 matches | 6s |
| COLMAP SfM | matches | **FAILED** | — |

**Total pipeline time:** ~35 minutes
**SfM failure root cause:** Turntable geometry too constrained (sequential 45° rotations → minimal parallax)

## Projected Full Scan (120 positions)

| Scenario | Est. Time | Bottleneck |
|----------|-----------|------------|
| WiFi (4MB/s) | ~2.2 hours | Transfer |
| Ethernet (direct) | ~55 min | Capture |
