"""
universal_sonar_analysis.py - Multi-project, Multi-build-system SonarCloud Analysis
With checkpoint support: resumes interrupted batches without re-analyzing completed issues.
"""

import os
import json
import subprocess
import time
import shutil
import requests
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ============================================================================
# CONFIGURATION
# ============================================================================
PROJECT_NAME   = os.getenv("PROJECT_NAME", "flink")
BATCH_NUMBER   = os.getenv("BATCH_NUMBER", "1")
SCRIPTS_PATH   = os.getenv("SCRIPTS_REPO_PATH", "../scripts")
CHECKPOINTS_DIR = os.path.join(SCRIPTS_PATH, "checkpoints")
PROGRESS_FILE  = os.path.join(CHECKPOINTS_DIR, f"{PROJECT_NAME}_progress.json")

with open(os.path.join(SCRIPTS_PATH, "project_configs.json")) as f:
    PROJECT_CONFIGS = json.load(f)

PROJECT_CONFIG = PROJECT_CONFIGS.get(PROJECT_NAME, {})

CONFIG = {
    "project_name":   PROJECT_NAME,
    "jira_json_path": f"../{PROJECT_NAME}_issues_batch_{BATCH_NUMBER}.json",
    "repo_path":      os.getenv("PROJECT_REPO_PATH", "."),
    "output_dir":     "output",

    "sonar_url":          "https://sonarcloud.io",
    "sonar_token":        os.getenv("SONAR_TOKEN"),
    "sonar_organization": os.getenv("SONAR_ORGANIZATION"),

    "build_system":     PROJECT_CONFIG.get("build_system", "maven"),
    "sonar_exclusions": PROJECT_CONFIG.get("sonar_exclusions", []),
    "maven_skip_flags": PROJECT_CONFIG.get("maven_skip_flags", []),
    "gradle_tasks":     PROJECT_CONFIG.get("gradle_tasks", ["clean", "build"]),
    "gradle_skip_flags":PROJECT_CONFIG.get("gradle_skip_flags", ["-x", "test"]),

    "java_homes": {
        "8":  os.getenv("JAVA_HOME_8_X64"),
        "11": os.getenv("JAVA_HOME_11_X64"),
        "17": os.getenv("JAVA_HOME_17_X64"),
    },
}

os.makedirs(CONFIG["output_dir"], exist_ok=True)
os.makedirs(CHECKPOINTS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(CONFIG["output_dir"], "analysis.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ============================================================================
# CHECKPOINT SYSTEM
# ============================================================================

def load_progress() -> dict:
    """
    Load the progress JSON from the checkpoints directory.

    Schema:
    {
        "FLINK-12345": {
            "status": "success" | "failed",
            "timestamp": "2026-03-29T22:40:40Z",
            "batch": "3"
        },
        ...
    }
    """
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"✓ Loaded progress file: {len(data)} issues tracked")
        return data
    else:
        logger.info("No existing progress file — starting fresh")
        return {}


def save_progress(progress: dict):
    """Persist the progress dict to the checkpoints directory."""
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)


def checkpoint_issue(issue_id: str, status: str, progress: dict,
                     report_path: str = None, log_path: str = None):
    """
    Save an issue's files to the checkpoints directory and push to the repo.

    Args:
        issue_id:    e.g. "FLINK-12345"
        status:      "success" or "failed"
        progress:    the in-memory progress dict (will be mutated + saved)
        report_path: path to the report JSON (None if failed before report was created)
        log_path:    path to the build/scan log
    """
    logger.info(f"  Saving checkpoint for {issue_id} ({status})...")

    committed_files = []

    # Copy report to checkpoints (only exists on success)
    if report_path and os.path.exists(report_path):
        dest = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_report.json")
        shutil.copy2(report_path, dest)
        committed_files.append(dest)
        logger.info(f"    ✓ Saved report → checkpoints/{issue_id}_report.json")

    # Copy build log to checkpoints (always, if it exists)
    if log_path and os.path.exists(log_path):
        dest = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_build.log")
        shutil.copy2(log_path, dest)
        committed_files.append(dest)
        logger.info(f"    ✓ Saved log    → checkpoints/{issue_id}_build.log")

    # Update and save progress JSON
    progress[issue_id] = {
        "status":    status,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "batch":     BATCH_NUMBER,
    }
    save_progress(progress)
    committed_files.append(PROGRESS_FILE)

    # Git commit + push to persist across workflow runs
    _git_push_checkpoints(issue_id, status, committed_files)


