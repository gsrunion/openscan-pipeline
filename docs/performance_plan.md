# AI Focus Blender Performance Optimization Plan

**Target:** Reduce 64MP bracket processing from 93s → <10s (9.3× speedup)
**Hardware:** Raspberry Pi 4/5, 4GB RAM baseline
**Constraint:** Memory must stay under 2.5 GB RSS

---

## 1. Baseline Profile Analysis

### Current Implementation Timeline (93s for 7-frame 9152×6944 bracket)

**Estimated breakdown (rough):**
- Load 7 frames: ~20s (I/O bound)
- Grayscale conversion × 7: ~5s
- Sharpness map computation × 7: ~30s (Gaussian blur + Laplacian on full res)
- Winner selection + upscaling: ~5s
- Seam smoothing (median blur): ~10s
- Frame selection (mask indexing loop): ~15s
- Metrics computation: ~5s
- **Enfuse baseline (parallel but waits on join):** ~20s (dominates when AI finishes early)

**Key observation:** Sharpness map computation (30s) is ~32% of runtime.

---

## 2. Optimization Opportunities

### 2.1 Sharpness Map Computation (HIGH IMPACT)

**Current:**
```python
def compute_sharpness_map(gray_norm: np.ndarray) -> np.ndarray:
    den = cv2.GaussianBlur(gray_norm, (0, 0), 0.8)      # Slow on full res
    lap = cv2.Laplacian(den, cv2.CV_32F, ksize=3)       # Laplacian
    sharp = np.abs(lap)
    sharp = cv2.GaussianBlur(sharp, (0, 0), 1.1)        # Second blur
    return sharp
```

**Problem:** Two full-resolution Gaussian blurs (0.8σ denoise, then 1.1σ smooth) on 64MP.

**Optimization 1A — Skip denoise step on Pi:**
- Laplacian variance is robust to noise at this scale
- Saves ~15s
```python
def compute_sharpness_map_fast(gray_norm: np.ndarray) -> np.ndarray:
    lap = cv2.Laplacian(gray_norm, cv2.CV_32F, ksize=3)
    sharp = np.abs(lap)
    # Optionally: light blur (0.5σ instead of 1.1σ)
    sharp = cv2.GaussianBlur(sharp, (0, 0), 0.5)
    return sharp
```

**Optimization 1B — Compute at reduced resolution:**
- If focus scale is already 0.25, compute sharpness at that scale
- Saves ~28s (inverse O(n²) for 2D images)
```python
def compute_sharpness_map_scaled(gray_norm: np.ndarray, scale: float = 0.25) -> np.ndarray:
    h_small, w_small = int(gray_norm.shape[0]*scale), int(gray_norm.shape[1]*scale)
    gray_small = cv2.resize(gray_norm, (w_small, h_small), interpolation=cv2.INTER_AREA)
    lap = cv2.Laplacian(gray_small, cv2.CV_32F, ksize=3)
    sharp_small = np.abs(lap)
    # Upsample back to original size (interpolation is cheap)
    sharp = cv2.resize(sharp_small, (gray_norm.shape[1], gray_norm.shape[0]),
                       interpolation=cv2.INTER_LINEAR)
    return sharp
```

**Combined impact: ~25–30s saved (27–32% of total)**

---

### 2.2 Frame Selection Loop (MEDIUM IMPACT)

**Current:**
```python
out = np.zeros((h, w, 3), dtype=np.uint16)
for i, p in enumerate(bracket_paths):
    frame = load_16bit_bgr(p)                  # Re-loads frame from disk
    mask = winner_full == i
    out[mask] = frame[mask]
    ensure_memory_limit()
```

**Problem:**
- Re-loads each frame from disk (should already be in memory from earlier sharpness computation)
- Mask indexing is slow in tight loop
- 7 iterations × disk I/O

**Optimization 2A — Cache frames in memory during sharpness computation:**
- Don't discard frames after sharpness map; keep them in list
- Reuse cached frames during selection

**Optimization 2B — Vectorized selection:**
```python
# Instead of loop, use advanced indexing
out = frames_bgr16[winner_full]  # Broadcast pixel selection
```
But winner_full is shape [h, w] containing frame indices, not directly indexable.

