"""
universal_sonar_analysis.py - Multi-project, Multi-build-system SonarCloud Analysis
Supports: Maven and Gradle projects
Checkpoint system: uploads per-issue reports/logs as gzip-compressed assets on
a per-project GitHub Release (with only the small progress index committed to
the scripts repo) so interrupted batches resume without re-analyzing already-
seen issues and never hit GitHub's 100 MB push limit.
"""

import os
import json
import gzip
import shutil
import subprocess
import time
import requests
import logging
import traceback
import re
from datetime import datetime, timezone
from pathlib import Path

# ============================================================================
# CONFIGURATION
# ============================================================================
PROJECT_NAME = os.getenv("PROJECT_NAME", "flink")
BATCH_NUMBER = os.getenv("BATCH_NUMBER", "1")

# SCRIPTS_REPO_PATH is the root of the checked-out scripts repository
# (i.e. $GITHUB_WORKSPACE on the runner, or a local equivalent).
# The internal layout is:
#   <SCRIPTS_REPO_PATH>/scripts/          ← Python scripts, JSON configs, batch files
#   <SCRIPTS_REPO_PATH>/checkpoints/      ← progress JSON (reports/logs live on the GitHub Release)
SCRIPTS_REPO_PATH = os.getenv("SCRIPTS_REPO_PATH", os.path.join(os.path.dirname(__file__), ".."))


def _resolve_repo_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(SCRIPTS_REPO_PATH, path))

# Load project-specific configuration
with open(os.path.join(SCRIPTS_REPO_PATH, "scripts", "project_configs.json")) as f:
    PROJECT_CONFIGS = json.load(f)

PROJECT_CONFIG = PROJECT_CONFIGS.get(PROJECT_NAME, {})

# Optional per-run Java enforcement (set via the FORCE_JAVA_VERSION workflow
# input). When left at the default ("auto"/empty), Java selection is fully
# automatic (year-based toolchain + pom.xml detection). When set to a concrete
# version it is hard-enforced for every scan in this run, overriding both the
# year-based toolchain and pom.xml auto-detection.
_FORCED_JAVA = (os.getenv("FORCE_JAVA_VERSION") or "").strip().lower()
if _FORCED_JAVA in ("", "auto", "default", "none"):
    _FORCED_JAVA = None

CONFIG = {
    "project_name": PROJECT_NAME,
    "jira_json_path": _resolve_repo_path(
        os.getenv(
            "JIRA_JSON_PATH",
            os.path.join(
                SCRIPTS_REPO_PATH, "scripts",
                f"{PROJECT_NAME}_issues_batch_{BATCH_NUMBER}.json"
            )
        )
    ),
    "repo_path": os.getenv("PROJECT_REPO_PATH", "."),
    "output_dir": "output",
    
    # SONAR_HOST_URL selects the backend: default SonarCloud, or a self-hosted
    # SonarQube (e.g. http://localhost:9000 running inside the runner).
    "sonar_url": os.getenv("SONAR_HOST_URL", "https://sonarcloud.io"),
    "sonar_token": os.getenv("SONAR_TOKEN"),
    # Empty/unset organization ⇒ self-hosted SonarQube (no org concept);
    # a value ⇒ SonarCloud, where component keys are namespaced under the org.
    "sonar_organization": (os.getenv("SONAR_ORGANIZATION") or "").strip() or None,
    
    "build_system": PROJECT_CONFIG.get("build_system", "maven"),
    "sonar_exclusions": PROJECT_CONFIG.get("sonar_exclusions", []),
    "maven_skip_flags": PROJECT_CONFIG.get("maven_skip_flags", []),
    "gradle_tasks": PROJECT_CONFIG.get("gradle_tasks", ["clean", "build"]),
    "gradle_skip_flags": PROJECT_CONFIG.get("gradle_skip_flags", []),
    
    "java_homes": {
        "8":  os.getenv("JAVA_HOME_8_X64"),
        "11": os.getenv("JAVA_HOME_11_X64"),
        "17": os.getenv("JAVA_HOME_17_X64"),
        "21": os.getenv("JAVA_HOME_21_X64")
    },

    # None → automatic Java selection; "8"/"11"/"17"/"21" → hard-enforced.
    "force_java_version": _FORCED_JAVA,
}

os.makedirs(CONFIG["output_dir"], exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(CONFIG["output_dir"], "analysis.log")),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

logger.info("="*70)
logger.info(f"Universal Sonar Analysis - {PROJECT_NAME}")
logger.info(f"Build System: {CONFIG['build_system']}")
if CONFIG["sonar_organization"]:
    logger.info(f"Sonar backend: SonarCloud (org '{CONFIG['sonar_organization']}') at {CONFIG['sonar_url']}")
else:
    logger.info(f"Sonar backend: self-hosted SonarQube at {CONFIG['sonar_url']} (no organization)")
if os.getenv("JIRA_JSON_PATH"):
    logger.info(f"Using filtered issue batch from JIRA_JSON_PATH: {CONFIG['jira_json_path']}")
if CONFIG["force_java_version"]:
    logger.info(f"Java selection: ENFORCED to Java {CONFIG['force_java_version']} "
                f"(workflow input; year toolchain + pom.xml auto-detection disabled)")
else:
    logger.info("Java selection: AUTOMATIC (year-based toolchain + pom.xml detection)")
logger.info("="*70)


# ============================================================================
# CHECKPOINT SYSTEM
# ============================================================================

# checkpoints/ lives at the root of the scripts repo so git can track it.
CHECKPOINTS_DIR = os.path.join(SCRIPTS_REPO_PATH, "checkpoints")

# One progress file per project (not per batch) so the tracker accumulates
# across all batches and we never re-analyze an issue even if it appears in
# a different batch run.
PROGRESS_FILE = os.path.join(CHECKPOINTS_DIR, f"{PROJECT_NAME}_progress.json")

os.makedirs(CHECKPOINTS_DIR, exist_ok=True)


# ── Release-asset checkpoint store ──────────────────────────────────────────
# Per-issue reports (and build/scan logs) can exceed GitHub's 100 MB push limit,
# and a single oversized commit blocks every later push in the run. So the heavy
# artefacts are stored as gzip-compressed assets on a per-project GitHub Release
# instead of being committed to git; only the small progress JSON stays under
# version control. Assets are keyed by issue id, so parallel batches of the same
# project never collide (same-issue re-runs overwrite via --clobber).

RELEASE_TAG = f"checkpoints-{PROJECT_NAME}"

# GitHub Actions always sets GITHUB_REPOSITORY (e.g. "SmallKlaus/sonar-analysis").
# When unset (local runs) gh auto-detects the repo from the origin remote.
_GH_REPO = (os.getenv("GITHUB_REPOSITORY") or "").strip()
_GH_REPO_ARGS = ["--repo", _GH_REPO] if _GH_REPO else []

# gh authenticates from GH_TOKEN/GITHUB_TOKEN, already present in the workflow env.
_GH_AVAILABLE = shutil.which("gh") is not None
if not _GH_AVAILABLE:
    logger.warning(
        "⚠ gh CLI not found — release-asset checkpoints disabled. Reports are "
        "still written to output/ and uploaded as the run artifact, but there "
        "is no cross-run resume."
    )

# Transient area for compress/decompress; files are deleted after each op so
# nothing here is committed or bloats the run artifact. MUST be absolute: gh is
# invoked with cwd=SCRIPTS_REPO_PATH (repo root) while Python's cwd is project/,
# so a relative path would make gh look for staged files in the wrong directory
# ("no matches found" on upload; downloads landing where gunzip can't see them).
RELEASE_STAGING_DIR = os.path.abspath(os.path.join(CONFIG["output_dir"], "_release_staging"))
os.makedirs(RELEASE_STAGING_DIR, exist_ok=True)

# Asset names present on the release (e.g. "HDFS-14678_report.json.gz"),
# populated once at startup by refresh_release_assets().
_RELEASE_ASSETS: set = set()