def _git_push_checkpoints(issue_id: str, status: str, files: list):
    """
    Stage, commit, and push checkpoint files back to the scripts repository.
    Retries up to 3 times to handle concurrent push conflicts.
    """
    for attempt in range(1, 4):
        try:
            # Stage only the checkpoint files
            for f in files:
                subprocess.run(
                    ["git", "add", f],
                    cwd=SCRIPTS_PATH, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )

            # Check if there's anything to commit
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=SCRIPTS_PATH
            )
            if result.returncode == 0:
                logger.info("    ✓ No changes to commit (already up to date)")
                return

            # Commit
            subprocess.run(
                ["git", "commit", "-m",
                 f"checkpoint({PROJECT_NAME}): {issue_id} [{status}] batch {BATCH_NUMBER}"],
                cwd=SCRIPTS_PATH, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            # Pull with rebase to handle any remote changes
            subprocess.run(
                ["git", "pull", "--rebase", "origin", "HEAD"],
                cwd=SCRIPTS_PATH,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            # Push
            subprocess.run(
                ["git", "push", "origin", "HEAD"],
                cwd=SCRIPTS_PATH, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            logger.info(f"    ✓ Checkpoint committed and pushed (attempt {attempt})")
            return

        except subprocess.CalledProcessError as e:
            logger.warning(f"    ⚠ Git push failed (attempt {attempt}/3): {e}")
            if attempt < 3:
                time.sleep(5 * attempt)

    logger.error("    ✗ Failed to push checkpoint after 3 attempts — files saved locally only")


def restore_from_checkpoint(issue_id: str, progress: dict) -> bool:
    """
    If issue is already in progress, copy its files from checkpoints to output
    and return True (so the main loop can skip analysis).
    Returns False if the issue has not been checkpointed yet.
    """
    if issue_id not in progress:
        return False

    entry   = progress[issue_id]
    status  = entry["status"]
    logger.info(f"  Found in checkpoints (status={status}, batch={entry.get('batch')}, "
                f"ts={entry.get('timestamp')})")

    # Copy report if it exists
    report_src = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_report.json")
    if os.path.exists(report_src):
        report_dst = os.path.join(CONFIG["output_dir"], f"{issue_id}_report.json")
        shutil.copy2(report_src, report_dst)
        logger.info(f"    ✓ Restored report  → output/{issue_id}_report.json")

    # Copy log if it exists
    log_src = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_build.log")
    if os.path.exists(log_src):
        log_dst = os.path.join(CONFIG["output_dir"], f"{issue_id}_build.log")
        shutil.copy2(log_src, log_dst)
        logger.info(f"    ✓ Restored log     → output/{issue_id}_build.log")

    return True


# ============================================================================
# VERSION SELECTION
# ============================================================================
def get_toolchain(year: int) -> dict:
    if year <= 2017:
        return {"java_major": "8",  "java_source": "1.8"}
    elif year <= 2019:
        return {"java_major": "8",  "java_source": "1.8"}
    elif year <= 2021:
        return {"java_major": "8",  "java_source": "1.8"}
    elif year <= 2023:
        return {"java_major": "11", "java_source": "11"}
    elif year <= 2024:
        return {"java_major": "11", "java_source": "11"}
    else:
        return {"java_major": "17", "java_source": "17"}


def year_from_iso(date_str: str) -> int:
    try:
        return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").year
    except Exception:
        return datetime.now(timezone.utc).year


# ============================================================================
# GIT HELPERS
# ============================================================================
def git_checkout(repo_path: str, sha: str) -> bool:
    logger.info(f"Git checkout: {sha[:10]}")
    try:
        subprocess.run(["git", "checkout", "--force", sha],
                       cwd=repo_path, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        subprocess.run(["git", "clean", "-fd"],
                       cwd=repo_path, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        logger.info(f"✓ Checked out {sha[:10]}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"✗ Git checkout failed: {e.stderr.decode()[:300]}")
        return False


# ============================================================================
# BUILD SYSTEM ABSTRACTION
# ============================================================================
class BuildSystem:
    def __init__(self, repo_path, toolchain, log_file):
        self.repo_path = repo_path
        self.toolchain = toolchain
        self.log_file  = log_file
        self.java_home = CONFIG["java_homes"].get(toolchain["java_major"])

    def build(self) -> bool:
        raise NotImplementedError

    def get_source_dirs(self) -> list:
        raise NotImplementedError

    def get_binary_dirs(self) -> list:
        raise NotImplementedError

    def _run_build(self, cmd: list, env: dict) -> bool:
        try:
            with open(self.log_file, "w", encoding="utf-8") as lf:
                proc = subprocess.Popen(cmd, cwd=self.repo_path, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        encoding="utf-8", errors="replace")
                for line in proc.stdout:
                    lf.write(line)
                    if any(k in line.lower() for k in ["building", "task :", "error", "failure"]):
                        logger.info(f"  {line.rstrip()}")
                proc.wait(timeout=2700)
            if proc.returncode == 0:
                logger.info("✓ Build succeeded")
                return True
            logger.error(f"✗ Build failed (exit {proc.returncode})")
            return False
        except subprocess.TimeoutExpired:
            logger.error("✗ Build timeout (45 min)")
            return False


class MavenBuildSystem(BuildSystem):
    def build(self) -> bool:
        logger.info(f"Maven Build — Java {self.toolchain['java_major']}")
        env = os.environ.copy()
        env["JAVA_HOME"] = self.java_home
        env["PATH"]      = f"{self.java_home}/bin:{env['PATH']}"
        env["MAVEN_OPTS"] = (
            "-Dfile.encoding=UTF-8"
            " -Dmaven.wagon.http.retryHandler.count=5"
            " -Dmaven.wagon.http.connectionTimeout=60000"
        )
        cmd = [
            "mvn", "clean", "install",
            "-DskipTests",
            "-Dmaven.javadoc.skip=true",
            "-Dcheckstyle.skip=true",
            "-Denforcer.skip=true",
            "-Drat.skip=true",
            f"-Dmaven.compiler.source={self.toolchain['java_source']}",
            f"-Dmaven.compiler.target={self.toolchain['java_source']}",
            "--batch-mode", "--no-transfer-progress",
        ] + CONFIG["maven_skip_flags"]
        return self._run_build(cmd, env)

    def get_source_dirs(self) -> list:
        return [
            os.path.relpath(root, self.repo_path)
            for root, *_ in os.walk(self.repo_path)
            if root.endswith("src/main/java")
        ]

    def get_binary_dirs(self) -> list:
        return [
            os.path.relpath(root, self.repo_path)
            for root, *_ in os.walk(self.repo_path)
            if root.endswith("target/classes") and os.path.isdir(root)
        ]


class GradleBuildSystem(BuildSystem):
    def build(self) -> bool:
        logger.info(f"Gradle Build — Java {self.toolchain['java_major']}")
        env = os.environ.copy()
        env["JAVA_HOME"] = self.java_home
        env["PATH"]      = f"{self.java_home}/bin:{env['PATH']}"
        gradle_cmd = "./gradlew" if os.path.exists(
            os.path.join(self.repo_path, "gradlew")) else "gradle"
        cmd = [gradle_cmd] + CONFIG["gradle_tasks"] + CONFIG["gradle_skip_flags"]
        return self._run_build(cmd, env)

    def get_source_dirs(self) -> list:
        sources = []
        for root, dirs, _ in os.walk(self.repo_path):
            if root.endswith("src/main/java") or root.endswith("src/main/kotlin"):
                sources.append(os.path.relpath(root, self.repo_path))
        return sources

    def get_binary_dirs(self) -> list:
        binaries = []
        for root, dirs, _ in os.walk(self.repo_path):
            if ("build/classes/java/main" in root
                    or "build/classes/kotlin/main" in root):
                binaries.append(os.path.relpath(root, self.repo_path))
        return binaries


def get_build_system(repo_path, toolchain, log_file) -> BuildSystem:
    build_type = CONFIG["build_system"]
    if build_type == "maven":
        return MavenBuildSystem(repo_path, toolchain, log_file)
    elif build_type == "gradle":
        return GradleBuildSystem(repo_path, toolchain, log_file)
    raise ValueError(f"Unsupported build system: {build_type}")


# ============================================================================
# SONARCLOUD
# ============================================================================
def create_public_project(project_key: str) -> bool:
    full_key = f"{CONFIG['sonar_organization']}_{project_key}"
    auth = (CONFIG["sonar_token"], "")
    res = requests.post(f"{CONFIG['sonar_url']}/api/projects/create",
                        auth=auth, timeout=30,
                        data={"organization": CONFIG["sonar_organization"],
                              "project": full_key, "name": project_key,
                              "visibility": "public"})
    if res.status_code == 200 or "already exists" in res.text.lower():
        logger.info(f"✓ Project ready: {full_key}")
        return True
    logger.error(f"✗ Failed to create project: {res.text}")
    return False


def delete_sonar_project(project_key: str):
    full_key = f"{CONFIG['sonar_organization']}_{project_key}"
    requests.post(f"{CONFIG['sonar_url']}/api/projects/delete",
                  auth=(CONFIG["sonar_token"], ""),
                  data={"project": full_key}, timeout=30)


def sonar_scan(repo_path: str, project_key: str, build_system: BuildSystem) -> str | None:
    logger.info("Starting SonarCloud scan...")

    sources  = ",".join(build_system.get_source_dirs()) or "."
    binaries = ",".join(build_system.get_binary_dirs()) or "."
    full_key = f"{CONFIG['sonar_organization']}_{project_key}"
    exclusions = ",".join(CONFIG["sonar_exclusions"])

    props = f"""
sonar.projectKey={full_key}
sonar.sources={sources}
sonar.java.binaries={binaries}
sonar.java.source={build_system.toolchain['java_source']}
sonar.sourceEncoding=UTF-8
sonar.scm.disabled=true
sonar.exclusions={exclusions}
sonar.cpd.skip=true
sonar.dbd.enabled=false
sonar.coverage.exclusions=**/*
"""
    props_file = os.path.join(repo_path, "sonar-project.properties")
    with open(props_file, "w") as f:
        f.write(props)

    try:
        env = os.environ.copy()
        env["JAVA_HOME"] = CONFIG["java_homes"]["17"]
        env["PATH"]      = f"{CONFIG['java_homes']['17']}/bin:{env['PATH']}"

        proc = subprocess.Popen(
            ["sonar-scanner",
             f"-Dsonar.host.url={CONFIG['sonar_url']}",
             f"-Dsonar.token={CONFIG['sonar_token']}"],
            cwd=repo_path, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8"
        )

        task_id = None
        for line in proc.stdout:
            if "task?id=" in line:
                task_id = line.split("task?id=")[1].strip()

        proc.wait(timeout=3600)

        if proc.returncode == 0 and task_id:
            logger.info(f"✓ Scan complete, task: {task_id}")
            return task_id

        logger.error("✗ Scan failed")
        return None
    finally:
        if os.path.exists(props_file):
            os.remove(props_file)


def wait_for_task(task_id: str) -> bool:
    if not task_id:
        return False
    url  = f"{CONFIG['sonar_url']}/api/ce/task?id={task_id}"
    auth = (CONFIG["sonar_token"], "")
    for _ in range(270):
        try:
            status = requests.get(url, auth=auth, timeout=10).json()["task"]["status"]
            if status == "SUCCESS":
                logger.info("✓ Task succeeded")
                return True
            if status in ("FAILED", "CANCELED"):
                logger.error(f"✗ Task {status}")
                return False
        except Exception:
            pass
        time.sleep(10)
    logger.error("✗ Task polling timed out")
    return False


def get_measures(project_key: str) -> dict:
    full_key = f"{CONFIG['sonar_organization']}_{project_key}"
    res = requests.get(f"{CONFIG['sonar_url']}/api/measures/component",
                       auth=(CONFIG["sonar_token"], ""), timeout=30,
                       params={"component": full_key,
                               "metricKeys": "ncloc,complexity,violations,sqale_index"})
    if res.status_code == 200:
        return {m["metric"]: m["value"]
                for m in res.json()["component"]["measures"]}
    return {}


def fetch_issues(project_key: str, **filters) -> list:
    full_key = f"{CONFIG['sonar_organization']}_{project_key}"
    url      = f"{CONFIG['sonar_url']}/api/issues/search"
    auth     = (CONFIG["sonar_token"], "")
    all_issues = []

    for issue_type in ["BUG", "VULNERABILITY", "CODE_SMELL"]:
        page = 1
        while True:
            params = {"componentKeys": full_key, "types": issue_type,
                      "ps": 500, "p": page, **filters}
            try:
                res = requests.get(url, auth=auth, params=params, timeout=30)
                if res.status_code != 200:
                    break
                issues = res.json().get("issues", [])
                all_issues.extend(issues)
                if len(issues) < 500:
                    break
                page += 1
            except Exception:
                break

    logger.info(f"✓ Fetched {len(all_issues)} issues")
    return all_issues


# ============================================================================
# MAIN
# ============================================================================
def main():
    logger.info("="*70)
    logger.info(f"Universal SonarCloud Analysis — {PROJECT_NAME} batch {BATCH_NUMBER}")
    logger.info(f"Build system : {CONFIG['build_system']}")
    logger.info(f"Checkpoints  : {CHECKPOINTS_DIR}")
    logger.info("="*70)

    # Load checkpoint progress at the start
    progress = load_progress()

    with open(CONFIG["jira_json_path"], encoding="utf-8") as f:
        issues = json.load(f)

    logger.info(f"✓ Loaded {len(issues)} issues from batch {BATCH_NUMBER}")

    successes, failures, restored = [], [], []

    for idx, (issue_id, item) in enumerate(issues.items(), 1):
        logger.info(f"\n{'='*70}")
        logger.info(f"[{idx}/{len(issues)}] {issue_id}")
        logger.info("="*70)

        # ── CHECKPOINT CHECK ──────────────────────────────────────────────
        if restore_from_checkpoint(issue_id, progress):
            status = progress[issue_id]["status"]
            logger.info(f"  ↩ Skipping analysis (already checkpointed as {status})")
            restored.append(issue_id)
            (successes if status == "success" else failures).append(issue_id)
            continue
        # ─────────────────────────────────────────────────────────────────

        sha_before = item.get("sha_before", "").strip()
        commits    = item.get("commits", [])

        if not sha_before or not commits:
            logger.warning("  ✗ Missing sha_before or commits — skipping")
            checkpoint_issue(issue_id, "failed", progress)
            failures.append(issue_id)
            continue

        sha_after      = commits[0]["sha"]
        before_year    = year_from_iso(commits[-1].get("date", ""))
        after_year     = year_from_iso(commits[0].get("date", ""))
        before_toolchain = get_toolchain(before_year)
        after_toolchain  = get_toolchain(after_year)

        project_key = f"{PROJECT_NAME}:{issue_id}"
        log_file    = os.path.join(CONFIG["output_dir"], f"{issue_id}_build.log")
        report_path = os.path.join(CONFIG["output_dir"], f"{issue_id}_report.json")

        try:
            delete_sonar_project(project_key)
            if not create_public_project(project_key):
                raise RuntimeError("Could not create SonarCloud project")

            # ── BEFORE scan ───────────────────────────────────────────────
            logger.info("\n▶ BEFORE SCAN")
            if not git_checkout(CONFIG["repo_path"], sha_before):
                raise RuntimeError("BEFORE git checkout failed")

            bs_before = get_build_system(CONFIG["repo_path"], before_toolchain, log_file)
            if not bs_before.build():
                raise RuntimeError("BEFORE build failed")

            before_task = sonar_scan(CONFIG["repo_path"], project_key, bs_before)
            if not before_task or not wait_for_task(before_task):
                raise RuntimeError("BEFORE sonar scan/task failed")

            before_metrics  = get_measures(project_key)
            baseline_issues = fetch_issues(project_key, statuses="OPEN,CONFIRMED,REOPENED")
            scan_time       = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
            logger.info(f"  ✓ BEFORE complete — {len(baseline_issues)} baseline issues")

            # ── AFTER scan ────────────────────────────────────────────────
            logger.info("\n▶ AFTER SCAN")
            if not git_checkout(CONFIG["repo_path"], sha_after):
                raise RuntimeError("AFTER git checkout failed")

            bs_after = get_build_system(CONFIG["repo_path"], after_toolchain, log_file)
            if not bs_after.build():
                raise RuntimeError("AFTER build failed")

            after_task = sonar_scan(CONFIG["repo_path"], project_key, bs_after)
            if not after_task or not wait_for_task(after_task):
                raise RuntimeError("AFTER sonar scan/task failed")

            after_metrics = get_measures(project_key)
            fixed_issues  = fetch_issues(project_key, statuses="CLOSED",
                                         resolutions="FIXED", updated_after=scan_time)
            new_issues    = fetch_issues(project_key, statuses="OPEN,CONFIRMED,REOPENED",
                                         created_after=scan_time)
            logger.info(f"  ✓ AFTER complete — fixed={len(fixed_issues)}, new={len(new_issues)}")

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
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)

            # ── CHECKPOINT (success) ──────────────────────────────────────
            checkpoint_issue(issue_id, "success", progress,
                             report_path=report_path, log_path=log_file)

            logger.info(f"\n  ✓ SUCCESS: {issue_id}")
            successes.append(issue_id)

        except Exception as exc:
            logger.error(f"\n  ✗ FAILED: {issue_id} — {exc}")
            logger.error(traceback.format_exc())

            # ── CHECKPOINT (failure) ──────────────────────────────────────
            checkpoint_issue(issue_id, "failed", progress,
                             report_path=None, log_path=log_file)

            failures.append(issue_id)

    # ── Final summary ─────────────────────────────────────────────────────
    logger.info("\n" + "="*70)
    logger.info("ANALYSIS COMPLETE")
    logger.info("="*70)
    logger.info(f"  ✓ Successful : {len(successes)}")
    logger.info(f"  ✗ Failed     : {len(failures)}")
    logger.info(f"  ↩ Restored   : {len(restored)} (skipped from checkpoints)")
    if failures:
        logger.info(f"\n  Failed issues: {failures}")


if __name__ == "__main__":
    main()
