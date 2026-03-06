# Phase C Session Log — On-Pi Focus Blending Integration
**Date:** 2026-03-06
**Status:** ✅ INTEGRATED (performance tuning still open)
**Scope:** Implement and validate AI-guided focus blending integration in the live Pi capture flow.

---

## Decision Point (Recorded)

The term "AI-guided" in this phase refers to a **deterministic computer-vision blending pipeline** implemented in Python/OpenCV.

- **No LLM inference is used in the runtime blending path** on the Pi.
- Ollama/LLM usage in this project remains for planning/design assistance only.
- Runtime blending execution is performed by:
  - `~/photoscan/src/ai_focus_blender.py`
  - `~/photoscan/src/demosaic.py`
  - `~/photoscan/src/focus_bracket_driver.py` (integration entrypoint)

Rationale:
- 64MP frame sizes make model inference on Pi impractical for current timing goals.
- Deterministic CV path is easier to profile, debug, and harden for unattended capture sessions.

---

## Work Completed This Session

### 1) Implemented AI blending module
**Workstation source:** `~/OpenScan3/photogrammetry/ai_focus_blender.py`

Implemented:
- Sharpness-map based focus selection (Laplacian-based)
- Seam handling (soft-mask mode)
- Winner selection vs Enfuse baseline (when enabled)
- Fallback policy and metrics export JSON
- Batch and single-bracket CLI modes

### 2) Integrated into live capture driver
**File:** `~/OpenScan3/photogrammetry/focus_bracket_driver.py`

Added capture-time options:
- `--ai-blend`
- `--ai-no-enfuse`
- `--corrected-dir`
- `--blended-output-dir`
- `--metrics-dir`

Behavior:
- Capture raw bracket on Pi
- Demosaic bracket
- Run AI blending automatically
- Write blended output + metrics per pose

### 3) Live Pi validation and bug fixes
Validated against live device `photoscan-pi.local`.

Findings and fixes:
- Initial run hit OOM during full-memory blend path.
- Added non-stalling fallback path so pipeline always outputs a result.
- Added low-memory blend mode (`low_memory_hardmask`) that avoids loading all 64MP frames simultaneously.
- Updated memory policy:
  - 2.5 GB is now treated as a target warning level
  - hard fail uses higher configurable ceiling (`AI_BLEND_HARD_MEMORY_MB`, default 5000)

---

## Live Test Outcomes

### End-to-end capture run (`--capture --ai-blend --ai-no-enfuse`)
- Raw capture: ✅
- Demosaic: ✅
- Blend integration call path: ✅
- First attempt: ❌ failed due OOM in early blend version
- After low-memory patch: ✅ produced output + metrics

Output produced on Pi:
- `~/scan/inbox/stacked/scan_az000.00_el000.00_ai_selected.tif`
- `~/scan/inbox/stacked/metrics/scan_az000.00_el000.00_ai_metrics.json`

Most recent metrics indicated:
- Blend mode: `low_memory_hardmask`
- Peak RSS roughly ~1.1 GB (no OOM)
- Runtime still high (~93s on tested 64MP bracket), exceeding `<10s` target

---

## Current Project State (as of 2026-03-06)

Implemented and working:
- Raw-on-Pi capture + sidecars
- Workstation/Pi demosaic utility
- Enfuse baseline path
- On-Pi deterministic focus blending integration with safe fallback

Not yet meeting target:
- `<10s` full 64MP on-Pi blend runtime

Open performance work:
1. Tune low-memory path (tile-wise processing, vectorized compositing, reduced passes)
2. Optional multi-resolution strategy (preview focus map + full-res compose refinements)
3. Re-test with Enfuse comparison enabled under real capture load

---

## Files Added/Updated This Session

Workstation repo files:
- `~/OpenScan3/photogrammetry/ai_focus_blender.py` (new)
- `~/OpenScan3/photogrammetry/focus_bracket_driver.py` (updated)
- `~/OpenScan3/photogrammetry/llm_prompts/focus_blending_prompt.md` (updated earlier in session)
- `~/OpenScan3/photogrammetry/phase_c_session_log.md` (this file)

Pi deployed files:
- `~/photoscan/src/ai_focus_blender.py`
- `~/photoscan/src/focus_bracket_driver.py`
- `~/photoscan/src/demosaic.py`
- `~/photoscan/src/focus_stacker.py`

---

## Safe Shutdown Note

No long-running process from this session needs to remain attached to this terminal session.
It is safe to end/kill this chat session.
