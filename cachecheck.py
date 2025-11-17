import os
import re
import json
import time

from yt_dlp import YoutubeDL

class QuietLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        print(f"[yt-dlp error] {msg}")

URL_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/")


def is_url(s: str) -> bool:
    return bool(URL_RE.match(s))


def format_duration(sec: int) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def format_total_seconds(total: int) -> str:
    total = int(total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{total} seconds (~{h}h {m}m {s}s)"
    if m:
        return f"{total} seconds (~{m}m {s}s)"
    return f"{total} seconds"


def load_cache_file(path: str) -> list[dict]:
    if not os.path.exists(path):
        print(f"Cache file not found: {path}")
        return []

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries: list[dict] = []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        for k, v in data.items():
            entry = {"keys": [k]}
            entry.update(v)
            entries.append(entry)
    else:
        print("Unsupported JSON format, expected list or dict at top level.")
        return []

    return entries


def save_cache_file(path: str, entries: list[dict]):
    base, ext = os.path.splitext(path)
    out_path = base + "_checked" + (ext or ".json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        print(f"Saved checked cache to: {out_path}")
    except Exception as e:
        print(f"Failed to save checked cache: {e}")


def make_ydl() -> YoutubeDL:
    cookiefile = "cookies.txt"
    ydl_opts = {
        "format": "bestaudio[abr<=64]/bestaudio/best",
        "quiet": True,
        "noplaylist": True, 
        "forcejson": True,
        "nocheckcertificate": True,
        "default_search": "auto",
        "source_address": "0.0.0.0",
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "extractor_args": {"youtube": {"player_client": ["android", "web"], "skip": ["dash"]}},
        "player_skip": ["webpage"],
        "noprogress": True,
        "concurrent_fragment_downloads": 1,
        "skip_download": True,
        "logger": QuietLogger(),
    }
    if os.path.exists(cookiefile) and os.path.getsize(cookiefile) > 0:
        ydl_opts["cookiefile"] = cookiefile

    return YoutubeDL(ydl_opts)


def verify_entry_video(ydl: YoutubeDL, entry: dict, delay: float = 0.7) -> tuple[bool, bool]:
    updated = False
    invalid = False
    url = entry.get("webpage_url") or entry.get("url")

    if not url:
        print("Entry missing webpage_url/url, marking invalid.")
        return False, True

    try:
        raw = ydl.extract_info(url, download=False)
        if "entries" in raw:
            raw = raw["entries"][0]

        changed = False
        for k in ("url", "webpage_url", "title", "duration", "uploader"):
            old = entry.get(k)
            new = raw.get(k)
            if new is not None and new != old:
                entry[k] = new
                changed = True

        if changed:
            updated = True

    except Exception as e:
        print(f"Error verifying {url}: {e}")
        invalid = True

    time.sleep(delay)
    return updated, invalid


def verify_search_keys(ydl: YoutubeDL, entry: dict, delay: float = 0.9) -> tuple[int, int]:
    mismatches = 0
    checked = 0

    keys = entry.get("keys") or []
    target_url = entry.get("webpage_url")

    if not target_url:
        return 0, 0

    for key in keys:
        if not isinstance(key, str):
            continue
        if is_url(key):
            continue

        query = f"ytsearch1:{key}"
        try:
            raw = ydl.extract_info(query, download=False)
            if "entries" not in raw or not raw["entries"]:
                print(f"No results for search key: {key}")
                mismatches += 1
            else:
                top = raw["entries"][0]
                result_url = top.get("webpage_url") or top.get("url")
                if result_url != target_url:
                    print(f"Mismatch for key '{key}': expected {target_url}, got {result_url}")
                    mismatches += 1

            checked += 1
        except Exception as e:
            print(f"Error searching for key '{key}': {e}")
            mismatches += 1

        time.sleep(delay)

    return checked, mismatches


def print_initial_stats(entries: list[dict]):
    total_entries = len(entries)
    total_keys = 0
    url_keys = 0
    non_url_keys = 0
    total_duration = 0
    duration_count = 0
    total_non_url_per_entry = 0

    for entry in entries:
        keys = entry.get("keys") or []
        total_keys += len(keys)

        non_url_for_this_entry = 0
        for k in keys:
            if not isinstance(k, str):
                continue
            if is_url(k):
                url_keys += 1
            else:
                non_url_keys += 1
                non_url_for_this_entry += 1
        total_non_url_per_entry += non_url_for_this_entry

        dur = entry.get("duration")
        if dur is not None:
            try:
                sec = int(dur)
                if sec > 0:
                    total_duration += sec
                    duration_count += 1
            except Exception:
                pass

    avg_duration = total_duration / duration_count if duration_count > 0 else 0
    avg_non_url_per_item = total_non_url_per_entry / total_entries if total_entries > 0 else 0

    print("Cache stats:")
    print(f"  Unique entries: {total_entries}")
    print(f"  Total keys: {total_keys}")
    print(f"  URL keys: {url_keys}")
    print(f"  Non-URL keys: {non_url_keys}")
    print(f"  Total duration: {format_total_seconds(total_duration) if duration_count > 0 else 'N/A'}")
    print(f"  Average duration: {format_duration(int(avg_duration)) if duration_count > 0 else 'N/A'}")
    print(f"  Average non-URL keys per entry: {avg_non_url_per_item:.2f}")
    print()


cache_path = input("Path to cache file (default: cache.json): ").strip()
if not cache_path:
    cache_path = "cache.json"

entries = load_cache_file(cache_path)
if not entries:
    print("No entries loaded, exiting.")
else:
    print_initial_stats(entries)

    check_search = input("Check non-URL search keys as well? (y/n): ").strip().lower().startswith("y")

    ydl = make_ydl()

    total = len(entries)
    meta_updated = 0
    meta_same = 0
    invalid_entries: list[dict] = []

    total_search_checked = 0
    total_search_mismatches = 0

    print(f"Loaded {total} entries from cache. Verifying video URLs...")

    for idx, entry in enumerate(entries):
        print(f"[{idx + 1}/{total}] Checking {entry.get('title')!r}")
        updated, invalid = verify_entry_video(ydl, entry)
        if invalid:
            invalid_entries.append(entry)
        elif updated:
            meta_updated += 1
        else:
            meta_same += 1

        print(
            f"  -> validated {idx + 1}/{total} "
            f"(updated: {meta_updated}, unchanged: {meta_same}, invalid: {len(invalid_entries)})"
        )

    print("\nURL validation summary:")
    print(f"  Total entries: {total}")
    print(f"  Metadata updated: {meta_updated}")
    print(f"  Metadata unchanged: {meta_same}")
    print(f"  Invalid entries so far: {len(invalid_entries)}")

    if check_search:
        print("\nDetermining entries with non-URL search keys...")
        search_entries_total = 0
        for e in entries:
            if e in invalid_entries:
                continue
            keys = e.get("keys") or []
            if any(isinstance(k, str) and not is_url(k) for k in keys):
                search_entries_total += 1

        print(f"Entries with non-URL keys to validate: {search_entries_total}")
        search_entries_done = 0

        if search_entries_total > 0:
            print("Now verifying search keys -> video mapping...")
            for entry in entries:
                if entry in invalid_entries:
                    continue
                keys = entry.get("keys") or []
                if not any(isinstance(k, str) and not is_url(k) for k in keys):
                    continue

                search_entries_done += 1
                print(
                    f"[search {search_entries_done}/{search_entries_total}] "
                    f"Checking keys for {entry.get('title')!r}"
                )
                checked, mismatches = verify_search_keys(ydl, entry)
                total_search_checked += checked
                if mismatches > 0 and entry not in invalid_entries:
                    invalid_entries.append(entry)
                total_search_mismatches += mismatches

                print(
                    f"  -> search validated {search_entries_done}/{search_entries_total} "
                    f"(keys checked: {total_search_checked}, mismatches/errors: {total_search_mismatches})"
                )

        print("\nSearch-key validation summary:")
        print(f"  Entries with non-URL keys: {search_entries_total}")
        print(f"  Search keys checked: {total_search_checked}")
        print(f"  Search mismatches/errors: {total_search_mismatches}")
        print(f"  Invalid entries after search phase: {len(invalid_entries)}")

    print("\nFinal summary:")
    print(f"  Total entries: {total}")
    print(f"  Metadata updated: {meta_updated}")
    print(f"  Metadata unchanged: {meta_same}")
    print(f"  Invalid entries (errors or search mismatches): {len(invalid_entries)}")
    if check_search:
        print(f"  Search keys checked: {total_search_checked}")
        print(f"  Search mismatches/errors: {total_search_mismatches}")

    to_delete = "n"
    if invalid_entries:
        to_delete = input("Delete invalid entries before saving copy? (y/n): ").strip().lower()

    if to_delete.startswith("y"):
        entries = [e for e in entries if e not in invalid_entries]
        print(f"Removed {len(invalid_entries)} invalid entries, {len(entries)} remain.")

    save_cache_file(cache_path, entries)
