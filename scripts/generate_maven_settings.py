"""Generate Maven settings.xml with project-specific repositories and mirrors"""
import sys
import json
from pathlib import Path

project = sys.argv[1] if len(sys.argv) > 1 else "flink"

with open("scripts/project_configs.json") as f:
    configs = json.load(f)

extra_repos = configs.get(project, {}).get("additional_maven_repos", [])

# ── Fixed repos always present for all projects ───────────────────────────
fixed_repos = [
    {
        "id": "github-packages",
        "url": "https://maven.pkg.github.com/SmallKlaus/maven-artifacts",
        "snapshots": "true",
        "releases":  "true",
    },
    {
        "id": "apache-snapshots",
        "url": "https://repository.apache.org/content/repositories/snapshots",
        "snapshots": "true",
        "releases":  "false",
    },
    {
        "id": "maven-central",
        "url": "https://repo.maven.apache.org/maven2",
        "snapshots": "false",
        "releases":  "true",
    },
    {
        "id": "mapr-public",
        "url": "https://repository.mapr.com/nexus/content/groups/mapr-public/",
        "snapshots": "false",
        "releases":  "true",
    },
    {
        "id": "apache-group-snapshots",
        "url": "https://repository.apache.org/content/groups/snapshots/",
        "snapshots": "true",
        "releases": "false"
    },
]

def repo_xml(repo: dict) -> str:
    # Extra repos from project_configs.json may not have snapshots/releases keys
    snapshots = repo.get("snapshots", "false")
    releases  = repo.get("releases",  "true")
    return f"""        <repository>
          <id>{repo['id']}</id>
          <url>{repo['url']}</url>
          <snapshots><enabled>{snapshots}</enabled></snapshots>
          <releases><enabled>{releases}</enabled></releases>
        </repository>
"""

all_repos = fixed_repos + extra_repos

settings_xml = """<settings>
  <servers>
    <server>
      <id>github-packages</id>
      <username>${env.GITHUB_ACTOR}</username>
      <password>${env.GITHUB_TOKEN}</password>
    </server>
  </servers>

  <mirrors>
    <mirror>
      <id>confluent-https</id>
      <mirrorOf>confluent</mirrorOf>
      <url>https://packages.confluent.io/maven/</url>
    </mirror>
    <mirror>
      <id>maven-default-http-blocker</id>
      <mirrorOf>dummy</mirrorOf>
      <name>Dummy mirror to override default blocking</name>
      <url>http://0.0.0.0/</url>
      <blocked>false</blocked>
    </mirror>
  </mirrors>

  <profiles>
    <profile>
      <id>repos</id>
      <repositories>
"""

for repo in all_repos:
    settings_xml += repo_xml(repo)

settings_xml += """      </repositories>
    </profile>
  </profiles>

  <activeProfiles>
    <activeProfile>repos</activeProfile>
  </activeProfiles>
</settings>"""

Path.home().joinpath(".m2").mkdir(exist_ok=True)
Path.home().joinpath(".m2", "settings.xml").write_text(settings_xml)
print(f"✓ Maven settings.xml created: {len(fixed_repos)} fixed repos "
      f"+ {len(extra_repos)} project-specific repos for '{project}'")