def _run_gh(args: list, check: bool = True, timeout: int = 600):
    """Run a gh CLI command (cwd = scripts repo, so origin auto-detect works)."""
    return subprocess.run(
        ["gh"] + args,
        cwd=SCRIPTS_REPO_PATH, check=check, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _err_text(e: Exception) -> str:
    """Best-effort stderr/message extraction from a subprocess exception."""
    raw = getattr(e, "stderr", None)
    if raw:
        return raw.decode("utf-8", errors="replace").strip() if isinstance(raw, bytes) else str(raw).strip()
    return str(e)


def _gzip_file(src_path: str, dest_path: str):
    """Gzip src_path → dest_path (level 6: strong ratio at low CPU cost)."""
    with open(src_path, "rb") as f_in, gzip.open(dest_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)


def _gunzip_file(src_path: str, dest_path: str):
    """Decompress a .gz file → dest_path."""
    with gzip.open(src_path, "rb") as f_in, open(dest_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def ensure_release_exists():
    """
    Make sure the per-project checkpoint release exists. Idempotent and
    race-tolerant: parallel batches may call this at once, so a create that
    fails because the tag already exists is treated as success. Best-effort —
    never raises (a run can still produce its artifact without the release).
    """
    if not _GH_AVAILABLE:
        return
    try:
        if _run_gh(["release", "view", RELEASE_TAG] + _GH_REPO_ARGS, check=False).returncode == 0:
            return
        create = _run_gh(
            ["release", "create", RELEASE_TAG,
             "--title", f"Checkpoints: {PROJECT_NAME}",
             "--notes", "Automated per-issue Sonar analysis checkpoints "
                        "(gzip-compressed reports and build/scan logs).",
             "--latest=false"] + _GH_REPO_ARGS,
            check=False,
        )
        if create.returncode == 0:
            logger.info(f"✓ Created checkpoint release '{RELEASE_TAG}'")
            return
        # Lost a create race, or a transient error — re-check existence.
        if _run_gh(["release", "view", RELEASE_TAG] + _GH_REPO_ARGS, check=False).returncode == 0:
            logger.info(f"✓ Checkpoint release '{RELEASE_TAG}' already exists")
            return
        stderr = create.stderr.decode("utf-8", errors="replace").strip()
        logger.warning(f"⚠ Could not create release '{RELEASE_TAG}': {stderr}")
    except Exception as e:
        logger.warning(f"⚠ ensure_release_exists failed: {_err_text(e)}")


def refresh_release_assets():
    """
    Populate _RELEASE_ASSETS with every asset name on the checkpoint release,
    using the paginated assets API so releases with hundreds of issues are not
    truncated. Best-effort: on any failure the set stays empty, which just means
    no restores (issues get re-analyzed) — always safe.
    """
    global _RELEASE_ASSETS
    _RELEASE_ASSETS = set()
    if not _GH_AVAILABLE:
        return

    repo = _GH_REPO
    if not repo:
        # Derive owner/repo from the origin remote for the gh api path.
        try:
            url = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=SCRIPTS_REPO_PATH, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ).stdout.decode().strip()
            m = re.search(r"github\.com[:/]+([^/]+/[^/.]+)", url)
            repo = m.group(1) if m else ""
        except Exception:
            repo = ""
    if not repo:
        logger.warning("⚠ Could not determine repo slug — skipping release asset listing")
        return

    try:
        rid = _run_gh(
            ["api", f"repos/{repo}/releases/tags/{RELEASE_TAG}", "--jq", ".id"]
        ).stdout.decode().strip()
        if not rid:
            return
        out = _run_gh(
            ["api", "--paginate", f"repos/{repo}/releases/{rid}/assets", "--jq", ".[].name"]
        ).stdout.decode()
        _RELEASE_ASSETS = {ln.strip() for ln in out.splitlines() if ln.strip()}
        logger.info(f"✓ Release '{RELEASE_TAG}' holds {len(_RELEASE_ASSETS)} checkpoint asset(s)")
    except Exception as e:
        logger.warning(f"⚠ Could not list release assets: {_err_text(e)}")


def _upload_asset(local_path: str, asset_name: str) -> bool:
    """
    Gzip local_path and upload it to the release as <asset_name>.gz, with
    retry/backoff. Returns True on success. On failure the artefact still lives
    in output/ and ships in the run artifact — only cross-run resume is lost.
    """
    if not _GH_AVAILABLE:
        return False
    gz_path = os.path.join(RELEASE_STAGING_DIR, asset_name + ".gz")
    try:
        _gzip_file(local_path, gz_path)
    except OSError as e:
        logger.error(f"    ✗ gzip failed for {os.path.basename(local_path)}: {e}")
        return False

    try:
        for attempt in range(1, 6):
            try:
                _run_gh(["release", "upload", RELEASE_TAG, gz_path, "--clobber"] + _GH_REPO_ARGS)
                _RELEASE_ASSETS.add(asset_name + ".gz")
                size_mb = os.path.getsize(gz_path) / (1024 * 1024)
                logger.info(f"    ✓ Uploaded → release:{asset_name}.gz ({size_mb:.1f} MB)")
                return True
            except subprocess.SubprocessError as e:
                logger.warning(f"    ⚠ Asset upload failed (attempt {attempt}/5): {_err_text(e)}")
                time.sleep(5 * attempt)
        logger.error(f"    ✗ Could not upload {asset_name}.gz after 5 attempts — kept in output/ only")
        return False
    finally:
        try:
            os.remove(gz_path)
        except OSError:
            pass


def _download_asset(asset_name: str, dest_path: str) -> bool:
    """
    Download <asset_name>.gz from the release and decompress it to dest_path,
    with retry/backoff. Returns True on success.
    """
    if not _GH_AVAILABLE:
        return False
    gz_name = asset_name + ".gz"
    gz_path = os.path.join(RELEASE_STAGING_DIR, gz_name)
    # gh refuses to overwrite an existing download target — clear any stale one.
    try:
        os.remove(gz_path)
    except OSError:
        pass
    try:
        for attempt in range(1, 6):
            try:
                _run_gh(["release", "download", RELEASE_TAG,
                         "--pattern", gz_name, "--dir", RELEASE_STAGING_DIR] + _GH_REPO_ARGS)
                if os.path.exists(gz_path):
                    _gunzip_file(gz_path, dest_path)
                    return True
                logger.warning(f"    ⚠ Download of {gz_name} produced no file (attempt {attempt}/5)")
            except (subprocess.SubprocessError, OSError) as e:
                logger.warning(f"    ⚠ Asset download failed (attempt {attempt}/5): {_err_text(e)}")
            time.sleep(5 * attempt)
        logger.error(f"    ✗ Could not download {gz_name} after 5 attempts")
        return False
    finally:
        try:
            os.remove(gz_path)
        except OSError:
            pass


def load_progress() -> dict:
    """
    Load the progress JSON from the checkpoints directory.

    Schema:
    {
        "FLINK-12345": {
            "status":    "success" | "failed",
            "timestamp": "2026-03-29T22:40:40Z",
            "batch":     "3"
        },
        ...
    }
    """
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"✓ Loaded progress file: {len(data)} issues already tracked")
        return data
    logger.info("No existing progress file — starting fresh")
    return {}


def save_progress(progress: dict):
    """Write the in-memory progress dict back to disk."""
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)


def checkpoint_issue(issue_id: str, status: str, progress: dict,
                     report_path: str = None, log_path: str = None,
                     scan_log_path: str = None):
    """
    Persist an issue's artefacts and record its status.

    Heavy artefacts (report + build/scan logs) are gzip-compressed and uploaded
    as assets on the per-project GitHub Release, so they never hit the 100 MB
    git push limit. Only the small progress JSON is committed to the repo (its
    concurrent-batch merge is handled by _git_push_checkpoints).

    Args:
        issue_id:      e.g. "FLINK-12345"
        status:        "success" or "failed"
        progress:      in-memory progress dict (mutated in-place then saved)
        report_path:   absolute path to the JSON report (None if the issue
                       failed before a report was created)
        log_path:      absolute path to the build log
        scan_log_path: absolute path to the sonar scanner log (passed on failure
                       so scanner/server errors survive the runner)
    """
    logger.info(f"  Checkpointing {issue_id} ({status})...")

    # --- Upload heavy artefacts to the release (gzip-compressed) -------------
    if report_path and os.path.exists(report_path):
        _upload_asset(report_path, f"{issue_id}_report.json")
    if log_path and os.path.exists(log_path):
        _upload_asset(log_path, f"{issue_id}_build.log")
    if scan_log_path and os.path.exists(scan_log_path):
        _upload_asset(scan_log_path, f"{issue_id}_scan.log")

    # --- Update progress JSON (the only file kept in git) -------------------
    progress[issue_id] = {
        "status":    status,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "batch":     BATCH_NUMBER,
    }
    save_progress(progress)

    # --- Commit and push just the progress file to the remote repo ----------
    _git_push_checkpoints(issue_id, status, [PROGRESS_FILE])


