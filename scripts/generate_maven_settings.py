"""Generate Maven settings.xml with project-specific repositories and mirrors"""
import sys
import json
from pathlib import Path

project = sys.argv[1] if len(sys.argv) > 1 else "flink"

with open("scripts/project_configs.json") as f:
    configs = json.load(f)

repos = configs.get(project, {}).get("additional_maven_repos", [])

# Base settings with mirrors to fix Maven 3.8.1+ HTTP blocking
settings_xml = """<settings>
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
"""

# Only append profiles and repositories if the project config requires them
if repos:
    settings_xml += """  <profiles>
    <profile>
      <id>project-repos</id>
      <repositories>
"""
    for repo in repos:
        settings_xml += f"""        <repository>
          <id>{repo['id']}</id>
          <url>{repo['url']}</url>
        </repository>
"""
    settings_xml += """      </repositories>
    </profile>
  </profiles>
  <activeProfiles>
    <activeProfile>project-repos</activeProfile>
  </activeProfiles>
"""

settings_xml += "</settings>"

Path.home().joinpath(".m2").mkdir(exist_ok=True)
Path.home().joinpath(".m2", "settings.xml").write_text(settings_xml)
print("✓ Maven settings.xml created with HTTP blocker overrides")
