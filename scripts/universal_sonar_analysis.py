"""
universal_sonar_analysis.py - Multi-project, Multi-build-system SonarCloud Analysis
Supports: Maven and Gradle projects
Checkpoint system: persists per-issue results to the scripts repo so that
interrupted batches can be resumed without re-analyzing already-seen issues.
"""

import os
import json
import shutil
import subprocess
import time
import requests
import logging
import traceback
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
#   <SCRIPTS_REPO_PATH>/checkpoints/      ← persisted reports, logs, progress JSON
SCRIPTS_REPO_PATH = os.getenv("SCRIPTS_REPO_PATH", os.path.join(os.path.dirname(__file__), ".."))

# Load project-specific configuration
with open(os.path.join(SCRIPTS_REPO_PATH, "scripts", "project_configs.json")) as f:
    PROJECT_CONFIGS = json.load(f)

PROJECT_CONFIG = PROJECT_CONFIGS.get(PROJECT_NAME, {})

CONFIG = {
    "project_name": PROJECT_NAME,
    "jira_json_path": os.path.join(
        SCRIPTS_REPO_PATH, "scripts",
        f"{PROJECT_NAME}_issues_batch_{BATCH_NUMBER}.json"
    ),
    "repo_path": os.getenv("PROJECT_REPO_PATH", "."),
    "output_dir": "output",
    
    "sonar_url": "https://sonarcloud.io",
    "sonar_token": os.getenv("SONAR_TOKEN"),
    "sonar_organization": os.getenv("SONAR_ORGANIZATION"),
    
    "build_system": PROJECT_CONFIG.get("build_system", "maven"),
    "sonar_exclusions": PROJECT_CONFIG.get("sonar_exclusions", []),
    "maven_skip_flags": PROJECT_CONFIG.get("maven_skip_flags", []),
    "gradle_tasks": PROJECT_CONFIG.get("gradle_tasks", ["clean", "build"]),
    "gradle_skip_flags": PROJECT_CONFIG.get("gradle_skip_flags", []),
    
    "java_homes": {
        "8":  os.getenv("JAVA_HOME_8_X64"),
        "11": os.getenv("JAVA_HOME_11_X64"),
        "17": os.getenv("JAVA_HOME_17_X64"),
    },
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
logger.info(f"Universal SonarCloud Analysis - {PROJECT_NAME}")
logger.info(f"Build System: {CONFIG['build_system']}")
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
                     report_path: str = None, log_path: str = None):
    """
    Persist an issue's artefacts to checkpoints/ and push to the remote repo.

    Args:
        issue_id:    e.g. "FLINK-12345"
        status:      "success" or "failed"
        progress:    in-memory progress dict (mutated in-place then saved)
        report_path: absolute path to the JSON report (None if the issue failed
                     before a report was created)
        log_path:    absolute path to the build/scan log
    """
    logger.info(f"  Checkpointing {issue_id} ({status})...")
    committed_files = []

    # --- Copy report (success only) -----------------------------------------
    if report_path and os.path.exists(report_path):
        dest = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_report.json")
        shutil.copy2(report_path, dest)
        committed_files.append(dest)
        logger.info(f"    ✓ Saved report  → checkpoints/{issue_id}_report.json")

    # --- Copy build log (always, if present) --------------------------------
    if log_path and os.path.exists(log_path):
        dest = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_build.log")
        shutil.copy2(log_path, dest)
        committed_files.append(dest)
        logger.info(f"    ✓ Saved log     → checkpoints/{issue_id}_build.log")

    # --- Update progress JSON -----------------------------------------------
    progress[issue_id] = {
        "status":    status,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "batch":     BATCH_NUMBER,
    }
    save_progress(progress)
    committed_files.append(PROGRESS_FILE)

    # --- Commit and push to the remote repo ---------------------------------
    _git_push_checkpoints(issue_id, status, committed_files)


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
                ["git", "pull", "--rebase", "-X", "theirs", "origin", "master"],
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
                ["git", "push", "origin", "HEAD:refs/heads/master"],
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


