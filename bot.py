import importlib.metadata, requests, aiohttp, asyncio, discord, random, json, re, os, traceback, tempfile
from contextlib import suppress
from discord.ui import View, Button, button
from urllib.parse import urlparse, parse_qs
from discord import app_commands
from discord.ext import commands
from mutagen import File as MutagenFile

TOKEN = "bot token"
LEAVE_SOUND = "_leave.mp3"  # short, quiet chime bot exit chime (set to None to disable)
CACHE_FILE = "cache.json" # Json file to store cache
OWNER_ONLY = True # Restrict some commands to bot owner only (cache management commands)
USE_CACHE = True # Disabling bypasses cache entirely
LOW_BANDWIDTH_MODE = False # Restart the bot after changing. Reduces source bitrate and Discord voice bitrate.

NORMAL_SOURCE_ABR_LIMIT = 64
LOW_BANDWIDTH_SOURCE_ABR_LIMIT = 64
LOW_BANDWIDTH_MIN_SOURCE_ABR = 48
LOW_BANDWIDTH_DISCORD_BITRATE = 96
LOW_BANDWIDTH_DISCORD_BANDWIDTH = "full"

def ytdlp_updated() -> bool:
    try:
        local = importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return False

    resp = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=5)
    resp.raise_for_status()
    latest = resp.json()["info"]["version"]

    return local == latest, local, latest

try:
    updated, localver, latestver = ytdlp_updated()
    if not updated:
        print(f"Updating yt-dlp from {localver} to {latestver}...")
        os.system("python -m pip install -U yt-dlp")
    else:
        print(f"yt-dlp runnning {localver} (up to date)")

    from yt_dlp import YoutubeDL

except Exception as e:
    traceback.print_exc()
    print(f"Failed to check/update yt-dlp: {e}")

cache_entries: list[dict] = []
key_map: dict[str, dict] = {}

def load_cache():
    global cache_entries, key_map
    cache_entries, key_map = [], {}
    if USE_CACHE and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                cache_entries = data
            else:
                for k, v in data.items():
                    cache_entries.append({"keys": [k], **v})
            for entry in cache_entries:
                for k in entry.get("keys", []):
                    key_map[k] = entry
        except Exception as e:
            traceback.print_exc()
            cache_entries, key_map = [], {}

def save_cache():
    if not USE_CACHE:
        return
    tmp = CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache_entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        traceback.print_exc()
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception as e2:
            traceback.print_exc()

load_cache()

cookiefile = "cookies.txt"
def _stream_source_abr_limit() -> int:
    return LOW_BANDWIDTH_SOURCE_ABR_LIMIT if LOW_BANDWIDTH_MODE else NORMAL_SOURCE_ABR_LIMIT

def _build_stream_format() -> str:
    abr_limit = _stream_source_abr_limit()
    return f"bestaudio[abr<={abr_limit}]/bestaudio/best"

def _build_ydl_opts(*, extract_flat: bool = False) -> dict:
    opts = {
        "format": _build_stream_format(),
        "quiet": True,
        "noplaylist": True,
        "forcejson": True,
        "nocheckcertificate": True,
        "default_search": "auto",
        "source_address": "0.0.0.0",
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "extractor_args": {"youtube": {
            "player_client": ["android", "web"],
            "skip": ["dash"]
        }},
        "player_skip": ["webpage"],
        "noprogress": True,
        "concurrent_fragment_downloads": 1,
        "skip_download": True
    }
    if extract_flat:
        opts["extract_flat"] = True
    if os.path.exists(cookiefile) and os.path.getsize(cookiefile) > 0:
        opts["cookiefile"] = cookiefile
    return opts

def _format_bitrate_value(fmt: dict) -> float:
    return float(fmt.get("abr") or fmt.get("tbr") or 0)

def _pick_soundcloud_format(candidates: list[dict]) -> dict:
    if not LOW_BANDWIDTH_MODE:
        return max(candidates, key=_format_bitrate_value)

    positive = [fmt for fmt in candidates if _format_bitrate_value(fmt) > 0]
    if not positive:
        return candidates[0]

    acceptable = [fmt for fmt in positive if _format_bitrate_value(fmt) >= LOW_BANDWIDTH_MIN_SOURCE_ABR]
    pool = acceptable or positive
    return min(pool, key=_format_bitrate_value)

def _voice_playback_kwargs() -> dict:
    if not LOW_BANDWIDTH_MODE:
        return {}
    return {
        "bitrate": LOW_BANDWIDTH_DISCORD_BITRATE,
        "bandwidth": LOW_BANDWIDTH_DISCORD_BANDWIDTH,
    }

ydl_opts = _build_ydl_opts()

stream_ydl = YoutubeDL(ydl_opts)
search_ydl = YoutubeDL(_build_ydl_opts(extract_flat=True))
url_re = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE)
soundcloud_re = re.compile(r"(https?://)?(www\.)?(m\.)?(soundcloud\.com|on\.soundcloud\.com)/", re.IGNORECASE)
generic_http_re = re.compile(r"^https?://", re.IGNORECASE)

intents = discord.Intents.all()
intents.voice_states = True
bot = commands.Bot(command_prefix="/", intents=intents)

music_queue: list[dict] = []
music_history: list[dict] = []
voice_client: discord.VoiceClient | None = None
text_channel: discord.TextChannel | None = None
now_playing_msg: discord.Message | None = None
disconnect_task: asyncio.Task | None = None
shutting_down = False

_http_session: aiohttp.ClientSession | None = None

def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
    return _http_session

async def close_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None

async def _cancel_disconnect_task():
    global disconnect_task
    task = disconnect_task
    disconnect_task = None
    if task is None:
        return
    if task is asyncio.current_task():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

def format_duration(sec: int) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def canonical_url(raw_url: str) -> str | None:
    o = urlparse(raw_url)
    net = o.netloc.lower()
    if "youtu.be" in net:
        vid = o.path.lstrip("/")
    elif "youtube.com" in net:
        if o.path == "/watch":
            vid = parse_qs(o.query).get("v", [None])[0]
        elif o.path.startswith("/shorts/"):
            parts = o.path.split("/")
            vid = parts[2] if len(parts) >= 3 else None
        else:
            return None
    else:
        return None
    return f"https://www.youtube.com/watch?v={vid}" if vid else None

async def ensure_voice(inter: discord.Interaction) -> bool:
    global voice_client, text_channel
    vc = getattr(inter.user.voice, "channel", None)
    if vc is None:
        await inter.followup.send("🚫 You must be in a voice channel first.", ephemeral=True)
        return False
    if voice_client is None or not voice_client.is_connected():
        voice_client = await vc.connect()
    elif voice_client.channel != vc:
        await inter.followup.send("🚫 You must be in the **same** voice channel as me.", ephemeral=True)
        return False
    text_channel = inter.channel
    return True