**Optimization 2C — Decompose into channels, select once:**
```python
out = np.zeros((h, w, 3), dtype=np.uint16)
for c in range(3):
    # For each channel, use np.choose or similar
    channel_stack = np.stack([f[:, :, c] for f in frames_bgr16], axis=0)  # [n,h,w]
    out[:, :, c] = channel_stack[winner_full, np.arange(h)[:, None], np.arange(w)]
```

**Combined impact: ~8–12s saved (9–13% of total)**

---

### 2.3 Seam Smoothing — Median Blur (LOW-MEDIUM IMPACT)

**Current:**
```python
winner_full = cv2.medianBlur(winner_full, 3)  # ~10s on 64MP
```

**Problem:** Median filter is O(5n) per pixel (3×3 window).

**Optimization 3A — Skip median blur entirely:**
- Soft mask in full-memory path already smooths transitions
- Low-memory hardmask INTER_NEAREST upscaling creates clean boundaries (median blur minimal gain)
- **Savings: ~10s with minimal quality loss**

**Optimization 3B — Use fast binary dilation instead:**
```python
# If we only care about small speckle removal:
kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
winner_full = cv2.morphologyEx(winner_full, cv2.MORPH_CLOSE, kernel)  # Faster
```

**Combined impact: 10s saved if skipped entirely**

---

### 2.4 SSIM Computation (MEDIUM IMPACT)

**Current:**
```python
def ssim_gray(a: np.ndarray, b: np.ndarray) -> float:
    # Four 11×11 Gaussian blurs + arithmetic
```

**Problem:** Global SSIM requires 4 expensive Gaussian blurs on full resolution.

**Optimization 4A — Compute SSIM at reduced resolution:**
```python
def ssim_gray_fast(a: np.ndarray, b: np.ndarray, scale: float = 0.25) -> float:
    h, w = int(a.shape[0]*scale), int(a.shape[1]*scale)
    a_small = cv2.resize(a, (w, h), interpolation=cv2.INTER_AREA)
    b_small = cv2.resize(b, (w, h), interpolation=cv2.INTER_AREA)
    # Then compute SSIM on small images
    # Result is highly correlated with full-res SSIM
```

**Optimization 4B — Skip SSIM when not comparing (AI-only path):**
- If Enfuse disabled, skip SSIM computation entirely
- Saves ~5–8s

**Combined impact: ~5–8s saved**

---

### 2.5 Artifact Detection (MEDIUM-HIGH IMPACT)

**Current:**
```python
def detect_artifacts(ai_bgr16: np.ndarray, enfuse_bgr16: np.ndarray) -> dict:
    # Canny edge detection + multiple Gaussian blurs + variance calculations
```

**Problem:** 6 expensive operations (Canny, Gaussian blur, Laplacian, etc.) on full resolution.

**Optimization 5A — Disable artifact detection on Pi:**
- Artifact detection is defensive fallback; Enfuse is usually fallback anyway
- **Savings: ~3–5s**
- **Tradeoff:** Less nuanced winner selection, but Enfuse tiebreak handles most cases

**Optimization 5B — Compute on downsampled image:**
```python
def detect_artifacts_fast(ai_bgr16: np.ndarray, enfuse_bgr16: np.ndarray, scale: float = 0.25):
    ai_small = cv2.resize(ai_bgr16, ..., interpolation=cv2.INTER_AREA)
    en_small = cv2.resize(enfuse_bgr16, ..., interpolation=cv2.INTER_AREA)
    # Run artifact detection on small images
```

**Combined impact: 3–5s saved if disabled; 1–2s if downsampled**

---

### 2.6 I/O Optimization (LOW IMPACT on Pi)

**Current:**
- Load frames with `cv2.imread()` one at a time
- Potential disk cache contention

**Optimization 6A — Batch load frames upfront:**
- Pre-load all 7 frames before sharpness computation
- Allows OS to optimize sequential I/O
- **Savings: ~2–3s**

---

## 3. Composite Optimization Strategy

### Phase 1: Fast Wins (Target 20–30s savings)

