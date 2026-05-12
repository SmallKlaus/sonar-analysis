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
    Check if a report already exists in checkpoints/. If so, copy
    both the report and log into output/ and return True to skip analysis.
    """
    report_src = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_report.json")

    # A report file present = issue was successfully analyzed before
    # No report = either failed or never run — analyze it
    if not os.path.exists(report_src):
        return False

    logger.info(f"  ↩ Found in checkpoints — skipping analysis")

    # Restore report
    shutil.copy2(report_src, os.path.join(CONFIG["output_dir"], f"{issue_id}_report.json"))
    logger.info(f"    ✓ Restored report → output/{issue_id}_report.json")

    # Restore log if present (best effort)
    log_src = os.path.join(CHECKPOINTS_DIR, f"{issue_id}_build.log")
    if os.path.exists(log_src):
        shutil.copy2(log_src, os.path.join(CONFIG["output_dir"], f"{issue_id}_build.log"))
        logger.info(f"    ✓ Restored log    → output/{issue_id}_build.log")

    return True


# ============================================================================
# VERSION SELECTION
# ============================================================================

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

            if "force_java_version" in PROJECT_CONFIG:
                forced_java = PROJECT_CONFIG["force_java_version"]
                before_toolchain["java_major"] = forced_java
                before_toolchain["java_source"] = forced_java
                after_toolchain["java_major"] = forced_java
                after_toolchain["java_source"] = forced_java
                
            project_key = f"{PROJECT_NAME}:{issue_id}"
            create_public_project(project_key)

            # ── BEFORE scan ───────────────────────────────────────────────
            logger.info("\n▶ BEFORE SCAN")
            if not git_checkout(CONFIG["repo_path"], sha_before):
                raise RuntimeError("BEFORE git checkout failed")
            apply_project_compat_patches(CONFIG["repo_path"])

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
            apply_project_compat_patches(CONFIG["repo_path"])
            
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