async def clear_all(play_leave_sound: bool = True, force_disconnect: bool = False, cleanup_message: bool = True):
    global music_queue, music_history, voice_client, now_playing_msg, text_channel
    vc = voice_client
    msg = now_playing_msg
    await _cancel_disconnect_task()
    music_queue.clear()
    music_history.clear()
    voice_client = None
    now_playing_msg = None
    text_channel = None
    if vc and vc.is_connected():
        try:
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
            except Exception:
                traceback.print_exc()

            if play_leave_sound and LEAVE_SOUND and os.path.exists(LEAVE_SOUND):
                try:
                    done = asyncio.Event()
                    def _after(_):
                        bot.loop.call_soon_threadsafe(done.set)
                    vc.play(discord.FFmpegPCMAudio(LEAVE_SOUND), after=_after)
                    try:
                        await asyncio.wait_for(done.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        pass
                except Exception:
                    traceback.print_exc()
            await vc.disconnect(force=force_disconnect)
        except (discord.HTTPException, discord.ClientException) as e:
            if not shutting_down:
                print(f"Failed to disconnect: {e}")
    if msg and cleanup_message:
        try:
            await msg.delete()
        except discord.HTTPException as e:
            if not shutting_down:
                print(f"Failed to delete now playing message: {e}")

async def auto_disconnect():
    global voice_client, disconnect_task
    await asyncio.sleep(300)

    if not voice_client or not voice_client.is_connected():
        return

    channel = voice_client.channel
    non_bot_members = []
    if channel:
        non_bot_members = [m for m in channel.members if not m.bot]

    if not non_bot_members and voice_client.is_paused():
        await clear_all()
        return

    if not voice_client.is_playing() and not voice_client.is_paused() and not music_queue:
        await clear_all()

async def shutdown_cleanup():
    global shutting_down
    if shutting_down:
        await close_session()
        return

    shutting_down = True
    try:
        await clear_all(play_leave_sound=False, force_disconnect=True, cleanup_message=False)
    except Exception:
        traceback.print_exc()
    finally:
        await close_session()
        
def _arm_idle_timer():
    global disconnect_task
    if disconnect_task:
        disconnect_task.cancel()
    disconnect_task = bot.loop.create_task(auto_disconnect())

@bot.tree.error
async def on_app_command_error(inter: discord.Interaction, error):
    if isinstance(error, TypeError) and "NoneType" in str(error):
        global cache_entries, key_map
        global music_queue, music_history, voice_client
        global text_channel, now_playing_msg, disconnect_task
        cache_entries, key_map = [], {}
        music_queue, music_history = [], []
        voice_client = text_channel = now_playing_msg = None
        disconnect_task = None
        load_cache()
        await inter.followup.send("🔄 Internal state reset – try again!", ephemeral=True)
        return
    raise error

async def _url_is_valid(u: str) -> bool:
    try:
        sess = get_session()
        async with sess.head(u, allow_redirects=True) as resp:
            return 200 <= resp.status < 400
    except Exception as e:
        traceback.print_exc()
        return False

async def _refresh_entry_in_place(entry: dict) -> dict:
    raw = await asyncio.to_thread(lambda: stream_ydl.extract_info(entry["webpage_url"], download=False))
    if "entries" in raw:
        entries = raw.get("entries") or []
        if not entries:
            raise RuntimeError("No entries returned while refreshing cache entry")
        raw = entries[0]
    for k in ("url", "duration", "title", "uploader"):
        entry[k] = raw.get(k)
    save_cache()
    return entry

class TrackResolveError(RuntimeError):
    pass

class PlaylistFormatError(RuntimeError):
    pass

def _rebuild_key_map():
    key_map.clear()
    for entry in cache_entries:
        for key in entry.get("keys", []):
            key_map[key] = entry

def _store_cache_entry(info: dict, *keys: str) -> dict:
    entry = next((e for e in cache_entries if e["webpage_url"] == info["webpage_url"]), None)
    if entry:
        entry.update(info)
    else:
        entry = {"keys": [], **info}
        cache_entries.append(entry)

    if USE_CACHE:
        canon = canonical_url(info.get("webpage_url") or "")
        for key in (*keys, canon):
            if key and key not in entry["keys"]:
                entry["keys"].append(key)
        _rebuild_key_map()
        save_cache()

    return entry

def _content_type_to_suffix(content_type: str | None) -> str:
    ct_map = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/flac": ".flac",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/x-matroska": ".mkv",
    }
    return ct_map.get((content_type or "").lower(), "")

def _extract_media_tags(path: str, fallback_title: str, fallback_uploader: str) -> tuple[str, str, int]:
    title = fallback_title or "Unknown title"
    uploader = fallback_uploader or "Unknown"
    duration = 0

    try:
        audio = MutagenFile(path, easy=True)
        if audio is not None:
            if audio.tags:
                if "title" in audio.tags and audio.tags["title"]:
                    title = str(audio.tags["title"][0])
                if "artist" in audio.tags and audio.tags["artist"]:
                    uploader = str(audio.tags["artist"][0])
            if hasattr(audio, "info") and getattr(audio.info, "length", None) is not None:
                duration = int(audio.info.length)
    except Exception:
        traceback.print_exc()

    return title, uploader, duration

def _make_queue_item(entry: dict, requester: discord.abc.User) -> dict:
    return {
        "url": entry["url"],
        "webpage_url": entry["webpage_url"],
        "title": entry["title"],
        "duration": entry["duration"],
        "uploader": entry["uploader"],
        "requester": requester,
    }

def _enqueue_item(item: dict, front: bool) -> int:
    if front:
        music_queue.insert(0, item)
        return 1

    music_queue.append(item)
    return len(music_queue)

def _normalize_query(query: str) -> str:
    raw_query = query.strip()
    if raw_query.lower().startswith("query:"):
        raw_query = raw_query[6:].strip()
    return raw_query

def _is_generic_title(title: str | None, source_url: str | None = None) -> bool:
    cleaned = (title or "").strip()
    if not cleaned:
        return True

    lowered = cleaned.lower()
    if lowered in {"unknown title", "unknown"}:
        return True

    path = urlparse(source_url or "").path or ""
    basename = os.path.basename(path)
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    if stem and lowered == stem.lower():
        return True

    if re.fullmatch(r"[a-f0-9]{24,}", cleaned, re.IGNORECASE):
        return True

    return False

def _apply_playlist_metadata(entry: dict, metadata: dict | None) -> dict:
    if not metadata:
        return entry

    merged = dict(entry)
    title = (metadata.get("title") or "").strip()
    duration = metadata.get("duration")

    if title and _is_generic_title(merged.get("title"), merged.get("webpage_url")):
        merged["title"] = title

    if duration and not merged.get("duration"):
        merged["duration"] = int(duration)

    return merged

async def _prefetch_next(n: int = 2):
    tasks = []
    for item in music_queue[:max(0, n)]:
        async def ensure_item(i=item):
            ok = await _url_is_valid(i["url"])
            if not ok:
                entry = key_map.get(i["webpage_url"])
                if entry:
                    await _refresh_entry_in_place(entry)
                    for k in ("url", "duration", "title", "uploader"):
                        i[k] = entry[k]
        tasks.append(asyncio.create_task(ensure_item()))
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                traceback.print_exception(type(r), r, r.__traceback__)

