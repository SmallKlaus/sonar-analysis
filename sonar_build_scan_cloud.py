"""
sonar_build_scan_cloud.py - GitHub Actions + SonarCloud version
WITH DETAILED LOGGING FOR TROUBLESHOOTING
"""

import os
import json
import subprocess
import time
import requests
import logging
from datetime import datetime, timezone

# ============================================================================
# CONFIGURATION
# ============================================================================
CONFIG = {
    "jira_json_path": os.getenv("JIRA_JSON_PATH", "flink_issues_batch_1.json"),
    "repo_path": os.getenv("GITHUB_WORKSPACE", "."),
    "output_dir": "output",
    
    "sonar_url": "https://sonarcloud.io",
    "sonar_token": os.getenv("SONAR_TOKEN"),
    "sonar_organization": os.getenv("SONAR_ORGANIZATION"),
    
    "java_homes": {
        "8":  os.getenv("JAVA_HOME_8_X64", "/opt/hostedtoolcache/Java_Temurin-Hotspot_jdk/8.0.432-6/x64"),
        "11": os.getenv("JAVA_HOME_11_X64", "/opt/hostedtoolcache/Java_Temurin-Hotspot_jdk/11.0.25-9/x64"),
        "17": os.getenv("JAVA_HOME_17_X64", "/opt/hostedtoolcache/Java_Temurin-Hotspot_jdk/17.0.13-11/x64"),
    },
    
    "maven_bin": "mvn",
    "sonar_scanner_bin": "sonar-scanner",
}

os.makedirs(CONFIG["output_dir"], exist_ok=True)

# Enhanced logging - both to file AND console
logging.basicConfig(
    level=logging.INFO,  # Show INFO level in console
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(CONFIG["output_dir"], "sonar_build_scan_errors.log")),
        logging.StreamHandler()  # Also print to console
    ]
)

def log_step(message):
    """Helper to print prominent step messages"""
    print(f"\n{'='*70}")
    print(f">>> {message}")
    print(f"{'='*70}\n")
    logging.info(message)


# ============================================================================
# VERSION SELECTION
# ============================================================================
def get_toolchain(year: int) -> dict:
    if year <= 2017:
        return {"java_major": "8",  "maven": "3.0.5", "java_source": "1.8"}
    elif year <= 2019:
        return {"java_major": "8",  "maven": "3.5.4", "java_source": "1.8"}
    elif year <= 2021:
        return {"java_major": "8",  "maven": "3.8.1", "java_source": "1.8"}
    elif year <= 2023:
        return {"java_major": "11", "maven": "3.8.6", "java_source": "11"}
    elif year <= 2024:
        return {"java_major": "11", "maven": "3.9.9", "java_source": "11"}
    else:
        return {"java_major": "17", "maven": "3.9.9", "java_source": "17"}


def year_from_iso(date_str: str) -> int:
    try:
        return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").year
    except Exception:
        return datetime.now(timezone.utc).year


# ============================================================================
# GIT HELPERS
# ============================================================================
def git_checkout(repo_path: str, sha: str) -> bool:
    """
    Checkout a specific commit SHA.
    Assumes commit is already available via refs/td-analysis/* refs.
    """
    logger.info(f"Git checkout: {sha[:10]} in {repo_path}")
    
    # Verify commit exists (should always succeed now)
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", sha],
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10
        )
        if result.returncode != 0:
            logger.error(f"✗ Commit {sha[:10]} not found even after importing refs!")
            logger.error(f"  Check if bundle contains this commit")
            return False
    except Exception as e:
        logger.error(f"✗ Failed to verify commit: {e}")
        return False
    
    try:
        result = subprocess.run(
            ["git", "checkout", "--force", sha],
            cwd=repo_path, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace",
            timeout=30
        )
        
        result = subprocess.run(
            ["git", "clean", "-fd"],
            cwd=repo_path, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace",
            timeout=30
        )
        
        logger.info(f"✓ Successfully checked out {sha[:10]}")
        return True
        
    except subprocess.CalledProcessError as e:
        logger.error(f"✗ Git checkout failed for {sha}")
        logger.error(f"  Return code: {e.returncode}")
        logger.error(f"  Stderr: {e.stderr[:500]}")
        return False


