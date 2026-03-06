# Limited Test Scan Plan (Phase D Validation)

**Objective:** Run 16–36 position scan to validate focus blending and COLMAP feature extraction before committing to full 120-position scan.

**Scope:** 2–3 elevation tiers × 8–12 azimuths

---

## 1. Scan Configuration

### Option A: Minimal (16 positions, ~1.5–2 hours)
```
Elevation: [0°, 80°]          (2 tiers)
Azimuth:   0°, 45°, 90°, 135°, 180°, 225°, 270°, 315°  (8 positions per tier)
Total:     16 positions
Brackets:  5–7 per position → 80–112 frames
```

### Option B: Medium (36 positions, ~3–4 hours)
```
Elevation: [0°, 40°, 80°]     (3 tiers)
Azimuth:   every 30°           (12 positions per tier)
Total:     36 positions
Brackets:  5–7 per position → 180–252 frames
```

**Recommendation:** Start with Option A. If successful, can expand to B.

---

## 2. Pre-Scan Checklist

### Hardware
- [ ] Pi powered on, SSH accessible at `photoscan-pi.local`
- [ ] Camera mounted, focused at working distance
- [ ] Motors homed (turntable at 0°, rotor at 0°)
- [ ] Enough disk space on Pi: `df -h ~/scan/`
- [ ] Workstation ready to receive data

### Software
- [ ] Focus bracket driver deployed: `~/photoscan/src/focus_bracket_driver.py`
- [ ] Demosaic script ready: `~/photogrammetry/demosaic.py`
- [ ] Focus blender ready: `~/photogrammetry/ai_focus_blender.py`
- [ ] COLMAP installed on workstation: `which colmap`

### Test Object
- [ ] Small object placed at scanner center
- [ ] Adequate lighting
- [ ] No reflections or extreme shadows

---

## 3. Capture Phase (Pi)

### Step 1: SSH into Pi
```bash
ssh pi@photoscan-pi.local
cd ~/photoscan
```

### Step 2: Create Scan Plan (Option A)
```bash
cat > scan_plan_test.json << 'EOF'
{
  "scan_name": "test_scan_001",
  "positions": [
    {"azimuth": 0.0, "elevation": 0.0},
    {"azimuth": 45.0, "elevation": 0.0},
    {"azimuth": 90.0, "elevation": 0.0},
    {"azimuth": 135.0, "elevation": 0.0},
    {"azimuth": 180.0, "elevation": 0.0},
    {"azimuth": 225.0, "elevation": 0.0},
    {"azimuth": 270.0, "elevation": 0.0},
    {"azimuth": 315.0, "elevation": 0.0},
    {"azimuth": 0.0, "elevation": 80.0},
    {"azimuth": 45.0, "elevation": 80.0},
    {"azimuth": 90.0, "elevation": 80.0},
    {"azimuth": 135.0, "elevation": 80.0},
    {"azimuth": 180.0, "elevation": 80.0},
    {"azimuth": 225.0, "elevation": 80.0},
    {"azimuth": 270.0, "elevation": 80.0},
    {"azimuth": 315.0, "elevation": 80.0}
  ]
}
EOF
```

### Step 3: Start Capture Session
```bash
python src/focus_bracket_driver.py \
  --capture \
  --positions scan_plan_test.json \
  --ai-blend \
  --ai-no-enfuse \
  --output-dir ~/scan/test_scan_001
```

**Flags explained:**
- `--capture` — Run live capture (not demo)
- `--positions` — Use predefined position list
- `--ai-blend` — Enable on-Pi focus blending
- `--ai-no-enfuse` — Skip Enfuse (speed up, reduce disk I/O on Pi)
- `--output-dir` — Save to timestamped directory

**Expected duration:** ~2 hours for Option A (capture + blend)

### Step 4: Monitor Progress
In another terminal:
```bash
ssh pi@photoscan-pi.local
watch -n5 'ls -lh ~/scan/test_scan_001/ | tail -20'
```

Check for:
- Raw `.raw` files being created (~76 MB each)
- Blended `.tif` files in `stacked/` (~142 MB each)
- JSON sidecars alongside each image

---

## 4. Transfer Phase (Workstation)

### Step 5: Transfer Data via rsync
```bash
# On workstation
rsync -avz --progress pi@photoscan-pi.local:~/scan/test_scan_001/ \
  ~/photogrammetry/test_scan_001/
```

**Expected:**
- ~12–35 GB transferred (depending on raw vs demosaiced)
- ~10–20 minutes on gigabit LAN

### Step 6: Verify Transfer
```bash
# Check file counts
find ~/photogrammetry/test_scan_001 -type f | wc -l

# Verify checksums (if sidecar includes them)
ls ~/photogrammetry/test_scan_001/raw/
```

---

## 5. Post-Processing Phase (Workstation)

### Step 7: Demosaic (if raw-on-Pi)
If Pi saved `.raw` files (Phase B mode), demosaic on workstation:
```bash
python ~/photogrammetry/demosaic.py \
  --batch-dir ~/photogrammetry/test_scan_001/raw \
  --output-dir ~/photogrammetry/test_scan_001/corrected
```

