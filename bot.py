import os
import json
import re
import random
import asyncio
from urllib.parse import urlparse, parse_qs

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, button, Button
from yt_dlp import YoutubeDL

TOKEN       = "bot token"
USE_CACHE   = True
CACHE_FILE  = "cache.json"

cache_entries = []
key_map       = {}

def load_cache():
    global cache_entries, key_map
    cache_entries = []
    key_map = {}
    if USE_CACHE and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                cache_entries = data
            else:
                for k,v in data.items():
                    entry = {"keys":[k], **v}
                    cache_entries.append(entry)
            for entry in cache_entries:
                for k in entry.get("keys", []):
                    key_map[k] = entry
        except:
            cache_entries = []
            key_map = {}

def save_cache():
    if USE_CACHE:
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache_entries, f, ensure_ascii=False, indent=2)
        except:
            pass

load_cache()

ydl_opts = {
    'format': 'bestaudio[abr<=64]',
    'quiet': True,
    'noplaylist': True,
    'extract_flat': False,
    'forcejson': True,
    'nocheckcertificate': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'geo_bypass': True,
    'geo_bypass_country': 'US',
}

url_re = re.compile(r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/')

intents = discord.Intents.default()
intents.voice_states = True
bot = commands.Bot(command_prefix="/", intents=intents)

music_queue   = []
music_history = []
voice_client: discord.VoiceClient = None
text_channel: discord.TextChannel = None
now_playing_msg: discord.Message = None
disconnect_task: asyncio.Task = None

def format_duration(sec: int) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

async def ensure_voice(inter: discord.Interaction) -> bool:
    global voice_client, text_channel
    vc = getattr(inter.user.voice, "channel", None)
    if not vc:
        await inter.followup.send("🚫 You must be in a voice channel first.", ephemeral=True)
        return False
    if not voice_client or not voice_client.is_connected():
        voice_client = await vc.connect()
    elif voice_client.channel != vc:
        await inter.followup.send("🚫 You must be in the same VC as me to control me.", ephemeral=True)
        return False
    text_channel = inter.channel
    return True

def canonical_url(raw_url: str) -> str | None:
    o = urlparse(raw_url)
    net = o.netloc.lower()
    if 'youtu.be' in net:
        vid = o.path.lstrip('/')
    elif 'youtube.com' in net:
        if o.path == '/watch':
            q = parse_qs(o.query)
            vid = q.get('v',[None])[0]
        elif o.path.startswith('/shorts/'):
            vid = o.path.split('/')[2]
        else:
            return None
    else:
        return None
    if not vid:
        return None
    return f"https://www.youtube.com/watch?v={vid}"

async def auto_disconnect():
    global voice_client, disconnect_task
    await asyncio.sleep(300)
    if voice_client and not voice_client.is_playing() and not music_queue:
        await voice_client.disconnect()
        clear_all()

def clear_all():
    global music_queue, music_history, voice_client, now_playing_msg
    music_queue.clear()
    music_history.clear()
    if voice_client and voice_client.is_connected():
        asyncio.create_task(voice_client.disconnect())
    voice_client = None
    now_playing_msg = None

async def _play_next():
    global now_playing_msg, disconnect_task
    if not music_queue:
        e = discord.Embed(
            title="Queue Ended!",
            description="There are no more songs in the queue.\nYou can add songs with `/play` or `/next`",
            color=discord.Color.blue()
        )
        e.set_thumbnail(url=bot.user.avatar.url)
        await text_channel.send(embed=e)
        if disconnect_task:
            disconnect_task.cancel()
        disconnect_task = bot.loop.create_task(auto_disconnect())
        return

    item = music_queue.pop(0)
    if not item.get('url'):
        return await _play_next()

    source = discord.FFmpegPCMAudio(
        item['url'],
        before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
    )
    voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(_play_next(), bot.loop))

    if now_playing_msg:
        try: await now_playing_msg.delete()
        except: pass

    embed = discord.Embed(
        title="Now Playing",
        description=f"[{item['title']}]({item['webpage_url']}) [`{format_duration(item['duration'])}`]",
        color=discord.Color.blue()
    )
    embed.set_author(name="Now Playing", icon_url=item['requester'].avatar.url)
    embed.add_field(name="Requested By", value=item['requester'].mention, inline=True)
    embed.add_field(name="Duration",     value=f"`{format_duration(item['duration'])}`", inline=True)
    embed.add_field(name="Author",       value=f"`{item['uploader']}`", inline=True)

    view = NowPlayingView()
    now_playing_msg = await text_channel.send(
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions.none()
    )

