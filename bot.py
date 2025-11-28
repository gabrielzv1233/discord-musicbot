import aiohttp, asyncio, discord, random, json, re, os
from discord.ui import View, Button, button
from urllib.parse import urlparse, parse_qs
from discord import app_commands
from discord.ext import commands
from yt_dlp import YoutubeDL

TOKEN = "bot token"
CACHE_FILE = "cache.json" # Json file to store cache
OWNER_ONLY = True # Restrict some commands to bot owner only (cache management commands)
USE_CACHE = True # Disabling bypasses cache entirely

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
        except Exception:
            cache_entries, key_map = [], {}

def save_cache():
    if not USE_CACHE:
        return
    tmp = CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache_entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

load_cache()

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
    "extractor_args": {"youtube": {
        "player_client": ["android", "web"],
        "skip": ["dash"]
    }},
    "player_skip": ["webpage"],
    "noprogress": True,
    "concurrent_fragment_downloads": 1,
    "skip_download": True
}
if os.path.exists(cookiefile) and os.path.getsize(cookiefile) > 0:
    ydl_opts["cookiefile"] = cookiefile

stream_ydl = YoutubeDL(ydl_opts)
search_ydl = YoutubeDL({**ydl_opts, "extract_flat": True})
url_re = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/")

intents = discord.Intents.all()
intents.voice_states = True
bot = commands.Bot(command_prefix="/", intents=intents)

music_queue: list[dict] = []
music_history: list[dict] = []
voice_client: discord.VoiceClient | None = None
text_channel: discord.TextChannel | None = None
now_playing_msg: discord.Message | None = None
disconnect_task: asyncio.Task | None = None

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

def clear_all():
    global music_queue, music_history, voice_client, now_playing_msg

    vc = voice_client
    msg = now_playing_msg

    music_queue.clear()
    music_history.clear()
    voice_client = None
    now_playing_msg = None

    if vc and vc.is_connected():
        async def _disc(v: discord.VoiceClient):
            try:
                await v.disconnect()
            except discord.HTTPException as e:
                print(f"Failed to disconnect: {e}")

        asyncio.create_task(_disc(vc))

    if msg:
        async def _delete_msg(m: discord.Message):
            try:
                await m.delete()
            except discord.HTTPException as e:
                print(f"Failed to delete now playing message: {e}")

        asyncio.create_task(_delete_msg(msg))

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
        clear_all()
        return

    if not voice_client.is_playing() and not voice_client.is_paused() and not music_queue:
        clear_all()

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
    except Exception:
        return False

async def _refresh_entry_in_place(entry: dict) -> dict:
    raw = await asyncio.to_thread(lambda: stream_ydl.extract_info(entry["webpage_url"], download=False))
    if "entries" in raw:
        raw = raw["entries"][0]
    for k in ("url", "duration", "title", "uploader"):
        entry[k] = raw.get(k)
    save_cache()
    return entry

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
        await asyncio.gather(*tasks, return_exceptions=True)