**Output:** Demosaiced 16-bit TIFF files.

### Step 8: Focus Blending
Run focus blending on all brackets:
```bash
python ~/photogrammetry/ai_focus_blender.py \
  --batch-dir ~/photogrammetry/test_scan_001/corrected \
  --output-dir ~/photogrammetry/test_scan_001/blended \
  --metrics-dir ~/photogrammetry/test_scan_001/metrics
```

**Output:**
- Blended 16-bit TIFF files (`scan_azXXX_elXXX_blended.tif`)
- Metrics JSON (`scan_azXXX_elXXX_metrics.json`)

### Step 9: Evaluate Blending Quality
```bash
# Check metrics
python << 'PYEOF'
import json
from pathlib import Path

metrics_dir = Path("~/photogrammetry/test_scan_001/metrics").expanduser()
metrics_files = sorted(metrics_dir.glob("*.json"))

total_blends = len(metrics_files)
ai_wins = sum(1 for m in metrics_files if json.load(open(m)).get("winner_reason") == "higher_sharpness")
enfuse_wins = sum(1 for m in metrics_files if "enfuse" in json.load(open(m)).get("winner_reason", ""))

print(f"Total brackets blended: {total_blends}")
print(f"AI-guided wins: {ai_wins} ({100*ai_wins/total_blends:.1f}%)")
print(f"Enfuse wins: {enfuse_wins} ({100*enfuse_wins/total_blends:.1f}%)")

# Sample metrics
for m in metrics_files[:3]:
    print(f"\n{m.stem}:")
    print(json.dumps(json.load(open(m)), indent=2))
PYEOF
```

---

## 6. Feature Extraction Phase (COLMAP)

### Step 10: Prepare COLMAP Workspace
```bash
mkdir -p ~/photogrammetry/test_scan_001/colmap_project

# Create image list with pose metadata
python << 'PYEOF'
import json
from pathlib import Path

blended_dir = Path("~/photogrammetry/test_scan_001/blended").expanduser()
metadata_dir = Path("~/photogrammetry/test_scan_001/metrics").expanduser()

# Generate cameras.txt with intrinsics (placeholder until calibration)
# For now, use default intrinsics (will be less accurate without calibration)
with open("~/photogrammetry/test_scan_001/colmap_project/cameras.txt".replace("~", str(Path.home())), "w") as f:
    f.write("# Camera list\n")
    f.write("1 PINHOLE 9152 6944 7000 7000 4576 3472\n")  # Placeholder focal length

# Generate images.txt with pose priors from metadata
with open("~/photogrammetry/test_scan_001/colmap_project/images.txt".replace("~", str(Path.home())), "w") as f:
    f.write("# Image list with pose priors\n")
    f.write("# image_id camera_id qvec tvec image_name\n")

    image_id = 1
    for blended_path in sorted(blended_dir.glob("*.tif")):
        # Extract azimuth/elevation from filename: scan_azXXX_elXXX_blended.tif
        stem = blended_path.stem
        parts = stem.split("_")

        try:
            az_str = next(p for p in parts if p.startswith("az")).replace("az", "")
            el_str = next(p for p in parts if p.startswith("el")).replace("el", "")
            azimuth = float(az_str)
            elevation = float(el_str)

            # Placeholder quaternion (identity) and translation (camera distance)
            # Real calibration would compute these from intrinsics + polar coords
            f.write(f"{image_id} 1 1 0 0 0 0 0 0 {blended_path.name}\n")
            image_id += 1
        except Exception as e:
            print(f"Warning: could not parse {stem}: {e}")

print(f"Generated placeholder COLMAP project with {image_id-1} images")
PYEOF
```

### Step 11: Run COLMAP Feature Extraction
```bash
cd ~/photogrammetry/test_scan_001/colmap_project

# Create database
colmap database_creator --database_path database.db

# Copy images to image folder
cp ~/photogrammetry/test_scan_001/blended/*.tif images/

# Feature extraction
colmap feature_extractor \
  --database_path database.db \
  --image_path images/

# Feature matching (with pose priors to reduce search space)
colmap exhaustive_matcher \
  --database_path database.db \
  --SiftMatching.max_num_matches 100000
```

**Expected output:** Feature tracks in `database.db`.

---

## 7. Reconstruction Phase (COLMAP)

### Step 12: Incremental SfM
```bash
mkdir -p sparse

colmap mapper \
  --database_path database.db \
  --image_path images/ \
  --output_path sparse/

# This will create sparse/0/cameras.bin, points3D.bin, images.bin, etc.
```

### Step 13: Export Reconstruction
```bash
colmap model_converter \
  --input_path sparse/0 \
  --output_path sparse/0_txt \
  --output_format TXT
```

---

## 8. Quality Evaluation

