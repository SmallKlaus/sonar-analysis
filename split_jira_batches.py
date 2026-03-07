import json

# Load your main JIRA file
with open("flink_issues_1.json", "r", encoding="utf-8") as f:
    data = json.load(f)

# Extract the issues (adjust if your structure is different)
# If your JSON is: {"1": {"FLINK-123": {...}, "FLINK-456": {...}}}
if "1" in data:
    issues = list(data["1"].items())
else:
    # If it's just: {"FLINK-123": {...}, "FLINK-456": {...}}
    issues = list(data.items())

print(f"Total issues found: {len(issues)}")

# Split into 10 batches
num_batches = 10
batch_size = len(issues) // num_batches + 1

for i in range(num_batches):
    start = i * batch_size
    end = (i + 1) * batch_size
    batch_issues = dict(issues[start:end])
    
    if not batch_issues:  # Skip empty batches
        continue
    
    filename = f"flink_issues_batch_{i+1}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(batch_issues, f, indent=2, ensure_ascii=False)
    
    print(f"Created {filename} with {len(batch_issues)} issues")

print("\nDone! Created 10 batch files.")