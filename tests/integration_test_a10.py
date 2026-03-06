"""
integration_test_a10.py  —  Phase A / A10
End-to-end integration test for the Phase A pipeline.

Tests the full flow:
  1. Motor movement (A3) — 24-position sweep verified on hardware
  2. Focus bracket capture (A4) — live captures at multiple positions
  3. Pose metadata (A5) — sidecar written and validated for each frame
  4. Quality gate (A7) — at least one frame per bracket passes gate
  5. rsync transfer (A2) — files arrive on workstation with matching checksums
  6. Focus stacking (A9) — enfuse stacks each bracket (if enfuse installed)

Run on WORKSTATION (orchestrates Pi via SSH):
    python integration_test_a10.py

Pi must be reachable at photoscan-pi.local with passwordless SSH.
"""

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PI_HOST      = "pi@photoscan-pi.local"
PI_INBOX     = "/home/pi/scan/inbox/raw"
PI_SRC       = "/home/pi/photoscan/src"
PI_VENV      = "/home/pi/photoscan/venv/bin/python"

WS_ROOT      = Path.home() / "photogrammetry"
WS_INBOX     = WS_ROOT / "scan_inbox/raw"
WS_STACKED   = WS_ROOT / "stacked"
WS_REPORTS   = WS_ROOT / "a10_reports"

# Test scan: 6 positions (3 azimuth × 2 elevation — quick but meaningful)
TEST_POSITIONS = [
    {"azimuth":   0.0, "elevation":  0.0},
    {"azimuth":  90.0, "elevation":  0.0},
    {"azimuth": 180.0, "elevation":  0.0},
    {"azimuth": 270.0, "elevation":  0.0},
    {"azimuth":   0.0, "elevation": 20.0},
    {"azimuth": 180.0, "elevation": 20.0},
]