async def _handle_add(inter: discord.Interaction, query: str, front: bool):
    try:
        opts = ydl_opts.copy()
        def extract(q):
            with YoutubeDL(opts) as y:
                return y.extract_info(q, False)
        key = query.strip() if url_re.match(query) else query.strip().lower()
        if USE_CACHE and key in key_map:
            entry = key_map[key]
        else:
            search_str = query if url_re.match(query) else f"ytsearch1:{query}"
            raw = await asyncio.to_thread(extract, search_str)
            if 'entries' in raw:
                raw = raw['entries'][0]
            info = {
                'url': raw['url'],
                'webpage_url': raw['webpage_url'],
                'title': raw['title'],
                'duration': raw['duration'],
                'uploader': raw.get('uploader','')
            }
            entry = None
            for e in cache_entries:
                if e.get('webpage_url') == info['webpage_url']:
                    e.update(info)
                    entry = e
                    break
            if not entry:
                entry = {'keys':[], **info}
                cache_entries.append(entry)
            if key not in entry['keys']:
                entry['keys'].append(key)
            canon = canonical_url(info['webpage_url'])
            if canon and canon not in entry['keys']:
                entry['keys'].append(canon)
            key_map.clear()
            for e in cache_entries:
                for k in e['keys']:
                    key_map[k] = e
            save_cache()

        item = {k: entry[k] for k in ('url','webpage_url','title','duration','uploader')}
        item['requester'] = inter.user

        if front:
            music_queue.insert(0, item)
            title_text = "Song Added to Queue #1"
        else:
            music_queue.append(item)
            title_text = f"Song Added to Queue #{len(music_queue)}"

        if len(music_queue) == (1 if not front else 0) and not voice_client.is_playing() and not voice_client.is_paused():
            await _play_next()

        embed = discord.Embed(
            title=title_text,
            description=f"[{item['title']}]({item['webpage_url']}) [`{format_duration(item['duration'])}`]",
            color=discord.Color.blue()
        )
        embed.set_author(name=inter.user.display_name, icon_url=inter.user.avatar.url)
        await inter.followup.send(embed=embed)
    except Exception as e:
        await inter.followup.send(f"🚫 Error: {e}", ephemeral=True)

class NowPlayingView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="🔀 Shuffle", style=discord.ButtonStyle.primary, custom_id="shuffle")
    async def shuffle(self, inter, btn):
        if music_queue:
            random.shuffle(music_queue)
            desc = f"@{inter.user.name} shuffled the queue"
        else:
            desc = f"@{inter.user.name} tried to shuffle but the queue is empty"
        embed = discord.Embed(description=desc, color=discord.Color.blue())
        await inter.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @button(label="⏪ Back", style=discord.ButtonStyle.primary, custom_id="previous")
    async def previous(self, inter, btn):
        if music_history:
            prev = music_history.pop(0)
            music_queue.insert(0, prev)
            voice_client.stop()
            desc = f"@{inter.user.name} playing previous track: {prev['title']}"
        else:
            desc = f"@{inter.user.name} tried to go back but no previous track"
        embed = discord.Embed(description=desc, color=discord.Color.blue())
        await inter.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @button(label="⏸ Pause", style=discord.ButtonStyle.primary, custom_id="pauseplay")
    async def pauseplay(self, inter, btn):
        if voice_client.is_playing():
            voice_client.pause()
            btn.label = "▶️ Play"
            desc = f"@{inter.user.name} paused playback"
        elif voice_client.is_paused():
            voice_client.resume()
            btn.label = "⏸ Pause"
            desc = f"@{inter.user.name} resumed playback"
        elif music_queue:
            await _play_next()
            btn.label = "⏸ Pause"
            desc = f"@{inter.user.name} started playback"
        else:
            desc = f"@{inter.user.name} tried to play but the queue is empty"
            embed = discord.Embed(description=desc, color=discord.Color.blue())
            return await inter.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        await inter.response.edit_message(view=self)
        embed = discord.Embed(description=desc, color=discord.Color.blue())
        await inter.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @button(label="⏭ Next", style=discord.ButtonStyle.primary, custom_id="skip")
    async def skip(self, inter, btn):
        if voice_client.is_playing():
            voice_client.stop()
            desc = f"@{inter.user.name} skipped the track"
        else:
            desc = f"@{inter.user.name} tried to skip but nothing is playing"
        embed = discord.Embed(description=desc, color=discord.Color.blue())
        await inter.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @button(label="⏹ Stop", style=discord.ButtonStyle.danger, custom_id="stop")
    async def stop(self, inter, btn):
        clear_all()
        desc = f"@{inter.user.name} stopped playback and cleared the queue"
        embed = discord.Embed(description=desc, color=discord.Color.blue())
        await inter.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

