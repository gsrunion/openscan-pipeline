"""
colmap_reconstruct.py — COLMAP SfM with turntable pose priors.

Takes a directory of images captured on a turntable (with azimuth/elevation
known from filenames or JSON sidecars) and runs a full COLMAP sparse
reconstruction, bootstrapped with pose priors from the turntable geometry.

The pose priors solve the "no good initial pair" failure that COLMAP hits
with sequential 45° turntable rotations: we explicitly select the initial
pair with maximum angular separation and seed the database with approximate
camera positions so the mapper has the geometric context it needs.

Usage:
    python colmap_reconstruct.py --images /path/to/images --output /path/to/output

    # With explicit radius:
    python colmap_reconstruct.py --images /path/to/images --output /path/to/output --radius-mm 185

    # Skip OpenMVS (sparse only):
    python colmap_reconstruct.py --images /path/to/images --output /path/to/output --sparse-only
"""

import argparse
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Turntable geometry
# ---------------------------------------------------------------------------

DEFAULT_RADIUS_MM = 200.0   # placeholder — COLMAP will determine actual scale
DEFAULT_CALIBRATION_PATH = Path("data/calibration/calibration.json")


def camera_pose(azimuth_deg: float, elevation_deg: float, radius: float = 1.0):
    """
    Compute camera center C and world-to-camera rotation R for a turntable position.

    Coordinate system:
        - World origin: centre of subject on turntable
        - World Y: up (vertical)
        - World X: toward camera at az=0, el=0

    Returns:
        C: (3,) camera centre in world coordinates
        R: (3,3) world-to-camera rotation matrix (COLMAP convention)
            Camera axes: X right, Y down, Z forward
    """
    theta = np.radians(azimuth_deg)
    phi = np.radians(elevation_deg)

    C = radius * np.array([
        np.cos(phi) * np.cos(theta),
        np.sin(phi),
        np.cos(phi) * np.sin(theta),
    ])

    # Camera Z (forward) points toward origin
    z = -C / np.linalg.norm(C)

    # Avoid singularity when looking straight up/down
    world_up = np.array([0., 1., 0.])
    if abs(np.dot(z, world_up)) > 0.99:
        world_up = np.array([1., 0., 0.])

    # Camera X (right) and Y (down, COLMAP convention)
    x = np.cross(world_up, z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)

    R = np.stack([x, y, z], axis=0)   # rows = camera axes in world frame
    return C, R


def rotation_to_quaternion(R: np.ndarray) -> np.ndarray:
    """Convert 3×3 rotation matrix to quaternion (w, x, y, z)."""
    trace = R[0, 0] + R[1, 1] + R[2, 2]
    if trace > 0:
        s = 0.5 / np.sqrt(trace + 1.0)
        w = 0.25 / s
        x = (R[2, 1] - R[1, 2]) * s
        y = (R[0, 2] - R[2, 0]) * s
        z = (R[1, 0] - R[0, 1]) * s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    return np.array([w, x, y, z])


# ---------------------------------------------------------------------------
# Calibration parsing
# ---------------------------------------------------------------------------

def load_colmap_camera_from_calibration(calibration_path: Path) -> tuple[str, str]:
    """
    Convert the OpenCV calibration JSON into a COLMAP camera model + params string.

    The calibration produced by calibrate.py uses OpenCV's distortion ordering:
        [k1, k2, p1, p2, k3, ...]

    COLMAP's OPENCV camera model expects:
        fx, fy, cx, cy, k1, k2, p1, p2
    """
    data = json.loads(calibration_path.read_text())
    K = np.asarray(data["camera_matrix"], dtype=float)
    dist = np.asarray(data["dist_coeffs"], dtype=float).reshape(-1)

    fx = float(K[0, 0])
    fy = float(K[1, 1])
    cx = float(K[0, 2])
    cy = float(K[1, 2])

    coeffs = np.zeros(4, dtype=float)
    coeffs[:min(4, len(dist))] = dist[:4]
    if len(dist) > 4 and np.any(np.abs(dist[4:]) > 1e-9):
        logger.warning(
            "Calibration has %d distortion coefficients; COLMAP OPENCV will use the first 4",
            len(dist),
        )

    params = [fx, fy, cx, cy, *coeffs.tolist()]
    params_str = ",".join(f"{value:.10g}" for value in params)
    return "OPENCV", params_str


