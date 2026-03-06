# Focus Blending Design Prompt for Local 14B LLM

You are helping design an AI-guided focus blending algorithm for a photogrammetry pipeline running on a Raspberry Pi 4/5.

## Context

**Problem:** Macro photography has very shallow depth-of-field. A single captured frame is sharp only in a narrow plane. To get full depth-of-field, we capture 7 frames at different focus distances, then blend them into a single all-in-focus image.

**Hardware:** Raspberry Pi 4/5 with local 14B LLM (Ollama), running alongside capture/demosaic tasks.

**Execution decision (fixed):** AI-guided focus blending runs **on the Raspberry Pi**.

**Input:** 7 demosaiced 16-bit RGB images of the same scene, each focused at a different depth:
```
frames[0]: focused near (closest)
frames[1]: focused slightly farther
...
frames[6]: focused far (farthest)
```

All images are 6944 × 9152 pixels (64MP), uint16 color depth.

## Task: Design AI-Guided Frame Blending

**Goal:** Create a blended image that is sharp everywhere by intelligently combining the 7 input frames.

**Constraints:**
- Must run on Pi (limited CPU/RAM)
- Should complete in <10 seconds per bracket (7 frames)
- Output should be comparable to Enfuse (which we'll run in parallel as baseline)
- Comparison metric: SSIM (Structural Similarity) + Laplacian variance against Enfuse output
- Must be robust for Hawkeye 64MP captures (9152x6944, 16-bit pipeline artifacts/noise patterns)

## Key Decisions to Make

1. **Sharpness Detection**
   - How to identify which pixels are sharpest in which frame?
   - Simple: Laplacian variance per pixel per frame
   - Better: Edge detection + gradient magnitude?
   - How to handle noise (don't pick noise as "sharp")?

2. **Blending Strategy**
   - Pixel-level: For each pixel, take the sharpest value across all 7 frames (seam finding needed)
   - Regional: Divide into regions, use the frame that's sharpest in that region
   - Gradual: Blend frames weighted by sharpness (soft transitions)
   - Which is computationally feasible on Pi?

3. **Seam Handling (if doing pixel-level blending)**
   - Naive approach: visible seams where we switch between frames
   - Better: feather/smooth transitions at seam boundaries
   - GraphCut or Poisson blending? (expensive but better quality)

4. **Edge Cases**
   - What if a pixel is blurry in ALL frames? (Accept it, use median value?)
   - What if one frame has motion artifact? (Outlier detection?)
   - How to handle occlusions or reflections that change with focus?

## Implementation Requirements

- **Language:** Python 3.8+
- **Libraries available:** OpenCV, NumPy, PIL (all already on Pi)
- **Input:** List of 7 file paths (16-bit TIFF files)
- **Output:** Single 16-bit TIFF file (same format as Enfuse output for fair comparison)
- **Metrics export:** JSON with:
  ```json
  {
    "method": "ai_guided",
    "sharpness_score": <float>,
    "processing_time_s": <float>,
    "pixel_quality": <percent of pixels at >0.7 laplacian variance>
  }
  ```

## Known Pipeline Facts (Do Not Re-Design These)

- Camera/lens behavior and focus stepping are already implemented and validated.
- Focus bracket size is 7 frames per pose.
- Enfuse baseline exists and is used for A/B comparison.
- Current architecture work has introduced raw-on-Pi capture for bandwidth savings; however, this task specifically targets **on-Pi AI blending**.
- Assume input to this module is already available as 7 demosaiced 16-bit TIFF frames for one pose.
- Runtime implementation decision: use deterministic Python/OpenCV blending on Pi; do not depend on LLM inference in the blending hot path.

## Design Decisions (Answered)

### 1. Runtime Budget Split

**Total budget: <10 seconds per bracket (7 frames)**

Allocate as follows:
- **Sharpness detection (2–3s):** Compute Laplacian or edge confidence maps for all 7 frames
- **Blending & selection (3–4s):** Perform per-pixel or regional selection logic
- **Seam smoothing / feathering (1–2s):** Smooth transitions at seam boundaries
- **Metric calculation & I/O (1s):** Compute SSIM, Laplacian variance, export JSON

**Implication:** Use vectorized NumPy operations wherever possible. Avoid expensive algorithms like GraphCut or iterative refinement. Prefer simple feathering over Poisson blending.

### 2. Memory Ceiling

**Target: Stay under 2.5 GB RSS during AI blend.**

Rationale:
- Each uint16 frame: 6944 × 9152 × 2 bytes ≈ 127 MB
- 7 frames in memory: ~890 MB raw data
- Working memory (sharpness maps, indices, blended result): ~1.6–2.0 GB
- Leave headroom for capture/demosaic/Enfuse running in parallel

**Implication:**
- Load all 7 frames into memory (fits comfortably)
- Keep sharpness maps at full resolution (compute once, reuse)
- Do NOT use temporary copies or redundant arrays; vectorize in-place
- If memory pressure rises above 2.5 GB, fallback to Enfuse for that pose

### 3. Registration Policy (Focus Breathing)

**Assumption: Frames are already pixel-aligned.**

Rationale:
- Macro lens focus breathing is typically <2% magnification shift
- Capture is done with static camera on tripod
- Any residual misalignment is negligible at 64MP resolution
- Adding optical flow would exceed runtime budget

**Implication:**
- Do NOT add frame registration or optical flow step
- If visual inspection reveals misalignment artifacts, log it and auto-fallback to Enfuse
- Future versions may refine this if motion artifacts are observed

### 4. Winner Selection Rule (AI vs Enfuse)

**Comparison and tiebreaker logic:**

1. **Both methods computed:** Run AI-guided blend and Enfuse in parallel
2. **Compare SSIM:**
   - If AI SSIM > 0.85 AND Enfuse SSIM ≤ 0.80 → **AI wins**
   - If Enfuse SSIM > 0.85 AND AI SSIM ≤ 0.80 → **Enfuse wins**
   - If both SSIM > 0.85 or both SSIM ≤ 0.80 → Proceed to tiebreaker

3. **Tiebreaker (when SSIM scores are close):**
   - Compare Laplacian variance (sharpness): Winner is method with higher score
   - If Laplacian scores are within **5% of each other** → **Prefer Enfuse** (proven baseline)

4. **Artifact inspection:**
   - If AI shows visible haloing, color shifts, or seams → **Enfuse wins**
   - Use edge detection on difference map to detect localized artifacts

**Export decision in metrics JSON:**
```json
{
  "method": "ai_guided",                    // or "enfuse"
  "winner_reason": "higher_ssim",           // or "higher_sharpness", "enfuse_tiebreak", "fallback"
  "ssim_ai": 0.87,
  "ssim_enfuse": 0.82,
  "laplacian_ai": 1250.5,
  "laplacian_enfuse": 1190.3,
  "processing_time_s": 8.2,
  "decision_log": "AI exceeded 0.85 SSIM threshold"
}
```

### 5. Failure Fallback Behavior

**If AI-guided blend fails at any stage:**

1. **Quality check fails** (SSIM < 0.70 or Laplacian < baseline): Auto-fallback to Enfuse
2. **Runtime exceeds 10s:** Interrupt AI blend, use Enfuse result, log timeout
3. **Memory exceeds 2.5 GB:** Abort AI blend, use Enfuse, log memory event
4. **Crash or exception:** Catch, log error, use Enfuse (pipeline must not stall)

**Behavior:**
- Fallback is **automatic and silent** (no user intervention)
- Log all fallbacks with reason to session log
- Continue pipeline with Enfuse result
- Accumulate fallback statistics for debugging

### 6. Output Artifact Policy

**Artifact suppression weighted equally with sharpness.**

Artifact penalties:
- **Haloing (color fringing at edges):** Flag if edge contrast exceeds 15% between method results
- **Visible seams:** Flag if spatial discontinuity exceeds Laplacian variance by >10% locally
- **Color shifts:** Flag if RGB channel variances differ by >5% between methods
- **Noise amplification:** Flag if high-frequency content increased >20% vs input frames

**Decision rule:**
- If SSIM comparison shows AI ≥ Enfuse, use AI (SSIM implicitly penalizes artifacts)
- If SSIM is close and artifact inspection flags AI: defer to Enfuse
- Prioritize smooth, artifact-free output over marginal sharpness gains

**Success metric:** Output should appear **artifact-free and all-in-focus**, even if sharpness is 5% lower than the maximum possible.

---

## Expected Use in Pipeline

```
Pi: capture_bracket(7 frames)
  ├─ prepare 7 demosaiced 16-bit frames for current pose
  ├─ [parallel] run Enfuse on demosaiced
  ├─ [parallel] run AI-guided blending on demosaiced (THIS TASK)
  └─ compare metrics → select winner for this pose

Send: [winning_blend_image + metrics + metadata] to workstation
Workstation: downstream processing
```

For this design task, focus on the **AI-guided blending branch** and the winner-selection logic.

## What I Need From You

Design and explain:

1. **Sharpness detection method** — how to identify sharp pixels in each frame
   - Recommend specific kernel/algorithm
   - Estimate computation cost per frame

2. **Blending strategy** — how to combine the 7 frames
   - Step-by-step algorithm
   - Pseudocode or logic flow
   - Why this approach is better than alternatives

3. **Seam handling** — how to avoid visible transitions
   - Specific technique (feathering, GraphCut, etc.)
   - Implementation approach

4. **Edge case handling** — what to do when things go wrong
   - Blurry pixels
   - Motion artifacts
   - Occlusions

5. **Python module structure** — how to organize the code
   - Function signatures
   - Which operations to vectorize (NumPy) vs loop

6. **Metrics & comparison** — how to score against Enfuse
   - Which metrics to compute
   - How to select winner

## Success Criteria

- Output image is visually all-in-focus (no unsharp regions)
- SSIM score >0.85 compared to Enfuse (or better)
- Processing time <10 seconds per bracket on Pi
- Handles edge cases gracefully (no crashes)
- Implementation is realistic for Pi RAM/CPU limits at 64MP

---

Please provide a detailed design document addressing all the points above. Include concrete examples and pseudocode where helpful.