# ============================================================================
# SONARCLOUD HELPERS
# ============================================================================
def delete_sonar_project(project_key: str):
    log_step(f"Deleting SonarCloud project: {project_key}")
    
    full_key = f"{CONFIG['sonar_organization']}_{project_key}"
    url = f"{CONFIG['sonar_url']}/api/projects/delete"
    auth = (CONFIG["sonar_token"], "")
    
    try:
        res = requests.post(url, auth=auth, data={"project": full_key}, timeout=30)
        if res.status_code in (200, 204):
            print(f"✓ Deleted existing project '{full_key}'")
        elif res.status_code == 404:
            print(f"✓ Project doesn't exist (clean slate)")
        else:
            print(f"⚠ Could not delete project: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"⚠ Delete request failed: {e}")
        logging.error(f"delete_sonar_project error: {e}")


def _is_transient_error(output: str) -> bool:
    output_lower = output.lower()
    return any(phrase in output_lower for phrase in [
        "500 internal server error",
        "502 bad gateway",
        "503 service unavailable",
        "connection reset",
        "connection refused",
        "could not transfer artifact",
        "failed to read artifact descriptor",
        "failed to retrieve",
    ])


# ============================================================================
# PHASE 1: MAVEN BUILD
# ============================================================================
def maven_build_phase(repo_path: str, project_key: str, toolchain: dict,
                      log_file: str, max_retries: int = 3) -> bool:
    
    log_step(f"Maven Build Phase - Java {toolchain['java_major']}")
    
    java_home = CONFIG["java_homes"].get(toolchain["java_major"])
    
    if not java_home or not os.path.isdir(java_home):
        print(f"✗ Java home not found: {java_home}")
        logging.error(f"Java home not found for major={toolchain['java_major']}: {java_home}")
        return False
    
    print(f"Using Java: {java_home}")
    
    env = os.environ.copy()
    env["JAVA_HOME"] = java_home
    env["PATH"] = f"{java_home}/bin:{env.get('PATH', '')}"
    env["MAVEN_OPTS"] = env.get("MAVEN_OPTS", "") + (
        " -Dfile.encoding=UTF-8"
        " -Dmaven.wagon.http.retryHandler.count=5"
        " -Dmaven.wagon.http.retryHandler.requestSentEnabled=true"
        " -Dmaven.wagon.httpconnectionManager.ttlSeconds=25"
        " -Dmaven.wagon.http.connectionTimeout=60000"
        " -Dmaven.wagon.http.readTimeout=60000"
    )

    cmd_build = [
        "mvn",
        "clean", "install",
        "-DskipTests",
        "-Dmaven.javadoc.skip=true",
        "-Dcheckstyle.skip=true",
        "-Denforcer.skip=true",
        "-Drat.skip=true",
        "-Dfindbugs.skip=true",
        "-Dpmd.skip=true",
        f"-Dmaven.compiler.source={toolchain['java_source']}",
        f"-Dmaven.compiler.target={toolchain['java_source']}",
        "--batch-mode",
        "--no-transfer-progress",
    ]
    
    print(f"Maven command: {' '.join(cmd_build[:5])}...")

    for attempt in range(1, max_retries + 1):
        print(f"\n>>> Build attempt {attempt}/{max_retries} (timeout: 45 min)")

        build_output = []

        try:
            with open(log_file, "w", encoding="utf-8", errors="replace") as log_fh:
                log_fh.write(f"=== BUILD PHASE - Attempt {attempt}/{max_retries} ===\n")
                log_fh.write(f"Java Home: {java_home}\n")
                log_fh.write(f"Command: {' '.join(cmd_build)}\n")
                log_fh.write(f"Started: {datetime.now()}\n\n")
                log_fh.flush()

                process = subprocess.Popen(
                    cmd_build,
                    cwd=repo_path,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                )

                line_count = 0
                for line in iter(process.stdout.readline, ''):
                    if not line:
                        break
                    build_output.append(line)
                    log_fh.write(line)
                    log_fh.flush()
                    
                    line_count += 1
                    # Show progress every 100 lines
                    if line_count % 100 == 0:
                        print(".", end="", flush=True)
                    
                    # Show important lines
                    if "building" in line.lower() and any(x in line.lower() for x in ["module", "project"]):
                        print(f"\n  {line.strip()}")
                    elif "error" in line.lower() or "failure" in line.lower():
                        print(f"\n  ⚠ {line.strip()}")

                try:
                    process.wait(timeout=2700)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    print(f"\n✗ BUILD timed out after 45 minutes")
                    logging.error(f"Build timeout on attempt {attempt}")
                    return False

                if process.returncode != 0:
                    raise subprocess.CalledProcessError(
                        process.returncode, cmd_build, output="".join(build_output)
                    )

            print(f"\n✓ Build completed successfully")
            return True

        except subprocess.CalledProcessError as e:
            full_output = "".join(build_output)
            
            # Show last 30 lines of output for debugging
            print(f"\n✗ Build FAILED (exit code {e.returncode})")
            print(f"\nLast 30 lines of build output:")
            print("─" * 70)
            for line in build_output[-30:]:
                print(line.rstrip())
            print("─" * 70)
            
            if _is_transient_error(full_output) and attempt < max_retries:
                wait_time = 30 * attempt
                print(f"\n⟳ Transient error detected. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                logging.error(f"Build failed after {attempt} attempts:\n{full_output[-2000:]}")
                return False

    return False


# ============================================================================
# DISCOVER PROJECT STRUCTURE
# ============================================================================
def discover_project_structure(repo_path: str):
    log_step("Discovering project structure")
    
    source_dirs = []
    binary_dirs = []

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['node_modules', 'tmp', 'temp', '.git']]
        
        if root.endswith("src/main/java"):
            rel_src = os.path.relpath(root, repo_path)
            source_dirs.append(rel_src)
            
            module_root = os.path.dirname(os.path.dirname(os.path.dirname(root)))
            target_classes = os.path.join(module_root, "target", "classes")
            
            if os.path.isdir(target_classes):
                rel_bin = os.path.relpath(target_classes, repo_path)
                binary_dirs.append(rel_bin)

    source_dirs = sorted(set(source_dirs))
    binary_dirs = sorted(set(binary_dirs))

    sources_str = ",".join(source_dirs) if source_dirs else "."
    binaries_str = ",".join(binary_dirs) if binary_dirs else "target/classes"

    print(f"✓ Found {len(source_dirs)} source directories")
    print(f"✓ Found {len(binary_dirs)} binary directories")
    
    if len(source_dirs) <= 5:
        print(f"  Sources: {sources_str}")
    
    return sources_str, binaries_str


# ============================================================================
# PHASE 2: SONARCLOUD SCAN
# ============================================================================
def sonar_scan_phase(repo_path: str, project_key: str, toolchain: dict, log_file: str) -> str | None:
    log_step("SonarCloud Scan Phase")
    
    scanner_bin = CONFIG["sonar_scanner_bin"]
    scanner_java = CONFIG["java_homes"]["17"]
    project_java = CONFIG["java_homes"].get(toolchain["java_major"])

    print(f"Scanner Java: {scanner_java}")
    print(f"Project Java: {project_java}")

    sources, binaries = discover_project_structure(repo_path)

    full_project_key = f"{CONFIG['sonar_organization']}_{project_key}"
    
    print(f"Project key: {full_project_key}")

    props_file = os.path.join(repo_path, "sonar-project.properties")
    props_content = f"""# Generated by sonar_build_scan_cloud.py
sonar.organization={CONFIG['sonar_organization']}
sonar.projectKey={full_project_key}
sonar.projectName={project_key}
sonar.sources={sources}
sonar.java.binaries={binaries}
sonar.java.source={toolchain['java_source']}
sonar.java.target={toolchain['java_source']}
sonar.java.jdkHome={project_java}
sonar.sourceEncoding=UTF-8
sonar.scm.disabled=true
sonar.exclusions=**/archetype-resources/**,**/target/classes/archetype-resources/**
"""

    try:
        with open(props_file, "w", encoding="utf-8") as f:
            f.write(props_content)
        
        print(f"✓ Created sonar-project.properties")

        env = os.environ.copy()
        env["JAVA_HOME"] = scanner_java
        env["PATH"] = f"{scanner_java}/bin:{env.get('PATH', '')}"

        cmd_scan = [
            scanner_bin,
            f"-Dsonar.host.url={CONFIG['sonar_url']}",
            f"-Dsonar.token={CONFIG['sonar_token']}",
            f"-Dproject.settings={props_file}",
        ]

        print(f"Starting SonarCloud scan (timeout: 60 min)...")

        scan_output = []
        task_id = None

        with open(log_file, "a", encoding="utf-8", errors="replace") as log_fh:
            log_fh.write(f"\n\n=== SONARCLOUD SCAN PHASE ===\n")
            log_fh.write(f"Project Key: {full_project_key}\n")
            log_fh.write(f"Properties:\n{props_content}\n")
            log_fh.write(f"Started: {datetime.now()}\n\n")
            log_fh.flush()

            process = subprocess.Popen(
                cmd_scan,
                cwd=repo_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
            )

            line_count = 0
            for line in iter(process.stdout.readline, ''):
                if not line:
                    break
                scan_output.append(line)
                log_fh.write(line)
                log_fh.flush()
                
                line_count += 1
                if line_count % 50 == 0:
                    print(".", end="", flush=True)
                
                # Show important lines
                if any(word in line.lower() for word in ["analysis", "executing", "error", "warn"]):
                    print(f"\n  {line.strip()}")
                
                # Parse task ID
                if "task?id=" in line or "ceTaskId=" in line:
                    if "task?id=" in line:
                        task_id = line.split("task?id=")[1].strip()
                    else:
                        task_id = line.split("ceTaskId=")[1].strip()
                    print(f"\n✓ Task ID captured: {task_id}")

            try:
                process.wait(timeout=3600)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                print(f"\n✗ Sonar scan timed out after 60 minutes")
                logging.error(f"Sonar timeout")
                return None

            if process.returncode != 0:
                print(f"\n✗ Sonar scan FAILED (exit code {process.returncode})")
                print(f"\nLast 30 lines of scan output:")
                print("─" * 70)
                for line in scan_output[-30:]:
                    print(line.rstrip())
                print("─" * 70)
                logging.error(f"Sonar scan failed:\n{''.join(scan_output[-1000:])}")
                return None
        
        print(f"\n✓ Scan completed successfully")
        return task_id

    finally:
        if os.path.exists(props_file):
            try:
                os.remove(props_file)
            except Exception:
                pass


# ============================================================================
# ORCHESTRATOR
# ============================================================================
def build_and_scan_mvn(repo_path: str, project_key: str, toolchain: dict) -> str | None:
    log_file = os.path.join(
        CONFIG["output_dir"],
        f"{project_key.replace(':', '_')}_maven.log"
    )
    
    if not maven_build_phase(repo_path, project_key, toolchain, log_file):
        print(f"✗ Maven build failed - aborting scan")
        return None
    
    return sonar_scan_phase(repo_path, project_key, toolchain, log_file)


def wait_for_task(task_id: str, timeout_seconds: int = 2700) -> bool:
    if not task_id:
        print(f"✗ No task ID - cannot wait")
        return False

    log_step(f"Waiting for SonarCloud task: {task_id}")
    
    url = f"{CONFIG['sonar_url']}/api/ce/task?id={task_id}"
    auth = (CONFIG["sonar_token"], "")

    deadline = time.time() + timeout_seconds
    elapsed = 0
    
    while time.time() < deadline:
        try:
            resp = requests.get(url, auth=auth, timeout=10)
            status = resp.json()["task"]["status"]
            
            elapsed = int(time.time() - (deadline - timeout_seconds))
            print(f"  Status: {status} (elapsed: {elapsed}s)", end="\r")
            
            if status == "SUCCESS":
                print(f"\n✓ Task completed successfully ({elapsed}s)")
                return True
            elif status in ("FAILED", "CANCELED"):
                print(f"\n✗ Task status: {status}")
                return False
        except Exception as e:
            print(f"\n⚠ Polling error: {e}")
            logging.error(f"Polling task {task_id}: {e}")
        
        time.sleep(10)

    print(f"\n✗ Task timed out after {timeout_seconds // 60} minutes")
    return False


def get_measures(project_key: str) -> dict:
    full_key = f"{CONFIG['sonar_organization']}_{project_key}"
    url = f"{CONFIG['sonar_url']}/api/measures/component"
    auth = (CONFIG["sonar_token"], "")
    params = {
        "component": full_key,
        "metricKeys": "ncloc,complexity,violations,sqale_index,reliability_rating,security_rating,sqale_rating",
    }
    try:
        res = requests.get(url, auth=auth, params=params, timeout=30)
        if res.status_code == 200:
            data = res.json()
            if "component" in data and "measures" in data["component"]:
                measures = {m["metric"]: m["value"] for m in data["component"]["measures"]}
                print(f"✓ Retrieved {len(measures)} metrics")
                return measures
        else:
            print(f"⚠ Failed to get measures: {res.status_code}")
    except Exception as e:
        print(f"⚠ Error getting measures: {e}")
        logging.error(f"get_measures({full_key}): {e}")
    return {}


def fetch_issues(project_key: str, statuses=None, resolutions=None, 
                created_after=None, updated_after=None) -> list:
    full_key = f"{CONFIG['sonar_organization']}_{project_key}"
    auth = (CONFIG["sonar_token"], "")
    url = f"{CONFIG['sonar_url']}/api/issues/search"
    page_size = 500
    MAX_RESULTS = 10_000

    base_params = {
        "componentKeys": full_key,
        "ps": page_size,
        "additionalFields": "_all",
    }
    if statuses:      base_params["statuses"] = statuses
    if resolutions:   base_params["resolutions"] = resolutions
    if created_after: base_params["createdAfter"] = created_after
    if updated_after: base_params["updatedAfter"] = updated_after

    all_issues = []

    for issue_type in ["BUG", "VULNERABILITY", "CODE_SMELL"]:
        params = {**base_params, "types": issue_type}
        page = 1

        while True:
            if (page - 1) * page_size >= MAX_RESULTS:
                print(f"  ⚠ {issue_type} hit 10,000 cap")
                break

            params["p"] = page
            try:
                res = requests.get(url, auth=auth, params=params, timeout=30)
                if res.status_code != 200:
                    print(f"  ⚠ HTTP {res.status_code} fetching {issue_type}")
                    logging.error(f"fetch_issues HTTP {res.status_code}: {res.text}")
                    break

                issues = res.json().get("issues", [])
                all_issues.extend(issues)

                if len(issues) < page_size:
                    break
                page += 1

            except Exception as e:
                print(f"  ⚠ Error fetching {issue_type}: {e}")
                logging.error(f"fetch_issues: {e}")
                break

    print(f"✓ Fetched {len(all_issues)} issues total")
    return all_issues


# ============================================================================
# MAIN
# ============================================================================
def main():
    log_step("Starting SonarCloud Analysis Pipeline")
    
    print(f"Configuration:")
    print(f"  JIRA JSON: {CONFIG['jira_json_path']}")
    print(f"  Repository: {CONFIG['repo_path']}")
    print(f"  Output dir: {CONFIG['output_dir']}")
    print(f"  Organization: {CONFIG['sonar_organization']}")
    print(f"  Java homes: {CONFIG['java_homes']}")
    
    try:
        with open(CONFIG["jira_json_path"], "r", encoding="utf-8") as fh:
            jira_issues = json.load(fh)
        print(f"✓ Loaded {len(jira_issues)} issues from JSON")
    except FileNotFoundError:
        print(f"✗ JIRA JSON not found: {CONFIG['jira_json_path']}")
        return

    failures = []
    successes = []

    for idx, (jira_id, item) in enumerate(jira_issues.items(), 1):
        output_path = os.path.join(CONFIG["output_dir"], f"{jira_id}_report.json")

        if os.path.exists(output_path):
            print(f"\n[{idx}/{len(jira_issues)}] {jira_id}: Already processed ✓")
            continue

        print(f"\n{'='*70}")
        print(f"[{idx}/{len(jira_issues)}] Processing {jira_id}")
        print(f"{'='*70}")

        try:
            sha_before = item.get("sha_before", "").strip()
            commits = item.get("commits", [])

            if not sha_before or not commits:
                print(f"✗ Missing data - skipping")
                continue

            sha_after = commits[0]["sha"]
            after_date = commits[0].get("date", "")
            before_date = commits[-1].get("date", "")

            before_toolchain = get_toolchain(year_from_iso(before_date) if before_date else datetime.now().year)
            after_toolchain = get_toolchain(year_from_iso(after_date) if after_date else datetime.now().year)

            print(f"  Before: {sha_before[:10]} (Java {before_toolchain['java_major']})")
            print(f"  After:  {sha_after[:10]} (Java {after_toolchain['java_major']})")

            project_key = f"jira:{jira_id}"
            delete_sonar_project(project_key)

            # BEFORE scan
            print(f"\n▶ BEFORE SCAN")
            if not git_checkout(CONFIG["repo_path"], sha_before):
                failures.append(jira_id)
                continue

            before_task = build_and_scan_mvn(CONFIG["repo_path"], project_key, before_toolchain)
            if not before_task or not wait_for_task(before_task):
                print(f"✗ BEFORE scan failed")
                failures.append(jira_id)
                continue

            before_metrics = get_measures(project_key)
            baseline_issues = fetch_issues(project_key, statuses="OPEN,CONFIRMED,REOPENED")
            scan_time_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")

            # AFTER scan
            print(f"\n▶ AFTER SCAN")
            if not git_checkout(CONFIG["repo_path"], sha_after):
                failures.append(jira_id)
                continue

            after_task = build_and_scan_mvn(CONFIG["repo_path"], project_key, after_toolchain)
            if not after_task or not wait_for_task(after_task):
                print(f"✗ AFTER scan failed")
                failures.append(jira_id)
                continue

            after_metrics = get_measures(project_key)
            fixed_issues = fetch_issues(project_key, statuses="CLOSED", resolutions="FIXED", updated_after=scan_time_iso)
            new_issues = fetch_issues(project_key, statuses="OPEN,CONFIRMED,REOPENED", created_after=scan_time_iso)

            report = {
                "jira_id": jira_id,
                "sha_before": sha_before,
                "sha_after": sha_after,
                "before_toolchain": before_toolchain,
                "after_toolchain": after_toolchain,
                "metrics_comparison": {
                    "before": before_metrics,
                    "after": after_metrics,
                },
                "issues": {
                    "baseline_count": len(baseline_issues),
                    "fixed_count": len(fixed_issues),
                    "new_count": len(new_issues),
                    "baseline_issues": baseline_issues,
                    "fixed_issues": fixed_issues,
                    "new_issues": new_issues,
                },
            }

            with open(output_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=4, ensure_ascii=False)

            print(f"\n✓ SUCCESS: {jira_id}")
            print(f"  Baseline: {len(baseline_issues)} | Fixed: {len(fixed_issues)} | New: {len(new_issues)}")
            print(f"  Report: {output_path}")
            successes.append(jira_id)

        except Exception as exc:
            print(f"\n✗ EXCEPTION: {exc}")
            logging.error(f"Error processing {jira_id}: {exc}", exc_info=True)
            failures.append(jira_id)

    log_step("Analysis Complete")
    print(f"Successes: {len(successes)}")
    print(f"Failures:  {len(failures)}")
    
    if successes:
        print(f"\n✓ Successful: {successes[:10]}{'...' if len(successes) > 10 else ''}")
    if failures:
        print(f"\n✗ Failed: {failures[:10]}{'...' if len(failures) > 10 else ''}")


if __name__ == "__main__":
    main()