def restore_from_checkpoint(issue_id: str, progress: dict) -> bool:
    """
    If the issue already has a checkpoint entry, copy its files from
    checkpoints/ into the run's output/ directory and return True so
    the main loop can skip re-analysis.

    Returns False when the issue has not been checkpointed yet.
    """
    if issue_id not in progress:
        return False

    entry  = progress[issue_id]
    status = entry["status"]
    logger.info(
        f"  ↩ Already checkpointed — status={status}, "
        f"batch={entry.get('batch')}, ts={entry.get('timestamp')}"
    )

    # Restore report
    src = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_report.json")
    if os.path.exists(src):
        dst = os.path.join(CONFIG["output_dir"], f"{issue_id}_report.json")
        shutil.copy2(src, dst)
        logger.info(f"    ✓ Restored report → output/{issue_id}_report.json")

    # Restore build log
    src = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_build.log")
    if os.path.exists(src):
        dst = os.path.join(CONFIG["output_dir"], f"{issue_id}_build.log")
        shutil.copy2(src, dst)
        logger.info(f"    ✓ Restored log    → output/{issue_id}_build.log")

    return True


# ============================================================================
# VERSION SELECTION
# ============================================================================
def get_toolchain(year: int) -> dict:
    """Returns appropriate Java/build tool versions for the year"""
    if year <= 2017:
        return {"java_major": "8", "maven": "3.0.5", "gradle": "4.10", "java_source": "1.8"}
    elif year <= 2019:
        return {"java_major": "8", "maven": "3.5.4", "gradle": "5.6", "java_source": "1.8"}
    elif year <= 2021:
        return {"java_major": "8", "maven": "3.8.1", "gradle": "6.9", "java_source": "1.8"}
    elif year <= 2023:
        return {"java_major": "11", "maven": "3.8.6", "gradle": "7.6", "java_source": "11"}
    elif year <= 2024:
        return {"java_major": "11", "maven": "3.9.9", "gradle": "8.5", "java_source": "11"}
    else:
        return {"java_major": "17", "maven": "3.9.9", "gradle": "8.7", "java_source": "17"}


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
        
        cmd = [
            "mvn", "clean", "install",
            "-DskipTests",
            "-Dmaven.javadoc.skip=true",
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
        
        # Use gradlew if it exists, otherwise gradle
        gradle_cmd = "./gradlew" if os.path.exists(os.path.join(self.repo_path, "gradlew")) else "gradle"
        
        cmd = [gradle_cmd] + CONFIG["gradle_tasks"] + CONFIG["gradle_skip_flags"]
        
        return self._execute_build(cmd, env)
    
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
def create_public_project(project_key: str):
    full_key = f"{CONFIG['sonar_organization']}_{project_key}"
    url = f"{CONFIG['sonar_url']}/api/projects/create"
    auth = (CONFIG["sonar_token"], "")
    
    data = {
        "organization": CONFIG["sonar_organization"],
        "project": full_key,
        "name": project_key,
        "visibility": "public"
    }
    
    try:
        res = requests.post(url, auth=auth, data=data, timeout=30)
        if res.status_code == 200:
            logger.info(f"✓ Created public project: {full_key}")
            return True
        elif "already exists" in res.text.lower():
            logger.info(f"✓ Project exists: {full_key}")
            return True
        else:
            logger.error(f"✗ Failed to create project: {res.text}")
            return False
    except Exception as e:
        logger.error(f"✗ Error creating project: {e}")
        return False


def sonar_scan(repo_path: str, project_key: str, build_system: BuildSystem) -> str:
    logger.info("Starting SonarCloud scan...")
    
    sources = build_system.get_source_dirs()
    binaries = build_system.get_binary_dirs()
    
    sources_str = ",".join(sources) if sources else "."
    binaries_str = ",".join(binaries) if binaries else "."
    
    full_project_key = f"{CONFIG['sonar_organization']}_{project_key}"
    
    exclusions = ",".join(CONFIG["sonar_exclusions"]) if CONFIG["sonar_exclusions"] else ""
    
    # ADDED: sonar.organization is now explicitly passed to the scanner
    props_content = f"""
sonar.organization={CONFIG['sonar_organization']}
sonar.projectKey={full_project_key}
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
        env["JAVA_HOME"] = CONFIG["java_homes"]["17"]
        
        process = subprocess.Popen(
            ["sonar-scanner",
             f"-Dsonar.host.url={CONFIG['sonar_url']}",
             f"-Dsonar.token={CONFIG['sonar_token']}"],
            cwd=repo_path, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8"
        )
        
        task_id = None
        error_logs = []
        
        safe_key = project_key.replace(":", "_")
        scan_log_path = os.path.join(CONFIG["output_dir"], f"sonar_scan_{safe_key}.log")
        
        with open(scan_log_path, "w", encoding="utf-8") as log_fh:
            for line in process.stdout:
                log_fh.write(line)
                
                if "task?id=" in line:
                    task_id = line.split("task?id=")[1].strip()
                
                if "ERROR" in line or "Exception" in line:
                    error_logs.append(line.strip())
        
        process.wait(timeout=3600)
        
        if process.returncode == 0 and task_id:
            logger.info(f"✓ Scan complete, task: {task_id}")
            return task_id
        else:
            logger.error(f"✗ Scan failed (Exit Code {process.returncode}). Full log at: {scan_log_path}")
            
            if error_logs:
                logger.error("--- Scanner Error Output ---")
                for err in error_logs[-15:]:
                    logger.error(f"  {err}")
                logger.error("----------------------------")
            
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
            status = resp.json()["task"]["status"]
            
            if status == "SUCCESS":
                logger.info("✓ Task succeeded")
                return True
            elif status in ("FAILED", "CANCELED"):
                logger.error(f"✗ Task {status}")
                return False
        except:
            pass
        time.sleep(10)
    
    return False


def get_measures(project_key: str) -> dict:
    full_key = f"{CONFIG['sonar_organization']}_{project_key}"
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


def fetch_issues(project_key: str, **filters) -> list:
    full_key = f"{CONFIG['sonar_organization']}_{project_key}"
    url = f"{CONFIG['sonar_url']}/api/issues/search"
    auth = (CONFIG["sonar_token"], "")
    
    all_issues = []
    for issue_type in ["BUG", "VULNERABILITY", "CODE_SMELL"]:
        page = 1
        while True:
            params = {"componentKeys": full_key, "types": issue_type, 
                     "ps": 500, "p": page, **filters}
            
            try:
                res = requests.get(url, auth=auth, params=params, timeout=30)
                if res.status_code == 200:
                    issues = res.json().get("issues", [])
                    all_issues.extend(issues)
                    if len(issues) < 500:
                        break
                    page += 1
                else:
                    break
            except:
                break
    
    return all_issues


# ============================================================================
# MAIN ANALYSIS LOOP
# ============================================================================
def main():
    logger.info(f"Starting analysis: {PROJECT_NAME} batch {BATCH_NUMBER}")
    logger.info(f"Checkpoints directory: {CHECKPOINTS_DIR}")

    # Load the persisted progress map once at startup
    progress = load_progress()

    with open(CONFIG["jira_json_path"]) as f:
        issues = json.load(f)

    logger.info(f"✓ Loaded {len(issues)} issues from batch {BATCH_NUMBER}")

    successes, failures, restored = [], [], []

    for idx, (issue_id, item) in enumerate(issues.items(), 1):
        logger.info(f"\n{'='*70}")
        logger.info(f"[{idx}/{len(issues)}] {issue_id}")
        logger.info("="*70)

        # ── CHECKPOINT CHECK ──────────────────────────────────────────────
        # If the issue was already processed in a previous run (success OR
        # failure), restore its files into output/ and skip re-analysis.
        if restore_from_checkpoint(issue_id, progress):
            status = progress[issue_id]["status"]
            logger.info(f"  ↩ Skipping — already checkpointed as '{status}'")
            restored.append(issue_id)
            (successes if status == "success" else failures).append(issue_id)
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
            
            project_key = f"{PROJECT_NAME}:{issue_id}"
            create_public_project(project_key)
            
            # ── BEFORE scan ───────────────────────────────────────────────
            logger.info("\n▶ BEFORE SCAN")
            if not git_checkout(CONFIG["repo_path"], sha_before):
                raise RuntimeError("BEFORE git checkout failed")

            build_system = get_build_system(
                CONFIG["build_system"], CONFIG["repo_path"], before_toolchain, log_file
            )
            if not build_system.build():
                raise RuntimeError("BEFORE build failed")
            
            before_task = sonar_scan(CONFIG["repo_path"], project_key, build_system)
            if not before_task or not wait_for_task(before_task):
                raise RuntimeError("BEFORE sonar scan/task failed")
            
            before_metrics  = get_measures(project_key)
            baseline_issues = fetch_issues(project_key, statuses="OPEN,CONFIRMED,REOPENED")
            scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
            
            # ── AFTER scan ────────────────────────────────────────────────
            logger.info("\n▶ AFTER SCAN")
            if not git_checkout(CONFIG["repo_path"], sha_after):
                raise RuntimeError("AFTER git checkout failed")
            
            build_system = get_build_system(
                CONFIG["build_system"], CONFIG["repo_path"], after_toolchain, log_file
            )
            if not build_system.build():
                raise RuntimeError("AFTER build failed")
            
            after_task = sonar_scan(CONFIG["repo_path"], project_key, build_system)
            if not after_task or not wait_for_task(after_task):
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
            checkpoint_issue(issue_id, "failed", progress,
                             report_path=None, log_path=log_file)
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
