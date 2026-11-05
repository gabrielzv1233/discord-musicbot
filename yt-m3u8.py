from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn
import yt_dlp

url = input("YouTube URL: ").strip()
m3u8_file = "playlist.m3u8"

class ErrorOnlyLogger:
    def debug(self, msg): ...
    def info(self, msg): ...
    def warning(self, msg): ...
    def error(self, msg): print(msg)

with yt_dlp.YoutubeDL({
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": True,
    "logger": ErrorOnlyLogger()
}) as ydl:
    info = ydl.extract_info(url, download=False)

entries = info.get("entries")
playlist_title = info.get("title", "Unknown Playlist")

try:
    with open(m3u8_file, "x", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
except FileExistsError:
    pass

def fmt(entry):
    artist = entry.get("artist") or entry.get("uploader") or entry.get("channel") or ""
    title = entry.get("title") or "Unknown Title"
    title = title.replace("\n", " ").strip()
    artist = artist.replace("\n", " ").strip()

    if artist and not title.lower().startswith(artist.lower()):
        return f"{artist} - {title}"
    return title

with Progress(
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    TaskProgressColumn(),
    MofNCompleteColumn(),
    TimeRemainingColumn()
) as progress:
    if entries:
        task = progress.add_task(f"Writing {playlist_title}", total=len(entries))
        with open(m3u8_file, "a", encoding="utf-8") as f:
            for entry in entries:
                if not entry:
                    progress.advance(task)
                    continue

                name = fmt(entry)
                entry_url = entry.get("url") or entry.get("webpage_url") or ""
                if entry_url and not entry_url.startswith("http"):
                    entry_url = f"https://www.youtube.com/watch?v={entry_url}"

                if entry_url:
                    f.write(f"#EXTINF:-1,{name}\n{entry_url}\n")

                progress.advance(task)
    else:
        task = progress.add_task(f"Writing {playlist_title}", total=1)
        with open(m3u8_file, "a", encoding="utf-8") as f:
            f.write(f"#EXTINF:-1,{playlist_title}\n{url}\n")
        progress.advance(task)

print(f"Added: {playlist_title}")