async def _play_next():
    global now_playing_msg
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

    if not await _url_is_valid(item["url"]):
        entry = key_map.get(item["webpage_url"])
        if entry:
            try:
                await _refresh_entry_in_place(entry)
                for k in ("url", "duration", "title", "uploader"):
                    item[k] = entry[k]
            except Exception:
                try:
                    flat = await asyncio.to_thread(lambda: search_ydl.extract_info(f"ytsearch1:{entry['title']}", download=False))
                    vid = flat["entries"][0]["id"]
                    raw = await asyncio.to_thread(lambda: stream_ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False))
                    for k in ("url", "duration", "title", "uploader"):
                        entry[k] = raw.get(k)
                        item[k] = raw.get(k)
                    save_cache()
                except Exception:
                    pass

    def _after_playback(_):
        fut = asyncio.run_coroutine_threadsafe(_play_next(), bot.loop)
        try:
            fut.result()
        except Exception:
            pass

    voice_client.play(
        discord.FFmpegPCMAudio(
            item["url"],
            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        ),
        after=_after_playback,
    )

    asyncio.create_task(_prefetch_next(2))

    if now_playing_msg:
        try:
            await now_playing_msg.delete()
        except discord.HTTPException:
            pass

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
            await inter.response.send_message(f"🚫 {e}", ephemeral=True)

    @button(label="⏸ Pause", style=discord.ButtonStyle.primary, custom_id="pauseplay")
    async def pauseplay(self, inter: discord.Interaction, btn: Button):
        try:
            if not voice_client or not voice_client.is_connected():
                return await inter.response.send_message("🚫 I'm not in a voice channel.", ephemeral=True)

            if voice_client.is_playing():
                voice_client.pause()
                btn.label = "▶️ Play"
                _arm_idle_timer()  # start 5-minute timer
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
            if not inter.response.is_done():
                await inter.response.send_message(f"🚫 {e}", ephemeral=True)
            else:
                await inter.followup.send(f"🚫 {e}", ephemeral=True)

        except Exception as e:
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
            await inter.response.send_message(f"🚫 {e}", ephemeral=True)

    @button(label="⏹ Stop", style=discord.ButtonStyle.danger, custom_id="stop")
    async def stop(self, inter: discord.Interaction, _btn: Button):
        try:
            clear_all()
            await inter.response.send_message(
                embed=discord.Embed(
                    description=f"{inter.user.mention} stopped playback and cleared the queue",
                    color=discord.Color.blue()
                ),
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=False
            )
        except Exception as e:
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

async def _handle_add(inter: discord.Interaction, query: str, front: bool):
    try:
        key = query.strip() if url_re.match(query) else query.strip().lower()
        if USE_CACHE and key in key_map:
            entry = key_map[key]
        else:
            search_str = query if url_re.match(query) else f"ytsearch1:{query}"
            raw = await asyncio.to_thread(lambda: stream_ydl.extract_info(search_str, download=False))
            if "entries" in raw:
                raw = raw["entries"][0]
            info = {k: raw.get(k) for k in ("url", "webpage_url", "title", "duration", "uploader")}
            entry = next((e for e in cache_entries if e["webpage_url"] == info["webpage_url"]), None)
            if entry:
                entry.update(info)
            else:
                entry = {"keys": [], **info}
                cache_entries.append(entry)
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

        item = {k: entry[k] for k in ("url", "webpage_url", "title", "duration", "uploader")}
        item["requester"] = inter.user

        if front:
            music_queue.insert(0, item)
            pos = 1
        else:
            music_queue.append(item)
            pos = len(music_queue)

        if pos == 1 and not voice_client.is_playing() and not voice_client.is_paused():
            await _play_next()
        else:
            asyncio.create_task(_prefetch_next(2))

        await inter.followup.send(embed=discord.Embed(title=f"Added to Queue #{pos}", description=f"[{item['title']}]({item['webpage_url']}) " f"`{format_duration(item['duration'])}`", color=discord.Color.blue()), ephemeral=False)
    except Exception as e:
        await inter.followup.send(f"🚫 Error: {e}", ephemeral=True)

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
        except discord.HTTPException:
            pass

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
@app_commands.describe(query="YouTube URL or search terms")
async def play(inter: discord.Interaction, query: str):
    await inter.response.defer(thinking=True, ephemeral=False)
    if not await ensure_voice(inter):
        return
    asyncio.create_task(_handle_add(inter, query, False))

@bot.tree.command(name="next", description="Add a song next in queue")
@app_commands.describe(query="YouTube URL or search terms")
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
    clear_all()
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
        raw = raw["entries"][0]
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
    global music_queue, music_history, voice_client

    if member == bot.user and before.channel and not after.channel:
        music_queue.clear()
        music_history.clear()
        return

    if voice_client and before.channel == voice_client.channel and after.channel != voice_client.channel:
        channel = voice_client.channel
        if channel:
            non_bot_members = [m for m in channel.members if not m.bot]
            if not non_bot_members and voice_client.is_paused():
                _arm_idle_timer()

bot.run(TOKEN)
