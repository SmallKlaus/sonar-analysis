"""
universal_sonar_analysis.py - Multi-project, Multi-build-system SonarCloud Analysis
Supports: Maven and Gradle projects
"""

import os
import json
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

# Load project-specific configuration
with open("../scripts/project_configs.json") as f:
    PROJECT_CONFIGS = json.load(f)

PROJECT_CONFIG = PROJECT_CONFIGS.get(PROJECT_NAME, {})

CONFIG = {
    "project_name": PROJECT_NAME,
    "jira_json_path": f"../{PROJECT_NAME}_issues_batch_{BATCH_NUMBER}.json",
    "repo_path": os.getenv("PROJECT_REPO_PATH", "."),
    "output_dir": "../output",
    
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
        subprocess.run(
            ["git", "clean", "-fd"],
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
    
    props_content = f"""
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
        for line in process.stdout:
            if "task?id=" in line:
                task_id = line.split("task?id=")[1].strip()
        
        process.wait(timeout=3600)
        
        if process.returncode == 0 and task_id:
            logger.info(f"✓ Scan complete, task: {task_id}")
            return task_id
        else:
            logger.error("✗ Scan failed")
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
    
    with open(CONFIG["jira_json_path"]) as f:
        issues = json.load(f)
    
    for idx, (issue_id, item) in enumerate(issues.items(), 1):
        logger.info(f"\n[{idx}/{len(issues)}] Processing {issue_id}")
        
        output_path = os.path.join(CONFIG["output_dir"], f"{issue_id}_report.json")
        if os.path.exists(output_path):
            logger.info("  Already processed")
            continue
        
        try:
            sha_before = item["sha_before"]
            sha_after = item["commits"][0]["sha"]
            
            before_year = year_from_iso(item["commits"][-1].get("date", ""))
            after_year = year_from_iso(item["commits"][0].get("date", ""))
            
            before_toolchain = get_toolchain(before_year)
            after_toolchain = get_toolchain(after_year)
            
            project_key = f"{PROJECT_NAME}:{issue_id}"
            create_public_project(project_key)
            
            # BEFORE
            if not git_checkout(CONFIG["repo_path"], sha_before):
                continue
            
            log_file = os.path.join(CONFIG["output_dir"], f"{issue_id}_build.log")
            build_system = get_build_system(
                CONFIG["build_system"], 
                CONFIG["repo_path"], 
                before_toolchain, 
                log_file
            )
            
            if not build_system.build():
                continue
            
            before_task = sonar_scan(CONFIG["repo_path"], project_key, build_system)
            if not before_task or not wait_for_task(before_task):
                continue
            
            before_metrics = get_measures(project_key)
            baseline_issues = fetch_issues(project_key, statuses="OPEN,CONFIRMED,REOPENED")
            scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
            
            # AFTER
            if not git_checkout(CONFIG["repo_path"], sha_after):
                continue
            
            build_system = get_build_system(
                CONFIG["build_system"],
                CONFIG["repo_path"],
                after_toolchain,
                log_file
            )
            
            if not build_system.build():
                continue
            
            after_task = sonar_scan(CONFIG["repo_path"], project_key, build_system)
            if not after_task or not wait_for_task(after_task):
                continue
            
            after_metrics = get_measures(project_key)
            fixed_issues = fetch_issues(project_key, statuses="CLOSED", 
                                       resolutions="FIXED", updated_after=scan_time)
            new_issues = fetch_issues(project_key, statuses="OPEN,CONFIRMED,REOPENED",
                                     created_after=scan_time)
            
            # Save report
            report = {
                "issue_id": issue_id,
                "project": PROJECT_NAME,
                "sha_before": sha_before,
                "sha_after": sha_after,
                "build_system": CONFIG["build_system"],
                "metrics_comparison": {
                    "before": before_metrics,
                    "after": after_metrics
                },
                "issues": {
                    "baseline_count": len(baseline_issues),
                    "fixed_count": len(fixed_issues),
                    "new_count": len(new_issues),
                    "baseline_issues": baseline_issues,
                    "fixed_issues": fixed_issues,
                    "new_issues": new_issues
                }
            }
            
            with open(output_path, "w") as f:
                json.dump(report, f, indent=2)
            
            logger.info(f"✓ SUCCESS: Fixed={len(fixed_issues)}, New={len(new_issues)}")
            
        except Exception as e:
            logger.error(f"✗ Error: {e}")
            traceback.print_exc()

    logger.info("\n=== Analysis Complete ===")


if __name__ == "__main__":
    main()
