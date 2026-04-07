"""Generate Maven settings.xml with project-specific repositories"""
import sys
import json
from pathlib import Path

project = sys.argv[1] if len(sys.argv) > 1 else "flink"

with open("scripts/project_configs.json") as f:
    configs = json.load(f)

repos = configs.get(project, {}).get("additional_maven_repos", [])

if not repos:
    print("No additional Maven repos needed")
    sys.exit(0)

settings_xml = f"""<settings>
  <profiles>
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
</settings>"""

Path.home().joinpath(".m2").mkdir(exist_ok=True)
Path.home().joinpath(".m2", "settings.xml").write_text(settings_xml)
print("✓ Maven settings.xml created")