def _git_push_checkpoints(issue_id: str, status: str, files: list):
    # Stage files once
    for f in files:
        try:
            subprocess.run(
                ["git", "add", "--force", f],
                cwd=SCRIPTS_REPO_PATH, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode("utf-8", errors="replace").strip()
            logger.error(f"    ✗ git add failed for {f}: {stderr}")
            return

    # Nothing staged?
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=SCRIPTS_REPO_PATH
    )
    if diff.returncode == 0:
        logger.info("    ✓ Checkpoint unchanged — nothing to push")
        return

    # Commit once
    try:
        subprocess.run(
            ["git", "commit", "-m",
             f"checkpoint({PROJECT_NAME}): {issue_id} [{status}] batch {BATCH_NUMBER}"],
            cwd=SCRIPTS_REPO_PATH, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace").strip()
        logger.error(f"    ✗ git commit failed: {stderr}")
        return

    # Retry loop: abort any broken rebase state, pull, then push
    for attempt in range(1, 6):
        try:
            # ── Always abort any in-progress rebase before trying ────────
            # This is a no-op if no rebase is in progress, so it's safe
            # to run unconditionally on every attempt.
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=SCRIPTS_REPO_PATH,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            # ── Pull remote changes, preferring theirs on conflict ───────
            # -X theirs tells git: if there's a conflict during the rebase,
            # automatically accept the remote version of the conflicting
            # chunk. This is safe here because the progress JSON is
            # append-only — our new keys will be re-added by the commit
            # that follows the rebase.
            pull = subprocess.run(
                ["git", "pull", "--rebase", "-X", "theirs", "origin", "main"],
                cwd=SCRIPTS_REPO_PATH,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if pull.returncode != 0:
                stderr = pull.stderr.decode("utf-8", errors="replace").strip()
                logger.warning(f"    ⚠ git pull --rebase failed (attempt {attempt}/5): {stderr}")
                time.sleep(5 * attempt)
                continue

            # ── Re-stage and amend after the rebase ──────────────────────
            # After rebasing onto the remote, our checkpoint files need to
            # be re-staged because the rebase may have dropped them if
            # -X theirs resolved the conflict by taking the remote version.
            for f in files:
                subprocess.run(
                    ["git", "add", "--force", f],
                    cwd=SCRIPTS_REPO_PATH,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )

            # Only amend if there's something new to add
            diff_after = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=SCRIPTS_REPO_PATH
            )
            if diff_after.returncode != 0:
                subprocess.run(
                    ["git", "commit", "--amend", "--no-edit"],
                    cwd=SCRIPTS_REPO_PATH,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )

            # ── Push ──────────────────────────────────────────────────────
            subprocess.run(
                ["git", "push", "origin", "HEAD:refs/heads/main"],
                cwd=SCRIPTS_REPO_PATH, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            logger.info(f"    ✓ Checkpoint pushed (attempt {attempt})")
            return

        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode("utf-8", errors="replace").strip()
            logger.warning(f"    ⚠ Git push failed (attempt {attempt}/5): {e}")
            logger.warning(f"      stderr: {stderr}")
            wait = 5 * attempt
            logger.warning(f"      Retrying in {wait}s...")
            time.sleep(wait)

    logger.error("    ✗ Could not push checkpoint after 5 attempts — files saved locally only")


def restore_from_checkpoint(issue_id: str) -> bool:
    """
    Return True (and stage the issue's report + build log into output/) if the
    issue was already analyzed in a prior run, so it can be skipped. Checks the
    local checkpoints dir first (legacy layout / manual local drops), then the
    release assets. A report present = a prior success; failures leave no report
    and are retried.
    """
    out_report = os.path.join(CONFIG["output_dir"], f"{issue_id}_report.json")

    # 1) Local checkpoints dir (legacy layout / manual local drops).
    local_report = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_report.json")
    if os.path.exists(local_report):
        logger.info(f"  ↩ Found in local checkpoints — skipping analysis")
        shutil.copy2(local_report, out_report)
        logger.info(f"    ✓ Restored report → output/{issue_id}_report.json")
        local_log = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_build.log")
        if os.path.exists(local_log):
            shutil.copy2(local_log, os.path.join(CONFIG["output_dir"], f"{issue_id}_build.log"))
            logger.info(f"    ✓ Restored log    → output/{issue_id}_build.log")
        return True

    # 2) Release asset (normal cross-run resume path).
    if f"{issue_id}_report.json.gz" in _RELEASE_ASSETS:
        logger.info(f"  ↩ Found in release checkpoints — downloading")
        if _download_asset(f"{issue_id}_report.json", out_report):
            logger.info(f"    ✓ Restored report → output/{issue_id}_report.json")
            if f"{issue_id}_build.log.gz" in _RELEASE_ASSETS:
                if _download_asset(f"{issue_id}_build.log",
                                   os.path.join(CONFIG["output_dir"], f"{issue_id}_build.log")):
                    logger.info(f"    ✓ Restored log    → output/{issue_id}_build.log")
            return True
        logger.warning(f"  ⚠ Release report download failed — will re-analyze {issue_id}")

    return False


# ============================================================================
# VERSION SELECTION
# ============================================================================

#test toolchain for CAMEL expectations
def get_toolchain(year: int) -> dict:
    if year <= 2017:
        return {"java_major": "8", "java_source": "1.8", "gradle": "4.10.3", "maven": "3.3.9", "year": year}

    elif year <= 2019:
        return {"java_major": "8", "java_source": "1.8", "gradle": "5.6.4", "maven": "3.5.4", "year": year}

    elif year <= 2021:
        return {"java_major": "11", "java_source": "11", "gradle": "6.9.4", "maven": "3.6.3", "year": year}

    elif year <= 2022:
        return {"java_major": "11", "java_source": "11", "gradle": "7.6.4", "maven": "3.8.1", "year": year}

    elif year <= 2024:
        return {"java_major": "17", "java_source": "17", "gradle": "8.5", "maven": "3.8.6", "year": year}

    else:
        return {"java_major": "21", "java_source": "21", "gradle": "8.7", "maven": "3.9.9", "year": year}

#standard tool_chain commented out
'''
def get_toolchain(year: int) -> dict:
    if year <= 2017:
        return {"java_major": "8",  "java_source": "1.8", "gradle": "4.10.3", "maven": "3.0.5", "year": year}
    elif year <= 2019:
        return {"java_major": "8",  "java_source": "1.8", "gradle": "5.6.4",  "maven": "3.5.4", "year": year}
    elif year <= 2021:
        return {"java_major": "8",  "java_source": "1.8", "gradle": "6.9.4",  "maven": "3.8.1", "year": year}
    elif year <= 2023:
        return {"java_major": "11", "java_source": "11",  "gradle": "7.6.4",  "maven": "3.8.6", "year": year}
    elif year <= 2024:
        return {"java_major": "11", "java_source": "11",  "gradle": "8.5", "maven": "3.9.9", "year": year}
    else:
        return {"java_major": "17", "java_source": "17",  "gradle": "8.7",  "maven": "3.9.9",  "year": year}
'''

def apply_forced_java(toolchain: dict, forced: str) -> None:
    """Override a toolchain's JDK in place to a user-chosen version.

    Java 8 uses the historical '1.8' source string; 11/17/21 use the bare
    number, matching what get_toolchain() emits.
    """
    toolchain["java_major"]  = forced
    toolchain["java_source"] = "1.8" if forced == "8" else forced


def get_protoc_bin(year: int) -> str:
    """
    Returns the path to the correct protoc binary for the given commit year.
    Hadoop 2.x and early 3.x used protobuf 2.5.0.
    Hadoop 3.3.x switched to protobuf 3.7.1.
    """
    if year <= 2020:
        return "/usr/local/bin/protoc-2.5.0"
    else:
        return "/usr/local/bin/protoc-3.7.1"

def year_from_iso(date_str: str) -> int:
    try:
        return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").year
    except Exception:
        return datetime.now(timezone.utc).year

def detect_required_java(repo_path: str) -> str | None:
    """
    Read the root pom.xml after checkout to determine the actual
    minimum JDK this commit requires.  Camel uses <jdk.min.version>.
    """
    pom_path = os.path.join(repo_path, "pom.xml")
    if not os.path.exists(pom_path):
        return None
    try:
        with open(pom_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None

    for pattern in [
        r"<jdk\.min\.version[^>]*>(\d+)",
        r"<maven\.compiler\.release>(\d+)",
    ]:
        match = re.search(pattern, content)
        if match:
            ver = match.group(1)
            if ver.startswith("1."):
                ver = ver[2:]
            if ver in ("8", "11", "17"):
                return ver
    return None


# ============================================================================
# GIT HELPERS
# ============================================================================
def git_checkout(repo_path: str, sha: str) -> bool:
    logger.info(f"Git checkout: {sha[:10]}")
    try:
        subprocess.run(
            ["git", "checkout", "--force", sha],
            cwd=repo_path, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30
        )
        # -e output/ prevents wiping out our reports
        subprocess.run(
            ["git", "clean", "-fd", "-e", "output/"],
            cwd=repo_path, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30
        )
        logger.info(f"✓ Checked out {sha[:10]}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"✗ Git checkout failed: {e}")
        return False


def patch_antrun_tasks_to_target(repo_path: str) -> int:
    """
    Compatibility patch for historical Maven projects.

    maven-antrun-plugin 3.x fails builds that still configure the run goal with
    <tasks>. Older Ozone commits rely on those Ant steps for generated sources,
    so skipping antrun is unsafe. Renaming <tasks> to <target> preserves the
    Ant actions while making the POM compatible with modern antrun versions.
    """
    patched = 0
    for pom_path in Path(repo_path).rglob("pom.xml"):
        try:
            content = pom_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = pom_path.read_text(encoding="latin-1")

        if "maven-antrun-plugin" not in content or "<tasks" not in content:
            continue

        updated = re.sub(r"<tasks(\s[^>]*)?>", r"<target\1>", content)
        updated = updated.replace("</tasks>", "</target>")
        if updated == content:
            continue

        pom_path.write_text(updated, encoding="utf-8")
        patched += 1
        logger.info(f"  Patched antrun <tasks> -> <target>: {pom_path.relative_to(repo_path)}")

    if patched:
        logger.info(f"✓ Applied antrun compatibility patch to {patched} POM(s)")
    return patched


def apply_project_compat_patches(repo_path: str):
    if PROJECT_CONFIG.get("patch_antrun_tasks_to_target"):
        patch_antrun_tasks_to_target(repo_path)

# ============================================================================
# BUILD SYSTEM ABSTRACTION
# ============================================================================
class BuildSystem:
    """Abstract base for build systems"""
    
    def __init__(self, repo_path: str, toolchain: dict, log_file: str):
        self.repo_path = repo_path
        self.toolchain = toolchain
        self.log_file = log_file
        self.java_home = CONFIG["java_homes"].get(toolchain["java_major"])
    
    def build(self) -> bool:
        raise NotImplementedError
    
    def get_source_dirs(self) -> list:
        raise NotImplementedError
    
    def get_binary_dirs(self) -> list:
        raise NotImplementedError


class MavenBuildSystem(BuildSystem):
    """Maven-specific build logic"""
    
    def build(self) -> bool:
        logger.info(f"Maven Build - Java {self.toolchain['java_major']}")
        
        env = os.environ.copy()
        env["JAVA_HOME"] = self.java_home
        env["PATH"] = f"{self.java_home}/bin:{env.get('PATH', '')}"
        env["MAVEN_OPTS"] = (
            " -Dfile.encoding=UTF-8"
            " -Dmaven.wagon.http.retryHandler.count=5"
            " -Dmaven.wagon.http.connectionTimeout=60000"
        )

        # Select correct protoc version for Hadoop projects
        if PROJECT_CONFIG.get("requires_protoc"):
            protoc_bin = get_protoc_bin(self.toolchain.get("year", 2021))
            env["HADOOP_PROTOC_PATH"] = protoc_bin
            logger.info(f"  Using protoc: {protoc_bin}")
        
        cmd = [
            "mvn", "clean", "install",
            "-DskipTests",
            f"-Dmaven.javadoc.skip={'true' if PROJECT_CONFIG.get('skip_javadoc', True) else 'false'}",
            "-Dcheckstyle.skip=true",
            "-Denforcer.skip=true",
            "-Drat.skip=true",
            f"-Dmaven.compiler.source={self.toolchain['java_source']}",
            f"-Dmaven.compiler.target={self.toolchain['java_source']}",
            "--batch-mode",
            "--no-transfer-progress",
        ]
        
        # Add project-specific skip flags
        cmd.extend(CONFIG["maven_skip_flags"])

        # Also pass protoc path as a Maven property (some Hadoop versions use this instead)
        if PROJECT_CONFIG.get("requires_protoc"):
            protoc_bin = get_protoc_bin(self.toolchain.get("year", 2021))
            cmd.append(f"-Dprotoc.path={protoc_bin}")
        
        return self._execute_build(cmd, env)
    
    def get_source_dirs(self) -> list:
        sources = []
        for root, dirs, files in os.walk(self.repo_path):
            if root.endswith("src/main/java"):
                sources.append(os.path.relpath(root, self.repo_path))
        return sources
    
    def get_binary_dirs(self) -> list:
        binaries = []
        for root, dirs, files in os.walk(self.repo_path):
            if root.endswith("target/classes"):
                binaries.append(os.path.relpath(root, self.repo_path))
        return binaries
    
    def _execute_build(self, cmd, env) -> bool:
        try:
            with open(self.log_file, "w", encoding="utf-8") as log_fh:
                process = subprocess.Popen(
                    cmd, cwd=self.repo_path, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    encoding="utf-8", errors="replace"
                )
                
                for line in process.stdout:
                    log_fh.write(line)
                    if "building" in line.lower():
                        logger.info(f"  {line.strip()}")
                
                process.wait(timeout=2700)
                
                if process.returncode == 0:
                    logger.info("✓ Build succeeded")
                    return True
                else:
                    logger.error(f"✗ Build failed (exit {process.returncode})")
                    return False
                    
        except subprocess.TimeoutExpired:
            logger.error("✗ Build timeout (45 min)")
            return False


class GradleBuildSystem(BuildSystem):
    """Gradle-specific build logic"""

    def build(self) -> bool:
        logger.info(f"Gradle Build - Java {self.toolchain['java_major']}")
    
        env = os.environ.copy()
        env["JAVA_HOME"] = self.java_home
        env["PATH"] = f"{self.java_home}/bin:{env.get('PATH', '')}"
    
        gradle_version = self.toolchain.get("gradle", "6.9.4")
        gradle_cmd = self._resolve_gradle_executable(gradle_version, env)
    
        if gradle_cmd is None:
            logger.error("✗ Could not obtain a Gradle executable")
            return False
    
        # Inject init script to redirect dead repositories
        self._inject_init_script()
    
        cmd = (
            [gradle_cmd]
            + CONFIG["gradle_tasks"]
            + self._normalize_skip_flags(CONFIG["gradle_skip_flags"])
        )
        return self._execute_build(cmd, env)

    @staticmethod
    def _normalize_skip_flags(skip_flags: list) -> list:
        """
        Expand ambiguous Gradle task exclusions into concrete task names.

        Kafka exposes checkstyleMain/checkstyleTest but no aggregate checkstyle
        task, so `-x checkstyle` fails before compilation can produce classes
        for Sonar analysis.
        """
        normalized = []
        i = 0
        while i < len(skip_flags):
            if (
                skip_flags[i] == "-x"
                and i + 1 < len(skip_flags)
                and skip_flags[i + 1] == "checkstyle"
            ):
                normalized.extend(["-x", "checkstyleMain", "-x", "checkstyleTest"])
                i += 2
                continue
            normalized.append(skip_flags[i])
            i += 1
        return normalized
    
    
    def _inject_init_script(self):
        init_dir = os.path.expanduser("~/.gradle/init.d")
        os.makedirs(init_dir, exist_ok=True)
    
        init_script = os.path.join(init_dir, "redirect-dead-repos.gradle")
    
        if os.path.exists(init_script):
            return
    
        github_token = os.getenv("GITHUB_TOKEN", "")
        github_actor = os.getenv("GITHUB_ACTOR", "")
    
        script_content = f"""
    allprojects {{
        buildscript {{
            repositories {{
                // Remove dead JCenter/Bintray repositories
                all {{ ArtifactRepository repo ->
                    if (repo instanceof MavenArtifactRepository) {{
                        def url = repo.url.toString()
                        if (url.contains('jcenter.bintray.com') ||
                            url.contains('dl.bintray.com')) {{
                            logger.lifecycle("Removing dead repo: ${{url}}")
                            remove repo
                        }}
                    }}
                }}
                // GitHub Packages — hosts artifacts missing from Central (e.g. grgit)
                maven {{
                    url 'https://maven.pkg.github.com/SmallKlaus/maven-artifacts'
                    credentials {{
                        username = '{github_actor}'
                        password = '{github_token}'
                    }}
                }}
                maven {{ url 'https://repo.maven.apache.org/maven2' }}
                maven {{ url 'https://plugins.gradle.org/m2/' }}
            }}
        }}
        repositories {{
            all {{ ArtifactRepository repo ->
                if (repo instanceof MavenArtifactRepository) {{
                    def url = repo.url.toString()
                    if (url.contains('jcenter.bintray.com') ||
                        url.contains('dl.bintray.com')) {{
                        logger.lifecycle("Removing dead repo: ${{url}}")
                        remove repo
                    }}
                }}
            }}
            maven {{
                url 'https://maven.pkg.github.com/SmallKlaus/maven-artifacts'
                credentials {{
                    username = '{github_actor}'
                    password = '{github_token}'
                }}
            }}
            maven {{ url 'https://repo.maven.apache.org/maven2' }}
            maven {{ url 'https://plugins.gradle.org/m2/' }}
        }}
    }}
    """
        with open(init_script, "w") as f:
            f.write(script_content)
    
        logger.info(f"  ✓ Gradle init script injected (GitHub Packages + dead repo redirect)")
    
    
    def _resolve_gradle_executable(self, version: str, env: dict) -> str | None:
        """
        Returns the path to a gradle executable at the correct version.
        
        Strategy:
          1. If gradlew exists → override wrapper properties → return './gradlew'
          2. If no gradlew → download the correct Gradle binary → return its path
        """
        gradlew = os.path.join(self.repo_path, "gradlew")
    
        if os.path.exists(gradlew):
            if PROJECT_CONFIG.get("use_existing_gradle_wrapper"):
                logger.info("  Using repository Gradle wrapper version")
            else:
                self._set_gradle_wrapper_version(version)
            # Make sure gradlew is executable
            os.chmod(gradlew, 0o755)
            return "./gradlew"
    
        # No gradlew — download Gradle directly
        logger.info(f"  No gradlew found — downloading Gradle {version} directly")
        return self._download_gradle(version)
    
    
    def _download_gradle(self, version: str) -> str | None:
        """
        Downloads a specific Gradle version to ~/.gradle-installs/ if not
        already present, and returns the path to its bin/gradle executable.
        """
        install_dir = os.path.expanduser(f"~/.gradle-installs/gradle-{version}")
        gradle_bin  = os.path.join(install_dir, "bin", "gradle")
    
        if os.path.exists(gradle_bin):
            logger.info(f"  ✓ Gradle {version} already downloaded at {gradle_bin}")
            return gradle_bin
    
        zip_url  = f"https://services.gradle.org/distributions/gradle-{version}-bin.zip"
        zip_path = f"/tmp/gradle-{version}-bin.zip"
        parent   = os.path.expanduser("~/.gradle-installs")
        os.makedirs(parent, exist_ok=True)
    
        logger.info(f"  Downloading {zip_url} ...")
        try:
            result = subprocess.run(
                ["wget", "-q", zip_url, "-O", zip_path],
                check=True, timeout=120
            )
            subprocess.run(
                ["unzip", "-q", zip_path, "-d", parent],
                check=True, timeout=60
            )
            os.remove(zip_path)
    
            # The zip extracts to gradle-{version}/ inside parent
            extracted = os.path.join(parent, f"gradle-{version}")
            if not os.path.isdir(extracted):
                logger.error(f"  ✗ Expected extracted dir not found: {extracted}")
                return None
    
            # Rename to our canonical install_dir if different
            if extracted != install_dir:
                os.rename(extracted, install_dir)
    
            os.chmod(gradle_bin, 0o755)
            logger.info(f"  ✓ Gradle {version} installed at {gradle_bin}")
            return gradle_bin
    
        except Exception as e:
            logger.error(f"  ✗ Failed to download Gradle {version}: {e}")
            return None
    
    
    def _set_gradle_wrapper_version(self, version: str):
        wrapper_props = None
        for root, dirs, files in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in ["build", ".gradle", "node_modules"]]
            if "gradle-wrapper.properties" in files:
                wrapper_props = os.path.join(root, "gradle-wrapper.properties")
                break
    
        if wrapper_props is None:
            logger.warning("  gradle-wrapper.properties not found — wrapper override skipped")
            return
    
        props_content = (
            "distributionBase=GRADLE_USER_HOME\n"
            "distributionPath=wrapper/dists\n"
            f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{version}-bin.zip\n"
            "zipStoreBase=GRADLE_USER_HOME\n"
            "zipStorePath=wrapper/dists\n"
        )
        with open(wrapper_props, "w") as f:
            f.write(props_content)
    
        logger.info(f"  ✓ Gradle wrapper overridden → {version} "
                    f"({os.path.relpath(wrapper_props, self.repo_path)})")
    
    def get_source_dirs(self) -> list:
        sources = []
        for root, dirs, files in os.walk(self.repo_path):
            if root.endswith("src/main/java") or root.endswith("src/main/kotlin"):
                sources.append(os.path.relpath(root, self.repo_path))
        return sources
    
    def get_binary_dirs(self) -> list:
        binaries = []
        for root, dirs, files in os.walk(self.repo_path):
            if "build/classes/java/main" in root or "build/classes/kotlin/main" in root:
                binaries.append(os.path.relpath(root, self.repo_path))
        return binaries
    
    def _execute_build(self, cmd, env) -> bool:
        try:
            with open(self.log_file, "w", encoding="utf-8") as log_fh:
                process = subprocess.Popen(
                    cmd, cwd=self.repo_path, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    encoding="utf-8", errors="replace"
                )
                
                for line in process.stdout:
                    log_fh.write(line)
                    if "task" in line.lower():
                        logger.info(f"  {line.strip()}")
                
                process.wait(timeout=2700)
                
                if process.returncode == 0:
                    logger.info("✓ Build succeeded")
                    return True
                else:
                    logger.error(f"✗ Build failed (exit {process.returncode})")
                    return False
                    
        except subprocess.TimeoutExpired:
            logger.error("✗ Build timeout (45 min)")
            return False


def get_build_system(build_type: str, repo_path: str, toolchain: dict, log_file: str) -> BuildSystem:
    """Factory function to get the appropriate build system"""
    if build_type == "maven":
        return MavenBuildSystem(repo_path, toolchain, log_file)
    elif build_type == "gradle":
        return GradleBuildSystem(repo_path, toolchain, log_file)
    else:
        raise ValueError(f"Unsupported build system: {build_type}")


# ============================================================================
# SONARCLOUD (Build-system agnostic)
# ============================================================================
def full_component_key(project_key: str) -> str:
    """Component/project key as the active Sonar backend expects it.

    SonarCloud namespaces every key under the organization (`<org>_<key>`);
    self-hosted SonarQube has no organizations, so the key is used verbatim.
    """
    org = CONFIG["sonar_organization"]
    return f"{org}_{project_key}" if org else project_key


def create_public_project(project_key: str, attempts: int = 3, backoff: int = 20):
    full_key = full_component_key(project_key)
    url = f"{CONFIG['sonar_url']}/api/projects/create"
    auth = (CONFIG["sonar_token"], "")

    data = {"project": full_key, "name": project_key}
    if CONFIG["sonar_organization"]:
        # SonarCloud-only parameters; self-hosted SonarQube rejects 'organization'
        data["organization"] = CONFIG["sonar_organization"]
        data["visibility"] = "public"
    
    # Retried with backoff: a missed creation only surfaces after the full
    # build, as a misleading scanner-side "Not authorized or project not
    # found" — so transient API failures (rate limits with parallel runners,
    # gateway errors) must not slip through here.
    for attempt in range(1, attempts + 1):
        try:
            res = requests.post(url, auth=auth, data=data, timeout=30)
            if res.status_code == 200:
                logger.info(f"✓ Created public project: {full_key}")
                return True
            elif "already exists" in res.text.lower():
                logger.info(f"✓ Project exists: {full_key}")
                return True
            else:
                logger.error(f"✗ Project create attempt {attempt}/{attempts} failed "
                             f"(HTTP {res.status_code}): {res.text[:300]}")
        except Exception as e:
            logger.error(f"✗ Error creating project (attempt {attempt}/{attempts}): {e}")
        if attempt < attempts:
            time.sleep(backoff)
    return False


def sonar_scan(repo_path: str, project_key: str, build_system: BuildSystem) -> str:
    logger.info("Starting Sonar scan...")

    sources = build_system.get_source_dirs()
    binaries = build_system.get_binary_dirs()

    sources_str = ",".join(sources) if sources else "."
    binaries_str = ",".join(binaries) if binaries else "."

    full_project_key = full_component_key(project_key)

    exclusions = ",".join(CONFIG["sonar_exclusions"]) if CONFIG["sonar_exclusions"] else ""

    # sonar.organization is a SonarCloud-only property; omit it for self-hosted.
    org_line = f"sonar.organization={CONFIG['sonar_organization']}\n" if CONFIG["sonar_organization"] else ""
    props_content = f"""
{org_line}sonar.projectKey={full_project_key}
sonar.sources={sources_str}
sonar.java.binaries={binaries_str}
sonar.java.source={build_system.toolchain['java_source']}
sonar.sourceEncoding=UTF-8
sonar.scm.disabled=true
sonar.exclusions={exclusions}
sonar.cpd.skip=true
sonar.dbd.enabled=false
"""
    
    props_file = os.path.join(repo_path, "sonar-project.properties")
    with open(props_file, "w") as f:
        f.write(props_content)
    
    try:
        env = os.environ.copy()
        # SonarCloud requires a Java 21+ scanner runtime (since July 2026). The
        # linux-x64 scanner bundle uses its embedded JRE 21 and ignores
        # JAVA_HOME; this matters only if a no-JRE distribution is ever used.
        env["JAVA_HOME"] = CONFIG["java_homes"]["21"]
        
        process = subprocess.Popen(
            ["sonar-scanner",
             f"-Dsonar.host.url={CONFIG['sonar_url']}",
             f"-Dsonar.token={CONFIG['sonar_token']}"],
            cwd=repo_path, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8"
        )
        
        task_id = None
        
        safe_key = project_key.replace(":", "_")
        scan_log_path = os.path.join(CONFIG["output_dir"], f"sonar_scan_{safe_key}.log")
        
        with open(scan_log_path, "w", encoding="utf-8") as log_fh:
            for line in process.stdout:
                log_fh.write(line)
                
                if "task?id=" in line:
                    task_id = line.split("task?id=")[1].strip()
                
        
        process.wait(timeout=3600)
        
        if process.returncode == 0 and task_id:
            logger.info(f"✓ Scan complete, task: {task_id}")
            return task_id
        else:
            logger.error(f"✗ Scan failed (Exit Code {process.returncode}). Full log at: {scan_log_path}")
            
            # Dump the raw tail unfiltered — scanner failure messages are often
            # bare text with no ERROR/Exception prefix (e.g. the Java 17
            # deprecation notice), so a keyword filter can hide the real cause.
            try:
                with open(scan_log_path, "r", encoding="utf-8") as fh:
                    tail = fh.readlines()[-40:]
                logger.error("--- Scanner Output (last 40 lines) ---")
                for tline in tail:
                    logger.error(f"  {tline.rstrip()}")
                logger.error("---------------------------------------")
            except Exception:
                pass
            
            return None
            
    finally:
        if os.path.exists(props_file):
            os.remove(props_file)


def wait_for_task(task_id: str) -> bool:
    if not task_id:
        return False
    
    url = f"{CONFIG['sonar_url']}/api/ce/task?id={task_id}"
    auth = (CONFIG["sonar_token"], "")
    
    for _ in range(270):  # 45 min timeout
        try:
            resp = requests.get(url, auth=auth, timeout=10)
            task = resp.json()["task"]
            status = task["status"]
            
            if status == "SUCCESS":
                logger.info("✓ Task succeeded")
                return True
            elif status in ("FAILED", "CANCELED"):
                logger.error(f"✗ Task {status}")
                # CE task records vanish when the project is deleted, so log
                # the server-side failure reason while it's still available.
                if task.get("errorType"):
                    logger.error(f"  errorType: {task['errorType']}")
                if task.get("errorMessage"):
                    logger.error(f"  errorMessage: {task['errorMessage']}")
                return False
        except:
            pass
        time.sleep(10)
    
    return False


def scan_with_retry(repo_path: str, project_key: str, build_system,
                    label: str, attempts: int = 2, backoff: int = 90) -> bool:
    """
    Run sonar_scan + wait_for_task, retrying once on failure. Rescues
    transient SonarCloud rejections (rate limits, gateway errors, CE hiccups)
    without re-running the build — compiled artefacts are still on disk.
    """
    for attempt in range(1, attempts + 1):
        task_id = sonar_scan(repo_path, project_key, build_system)
        if task_id and wait_for_task(task_id):
            return True
        if attempt < attempts:
            logger.warning(f"  ⚠ {label} scan/task attempt {attempt} failed — retrying in {backoff}s...")
            time.sleep(backoff)
    return False


def get_measures(project_key: str) -> dict:
    full_key = full_component_key(project_key)
    url = f"{CONFIG['sonar_url']}/api/measures/component"
    auth = (CONFIG["sonar_token"], "")
    
    params = {
        "component": full_key,
        "metricKeys": "ncloc,complexity,violations,sqale_index"
    }
    
    try:
        res = requests.get(url, auth=auth, params=params, timeout=30)
        if res.status_code == 200:
            return {m["metric"]: m["value"] 
                   for m in res.json()["component"]["measures"]}
    except:
        pass
    return {}


# ── Issue fetching — partition-aware, complete beyond the 10k API cap ────────
#
# api/issues/search refuses to return rows past 10,000 (p*ps > 10000 -> HTTP
# 400). The previous implementation paged per type and treated that 400 as
# end-of-data, silently truncating any type with >10k issues (large baselines
# lost thousands of issues; fixed/new fetches are small and were unaffected).
#
# The cap is per QUERY, not per project, so any query whose `total` exceeds
# the cap is split into disjoint sub-queries that each fit under it, then
# merged (deduped by issue key):
#     level 0: types                (BUG / VULNERABILITY / CODE_SMELL)
#     level 1: + severities         (x5)
#     level 2: + directories        (batched by facet counts to <= 9,500)
#     level 3: + individual files   (pathological single-directory case)
# Every partition is verified: if fewer issues are collected than the API
# reported, a warning is logged so truncation can never be silent again.

API_CAP = 10_000          # hard Sonar Web API limit on retrievable rows per query
PAGE_SIZE = 500
PARTITION_BUDGET = 9_500  # keep partitions safely under the cap
DIR_URL_CHAR_BUDGET = 1_500  # keep comma-joined directories= params URL-safe


def _sonar_get(url: str, params: dict) -> dict | None:
    auth = (CONFIG["sonar_token"], "")
    try:
        res = requests.get(url, auth=auth, params=params, timeout=30)
        if res.status_code == 200:
            return res.json()
        logger.warning(f"Sonar API {res.status_code} for {url} params={params}: {res.text[:200]}")
    except Exception as e:
        logger.warning(f"Sonar API request failed for {url}: {e}")
    return None


def _search_total(full_key: str, params: dict) -> int:
    """Probe a query's total row count (1 request, ps=1)."""
    data = _sonar_get(f"{CONFIG['sonar_url']}/api/issues/search",
                      {"componentKeys": full_key, "ps": 1, **params})
    return int(data.get("total", 0)) if data else 0


def _fetch_pages(full_key: str, params: dict, out: dict) -> None:
    """Plain paging for a query expected to fit under the API cap."""
    url = f"{CONFIG['sonar_url']}/api/issues/search"
    page, fetched = 1, 0
    while page * PAGE_SIZE <= API_CAP:
        data = _sonar_get(url, {"componentKeys": full_key, "ps": PAGE_SIZE, "p": page, **params})
        if data is None:
            break
        issues = data.get("issues", [])
        for issue in issues:
            out[issue["key"]] = issue          # dedupe across partitions
        fetched += len(issues)
        if len(issues) < PAGE_SIZE:
            break
        page += 1
    if fetched >= API_CAP:
        logger.warning(f"Query hit the {API_CAP} API cap — results truncated for params={params}")


def _all_directories(full_key: str) -> list:
    """All directory paths of the project (api/components/tree, DIR qualifier)."""
    url = f"{CONFIG['sonar_url']}/api/components/tree"
    dirs, page = [], 1
    while page * PAGE_SIZE <= API_CAP:
        data = _sonar_get(url, {"component": full_key, "qualifiers": "DIR",
                                "ps": PAGE_SIZE, "p": page})
        if data is None:
            break
        comps = data.get("components", [])
        dirs.extend(c.get("path") or c.get("name") for c in comps)
        if len(comps) < PAGE_SIZE:
            break
        page += 1
    return [d for d in dirs if d]


def _fetch_by_files_in_dir(full_key: str, params: dict, directory: str, out: dict) -> None:
    """Pathological case: one directory alone exceeds the partition budget.
    Fetch its files from the component tree and query per file."""
    logger.warning(f"Directory '{directory}' exceeds the partition budget — fetching per file")
    url = f"{CONFIG['sonar_url']}/api/components/tree"
    page = 1
    while page * PAGE_SIZE <= API_CAP:
        data = _sonar_get(url, {"component": full_key, "qualifiers": "FIL",
                                "ps": PAGE_SIZE, "p": page})
        if data is None:
            break
        comps = data.get("components", [])
        for comp in comps:
            path = comp.get("path") or ""
            if os.path.dirname(path) == directory:
                # params comes last in _fetch_pages' dict build, so this
                # componentKeys (the file key) overrides the project key
                _fetch_pages(full_key, {**params, "componentKeys": comp["key"]}, out)
        if len(comps) < PAGE_SIZE:
            break
        page += 1


def _fetch_by_directories(full_key: str, params: dict, out: dict) -> None:
    """Split an oversized partition by directories, batched so every batch
    stays under PARTITION_BUDGET issues (counts from the `directories` facet;
    directories beyond the facet's top-100 are bounded by the smallest facet
    count, since the facet is sorted by count)."""
    facet_data = _sonar_get(f"{CONFIG['sonar_url']}/api/issues/search",
                            {"componentKeys": full_key, "ps": 1,
                             "facets": "directories", **params})
    facet_counts = {}
    if facet_data:
        for facet in facet_data.get("facets", []):
            if facet.get("property") == "directories":
                facet_counts = {v["val"]: int(v["count"]) for v in facet.get("values", [])}

    all_dirs = _all_directories(full_key)
    known = [(d, facet_counts[d]) for d in all_dirs if d in facet_counts]
    unknown = [d for d in all_dirs if d not in facet_counts]
    if len(facet_counts) >= 100:
        # facet truncated at 100 values: unseen dirs can hold up to the
        # smallest facet count each
        max_unknown = min(facet_counts.values())
    elif facet_counts:
        max_unknown = 0   # facet covered every non-empty directory
    else:
        # facet request failed — assume a conservative bound and rely on the
        # completeness check in _fetch_partition to flag any shortfall
        max_unknown = 200

    batches, batch, batch_n, batch_chars = [], [], 0, 0

    def flush():
        nonlocal batch, batch_n, batch_chars
        if batch:
            batches.append(list(batch))
        batch, batch_n, batch_chars = [], 0, 0

    for d, n in known:
        if n > PARTITION_BUDGET:
            _fetch_by_files_in_dir(full_key, params, d, out)
            continue
        if (batch_n + n > PARTITION_BUDGET) or (batch_chars + len(d) + 1 > DIR_URL_CHAR_BUDGET):
            flush()
        batch.append(d); batch_n += n; batch_chars += len(d) + 1
    flush()

    if max_unknown > 0:
        for d in unknown:
            if (batch_n + max_unknown > PARTITION_BUDGET) or (batch_chars + len(d) + 1 > DIR_URL_CHAR_BUDGET):
                flush()
            batch.append(d); batch_n += max_unknown; batch_chars += len(d) + 1
        flush()

    for b in batches:
        _fetch_pages(full_key, {**params, "directories": ",".join(b)}, out)


def _fetch_partition(full_key: str, params: dict, out: dict, depth: int = 0) -> None:
    """Fetch one filter partition, recursing into finer partitions when its
    total exceeds the API cap. Verifies completeness afterwards."""
    total = _search_total(full_key, params)
    if total == 0:
        return
    n_before = len(out)

    if total <= API_CAP:
        _fetch_pages(full_key, params, out)
    elif depth == 0:
        logger.info(f"  partition of {total} rows exceeds the {API_CAP} cap — splitting by severity ({params})")
        for sev in ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]:
            _fetch_partition(full_key, {**params, "severities": sev}, out, depth + 1)
        return   # sub-partitions verified themselves
    else:
        logger.info(f"  partition of {total} rows exceeds the {API_CAP} cap — splitting by directories ({params})")
        _fetch_by_directories(full_key, params, out)

    n_added = len(out) - n_before
    if n_added < total:
        logger.warning(
            f"Partition shortfall: API reports {total} issues but {n_added} were "
            f"collected (params={params}) — some issues may be missing"
        )


def fetch_issues(project_key: str, **filters) -> list:
    """Fetch ALL issues matching the filters, complete beyond the 10k
    api/issues/search cap (recursive query partitioning, deduped by key)."""
    full_key = full_component_key(project_key)
    out: dict = {}
    for issue_type in ["BUG", "VULNERABILITY", "CODE_SMELL"]:
        _fetch_partition(full_key, {"types": issue_type, **filters}, out)
    logger.info(f"fetch_issues: {len(out)} unique issues for {full_key} filters={filters}")
    return list(out.values())

#PRE-BUILD STEP FOR HADOOP

def get_required_thirdparty_version(repo_path: str) -> str | None:
    pom_path = os.path.join(repo_path, "pom.xml")
    if not os.path.exists(pom_path):
        return None
    try:
        with open(pom_path, "r", encoding="utf-8") as f:
            content = f.read()
        import re
        # Try both common property names Hadoop uses
        for pattern in [
            r"<hadoop-thirdparty\.version>([\d.\-A-Z]+)</hadoop-thirdparty\.version>",
            r"<hadoop\.thirdparty\.version>([\d.\-A-Z]+)</hadoop\.thirdparty\.version>",
        ]:
            match = re.search(pattern, content)
            if match:
                base = match.group(1).replace("-SNAPSHOT", "")
                tag = f"rel/release-{base}"
                logger.info(f"  pom.xml requires hadoop-thirdparty: {match.group(1)} → {tag}")
                return tag

        # Also check the hadoop-common submodule pom directly
        common_pom = os.path.join(repo_path, "hadoop-common-project", "hadoop-common", "pom.xml")
        if os.path.exists(common_pom):
            with open(common_pom, "r", encoding="utf-8") as f:
                content = f.read()
            if "hadoop-shaded-protobuf" in content or "hadoop-thirdparty" in content:
                # Dependency exists but version inherited from parent — use root pom version
                logger.warning("  hadoop-thirdparty dependency found in submodule but version not in root pom")

        return None
    except Exception as e:
        logger.warning(f"  Could not parse pom.xml: {e}")
        return None

def build_hadoop_thirdparty(repo_path: str, toolchain: dict) -> bool:
    tag = get_required_thirdparty_version(repo_path)

    if tag is None:
        logger.info("  No hadoop-thirdparty needed — skipping")
        return True

    logger.info(f"  Building hadoop-thirdparty @ {tag}...")

    thirdparty_path = os.path.join(os.path.dirname(repo_path), "hadoop-thirdparty")

    if os.path.isdir(thirdparty_path):
        shutil.rmtree(thirdparty_path)

    result = subprocess.run(
        ["git", "clone", "--depth=1", "--branch", tag,
         "https://github.com/apache/hadoop-thirdparty.git", thirdparty_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        logger.error(f"✗ Failed to clone hadoop-thirdparty @ {tag}: {stderr}")
        return False

    java_home = CONFIG["java_homes"].get(toolchain["java_major"])
    env = os.environ.copy()
    env["JAVA_HOME"] = java_home
    env["PATH"] = f"{java_home}/bin:{env['PATH']}"

    result = subprocess.run(
        ["mvn", "install", "-DskipTests",
         "--batch-mode", "--no-transfer-progress"],
        cwd=thirdparty_path, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=600
    )

    if result.returncode == 0:
        logger.info(f"✓ hadoop-thirdparty @ {tag} installed to local Maven cache")
        return True

    logger.error("✗ hadoop-thirdparty build failed")
    logger.error(result.stdout.decode("utf-8", errors="replace")[-2000:])
    return False

# ============================================================================
# MAIN ANALYSIS LOOP
# ============================================================================
def main():
    logger.info(f"Starting analysis: {PROJECT_NAME} batch {BATCH_NUMBER}")
    logger.info(f"Checkpoints directory: {CHECKPOINTS_DIR}")

    # Ensure the per-project checkpoint release exists, then cache its asset
    # list so restore_from_checkpoint() can resume issues from prior runs.
    ensure_release_exists()
    refresh_release_assets()

    # Load the persisted progress map once at startup
    progress = load_progress()

    with open(CONFIG["jira_json_path"], encoding="utf-8") as f:
        issues = json.load(f)

    batch_label = "filtered noisy batch" if os.getenv("JIRA_JSON_PATH") else f"batch {BATCH_NUMBER}"
    logger.info(f"Loaded {len(issues)} issues from {batch_label}")

    if not issues:
        logger.info("No issues matched the noisy-report filter; skipping builds and scans.")

    
    successes, failures, restored = [], [], []
    
    for idx, (issue_id, item) in enumerate(issues.items(), 1):
        logger.info(f"\n{'='*70}")
        logger.info(f"[{idx}/{len(issues)}] {issue_id}")
        logger.info("="*70)

        # ── CHECKPOINT CHECK ──────────────────────────────────────────────
        # If the issue was already processed in a previous run (success OR
        # failure), restore its files into output/ and skip re-analysis.
        if restore_from_checkpoint(issue_id):
            logger.info(f"  ↩ Skipping — already checkpointed")
            restored.append(issue_id)
            successes.append(issue_id)  # restored = previously successful by definition
            continue
        # ─────────────────────────────────────────────────────────────────

        # Paths for this issue's artefacts (inside the runner's output/)
        report_path = os.path.join(CONFIG["output_dir"], f"{issue_id}_report.json")
        log_file    = os.path.join(CONFIG["output_dir"], f"{issue_id}_build.log")

        try:
            sha_before = item["sha_before"]
            sha_after  = item["sha_after"]
            
            before_year = year_from_iso(item["commits"][-1].get("date", ""))
            after_year  = year_from_iso(item["commits"][0].get("date", ""))
            
            before_toolchain = get_toolchain(before_year)
            after_toolchain  = get_toolchain(after_year)

            # Java version resolution priority:
            #   1. Per-run enforcement (FORCE_JAVA_VERSION workflow input): hard
            #      override — disables both the year toolchain and pom.xml detection.
            #   2. Per-project force_java_version (project_configs.json): a starting
            #      hint that pom.xml auto-detection below may still adjust.
            #   3. Automatic: year-based toolchain + pom.xml detection.
            enforce_java = CONFIG["force_java_version"]
            if enforce_java:
                apply_forced_java(before_toolchain, enforce_java)
                apply_forced_java(after_toolchain, enforce_java)
            elif "force_java_version" in PROJECT_CONFIG:
                forced_java = PROJECT_CONFIG["force_java_version"]
                apply_forced_java(before_toolchain, forced_java)
                apply_forced_java(after_toolchain, forced_java)
                
            project_key = f"{PROJECT_NAME}:{issue_id}"
            # Fail fast: a scan against a missing project only errors out
            # after the build, with a misleading "project not found" message.
            if not create_public_project(project_key):
                raise RuntimeError("SonarCloud project creation failed")

            # ── BEFORE scan ───────────────────────────────────────────────
            logger.info("\n▶ BEFORE SCAN")
            if not git_checkout(CONFIG["repo_path"], sha_before):
                raise RuntimeError("BEFORE git checkout failed")
            apply_project_compat_patches(CONFIG["repo_path"])

            # Detect JDK from this specific checkout (skipped when a version is
            # enforced for this run via the FORCE_JAVA_VERSION workflow input).
            if enforce_java:
                logger.info(f"  ⚙ BEFORE JDK enforced to Java {enforce_java} (pom.xml auto-detection disabled)")
            else:
                detected = detect_required_java(CONFIG["repo_path"])
                if detected:
                    jdk_order = ["8", "11", "17"]
                    if jdk_order.index(before_toolchain["java_major"]) < jdk_order.index(detected):
                        logger.info(f"  ↑ BEFORE JDK raised {before_toolchain['java_major']} → {detected} (from pom.xml)")
                        before_toolchain["java_major"] = detected
                    elif jdk_order.index(before_toolchain["java_major"]) > jdk_order.index(detected):
                        logger.info(f"  ↓ BEFORE JDK lowered {before_toolchain['java_major']} → {detected} (from pom.xml)")
                        before_toolchain["java_major"] = detected

            build_system = get_build_system(
                CONFIG["build_system"], CONFIG["repo_path"], before_toolchain, log_file
            )
            if not build_system.build():
                raise RuntimeError("BEFORE build failed")
            
            if not scan_with_retry(CONFIG["repo_path"], project_key, build_system, "BEFORE"):
                raise RuntimeError("BEFORE sonar scan/task failed")
            
            before_metrics  = get_measures(project_key)
            baseline_issues = fetch_issues(project_key, statuses="OPEN,CONFIRMED,REOPENED")
            scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
            
            # ── AFTER scan ────────────────────────────────────────────────
            logger.info("\n▶ AFTER SCAN")
            if not git_checkout(CONFIG["repo_path"], sha_after):
                raise RuntimeError("AFTER git checkout failed")
            apply_project_compat_patches(CONFIG["repo_path"])

            if enforce_java:
                logger.info(f"  ⚙ AFTER JDK enforced to Java {enforce_java} (pom.xml auto-detection disabled)")
            else:
                detected = detect_required_java(CONFIG["repo_path"])
                if detected:
                    jdk_order = ["8", "11", "17"]
                    if jdk_order.index(after_toolchain["java_major"]) < jdk_order.index(detected):
                        logger.info(f"  ↑ AFTER JDK raised {after_toolchain['java_major']} → {detected} (from pom.xml)")
                        after_toolchain["java_major"] = detected
                    elif jdk_order.index(after_toolchain["java_major"]) > jdk_order.index(detected):
                        logger.info(f"  ↓ AFTER JDK lowered {after_toolchain['java_major']} → {detected} (from pom.xml)")
                        after_toolchain["java_major"] = detected
            
            build_system = get_build_system(
                CONFIG["build_system"], CONFIG["repo_path"], after_toolchain, log_file
            )
            if not build_system.build():
                raise RuntimeError("AFTER build failed")
            
            if not scan_with_retry(CONFIG["repo_path"], project_key, build_system, "AFTER"):
                raise RuntimeError("AFTER sonar scan/task failed")
            
            after_metrics = get_measures(project_key)
            
            # FIXED: Changed updated_after to updatedAfter and created_after to createdAfter
            fixed_issues = fetch_issues(project_key, statuses="CLOSED", 
                                       resolutions="FIXED", updatedAfter=scan_time)
            new_issues   = fetch_issues(project_key, statuses="OPEN,CONFIRMED,REOPENED",
                                        createdAfter=scan_time)
            
            # ── Save report ───────────────────────────────────────────────
            report = {
                "issue_id":   issue_id,
                "project":    PROJECT_NAME,
                "sha_before": sha_before,
                "sha_after":  sha_after,
                "build_system": CONFIG["build_system"],
                "metrics_comparison": {
                    "before": before_metrics,
                    "after":  after_metrics,
                },
                "issues": {
                    "baseline_count": len(baseline_issues),
                    "fixed_count":    len(fixed_issues),
                    "new_count":      len(new_issues),
                    "baseline_issues": baseline_issues,
                    "fixed_issues":    fixed_issues,
                    "new_issues":      new_issues,
                },
            }
            
            with open(report_path, "w") as f:
                json.dump(report, f, indent=2)
            
            logger.info(f"✓ SUCCESS: Fixed={len(fixed_issues)}, New={len(new_issues)}")

            # ── CHECKPOINT (success) ──────────────────────────────────────
            checkpoint_issue(issue_id, "success", progress,
                             report_path=report_path, log_path=log_file)
            successes.append(issue_id)
            
        except Exception as e:
            logger.error(f"✗ Error processing {issue_id}: {e}")
            traceback.print_exc()

            # ── CHECKPOINT (failure) ──────────────────────────────────────
            # Always checkpoint failures too — this prevents wasting time
            # re-attempting them on the next run.  Remove the entry from
            # checkpoints/{PROJECT}_progress.json manually if you want to
            # retry a specific failed issue.
            # Same name sonar_scan() uses: sonar_scan_<PROJECT>_<issue>.log
            scan_log = os.path.join(CONFIG["output_dir"],
                                    f"sonar_scan_{PROJECT_NAME}_{issue_id}.log")
            checkpoint_issue(issue_id, "failed", progress,
                             report_path=None, log_path=log_file,
                             scan_log_path=scan_log)
            failures.append(issue_id)

    # ── Final summary ─────────────────────────────────────────────────────
    logger.info("\n" + "="*70)
    logger.info("ANALYSIS COMPLETE")
    logger.info("="*70)
    logger.info(f"  ✓ Successful  : {len(successes)}")
    logger.info(f"  ✗ Failed      : {len(failures)}")
    logger.info(f"  ↩ Restored    : {len(restored)} (skipped — already checkpointed)")
    if failures:
        failed_new = [i for i in failures if i not in restored]
        if failed_new:
            logger.info(f"\n  Newly failed issues: {failed_new}")


if __name__ == "__main__":
    main()