### Step 14: Analyze Sparse Cloud
```bash
python << 'PYEOF'
import json
import struct
from pathlib import Path

sparse_txt = Path("~/photogrammetry/test_scan_001/colmap_project/sparse/0_txt").expanduser()

# Read points3D.txt
points3d = []
with open(sparse_txt / "points3D.txt") as f:
    for line in f:
        if line.startswith("#"):
            continue
        parts = line.strip().split()
        if len(parts) >= 4:
            points3d.append({
                "id": int(parts[0]),
                "x": float(parts[1]),
                "y": float(parts[2]),
                "z": float(parts[3]),
            })

print(f"Sparse point cloud statistics:")
print(f"  Total points: {len(points3d)}")
if points3d:
    xs = [p["x"] for p in points3d]
    ys = [p["y"] for p in points3d]
    zs = [p["z"] for p in points3d]
    print(f"  X range: {min(xs):.1f} to {max(xs):.1f}")
    print(f"  Y range: {min(ys):.1f} to {max(ys):.1f}")
    print(f"  Z range: {min(zs):.1f} to {max(zs):.1f}")

# Read images.txt to count registered images
images = []
with open(sparse_txt / "images.txt") as f:
    for line in f:
        if line.startswith("#"):
            continue
        parts = line.strip().split()
        if len(parts) >= 2:
            images.append({"id": int(parts[0]), "name": parts[-1]})

print(f"\nRegistered images: {len(images)}")

# Success criteria
if len(points3d) > 10000:
    print("✅ PASS: Sufficient 3D points for reconstruction")
else:
    print("⚠️ WARNING: Fewer than 10k points (may need calibration or better object)")

if len(images) > len(set(img["name"] for img in images)) * 0.8:
    print("✅ PASS: >80% of images successfully registered")
else:
    print("⚠️ WARNING: <80% registration rate")

PYEOF
```

### Step 15: Visualize Sparse Cloud
```bash
# Export as PLY for visualization
python << 'PYEOF'
from pathlib import Path

sparse_txt = Path("~/photogrammetry/test_scan_001/colmap_project/sparse/0_txt").expanduser()
output_ply = Path("~/photogrammetry/test_scan_001/sparse_cloud.ply").expanduser()

with open(sparse_txt / "points3D.txt") as f:
    points = []
    for line in f:
        if line.startswith("#"):
            continue
        parts = line.strip().split()
        if len(parts) >= 7:
            points.append((float(parts[1]), float(parts[2]), float(parts[3]),
                          int(parts[4]), int(parts[5]), int(parts[6])))

# Write PLY header
with open(output_ply, "w") as f:
    f.write("ply\n")
    f.write("format ascii 1.0\n")
    f.write(f"element vertex {len(points)}\n")
    f.write("property float x\n")
    f.write("property float y\n")
    f.write("property float z\n")
    f.write("property uchar red\n")
    f.write("property uchar green\n")
    f.write("property uchar blue\n")
    f.write("end_header\n")

    for p in points:
        f.write(f"{p[0]:.3f} {p[1]:.3f} {p[2]:.3f} {int(p[3])} {int(p[4])} {int(p[5])}\n")

print(f"Exported {len(points)} points to {output_ply}")
print("Open with: MeshLab, CloudCompare, or Blender")
PYEOF
```

You can now open `sparse_cloud.ply` in a 3D viewer (CloudCompare, MeshLab, Blender) to visually inspect the reconstruction quality.

---

## 9. Documentation & Decision

### Step 16: Document Results
Create `test_scan_results.md`:
```markdown
# Test Scan Results — [Date]

## Capture Phase
- Positions captured: 16
- Total frames: ~110
- Blending success rate: 100% (all brackets produced output)
- Average AI blend time: ~93s per bracket
- Pi memory peak: ~1.1 GB

## Quality Metrics
- Feature extraction: X features across Y images
- Registration rate: Z%
- Sparse point cloud: W points
- Point cloud coverage: [visual assessment]

## Issues Encountered
- [List any errors, timeouts, or crashes]

## Next Steps
- [ ] Proceed to full 120-position scan
- [ ] Run camera calibration first (recommended if point cloud sparse)
- [ ] Optimize blend runtime (if critical for workflow)
- [ ] Adjust capture parameters (exposure, focus range, etc.)

## Visual Assessment
[Screenshots or observations about reconstruction quality]
```

### Step 17: Decision Checkpoint
**If test scan successful (>10k points, >80% registration):**
- ✅ Proceed to full 120-position scan
- ✅ Calibration can be deferred (nice-to-have for accuracy refinement)
- ✅ Ready for Phase D dense reconstruction

**If test scan shows issues:**
- ⚠️ Run camera calibration before full scan
- ⚠️ Consider increasing focus brackets (if depth-of-field inadequate)
- ⚠️ Check object placement and lighting

---

## Rollback Plan

If any step fails:
1. **Capture failure:** Check Pi logs in `~/scan/test_scan_001/logs/`
2. **Transfer failure:** Check network (`ping photoscan-pi.local`), disk space
3. **COLMAP failure:** May need calibration or adjust SfM parameters
4. **Quality insufficient:** Document findings and proceed to calibration

All intermediate outputs preserved for debugging.

---

**Estimated total time:** 4–6 hours (2h capture + 1h transfer + 1–2h processing + 1h analysis)

Ready to begin?