# ---------------------------------------------------------------------------
# Pose parsing from filename / sidecar
# ---------------------------------------------------------------------------

_FILENAME_RE = re.compile(r"az(-?\d+(?:\.\d+)?)_el(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def parse_pose_from_name(name: str) -> Optional[tuple[float, float]]:
    """Extract (azimuth_deg, elevation_deg) from filename. Returns None if not found."""
    m = _FILENAME_RE.search(name)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def parse_pose_from_sidecar(image_path: Path) -> Optional[tuple[float, float]]:
    """Read az/el from a JSON sidecar alongside the image."""
    sidecar = image_path.with_suffix(".json")
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text())
        return float(data["azimuth_deg"]), float(data["elevation_deg"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return None


def get_image_poses(image_dir: Path) -> dict[str, tuple[float, float]]:
    """
    Return {image_filename: (azimuth_deg, elevation_deg)} for all images.
    Tries sidecar first, then filename pattern.
    """
    poses = {}
    for p in sorted(image_dir.glob("*.jpg")) + sorted(image_dir.glob("*.jpeg")) + \
             sorted(image_dir.glob("*.png")) + sorted(image_dir.glob("*.tif")):
        pose = parse_pose_from_sidecar(p) or parse_pose_from_name(p.stem)
        if pose:
            poses[p.name] = pose
        else:
            logger.warning("No pose found for %s — skipping", p.name)
    return poses


# ---------------------------------------------------------------------------
# COLMAP database helpers
# ---------------------------------------------------------------------------

def inject_pose_priors(db_path: Path, image_poses: dict[str, tuple[float, float]],
                       radius: float = DEFAULT_RADIUS_MM):
    """
    Write turntable pose priors into the COLMAP database.
    image_poses: {image_name: (azimuth_deg, elevation_deg)}
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    updated = 0
    for image_name, (az, el) in image_poses.items():
        cur.execute("SELECT image_id FROM images WHERE name = ?", (image_name,))
        row = cur.fetchone()
        if row is None:
            logger.warning("Image not in DB: %s", image_name)
            continue

        image_id = row[0]
        C, R = camera_pose(az, el, radius)
        q = rotation_to_quaternion(R)    # (w, x, y, z)
        t = -R @ C                        # world-to-camera translation

        cur.execute("""
            UPDATE images
            SET prior_qw=?, prior_qx=?, prior_qy=?, prior_qz=?,
                prior_tx=?, prior_ty=?, prior_tz=?
            WHERE image_id=?
        """, (float(q[0]), float(q[1]), float(q[2]), float(q[3]),
              float(t[0]), float(t[1]), float(t[2]),
              image_id))
        updated += 1

    conn.commit()
    conn.close()
    logger.info("Injected pose priors for %d/%d images", updated, len(image_poses))


def get_image_ids(db_path: Path) -> dict[str, int]:
    """Return {image_name: image_id} from the COLMAP database."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT name, image_id FROM images")
    result = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()
    return result


def ranked_initial_pairs(
    image_poses: dict[str, tuple[float, float]],
    image_ids: dict[str, int],
    target_angles: tuple[float, ...] = (45.0, 60.0, 75.0, 90.0),
    limit: int = 12,
) -> list[tuple[int, int]]:
    """
    Rank candidate initial image pairs by useful angular separation.

    For single-elevation turntable data, 180° pairs see opposite faces of
    the subject and only match background, giving near-zero triangulation
    angle despite many inliers. 45-90° pairs share object features and
    triangulate cleanly.
    """
    names = [n for n in image_poses if n in image_ids]
    if len(names) < 2:
        raise ValueError("Need at least 2 images with known poses and DB IDs")

    candidates: list[tuple[float, float, int, int]] = []

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            C_i, _ = camera_pose(*image_poses[names[i]])
            C_j, _ = camera_pose(*image_poses[names[j]])
            ci = C_i / np.linalg.norm(C_i)
            cj = C_j / np.linalg.norm(C_j)
            angle = np.degrees(np.arccos(np.clip(np.dot(ci, cj), -1, 1)))
            score = min(abs(angle - target) for target in target_angles)
            candidates.append((score, -angle, image_ids[names[i]], image_ids[names[j]]))

    candidates.sort()
    ranked = [(id1, id2) for _, _, id1, id2 in candidates[:limit]]
    logger.info("Candidate initial pairs: %s", ranked)
    return ranked


# ---------------------------------------------------------------------------
# COLMAP command wrappers
# ---------------------------------------------------------------------------

def run(cmd: list[str], description: str):
    """Run a subprocess, log output, raise on failure."""
    logger.info("Running: %s", description)
    logger.debug("Command: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        logger.debug(result.stdout[-2000:])
    if result.returncode != 0:
        logger.error("FAILED: %s\n%s", description, result.stderr[-1000:])
        raise RuntimeError(f"{description} failed (exit {result.returncode})")
    logger.info("Done: %s", description)


def feature_extraction(
    db_path: Path,
    image_path: Path,
    single_camera: bool = True,
    camera_model: str = "SIMPLE_RADIAL",
    camera_params: Optional[str] = None,
):
    cmd = [
        "colmap", "feature_extractor",
        "--database_path", str(db_path),
        "--image_path", str(image_path),
        "--ImageReader.camera_model", camera_model,
        "--ImageReader.single_camera", "1" if single_camera else "0",
        "--SiftExtraction.use_gpu", "1",
        "--SiftExtraction.max_image_size", "3200",
        "--SiftExtraction.max_num_features", "8192",
    ]
    if camera_params:
        cmd.extend(["--ImageReader.camera_params", camera_params])
    run(cmd, "Feature extraction")


def exhaustive_matching(db_path: Path):
    run([
        "colmap", "exhaustive_matcher",
        "--database_path", str(db_path),
        "--SiftMatching.use_gpu", "1",
        "--SiftMatching.max_ratio", "0.9",
        "--SiftMatching.max_distance", "0.9",
        "--TwoViewGeometry.min_num_inliers", "5",
        "--TwoViewGeometry.min_inlier_ratio", "0.1",
    ], "Exhaustive matching")


def run_mapper(
    db_path: Path,
    image_path: Path,
    output_path: Path,
    init_id1: int,
    init_id2: int,
    init_min_num_inliers: int,
    init_min_tri_angle: float,
    abs_pose_min_num_inliers: int,
) -> bool:
    if output_path.exists():
        shutil.rmtree(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    cmd = [
        "colmap", "mapper",
        "--database_path", str(db_path),
        "--image_path", str(image_path),
        "--output_path", str(output_path),
        "--Mapper.init_image_id1", str(init_id1),
        "--Mapper.init_image_id2", str(init_id2),
        "--Mapper.abs_pose_min_num_inliers", str(abs_pose_min_num_inliers),
        "--Mapper.abs_pose_min_inlier_ratio", "0.1",
        "--Mapper.init_min_num_inliers", str(init_min_num_inliers),
        "--Mapper.init_min_tri_angle", str(init_min_tri_angle),
        "--Mapper.filter_min_tri_angle", "0.5",
        "--Mapper.ba_global_max_num_iterations", "50",
    ]
    logger.info(
        "Running: Sparse reconstruction (mapper), pair=(%d,%d), init_inliers=%d, tri_angle=%.1f",
        init_id1, init_id2, init_min_num_inliers, init_min_tri_angle,
    )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        logger.info("Done: Sparse reconstruction (mapper)")
        return True
    logger.warning("Mapper failed for pair (%d,%d): %s", init_id1, init_id2, result.stderr[-1000:])
    return False


def bundle_adjustment(input_path: Path, output_path: Path, image_path: Path,
                      refine_intrinsics: bool = True):
    output_path.mkdir(parents=True, exist_ok=True)
    run([
        "colmap", "bundle_adjuster",
        "--input_path", str(input_path),
        "--output_path", str(output_path),
        "--BundleAdjustment.refine_focal_length", "1" if refine_intrinsics else "0",
        "--BundleAdjustment.refine_extra_params", "1" if refine_intrinsics else "0",
    ], "Bundle adjustment")


def convert_model(input_path: Path, output_path: Path):
    """Convert binary COLMAP model to text for inspection."""
    output_path.mkdir(parents=True, exist_ok=True)
    run([
        "colmap", "model_converter",
        "--input_path", str(input_path),
        "--output_path", str(output_path),
        "--output_type", "TXT",
    ], "Model conversion to text")


# ---------------------------------------------------------------------------
# Sparse model stats
# ---------------------------------------------------------------------------

def read_model_stats(sparse_dir: Path) -> dict:
    """Read registered images and points from a text-format sparse model."""
    stats = {"registered_images": 0, "points3d": 0}
    cameras_txt = sparse_dir / "cameras.txt"
    images_txt = sparse_dir / "images.txt"
    points_txt = sparse_dir / "points3D.txt"

    if images_txt.exists():
        lines = [l for l in images_txt.read_text().splitlines()
                 if l and not l.startswith("#")]
        # Every other line is an image (odd lines are keypoint lines)
        stats["registered_images"] = len(lines) // 2

    if points_txt.exists():
        lines = [l for l in points_txt.read_text().splitlines()
                 if l and not l.startswith("#")]
        stats["points3d"] = len(lines)

    return stats


# ---------------------------------------------------------------------------
# Main reconstruction pipeline
# ---------------------------------------------------------------------------

def reconstruct(image_dir: Path, output_dir: Path, radius_mm: float,
                sparse_only: bool = False,
                calibration_path: Optional[Path] = None,
                refine_intrinsics: bool = False):
    image_dir = image_dir.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    db_path = output_dir / "database.db"
    sparse_dir = output_dir / "sparse"
    sparse_ba_dir = output_dir / "sparse_ba"
    sparse_txt_dir = output_dir / "sparse_txt"

    # Parse poses from images
    image_poses = get_image_poses(image_dir)
    if not image_poses:
        raise RuntimeError(f"No images with parseable poses found in {image_dir}")
    logger.info("Found %d images with poses", len(image_poses))

    camera_model = "SIMPLE_RADIAL"
    camera_params = None
    if calibration_path is not None:
        calibration_path = calibration_path.resolve()
        camera_model, camera_params = load_colmap_camera_from_calibration(calibration_path)
        logger.info("Using calibration from %s", calibration_path)
        logger.info("COLMAP camera model: %s", camera_model)
    else:
        logger.info("No calibration supplied; using COLMAP self-calibration (%s)", camera_model)

    # 1. Feature extraction
    if db_path.exists():
        logger.info("Removing existing database")
        db_path.unlink()
    feature_extraction(
        db_path,
        image_dir,
        camera_model=camera_model,
        camera_params=camera_params,
    )

    # 2. Exhaustive matching (relaxed for turntable geometry)
    exhaustive_matching(db_path)

    # 3. Inject turntable pose priors
    image_ids = get_image_ids(db_path)
    inject_pose_priors(db_path, image_poses, radius=radius_mm)

    # 4. Try several initial pairs with progressively looser startup constraints
    init_pairs = ranked_initial_pairs(image_poses, image_ids)
    mapper_succeeded = False
    mapper_attempts = [
        {"init_min_num_inliers": 10, "init_min_tri_angle": 2.0, "abs_pose_min_num_inliers": 10},
        {"init_min_num_inliers": 8, "init_min_tri_angle": 1.0, "abs_pose_min_num_inliers": 8},
        {"init_min_num_inliers": 6, "init_min_tri_angle": 0.5, "abs_pose_min_num_inliers": 6},
    ]
    for id1, id2 in init_pairs:
        for attempt in mapper_attempts:
            ok = run_mapper(
                db_path,
                image_dir,
                sparse_dir,
                id1,
                id2,
                init_min_num_inliers=attempt["init_min_num_inliers"],
                init_min_tri_angle=attempt["init_min_tri_angle"],
                abs_pose_min_num_inliers=attempt["abs_pose_min_num_inliers"],
            )
            if ok:
                mapper_succeeded = True
                break
        if mapper_succeeded:
            break

    # Find the best model (most registered images)
    if not mapper_succeeded:
        raise RuntimeError("Mapper produced no models — reconstruction failed")
    models = sorted(sparse_dir.iterdir()) if sparse_dir.exists() else []
    if not models:
        raise RuntimeError("Mapper produced no models — reconstruction failed")

    best_model = max(models, key=lambda m: len(list(m.glob("images.bin"))))
    logger.info("Best model: %s", best_model)

    # 6. Bundle adjustment
    bundle_adjustment(best_model, sparse_ba_dir, image_dir, refine_intrinsics=refine_intrinsics)

    # 7. Convert to text for inspection
    convert_model(sparse_ba_dir, sparse_txt_dir)

    stats = read_model_stats(sparse_txt_dir)
    logger.info("")
    logger.info("=" * 50)
    logger.info("RECONSTRUCTION COMPLETE")
    logger.info("  Registered images: %d / %d", stats["registered_images"], len(image_poses))
    logger.info("  3D points:         %d", stats["points3d"])
    logger.info("  Sparse model:      %s", sparse_ba_dir)
    logger.info("=" * 50)

    if stats["registered_images"] == 0:
        logger.error("No images were registered — reconstruction failed.")
        return stats

    if not sparse_only:
        _run_openmvs(output_dir, sparse_ba_dir, image_dir)

    return stats


def _run_openmvs(output_dir: Path, sparse_dir: Path, image_dir: Path):
    """Run OpenMVS dense reconstruction if available."""
    if not shutil.which("OpenMVS") and not shutil.which("DensifyPointCloud"):
        logger.warning("OpenMVS not found — skipping dense reconstruction")
        logger.info("Install OpenMVS to continue: sudo apt install openmvs")
        return

    mvs_dir = output_dir / "mvs"
    mvs_dir.mkdir(exist_ok=True)

    # Convert COLMAP sparse model to OpenMVS format
    colmap2mvs = shutil.which("InterfaceCOLMAP") or shutil.which("colmap2openmvs")
    if not colmap2mvs:
        logger.warning("InterfaceCOLMAP not found — cannot convert to OpenMVS")
        return

    scene_mvs = mvs_dir / "scene.mvs"
    run([
        colmap2mvs,
        "--input-file", str(sparse_dir),
        "--image-folder", str(image_dir),
        "--output-file", str(scene_mvs),
    ], "Convert COLMAP → OpenMVS")

    densify = shutil.which("DensifyPointCloud")
    if densify and scene_mvs.exists():
        run([densify, "--input-file", str(scene_mvs), "--resolution-level", "1"],
            "Dense point cloud")

    mesh = shutil.which("ReconstructMesh")
    dense_mvs = mvs_dir / "scene_dense.mvs"
    if mesh and dense_mvs.exists():
        run([mesh, "--input-file", str(dense_mvs)], "Mesh reconstruction")

    refine = shutil.which("RefineMesh")
    mesh_ply = mvs_dir / "scene_dense_mesh.ply"
    if refine and mesh_ply.exists():
        run([refine, "--input-file", str(dense_mvs),
             "--mesh-file", str(mesh_ply)], "Mesh refinement")

    logger.info("OpenMVS output: %s", mvs_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="COLMAP sparse reconstruction with turntable pose priors"
    )
    parser.add_argument("--images", type=Path, required=True,
                        help="Directory of input images (JPEG/PNG/TIFF)")
    parser.add_argument("--output", type=Path, required=True,
                        help="Output directory for COLMAP project")
    parser.add_argument("--radius-mm", type=float, default=DEFAULT_RADIUS_MM,
                        help=f"Approximate camera-to-subject distance in mm "
                             f"(default: {DEFAULT_RADIUS_MM}). "
                             f"Scale only — COLMAP determines actual scale from matching.")
    parser.add_argument("--calibration", type=Path, default=None,
                        help="Path to calibration JSON from calibrate.py/calibrate_guided.py")
    parser.add_argument("--refine-intrinsics", action="store_true",
                        help="Allow COLMAP bundle adjustment to refine the supplied calibration")
    parser.add_argument("--sparse-only", action="store_true",
                        help="Stop after sparse reconstruction (skip OpenMVS)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
        stats = reconstruct(
            image_dir=args.images,
            output_dir=args.output,
            radius_mm=args.radius_mm,
            sparse_only=args.sparse_only,
            calibration_path=args.calibration,
            refine_intrinsics=args.refine_intrinsics,
        )
        ok = stats.get("registered_images", 0) > 0
        sys.exit(0 if ok else 1)
    except Exception as e:
        logger.error("Reconstruction failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