**Apply immediately:**
1. **Skip denoise in sharpness map** (-15s)
2. **Skip median blur** (-10s)
3. **Cache frames instead of re-loading** (-5s)
4. **Skip artifact detection** (-3s)

**Total Phase 1: ~33s saved → 60s total runtime (6× improvement)**

### Phase 2: Algorithmic Improvements (Target additional 30–40s)

**Apply after Phase 1 if still above 10s:**
1. **Compute sharpness at reduced scale** (-25–28s, if needed)
2. **Compute SSIM at reduced scale** (-5s, if needed)
3. **Vectorize frame selection** (-8s, if measurable)

**Total Phase 2: ~38–41s additional savings → 19–22s total runtime (4.2–4.8× improvement)**

### Phase 3: Structural Changes (if still above 10s)

**Consider only if Phase 1+2 insufficient:**
1. **Disable Enfuse comparison on Pi** (saves Enfuse overhead, but loses baseline)
2. **Tile-wise processing** (complex, high risk of quality regression)
3. **Use GPU acceleration** (picamera2 + CUDA, if available)

---

## 4. Implementation Roadmap

### Step 1: Create Optimized Version
- New file: `ai_focus_blender_fast.py` (keeps original for reference)
- Implement Phase 1 optimizations
- Add CLI flag: `--fast` to enable fast path

### Step 2: Benchmark on Workstation
- Load 7-frame test bracket from existing stacked/ outputs
- Run `python ai_focus_blender_fast.py --stack ...` with timing
- Compare output quality (SSIM vs. original)

### Step 3: Deploy & Test on Pi
- Copy to Pi: `~/photoscan/src/ai_focus_blender_fast.py`
- Test with real capture:
  ```bash
  ssh pi@photoscan-pi.local
  python ~/photoscan/src/ai_focus_blender_fast.py --stack f0.tif ... f6.tif --output out.tif --metrics m.json
  ```
- Measure runtime: `time python ...`
- Monitor memory: `watch -n1 'ps aux | grep python'`

### Step 4: Integrate into focus_bracket_driver.py
- Update capture driver to use fast blender by default
- Keep option to use original (for offline processing, where speed irrelevant)

### Step 5: Validate End-to-End
- Run Phase C test with `--ai-blend` and `--ai-no-enfuse`
- Measure bracket capture + blend time
- Confirm <10s target met

---

## 5. Expected Outcomes

| Phase | Expected Runtime | Speedup |
|-------|------------------|---------|
| Baseline | 93s | 1.0× |
| Phase 1 (fast wins) | 60s | 1.5× |
| Phase 1+2 (full opt) | 19–22s | 4.2–4.8× |
| Phase 1+2+3 (if needed) | 8–15s | 6–11× |

**Minimum acceptable:** Phase 1 (60s) — halves current runtime, provides buffer.
**Target achievable:** Phase 1+2 (20s) — meets all practical requirements (focus stacking time acceptable).
**Stretch goal:** Phase 1+2+3 <10s — not critical; 20s bracket + 5s I/O + 5s network = 30s per pose, × 120 poses = 1 hour for full scan.

---

## 6. Quality Assurance

### Metrics to Track
- **Runtime per bracket** (goal <10s, acceptable <30s)
- **Memory peak RSS** (must stay <2.5 GB)
- **Output SSIM vs. baseline** (should be ≥0.98 if changes are purely algorithmic)
- **PASS/FAIL on full scans** (zero crashes, completed sessions)

### Test Cases
1. **Single bracket (7 frames, 64MP)** — measure speed
2. **Multiple brackets back-to-back** — memory stability
3. **Compare outputs** — SSIM baseline vs. optimized version
4. **Real capture session** — end-to-end timing on Pi

---

## 7. Rollback Plan

If optimizations cause quality issues:
1. Keep original `ai_focus_blender.py` in repo
2. Use `--use-original-blender` flag to revert if needed
3. Incremental rollback: disable optimizations one by one to isolate issue

---

**Next Step:** Implement Phase 1 optimizations in `ai_focus_blender_fast.py`, benchmark on workstation, then deploy to Pi.