class QueueView(View):
    def __init__(self, page=0):
        super().__init__(timeout=None)
        self.page = page
        if page > 0:
            self.add_item(Button(label="Prev", custom_id="prev_page"))
        if (page+1)*10 < len(music_queue):
            self.add_item(Button(label="Next", custom_id="next_page"))

    @button(label="Prev", custom_id="prev_page")
    async def prev_page(self, inter, btn):
        self.page -= 1
        await inter.response.edit_message(embed=make_queue_embed(self.page), view=self)

    @button(label="Next", custom_id="next_page")
    async def next_page(self, inter, btn):
        self.page += 1
        await inter.response.edit_message(embed=make_queue_embed(self.page), view=self)

def make_queue_embed(page=0) -> discord.Embed:
    total = len(music_queue)
    guild = text_channel.guild
    e = discord.Embed(
        title=f"Queue for {guild.name} - [{total} Tracks]",
        color=discord.Color.blue()
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    start = page * 10
    for idx, item in enumerate(music_queue[start:start+10], start=start+1):
        title = (item['title'][:61] + "…") if len(item['title'])>61 else item['title']
        e.add_field(
            name="\u200b",
            value=f"`{idx}.` [{title}]({item['webpage_url']}) `{format_duration(item['duration'])}`",
            inline=False
        )
    return e

@bot.tree.command(name="join", description="Join your voice channel (or move me there)")
async def join(inter: discord.Interaction):
    await inter.response.defer(thinking=True)
    if await ensure_voice(inter):
        await inter.followup.send("✅ Joined your voice channel.", ephemeral=False)

@bot.tree.command(name="play", description="Add a song to the queue")
@app_commands.describe(query="YouTube URL or search terms")
async def play(inter: discord.Interaction, query: str):
    await inter.response.defer(thinking=True)
    if not await ensure_voice(inter):
        return
    asyncio.create_task(_handle_add(inter, query, False))

@bot.tree.command(name="next", description="Add a song next in queue")
@app_commands.describe(query="YouTube URL or search terms")
async def play_next_cmd(inter: discord.Interaction, query: str):
    await inter.response.defer(thinking=True)
    if not await ensure_voice(inter):
        return
    asyncio.create_task(_handle_add(inter, query, True))

@bot.tree.command(name="skip", description="Skip the current song")
async def skip(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if not voice_client.is_playing():
        return await inter.followup.send("⏭ Nothing is playing.", ephemeral=True)
    voice_client.stop()
    await inter.followup.send("⏭ Skipped.", ephemeral=False)

@bot.tree.command(name="previous", description="Play the previous song")
async def previous(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    if not music_history:
        return await inter.followup.send("🚫 No previous track.", ephemeral=True)
    prev = music_history.pop(0)
    music_queue.insert(0, prev)
    voice_client.stop()
    await inter.followup.send("⏮ Playing previous track.", ephemeral=False)

@bot.tree.command(name="pauseplay", description="Toggle pause/play")
async def pauseplay(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if voice_client.is_playing():
        voice_client.pause()
        return await inter.followup.send("⏸ Paused.", ephemeral=False)
    if voice_client.is_paused():
        voice_client.resume()
        return await inter.followup.send("▶ Resumed.", ephemeral=False)
    if music_queue:
        await _play_next()
        return await inter.followup.send("▶ Started playing.", ephemeral=False)
    await inter.followup.send("🚫 Nothing in queue.", ephemeral=True)

@bot.tree.command(name="queue", description="Display the current queue")
async def queue_cmd(inter: discord.Interaction):
    await inter.response.defer(thinking=True)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if not music_queue:
        return await inter.followup.send("📭 Queue is empty.", ephemeral=True)
    e = make_queue_embed(0)
    await inter.followup.send(embed=e, view=QueueView())

@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle_cmd(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if not music_queue:
        return await inter.followup.send("📭 Queue is empty.", ephemeral=True)
    random.shuffle(music_queue)
    await inter.followup.send("🔀 Queue shuffled.", ephemeral=False)

@bot.tree.command(name="remove", description="Remove a song by its position in the queue")
@app_commands.describe(position="Position (1‑based)")
async def remove_cmd(inter: discord.Interaction, position: int):
    await inter.response.defer(ephemeral=True)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if position < 1 or position > len(music_queue):
        return await inter.followup.send("🚫 Invalid position.", ephemeral=True)
    removed = music_queue.pop(position-1)
    embed = discord.Embed(
        title="Removed",
        description=f"[{removed['title']}]({removed['webpage_url']})",
        color=discord.Color.blue()
    )
    await inter.followup.send(embed=embed, ephemeral=False)

@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop_cmd(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    clear_all()
    await inter.followup.send("🛑 Stopped and cleared queue.", ephemeral=False)

@bot.tree.command(name="addkey", description="Add custom cache entry (query → video)")
@app_commands.describe(query="Search key", video_url="YouTube URL")
async def addkey(inter: discord.Interaction, query: str, video_url: str):
    await inter.response.defer(thinking=True)
    canon = canonical_url(video_url)
    if not canon:
        return await inter.followup.send("🚫 Invalid YouTube URL.", ephemeral=True)
    raw = await asyncio.to_thread(lambda: YoutubeDL(ydl_opts).extract_info(canon, False))
    if 'entries' in raw:
        raw = raw['entries'][0]
    info = {
        'url': raw['url'],
        'webpage_url': raw['webpage_url'],
        'title': raw['title'],
        'duration': raw['duration'],
        'uploader': raw.get('uploader','')
    }
    entry = None
    for e in cache_entries:
        if e['webpage_url'] == info['webpage_url']:
            e.update(info)
            entry = e
            break
    if not entry:
        entry = {'keys': [], **info}
        cache_entries.append(entry)
    lc_q = query.strip().lower()
    if lc_q not in entry['keys']:
        entry['keys'].append(lc_q)
    if canon not in entry['keys']:
        entry['keys'].append(canon)
    key_map.clear()
    for e in cache_entries:
        for k in e['keys']:
            key_map[k] = e
    save_cache()
    await inter.followup.send(f"✅ Cached `{lc_q}` → {canon}", ephemeral=False)

@bot.tree.command(name="reloadcache", description="Reload cache from disk")
async def reloadcache(inter: discord.Interaction):
    await inter.response.defer(thinking=True)
    load_cache()
    await inter.followup.send("✅ Cache reloaded from disk.", ephemeral=False)

@bot.tree.command(name="exportcache", description="Export cache as JSON (admin only)")
async def exportcache(inter: discord.Interaction):
    if not inter.user.guild_permissions.administrator:
        return await inter.response.send_message("🚫 Admins only.", ephemeral=True)
    await inter.response.send_message(
        "📁 Here is the cache file:",
        file=discord.File(CACHE_FILE),
        ephemeral=True
    )

@bot.tree.command(name="importcache", description="Import cache from JSON file (admin only)")
async def importcache(inter: discord.Interaction):
    if not inter.user.guild_permissions.administrator:
        return await inter.response.send_message("🚫 Admins only.", ephemeral=True)
    if not inter.attachments:
        return await inter.response.send_message("🚫 Attach a JSON file.", ephemeral=True)
    att = inter.attachments[0]
    await inter.response.defer(thinking=True)
    try:
        data = json.loads(await att.read())
    except json.JSONDecodeError as e:
        return await inter.followup.send(f"🚫 JSON parse error:\n```{e}```", ephemeral=True)
    if not isinstance(data, list):
        return await inter.followup.send("🚫 JSON must be a list of entries.", ephemeral=True)

    REQUIRED = {'keys','url','webpage_url','title','duration','uploader'}
    invalid = {}
    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            invalid[idx] = "Not an object"; continue
        if set(entry.keys()) != REQUIRED:
            invalid[idx] = f"Fields mismatch: {set(entry.keys()).symmetric_difference(REQUIRED)}"; continue
        info = {f: entry[f] for f in ('url','webpage_url','title','duration','uploader')}
        merged = None
        for e in cache_entries:
            if e['webpage_url'] == info['webpage_url']:
                e.update(info)
                merged = e
                break
        if not merged:
            merged = {'keys': [], **info}
            cache_entries.append(merged)
        for k in entry['keys']:
            if k not in merged['keys']:
                merged['keys'].append(k)

    key_map.clear()
    for e in cache_entries:
        for k in e['keys']:
            key_map[k] = e
    save_cache()

    if invalid:
        err = "\n".join(f"Entry {i}: {msg}" for i,msg in invalid.items())
        await inter.followup.send(f"⚠️ Skipped entries:\n```{err}```", ephemeral=True)

    await inter.followup.send("✅ Cache updated!", ephemeral=False)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

@bot.event
async def on_voice_state_update(member, before, after):
    if member == bot.user and before.channel and not after.channel:
        music_queue.clear()
        music_history.clear()

bot.run(TOKEN)