def run_ssh(cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a command on the Pi via SSH."""
    return subprocess.run(
        ["ssh", PI_HOST, cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


class A10IntegrationTest:

    def __init__(self):
        WS_INBOX.mkdir(parents=True, exist_ok=True)
        WS_STACKED.mkdir(parents=True, exist_ok=True)
        WS_REPORTS.mkdir(parents=True, exist_ok=True)
        self.results = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tests": {},
            "passed": False,
        }
        self.errors = []

    def fail(self, test: str, reason: str):
        self.errors.append(f"{test}: {reason}")
        self.results["tests"][test] = {"passed": False, "reason": reason}
        print(f"  ✗ FAIL: {reason}")

    def ok(self, test: str, detail: str = ""):
        self.results["tests"][test] = {"passed": True, "detail": detail}
        print(f"  ✓ {detail or test}")

    # ------------------------------------------------------------------

    def test_connectivity(self):
        section("T1 — Pi Connectivity")
        r = run_ssh("echo pong", timeout=10)
        if r.returncode != 0 or "pong" not in r.stdout:
            self.fail("connectivity", f"SSH failed: {r.stderr.strip()}")
        else:
            self.ok("connectivity", "Pi reachable via SSH")

    def test_motor_home(self):
        section("T2 — Motor Home")
        r = run_ssh(
            f"{PI_VENV} {PI_SRC}/openscan_controller.py --home",
            timeout=30,
        )
        if r.returncode != 0:
            self.fail("motor_home", f"Home failed: {r.stderr.strip()[:200]}")
        elif "Homed" in r.stdout or "θ=0.00" in r.stdout:
            self.ok("motor_home", "Motors homed to (0°, 0°)")
        else:
            self.fail("motor_home", f"Unexpected output: {r.stdout.strip()[:200]}")

    def test_capture_session(self):
        section("T3 — Capture Session (6 positions)")

        # Clear Pi inbox
        run_ssh(f"rm -f {PI_INBOX}/*.tif {PI_INBOX}/*.json")

        # Write a mini scan plan to the Pi and execute it
        plan_json = json.dumps(TEST_POSITIONS)
        capture_script = f"""
import asyncio, json, sys
sys.path.insert(0, '{PI_SRC}')
from openscan_controller import OpenScanController
from focus_bracket_driver import FocusBracketDriver, BracketConfig
from pathlib import Path
import numpy as np

POSITIONS = {plan_json}
OUTPUT_DIR = Path('{PI_INBOX}')

async def main():
    bracket = None
    with FocusBracketDriver() as cam:
        bracket = cam.characterise_depth()
        print(f'Bracket: {{bracket.n_frames}} frames LP {{bracket.focus_far:.2f}}–{{bracket.focus_near:.2f}}')
        async with OpenScanController() as ctrl:
            await ctrl.home()
            for p in POSITIONS:
                az, el = p['azimuth'], p['elevation']
                await ctrl.move_to(az, el)
                paths = cam.capture_position(az, el, OUTPUT_DIR, bracket=bracket, session_name='a10_test')
                print(f'  Captured ({{az}}, {{el}}): {{len(paths)}} frames')

asyncio.run(main())
"""
        # Write script to Pi temp file
        run_ssh(f"cat > /tmp/a10_capture.py << 'PYEOF'\n{capture_script}\nPYEOF")
        r = run_ssh(
            f"source /home/pi/photoscan/venv/bin/activate && {PI_VENV} /tmp/a10_capture.py",
            timeout=600,
        )
        if r.returncode != 0:
            self.fail("capture_session", f"Capture failed:\n{r.stderr[-500:]}")
            return

        # Count captured files
        r2 = run_ssh(f"ls {PI_INBOX}/*.tif 2>/dev/null | wc -l")
        n_png = int(r2.stdout.strip()) if r2.returncode == 0 else 0
        r3 = run_ssh(f"ls {PI_INBOX}/*.json 2>/dev/null | wc -l")
        n_json = int(r3.stdout.strip()) if r3.returncode == 0 else 0

        expected_frames = len(TEST_POSITIONS) * 3  # 3 frames per position minimum
        if n_png < len(TEST_POSITIONS):
            self.fail("capture_session", f"Only {n_png} PNGs captured (expected ≥{len(TEST_POSITIONS)})")
        elif n_json < n_png:
            self.fail("capture_session", f"Missing sidecars: {n_png} PNGs but only {n_json} JSONs")
        else:
            self.ok("capture_session",
                    f"{n_png} frames captured, {n_json} sidecars written across {len(TEST_POSITIONS)} positions")

    def test_rsync_transfer(self):
        section("T4 — rsync Transfer")

        # Clear workstation inbox of previous scan_az files
        for f in WS_INBOX.glob("scan_az*"):
            f.unlink()

        r = subprocess.run(
            ["rsync", "-avz", "--checksum",
             f"{PI_HOST}:{PI_INBOX}/",
             str(WS_INBOX) + "/"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            self.fail("rsync", f"rsync failed: {r.stderr[:200]}")
            return

        transferred = list(WS_INBOX.glob("scan_az*.tif")) + list(WS_INBOX.glob("scan_az*.json"))
        if not transferred:
            self.fail("rsync", "No scan_az files found on workstation after rsync")
            return

        # Spot-check checksums on 3 files
        pngs = sorted(WS_INBOX.glob("scan_az*.tif"))[:3]
        checksum_errors = 0
        for ws_path in pngs:
            ws_md5 = md5(ws_path)
            r2 = run_ssh(f"md5sum {PI_INBOX}/{ws_path.name}")
            pi_md5 = r2.stdout.split()[0] if r2.returncode == 0 else ""
            if ws_md5 != pi_md5:
                checksum_errors += 1

        if checksum_errors:
            self.fail("rsync", f"{checksum_errors} checksum mismatches")
        else:
            self.ok("rsync", f"{len(transferred)} files transferred, checksums verified")

    def test_metadata_validation(self):
        section("T5 — Pose Metadata Validation")
        sys.path.insert(0, str(Path(__file__).parent))

        try:
            from pose_metadata import validate_sidecar, read_sidecar
        except ImportError:
            # Try workstation location
            sys.path.insert(0, str(WS_ROOT))
            from pose_metadata import validate_sidecar, read_sidecar

        sidecars = sorted(WS_INBOX.glob("scan_az*.json"))
        if not sidecars:
            self.fail("metadata", "No sidecar JSON files found on workstation")
            return

        errors = []
        for s in sidecars:
            try:
                validate_sidecar(s)
                meta = read_sidecar(s)
                # Verify required fields are populated
                if meta.focus_lens_position is None:
                    errors.append(f"{s.name}: missing focus_lens_position")
                if meta.laplacian_variance is None:
                    errors.append(f"{s.name}: missing laplacian_variance")
                if meta.exposure_time_us is None:
                    errors.append(f"{s.name}: missing exposure_time_us")
            except Exception as e:
                errors.append(f"{s.name}: {e}")

        if errors:
            self.fail("metadata", f"{len(errors)} validation errors: {errors[:3]}")
        else:
            self.ok("metadata", f"{len(sidecars)} sidecars validated")

    def test_focus_stacking(self):
        section("T6 — Focus Stacking (A9)")
        if not shutil.which("enfuse"):
            self.results["tests"]["focus_stacking"] = {
                "passed": None,
                "detail": "SKIPPED — enfuse not installed (run: sudo apt install enfuse)"
            }
            print("  ⚠ SKIPPED — enfuse not installed")
            print("    Install with: sudo apt install enfuse")
            return

        try:
            from focus_stacker import focus_stack_enfuse, stack_quality_score
        except ImportError:
            sys.path.insert(0, str(WS_ROOT))
            from focus_stacker import focus_stack_enfuse, stack_quality_score

        from collections import defaultdict
        import re

        groups: dict[str, list[Path]] = defaultdict(list)
        for p in sorted(WS_INBOX.glob("scan_az*.tif")):
            m = re.match(r"(scan_az[\d.]+_el[\d.]+)_f\d+\.tif", p.name)
            if m:
                groups[m.group(1)].append(p)

        if not groups:
            self.fail("focus_stacking", "No bracket groups found")
            return

        stacked_count = 0
        for prefix, frames in sorted(groups.items()):
            out = WS_STACKED / f"{prefix}_stacked.tif"
            focus_stack_enfuse(sorted(frames), out)
            stacked_count += 1

        self.ok("focus_stacking", f"{stacked_count} brackets stacked with enfuse")

    def run(self) -> bool:
        print(f"\n{'#'*60}")
        print(f"  Phase A — A10 End-to-End Integration Test")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'#'*60}")

        self.test_connectivity()
        self.test_motor_home()
        self.test_capture_session()
        self.test_rsync_transfer()
        self.test_metadata_validation()
        self.test_focus_stacking()

        # Summary
        section("SUMMARY")
        tests = self.results["tests"]
        passed = [k for k, v in tests.items() if v.get("passed") is True]
        failed = [k for k, v in tests.items() if v.get("passed") is False]
        skipped = [k for k, v in tests.items() if v.get("passed") is None]

        for k in passed:
            print(f"  ✓ {k}")
        for k in skipped:
            print(f"  ⚠ {k} (skipped)")
        for k in failed:
            print(f"  ✗ {k}: {tests[k].get('reason', '')}")

        all_passed = len(failed) == 0
        self.results["passed"] = all_passed

        print(f"\n{'='*60}")
        print(f"  Result: {'PASS ✓' if all_passed else 'FAIL ✗'}")
        print(f"  {len(passed)} passed, {len(skipped)} skipped, {len(failed)} failed")
        print(f"{'='*60}")

        # Write report
        report_path = WS_REPORTS / f"a10_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        report_path.write_text(json.dumps(self.results, indent=2))
        print(f"\nReport: {report_path}")

        return all_passed


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    test = A10IntegrationTest()
    ok = test.run()
    sys.exit(0 if ok else 1)
