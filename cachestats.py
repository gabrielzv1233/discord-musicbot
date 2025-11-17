import json

FILENAME = "cache.json"

try:
    with open(FILENAME, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception as e:
    print(f"Failed to read {FILENAME}: {e}")
    raise

if not isinstance(data, list):
    print("JSON root must be a list of entries.")
    raise SystemExit(1)

items = [x for x in data if isinstance(x, dict)]
items_count = len(items)
total_keys = sum(len(x.get("keys", [])) for x in items)
total_duration = 0
for x in items:
    d = x.get("duration", 0)
    if isinstance(d, (int, float)):
        total_duration += int(d)

print(f"keys_stored: {total_keys}")
print(f"items_count: {items_count}")
print(f"total_duration_seconds: {total_duration}")