async def _extract_direct_media_info(url: str, content_type: str | None = None) -> dict:
    tmp_path = None
    try:
        sess = get_session()
        async with sess.get(url) as resp:
            resp.raise_for_status()
            path = urlparse(url).path or ""
            ext = os.path.splitext(path)[1]
            resp_ct = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            ext = ext or _content_type_to_suffix(content_type or resp_ct)

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext if ext else None)
            tmp_path = tmp.name
            try:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    if chunk:
                        tmp.write(chunk)
            finally:
                tmp.close()

        title = os.path.basename(path) or "Unknown title"
        if "." in title:
            title = ".".join(title.split(".")[:-1]) or title

        title, uploader, duration = _extract_media_tags(tmp_path, title, "Remote File")
        return {
            "url": url,
            "webpage_url": url,
            "title": title or "Unknown title",
            "duration": duration or 0,
            "uploader": uploader or "Remote File",
        }
    except Exception:
        traceback.print_exc()
        raise
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                traceback.print_exc()

async def _extract_cached_or_raw_entry(search_str: str, *cache_keys: str) -> dict:
    for key in cache_keys:
        if USE_CACHE and key in key_map:
            return key_map[key]

    raw = await asyncio.to_thread(lambda: stream_ydl.extract_info(search_str, download=False))
    if "entries" in raw:
        entries = raw.get("entries") or []
        if not entries:
            raise RuntimeError("No entries returned while resolving track")
        raw = entries[0]

    info = {k: raw.get(k) for k in ("url", "webpage_url", "title", "duration", "uploader")}
    if not info.get("url"):
        raise RuntimeError("No playable URL returned while resolving track")

    return _store_cache_entry(info, *cache_keys)

async def _resolve_http_entry(raw_query: str) -> dict:
    content_type = None
    sess = get_session()

    try:
        async with sess.head(raw_query, allow_redirects=True) as resp:
            ct = resp.headers.get("Content-Type", "").lower()
            if ct.startswith("audio/") or ct.startswith("video/"):
                content_type = ct.split(";", 1)[0]
    except Exception:
        traceback.print_exc()

    if content_type:
        try:
            return await _extract_direct_media_info(raw_query, content_type)
        except Exception as exc:
            raise TrackResolveError("Invalid media file or unsupported codec.") from exc

    try:
        raw = await asyncio.to_thread(lambda: stream_ydl.extract_info(raw_query, download=False))
        if "entries" in raw:
            entries = raw.get("entries") or []
            if not entries:
                raise RuntimeError("No entries returned for this URL")
            raw = entries[0]
        entry = {
            "url": raw.get("url"),
            "webpage_url": raw.get("webpage_url"),
            "title": raw.get("title"),
            "duration": raw.get("duration"),
            "uploader": raw.get("uploader"),
        }
    except Exception as exc:
        traceback.print_exc()
        raise TrackResolveError("Could not find any playable track from this URL.") from exc

    if not entry.get("url"):
        raise TrackResolveError("Could not find any playable track from this URL.")

    return entry

async def _resolve_track_entry(query: str) -> dict:
    raw_query = _normalize_query(query)
    if not raw_query:
        raise TrackResolveError("Query is empty.")

    try:
        if soundcloud_re.match(raw_query):
            entry = await _extract_soundcloud_info(raw_query)
        elif url_re.match(raw_query):
            entry = await _extract_cached_or_raw_entry(raw_query, raw_query, canonical_url(raw_query))
        elif generic_http_re.match(raw_query):
            entry = await _resolve_http_entry(raw_query)
        else:
            entry = await _extract_cached_or_raw_entry(f"ytsearch1:{raw_query}", raw_query.lower())
    except TrackResolveError:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise TrackResolveError("Could not find a playable track from that query or URL.") from exc

    if not entry or not entry.get("url"):
        raise TrackResolveError("Could not find a playable track from that query or URL.")

    return {
        "url": entry["url"],
        "webpage_url": entry.get("webpage_url") or raw_query,
        "title": entry.get("title") or "Unknown title",
        "duration": entry.get("duration") or 0,
        "uploader": entry.get("uploader") or "Unknown",
    }

async def _play_next():
    global now_playing_msg
    if shutting_down:
        return

    vc = voice_client
    if vc is None or not vc.is_connected():
        return

    if not music_queue:
        if text_channel:
            await text_channel.send(
                embed=discord.Embed(
                    title="Queue Ended!",
                    description="No more songs. Add some with `/play` or `/next`.",
                    color=discord.Color.blue(),
                )
            )
        _arm_idle_timer()
        return

    item = music_queue.pop(0)
    music_history.insert(0, item)

    try:
        if item["url"].startswith("http"):
            if not await _url_is_valid(item["url"]):
                entry = key_map.get(item["webpage_url"])
                if entry:
                    try:
                        await _refresh_entry_in_place(entry)
                        for k in ("url", "duration", "title", "uploader"):
                            item[k] = entry[k]
                    except Exception:
                        traceback.print_exc()
                        try:
                            flat = await asyncio.to_thread(
                                lambda: search_ydl.extract_info(f"ytsearch1:{entry['title']}", download=False)
                            )
                            entries = flat.get("entries") or []
                            if not entries:
                                raise RuntimeError("No entries returned while trying to recover track")
                            vid = entries[0]["id"]
                            raw = await asyncio.to_thread(
                                lambda: stream_ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
                            )
                            if "entries" in raw:
                                raw_entries = raw.get("entries") or []
                                if not raw_entries:
                                    raise RuntimeError("No entries returned for recovered video")
                                raw = raw_entries[0]
                            for k in ("url", "duration", "title", "uploader"):
                                entry[k] = raw.get(k)
                                item[k] = raw.get(k)
                            save_cache()
                        except Exception:
                            traceback.print_exc()
    except Exception:
        traceback.print_exc()

    def _after_playback(_):
        if shutting_down:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(_play_next(), bot.loop)
            fut.result()
        except Exception:
            if not shutting_down:
                traceback.print_exc()

    play_kwargs = {"after": _after_playback, **_voice_playback_kwargs()}
    try:
        vc.play(
            discord.FFmpegPCMAudio(
                item["url"],
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            ),
            **play_kwargs,
        )
    except discord.ClientException:
        if music_history and music_history[0] is item:
            music_history.pop(0)
        music_queue.insert(0, item)
        if not shutting_down:
            traceback.print_exc()
        return

    asyncio.create_task(_prefetch_next(2))

    if now_playing_msg:
        try:
            await now_playing_msg.delete()
        except discord.HTTPException as e:
            print(f"Failed to delete now playing message: {e}")

    embed = (
        discord.Embed(
            title="Now Playing",
            description=f"[{item['title']}]({item['webpage_url']}) ",
            color=discord.Color.blue(),
        )
        .set_author(name="Now Playing", icon_url=item["requester"].display_avatar.url)
        .add_field(name="Requested By", value=item["requester"].mention, inline=True)
        .add_field(name="Duration", value=f"`{format_duration(item['duration'])}`", inline=True)
        .add_field(name="Author", value=f"`{item['uploader']}`", inline=True)
    )

    if text_channel:
        now_playing_msg = await text_channel.send(
            embed=embed,
            view=NowPlayingView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )

def make_queue_embed(page: int = 0) -> discord.Embed:
    total = len(music_queue)
    e = discord.Embed(
        title=f"Queue - [{total} Tracks]",
        color=discord.Color.blue(),
    )
    if text_channel and text_channel.guild and text_channel.guild.icon:
        e.set_thumbnail(url=text_channel.guild.icon.url)

    start = page * 10
    lines = []
    for idx, item in enumerate(music_queue[start:start + 10], start=start + 1):
        title = (item["title"][:61] + "…") if len(item["title"]) > 61 else item["title"]
        lines.append(f"**{idx}.** [{title}]({item['webpage_url']}) `{format_duration(item['duration'])}`")

    e.description = "\n".join(lines) if lines else "_(empty)_"
    return e

class NowPlayingView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="🔀 Shuffle", style=discord.ButtonStyle.primary, custom_id="shuffle")
    async def shuffle(self, inter: discord.Interaction, _btn: Button):
        try:
            if music_queue:
                random.shuffle(music_queue)
                desc = f"{inter.user.mention} shuffled the queue"
            else:
                desc = f"{inter.user.mention} tried to shuffle but queue empty"
            await inter.response.send_message(
                embed=discord.Embed(description=desc, color=discord.Color.blue()),
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=False
            )
        except Exception as e:
            traceback.print_exc()
            await inter.response.send_message(f"🚫 {e}", ephemeral=True)

    @button(label="⏮️ Back", style=discord.ButtonStyle.primary, custom_id="previous")
    async def previous(self, inter: discord.Interaction, _btn: Button):
        try:
            if music_history and voice_client and voice_client.is_connected():
                prev = music_history.pop(0)
                music_queue.insert(0, prev)
                voice_client.stop()
                await inter.response.send_message(
                    embed=discord.Embed(
                        title="⏮ Playing Previous Track",
                        description=f"[{prev['title']}]({prev['webpage_url']})",
                        color=discord.Color.blue()
                    ),
                    allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                await inter.response.send_message(
                    embed=discord.Embed(
                        description=f"{inter.user.mention} tried to go back but none",
                        color=discord.Color.blue()
                    ),
                    allowed_mentions=discord.AllowedMentions.none()
                )
        except Exception as e:
            traceback.print_exc()
            await inter.response.send_message(f"🚫 {e}", ephemeral=True)

    @button(label="⏸ Pause", style=discord.ButtonStyle.primary, custom_id="pauseplay")
    async def pauseplay(self, inter: discord.Interaction, btn: Button):
        try:
            if not voice_client or not voice_client.is_connected():
                return await inter.response.send_message("🚫 I'm not in a voice channel.", ephemeral=True)

            if voice_client.is_playing():
                voice_client.pause()
                btn.label = "▶️ Play"
                _arm_idle_timer()
                await inter.response.edit_message(view=self)
                return await inter.followup.send(
                    embed=discord.Embed(description=f"{inter.user.mention} paused playback", color=discord.Color.blue()),
                    allowed_mentions=discord.AllowedMentions.none(),
                    ephemeral=False
                )

            if voice_client.is_paused():
                voice_client.resume()
                btn.label = "⏸ Pause"
                await inter.response.edit_message(view=self)
                return await inter.followup.send(
                    embed=discord.Embed(description=f"{inter.user.mention} resumed playback", color=discord.Color.blue()),
                    allowed_mentions=discord.AllowedMentions.none(),
                    ephemeral=False
                )

            if music_queue:
                await _play_next()
                return await inter.response.send_message(
                    embed=discord.Embed(description=f"{inter.user.mention} started playback", color=discord.Color.blue()),
                    allowed_mentions=discord.AllowedMentions.none(),
                    ephemeral=False
                )

            await inter.response.send_message(
                embed=discord.Embed(description=f"{inter.user.mention} tried to play but queue empty", color=discord.Color.blue()),
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=False
            )
        except Exception as e:
            traceback.print_exc()
            if not inter.response.is_done():
                await inter.response.send_message(f"🚫 {e}", ephemeral=True)
            else:
                await inter.followup.send(f"🚫 {e}", ephemeral=True)

        except Exception as e:
            traceback.print_exc()
            if not inter.response.is_done():
                await inter.response.send_message(f"🚫 {e}", ephemeral=True)
            else:
                await inter.followup.send(f"🚫 {e}", ephemeral=True)

    @button(label="⏭ Next", style=discord.ButtonStyle.primary, custom_id="skip")
    async def skip(self, inter: discord.Interaction, _btn: Button):
        try:
            if voice_client and voice_client.is_connected() and (voice_client.is_playing() or voice_client.is_paused()):
                current = music_history[0] if music_history else None
                voice_client.stop()
                if current:
                    await inter.response.send_message(
                        embed=discord.Embed(
                            title="⏭ Skipped",
                            description=f"[{current['title']}]({current['webpage_url']})",
                            color=discord.Color.blue()
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                        ephemeral=False
                    )
                else:
                    await inter.response.send_message(
                        embed=discord.Embed(
                            description=f"{inter.user.mention} skipped the track",
                            color=discord.Color.blue()
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                        ephemeral=False
                    )
            else:
                await inter.response.send_message(
                    embed=discord.Embed(
                        description=f"{inter.user.mention} tried to skip but nothing is playing",
                        color=discord.Color.blue()
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                    ephemeral=False
                )
        except Exception as e:
            traceback.print_exc()
            await inter.response.send_message(f"🚫 {e}", ephemeral=True)

    @button(label="⏹ Stop", style=discord.ButtonStyle.danger, custom_id="stop")
    async def stop(self, inter: discord.Interaction, _btn: Button):
        try:
            await clear_all()
            await inter.response.send_message(
                embed=discord.Embed(
                    description=f"{inter.user.mention} stopped playback and cleared the queue",
                    color=discord.Color.blue()
                ),
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=False
            )
        except Exception as e:
            traceback.print_exc()
            await inter.response.send_message(f"🚫 {e}", ephemeral=True)


class QueueView(View):
    def __init__(self, page: int = 0):
        super().__init__(timeout=None)
        self.page = page
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = (self.page + 1) * 10 >= len(music_queue)

    @button(label="⬅ Prev", style=discord.ButtonStyle.secondary, custom_id="prev_page")
    async def prev_page(self, inter: discord.Interaction, btn: Button):
        self.page -= 1
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = (self.page + 1) * 10 >= len(music_queue)
        await inter.response.edit_message(embed=make_queue_embed(self.page), view=self)

    @button(label="Next ➡", style=discord.ButtonStyle.secondary, custom_id="next_page")
    async def next_page(self, inter: discord.Interaction, btn: Button):
        self.page += 1
        self.next_page.disabled = (self.page + 1) * 10 >= len(music_queue)
        self.prev_page.disabled = self.page == 0
        await inter.response.edit_message(embed=make_queue_embed(self.page), view=self)

async def _extract_soundcloud_info(url: str) -> dict:
    def _run():
        soundcloud_opts = _build_ydl_opts()
        soundcloud_opts["geo_bypass"] = True
        soundcloud_opts["geo_bypass_country"] = "US"
        with YoutubeDL(soundcloud_opts) as ydl:
            return ydl.extract_info(url, download=False)

    raw = await asyncio.to_thread(_run)
    if "entries" in raw:
        entries = raw.get("entries") or []
        if not entries:
            raise RuntimeError("No entries returned for this SoundCloud URL")
        raw = entries[0]

    fmts = raw.get("formats") or []
    fmt_url = raw.get("url")
    chosen_headers = raw.get("http_headers") or {}

    if fmts:
        candidates = [f for f in fmts if f.get("acodec") not in (None, "none")]
        if not candidates:
            candidates = fmts
        best = _pick_soundcloud_format(candidates)
        fmt_url = best.get("url") or fmt_url
        if not chosen_headers:
            chosen_headers = best.get("http_headers") or chosen_headers

    if not fmt_url:
        print("SoundCloud extractor got no playable url for:", url)
        print("Raw info:", raw)
        raise RuntimeError("No playable audio URL found for this SoundCloud track")

    info = {
        "url": fmt_url,
        "webpage_url": raw.get("webpage_url") or url,
        "title": raw.get("title") or "Unknown title",
        "duration": raw.get("duration") or 0,
        "uploader": raw.get("uploader") or raw.get("uploader_id") or "Unknown",
        "http_headers": chosen_headers,
    }

    return info

async def _handle_add_legacy(inter: discord.Interaction, query: str, front: bool):
    try:
        raw_query = query.strip()
        if raw_query.lower().startswith("query:"):
            raw_query = raw_query[6:].strip()

        is_sc = bool(soundcloud_re.match(raw_query))
        is_yt = bool(url_re.match(raw_query))
        is_http = bool(generic_http_re.match(raw_query))

        entry = None

        if is_sc:
            info = await _extract_soundcloud_info(raw_query)
            entry = info

        elif is_yt:
            key = raw_query
            if USE_CACHE and key in key_map:
                entry = key_map[key]
            else:
                search_str = raw_query
                raw = await asyncio.to_thread(lambda: stream_ydl.extract_info(search_str, download=False))
                if "entries" in raw:
                    entries = raw.get("entries") or []
                    if not entries:
                        raise RuntimeError("No entries returned for this URL")
                    raw = entries[0]
                info = {k: raw.get(k) for k in ("url", "webpage_url", "title", "duration", "uploader")}
                entry = next((e for e in cache_entries if e["webpage_url"] == info["webpage_url"]), None)
                if entry:
                    entry.update(info)
                else:
                    entry = {"keys": [], **info}
                    cache_entries.append(entry)
                if USE_CACHE:
                    if key not in entry["keys"]:
                        entry["keys"].append(key)
                    canon = canonical_url(info["webpage_url"])
                    if canon and canon not in entry["keys"]:
                        entry["keys"].append(canon)
                    key_map.clear()
                    for e in cache_entries:
                        for k2 in e["keys"]:
                            key_map[k2] = e
                    save_cache()

        elif is_http:
            content_type = None
            sess = get_session()
            try:
                async with sess.head(raw_query, allow_redirects=True) as resp:
                    ct = resp.headers.get("Content-Type", "").lower()
                    if ct.startswith("audio/") or ct.startswith("video/"):
                        content_type = ct.split(";", 1)[0]
            except Exception:
                traceback.print_exc()

            if content_type:
                try:
                    async with sess.get(raw_query) as resp:
                        resp.raise_for_status()
                        path = urlparse(raw_query).path or ""
                        ext = os.path.splitext(path)[1]

                        if not ext:
                            ct_map = {
                                "audio/mpeg": ".mp3",
                                "audio/mp3": ".mp3",
                                "audio/ogg": ".ogg",
                                "audio/opus": ".opus",
                                "audio/wav": ".wav",
                                "audio/x-wav": ".wav",
                                "audio/flac": ".flac",
                                "video/mp4": ".mp4",
                                "video/webm": ".webm",
                                "video/x-matroska": ".mkv",
                            }
                            ext = ct_map.get(content_type, "")

                        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext if ext else None)
                        tmp_path = tmp.name
                        try:
                            async for chunk in resp.content.iter_chunked(1024 * 64):
                                if chunk:
                                    tmp.write(chunk)
                        finally:
                            tmp.close()

                    title = os.path.basename(path) or "Unknown title"
                    if "." in title:
                        title = ".".join(title.split(".")[:-1]) or title

                    uploader = "Remote File"
                    duration = 0

                    try:
                        audio = MutagenFile(tmp_path, easy=True)
                        if audio is not None:
                            if audio.tags:
                                if "title" in audio.tags and audio.tags["title"]:
                                    title = str(audio.tags["title"][0])
                                if "artist" in audio.tags and audio.tags["artist"]:
                                    uploader = str(audio.tags["artist"][0])
                            if hasattr(audio, "info") and getattr(audio.info, "length", None) is not None:
                                duration = int(audio.info.length)
                    except Exception:
                        traceback.print_exc()
                    finally:
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            traceback.print_exc()

                    entry = {
                        "url": raw_query,
                        "webpage_url": raw_query,
                        "title": title,
                        "duration": duration,
                        "uploader": uploader
                    }

                except Exception:
                    traceback.print_exc()
                    err_embed = discord.Embed(
                        title="Invalid Media",
                        description="URL downloaded but was not a valid audio/video file.",
                        color=discord.Color.red()
                    )
                    await inter.followup.send(embed=err_embed, ephemeral=True)
                    return

            else:
                try:
                    raw = await asyncio.to_thread(lambda: stream_ydl.extract_info(raw_query, download=False))
                    if "entries" in raw:
                        entries = raw.get("entries") or []
                        if not entries:
                            raise RuntimeError("No entries returned for this URL")
                        raw = entries[0]
                    entry = {
                        "url": raw.get("url"),
                        "webpage_url": raw.get("webpage_url"),
                        "title": raw.get("title"),
                        "duration": raw.get("duration"),
                        "uploader": raw.get("uploader"),
                    }
                except Exception:
                    traceback.print_exc()
                    err_embed = discord.Embed(
                        title="No Results",
                        description="Could not find any playable track from this URL.",
                        color=discord.Color.red()
                    )
                    await inter.followup.send(embed=err_embed, ephemeral=True)
                    return

        else:
            key = raw_query.lower()
            if USE_CACHE and key in key_map:
                entry = key_map[key]
            else:
                search_str = f"ytsearch1:{raw_query}"
                raw = await asyncio.to_thread(lambda: stream_ydl.extract_info(search_str, download=False))
                if "entries" in raw:
                    entries = raw.get("entries") or []
                    if not entries:
                        raise RuntimeError("No entries returned for this search")
                    raw = entries[0]
                info = {k: raw.get(k) for k in ("url", "webpage_url", "title", "duration", "uploader")}
                entry = next((e for e in cache_entries if e["webpage_url"] == info["webpage_url"]), None)
                if entry:
                    entry.update(info)
                else:
                    entry = {"keys": [], **info}
                    cache_entries.append(entry)
                if USE_CACHE:
                    if key not in entry["keys"]:
                        entry["keys"].append(key)
                    canon = canonical_url(info["webpage_url"])
                    if canon and canon not in entry["keys"]:
                        entry["keys"].append(canon)
                    key_map.clear()
                    for e in cache_entries:
                        for k2 in e["keys"]:
                            key_map[k2] = e
                    save_cache()

        if not entry or not entry.get("url"):
            err_embed = discord.Embed(
                title="No Results",
                description="Could not find a playable track from that query or URL.",
                color=discord.Color.red()
            )
            await inter.followup.send(embed=err_embed, ephemeral=True)
            return

        item = {
            "url": entry["url"],
            "webpage_url": entry["webpage_url"],
            "title": entry["title"],
            "duration": entry["duration"],
            "uploader": entry["uploader"],
            "requester": inter.user
        }

        if front:
            music_queue.insert(0, item)
            pos = 1
        else:
            music_queue.append(item)
            pos = len(music_queue)

        if pos == 1 and voice_client and not voice_client.is_playing() and not voice_client.is_paused():
            await _play_next()
        else:
            asyncio.create_task(_prefetch_next(2))

        await inter.followup.send(
            embed=discord.Embed(
                title=f"Added to Queue #{pos}",
                description=f"[{item['title']}]({item['webpage_url']}) `{format_duration(item['duration'])}`",
                color=discord.Color.blue()
            ),
            ephemeral=False
        )
    except Exception as e:
        traceback.print_exc()
        await inter.followup.send(f"🚫 Error: {e}", ephemeral=True)

async def _handle_file_add(inter: discord.Interaction, attachment: discord.Attachment, front: bool):
    try:
        if attachment is None:
            await inter.followup.send("🚫 No file provided.", ephemeral=True)
            return

        suffix = os.path.splitext(attachment.filename or "")[1]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix if suffix else None)
        tmp_path = tmp.name
        tmp.close()
        await attachment.save(tmp_path)

        title = attachment.filename or "Unknown title"
        uploader = "Local File"
        duration = 0

        try:
            audio = MutagenFile(tmp_path, easy=True)
            if audio is not None:
                if audio.tags:
                    if "title" in audio.tags and audio.tags["title"]:
                        title = str(audio.tags["title"][0])
                    if "artist" in audio.tags and audio.tags["artist"]:
                        uploader = str(audio.tags["artist"][0])
                if hasattr(audio, "info") and getattr(audio.info, "length", None) is not None:
                    duration = int(audio.info.length)
        except Exception:
            traceback.print_exc()

        try:
            os.remove(tmp_path)
        except Exception:
            traceback.print_exc()

        item = {
            "url": attachment.url,
            "webpage_url": attachment.url,
            "title": title,
            "duration": duration,
            "uploader": uploader,
            "requester": inter.user
        }

        if front:
            music_queue.insert(0, item)
            pos = 1
        else:
            music_queue.append(item)
            pos = len(music_queue)

        if pos == 1 and voice_client and not voice_client.is_playing() and not voice_client.is_paused():
            await _play_next()
        else:
            asyncio.create_task(_prefetch_next(2))

        await inter.followup.send(
            embed=discord.Embed(
                title=f"Added to Queue #{pos}",
                description=f"[{item['title']}]({item['webpage_url']}) " f"`{format_duration(item['duration'])}`",
                color=discord.Color.blue()
            ),
            ephemeral=False
        )
    except Exception as e:
        traceback.print_exc()
        await inter.followup.send(f"🚫 Error: {e}", ephemeral=True)

def _make_track_error_embed(exc: TrackResolveError) -> discord.Embed:
    lowered = str(exc).lower()
    title = "Invalid Media" if "invalid media" in lowered or "codec" in lowered else "No Results"
    return discord.Embed(title=title, description=str(exc), color=discord.Color.red())

def _parse_extinf(line: str) -> dict | None:
    try:
        payload = line.split(":", 1)[1].strip()
    except IndexError:
        return None

    if "," in payload:
        duration_raw, title_raw = payload.split(",", 1)
    else:
        duration_raw, title_raw = payload, ""

    duration = None
    try:
        parsed = int(float(duration_raw.strip()))
        if parsed > 0:
            duration = parsed
    except Exception:
        duration = None

    title = title_raw.strip() or None
    if not title and duration is None:
        return None

    return {"title": title, "duration": duration}

def _parse_playlist_entries(text: str, filename: str) -> tuple[list[dict], list[dict]]:
    suffix = os.path.splitext(filename or "")[1].lower()
    if suffix not in {".txt", ".m3u8"}:
        raise PlaylistFormatError("Unsupported playlist format. Upload a .txt or .m3u8 file.")

    entries: list[dict] = []
    errors: list[dict] = []
    pending_metadata = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if suffix == ".m3u8":
            if line.upper().startswith("#EXTINF:"):
                pending_metadata = _parse_extinf(line)
                continue
            if line.startswith("#"):
                continue

        if not generic_http_re.match(line):
            errors.append({
                "line": line_number,
                "error": "Invalid URL. Playlist files only accept absolute URLs."
            })
            if suffix == ".m3u8":
                pending_metadata = None
            continue

        entries.append({
            "line": line_number,
            "url": line,
            "metadata": pending_metadata if suffix == ".m3u8" else None,
        })
        pending_metadata = None

    return entries, errors

def _format_playlist_error_chunks(filename: str, errors: list[dict]) -> list[str]:
    if not errors:
        return []

    lines = []
    for item in sorted(errors, key=lambda err: err["line"]):
        reason = str(item["error"]).replace("`", "'")
        lines.append(f"Line {item['line']}: {reason}")

    chunks: list[str] = []
    current_lines: list[str] = []
    max_len = 1800

    for line in lines:
        candidate = "\n".join(current_lines + [line])
        if current_lines and len(candidate) > max_len:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        chunks.append("\n".join(current_lines))

    if len(chunks) == 1:
        return [f"Errors for `{filename}`\n{chunks[0]}"]

    total = len(chunks)
    return [f"Errors for `{filename}` ({idx}/{total})\n{chunk}" for idx, chunk in enumerate(chunks, start=1)]

class PlaylistErrorsView(View):
    def __init__(self, filename: str, errors: list[dict]):
        super().__init__(timeout=None)
        self.filename = filename
        self.errors = list(errors)
        self.show_errors.label = f"Show Errors ({len(self.errors)})"

    @button(label="Show Errors", style=discord.ButtonStyle.secondary, custom_id="playlist_show_errors")
    async def show_errors(self, inter: discord.Interaction, _btn: Button):
        chunks = _format_playlist_error_chunks(self.filename, self.errors)
        if not chunks:
            return await inter.response.send_message("No playlist errors recorded.", ephemeral=True)

        await inter.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await inter.followup.send(chunk, ephemeral=True)

async def _handle_add(inter: discord.Interaction, query: str, front: bool):
    try:
        entry = await _resolve_track_entry(query)
        item = _make_queue_item(entry, inter.user)
        pos = _enqueue_item(item, front)

        if pos == 1 and voice_client and not voice_client.is_playing() and not voice_client.is_paused():
            await _play_next()
        else:
            asyncio.create_task(_prefetch_next(2))

        await inter.followup.send(
            embed=discord.Embed(
                title=f"Added to Queue #{pos}",
                description=f"[{item['title']}]({item['webpage_url']}) `{format_duration(item['duration'])}`",
                color=discord.Color.blue()
            ),
            ephemeral=False
        )
    except TrackResolveError as e:
        await inter.followup.send(embed=_make_track_error_embed(e), ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        await inter.followup.send(f"Error: {e}", ephemeral=True)

async def _handle_playlist_add(inter: discord.Interaction, attachment: discord.Attachment, shuffle_queue: bool):
    progress_msg = None
    try:
        if attachment is None:
            await inter.followup.send("No file provided.", ephemeral=True)
            return

        filename = attachment.filename or "playlist"
        suffix = os.path.splitext(filename)[1].lower()
        if suffix not in {".txt", ".m3u8"}:
            await inter.followup.send("Unsupported playlist format. Upload a .txt or .m3u8 file.", ephemeral=True)
            return

        progress_msg = await inter.followup.send(
            embed=discord.Embed(
                title="Processing Playlist",
                description=f"Processing [{filename}]({attachment.url})",
                color=discord.Color.blue()
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            wait=True
        )

        raw = await attachment.read()
        text = raw.decode("utf-8-sig", errors="ignore")
        parsed_entries, errors = _parse_playlist_entries(text, filename)

        resolved_entries = []
        for parsed in parsed_entries:
            try:
                entry = await _resolve_track_entry(parsed["url"])
                resolved_entries.append(_apply_playlist_metadata(entry, parsed.get("metadata")))
            except TrackResolveError as exc:
                errors.append({"line": parsed["line"], "error": str(exc)})
            except Exception as exc:
                traceback.print_exc()
                errors.append({"line": parsed["line"], "error": f"Unexpected error: {exc}"})

        items = [_make_queue_item(entry, inter.user) for entry in resolved_entries]
        if items:
            music_queue.extend(items)
            if shuffle_queue:
                random.shuffle(music_queue)

        should_start = bool(items) and voice_client and not voice_client.is_playing() and not voice_client.is_paused()

        result_embed = discord.Embed(
            title=f"Added {len(items)} track{'s' if len(items) != 1 else ''} to queue",
            description=f"Imported from [{filename}]({attachment.url})",
            color=discord.Color.blue() if items else discord.Color.orange()
        )
        if errors:
            result_embed.add_field(name="Errors", value=str(len(errors)), inline=True)
        if shuffle_queue and items:
            result_embed.add_field(name="Queue", value="Shuffled", inline=True)

        view = PlaylistErrorsView(filename, errors) if errors else None
        await progress_msg.reply(
            embed=result_embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none()
        )
        if should_start:
            await _play_next()
        elif items:
            asyncio.create_task(_prefetch_next(2))
    except Exception as e:
        traceback.print_exc()
        error_embed = discord.Embed(
            title="Playlist Import Failed",
            description=str(e),
            color=discord.Color.red()
        )
        if progress_msg is not None:
            await progress_msg.reply(embed=error_embed, allowed_mentions=discord.AllowedMentions.none())
        else:
            await inter.followup.send(embed=error_embed, ephemeral=True)

@bot.tree.command(name="nowplaying", description="Refresh the Now Playing message")
async def nowplaying_cmd(inter: discord.Interaction):
    await inter.response.defer(thinking=True, ephemeral=False)

    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)

    if not (voice_client.is_playing() or voice_client.is_paused()) or not music_history:
        return await inter.followup.send("🚫 Nothing is currently playing.", ephemeral=True)

    item = music_history[0]

    global now_playing_msg
    if now_playing_msg:
        try:
            await now_playing_msg.delete()
        except discord.HTTPException as e:
            print(e)

    embed = (
        discord.Embed(
            title="Now Playing",
            description=f"[{item['title']}]({item['webpage_url']}) ",
            color=discord.Color.blue(),
        )
        .set_author(name="Now Playing", icon_url=item["requester"].display_avatar.url)
        .add_field(name="Requested By", value=item["requester"].mention, inline=True)
        .add_field(name="Duration", value=f"`{format_duration(item['duration'])}`", inline=True)
        .add_field(name="Author", value=f"`{item['uploader']}`", inline=True)
    )

    now_playing_msg = await inter.channel.send(
        embed=embed,
        view=NowPlayingView(),
        allowed_mentions=discord.AllowedMentions.none(),
    )

    await inter.followup.send("✅ Refreshed.", ephemeral=True)

@bot.tree.command(name="join", description="Join your voice channel")
async def join(inter: discord.Interaction):
    await inter.response.defer(thinking=True, ephemeral=False)
    if await ensure_voice(inter):
        await inter.followup.send("✅ Joined your voice channel.", ephemeral=False)

@bot.tree.command(name="play", description="Add a song to the queue")
@app_commands.describe(query="YouTube URL or search terms, or SoundCloud/other URL")
async def play(inter: discord.Interaction, query: str):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not await ensure_voice(inter):
        return
    asyncio.create_task(_handle_add(inter, query, False))

@bot.tree.command(name="playfile", description="Add an audio/video file to the queue")
@app_commands.describe(file="Audio or video file attachment")
async def playfile(inter: discord.Interaction, file: discord.Attachment):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not await ensure_voice(inter):
        return
    asyncio.create_task(_handle_file_add(inter, file, False))

@bot.tree.command(name="playlist", description="Import a .txt or .m3u8 playlist into the queue")
@app_commands.describe(file="Upload a .txt or .m3u8 playlist file", shuffle="Shuffle the queue before playback starts")
async def playlist(inter: discord.Interaction, file: discord.Attachment, shuffle: bool = False):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not await ensure_voice(inter):
        return
    asyncio.create_task(_handle_playlist_add(inter, file, shuffle))

@bot.tree.command(name="next", description="Add a song next in queue")
@app_commands.describe(query="YouTube URL or search terms, or SoundCloud/other URL")
async def play_next_cmd(inter: discord.Interaction, query: str):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not await ensure_voice(inter):
        return
    asyncio.create_task(_handle_add(inter, query, True))

@bot.tree.command(name="skip", description="Skip the current song")
async def skip(inter: discord.Interaction):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if not (voice_client.is_playing() or voice_client.is_paused()):
        return await inter.followup.send("⏭ Nothing is playing.", ephemeral=True)

    current = music_history[0] if music_history else None
    voice_client.stop()

    if current:
        return await inter.followup.send(
            embed=discord.Embed(
                title="⏭ Skipped",
                description=f"[{current['title']}]({current['webpage_url']})",
                color=discord.Color.blue()
            ),
            ephemeral=False
        )
    await inter.followup.send("⏭ Skipped.", ephemeral=False)

@bot.tree.command(name="previous", description="Play the previous song")
async def previous(inter: discord.Interaction):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if not music_history:
        return await inter.followup.send("🚫 No previous track.", ephemeral=True)

    prev = music_history.pop(0)
    music_queue.insert(0, prev)
    voice_client.stop()

    await inter.followup.send(
        embed=discord.Embed(
            title="⏮ Playing Previous Track",
            description=f"[{prev['title']}]({prev['webpage_url']})",
            color=discord.Color.blue()
        ),
        ephemeral=False
    )

@bot.tree.command(name="pauseplay", description="Toggle pause/play")
async def pauseplay(inter: discord.Interaction):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)

    if voice_client.is_playing():
        voice_client.pause()
        _arm_idle_timer()
        return await inter.followup.send("⏸ Paused.", ephemeral=False)

    if voice_client.is_paused():
        voice_client.resume()
        return await inter.followup.send("▶ Resumed.", ephemeral=False)

    if music_queue:
        await _play_next()
        return await inter.followup.send("▶ Started playing.", ephemeral=False)

    await inter.followup.send("🚫 Nothing in queue.")
        
@bot.tree.command(name="queue", description="Display the current queue")
async def queue_cmd(inter: discord.Interaction):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if not music_queue:
        return await inter.followup.send("📭 Queue is empty.", ephemeral=True)
    await inter.followup.send(embed=make_queue_embed(), view=QueueView(), ephemeral=False)

@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle_cmd(inter: discord.Interaction):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if not music_queue:
        return await inter.followup.send("📭 Queue is empty.", ephemeral=True)
    random.shuffle(music_queue)
    await inter.followup.send("🔀 Queue shuffled.", ephemeral=False)

@bot.tree.command(name="remove", description="Remove a song by its position")
@app_commands.describe(position="Position (1-based)")
async def remove_cmd(inter: discord.Interaction, position: int):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if position < 1 or position > len(music_queue):
        return await inter.followup.send("🚫 Invalid position.", ephemeral=True)
    removed = music_queue.pop(position - 1)
    embed = discord.Embed(
        title="Removed",
        description=f"[{removed['title']}]({removed['webpage_url']})",
        color=discord.Color.blue()
    )
    await inter.followup.send(embed=embed, ephemeral=False)

@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop_cmd(inter: discord.Interaction):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    await clear_all()
    await inter.followup.send("🛑 Stopped and cleared queue.", ephemeral=False)

async def check_permission(inter: discord.Interaction, OWNER_ONLY: bool):
    app_info = await inter.client.application_info()

    if OWNER_ONLY:
        allowed_ids = set()

        if app_info.owner:
            allowed_ids.add(app_info.owner.id)

        if hasattr(app_info, "team") and app_info.team:
            allowed_ids.update(m.id for m in app_info.team.members)

        if inter.user.id not in allowed_ids:
            await inter.response.send_message("🚫 Only the bot owner can run this.", ephemeral=True)
            return False
    else:
        if not inter.user.guild_permissions.administrator:
            await inter.response.send_message("🚫 Admins only.", ephemeral=True)
            return False
        
    return True

@bot.tree.command(name="addkey", description="Add custom cache entry")
@app_commands.describe(query="Search key", video_url="YouTube URL")
async def addkey(inter: discord.Interaction, query: str, video_url: str):
    if not await check_permission(inter, OWNER_ONLY=True):
        return
    
    await inter.response.defer(thinking=True, ephemeral=False)
    canon = canonical_url(video_url)
    if not canon:
        return await inter.followup.send("🚫 Invalid YouTube URL.", ephemeral=True)
    raw = await asyncio.to_thread(lambda: stream_ydl.extract_info(canon, download=False))
    if "entries" in raw:
        entries = raw.get("entries") or []
        if not entries:
            return await inter.followup.send("🚫 No entries returned for this URL.", ephemeral=True)
        raw = entries[0]
    info = {k: raw.get(k) for k in ("url", "webpage_url", "title", "duration", "uploader")}
    entry = next((e for e in cache_entries if e["webpage_url"] == info["webpage_url"]), None)
    if entry:
        entry.update(info)
    else:
        entry = {"keys": [], **info}
        cache_entries.append(entry)
    lc_q = query.strip().lower()
    for k in (lc_q, canon):
        if k not in entry["keys"]:
            entry["keys"].append(k)
    key_map.clear()
    for e in cache_entries:
        for kk in e["keys"]:
            key_map[kk] = e
    save_cache()
    await inter.followup.send(f"✅ Cached `{lc_q}` as `{canon}`", ephemeral=OWNER_ONLY)

@bot.tree.command(name="reloadcache", description="Reload cache from disk")
async def reloadcache(inter: discord.Interaction):
    if not await check_permission(inter, OWNER_ONLY=True):
        return
    
    await inter.response.defer(thinking=True, ephemeral=False)
    load_cache()
    await inter.followup.send("✅ Cache reloaded from disk.", ephemeral=OWNER_ONLY)

@bot.tree.command(name="exportcache", description="Export cache as JSON (admin only)")
async def exportcache(inter: discord.Interaction):
    if not await check_permission(inter, OWNER_ONLY=True):
        return
    
    await inter.response.send_message("📁 Here is the cache file:", file=discord.File(CACHE_FILE), ephemeral=True)

@bot.tree.command(name="importcache", description="Import cache from JSON file (admin only)")
@app_commands.describe(file="Upload the JSON cache export")
async def importcache(inter: discord.Interaction, file: discord.Attachment):
    if not await check_permission(inter, OWNER_ONLY=True):
        return

    if file is None:
        return await inter.response.send_message("🚫 Attach a JSON file.", ephemeral=True)

    await inter.response.defer()

    try:
        raw = await file.read()
        data = json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception as e:
        traceback.print_exc()
        return await inter.followup.send(f"🚫 Failed to read JSON: {e}", ephemeral=True)

    if not isinstance(data, list):
        return await inter.followup.send("🚫 JSON must be a list.", ephemeral=True)

    REQUIRED = {"keys", "url", "webpage_url", "title", "duration", "uploader"}
    invalid = {}

    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or set(entry.keys()) != REQUIRED:
            invalid[i] = "Fields mismatch"
            continue

        info = {f: entry[f] for f in ("url", "webpage_url", "title", "duration", "uploader")}
        merged = next((e for e in cache_entries if e["webpage_url"] == info["webpage_url"]), None)
        if merged:
            merged.update(info)
        else:
            merged = {"keys": [], **info}
            cache_entries.append(merged)

        for k in entry["keys"]:
            if k not in merged["keys"]:
                merged["keys"].append(k)

    key_map.clear()
    for e in cache_entries:
        for kk in e["keys"]:
            key_map[kk] = e

    save_cache()

    msg = "✅ Cache updated!"
    if invalid:
        err = "\n".join(f"Entry {i}: {m}" for i, m in invalid.items())
        msg += f"\n⚠️ Skipped:\n```{err}```"

    await inter.followup.send(msg, ephemeral=OWNER_ONLY)
    
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

@bot.event
async def on_disconnect():
    await close_session()

@bot.event
async def on_voice_state_update(member, before, after):
    global music_queue, music_history, voice_client, now_playing_msg, text_channel, disconnect_task

    if member == bot.user and before.channel and not after.channel:
        await _cancel_disconnect_task()
        music_queue.clear()
        music_history.clear()
        voice_client = None
        now_playing_msg = None
        text_channel = None
        return

    if voice_client and before.channel == voice_client.channel and after.channel != voice_client.channel:
        channel = voice_client.channel
        if channel:
            non_bot_members = [m for m in channel.members if not m.bot]
            if not non_bot_members and voice_client.is_paused():
                _arm_idle_timer()

async def _run_bot():
    async with bot:
        try:
            await bot.start(TOKEN)
        finally:
            await shutdown_cleanup()

def main():
    discord.utils.setup_logging(root=False)
    try:
        asyncio.run(_run_bot())
    except KeyboardInterrupt:
        return

if __name__ == "__main__":
    main()
