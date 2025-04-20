import re, random, asyncio
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, button, Button
from yt_dlp import YoutubeDL

# ─── CONFIG & CACHING ───────────────────────────────────────────────────────
TOKEN       = "bot token"
USE_CACHE   = True
track_cache = {}

ydl_opts = {
    'format':           'bestaudio/best',
    'quiet':            True,
    'noplaylist':       True,
    'extract_flat':     False,
    'forcejson':        True,
    'nocheckcertificate': True,
    'default_search':   'auto',
    'source_address':   '0.0.0.0',
    'geo_bypass':       True,
    'geo_bypass_country': 'US',
}
ydl      = YoutubeDL(ydl_opts)
url_re   = re.compile(r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/')

intents  = discord.Intents.default()
intents.voice_states = True
bot       = commands.Bot(command_prefix="/", intents=intents)

# ─── GLOBAL STATE ─────────────────────────────────────────────────────────
music_queue       = []
music_history     = []
voice_client: discord.VoiceClient = None
text_channel: discord.TextChannel       = None
now_playing_msg: discord.Message         = None
disconnect_task: asyncio.Task            = None

# ─── HELPERS ───────────────────────────────────────────────────────────────
def format_duration(sec: int) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s   = divmod(rem, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

async def ensure_voice(inter: discord.Interaction) -> bool:
    global voice_client, text_channel
    user_vc = getattr(inter.user.voice, "channel", None)
    if not user_vc:
        await inter.followup.send("🚫 You must be in a voice channel first.", ephemeral=True)
        return False
    if not voice_client or not voice_client.is_connected():
        voice_client = await user_vc.connect()
    elif voice_client.channel != user_vc:
        await inter.followup.send("🚫 You must be in the same VC as me to control me.", ephemeral=True)
        return False
    text_channel = inter.channel
    return True

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
    if now_playing_msg:
        asyncio.create_task(now_playing_msg.delete())
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

    # --- NOW PLAYING EMBED with requester's icon + inline fields ---
    embed = discord.Embed(
        title="Now Playing",
        description=f"[{item['title']}]({item['webpage_url']}) [`{format_duration(item['duration'])}`]",
        color=discord.Color.blue()
    )
    embed.set_author(
        name="Now Playing",
        icon_url=item['requester'].avatar.url
    )
    embed.add_field(name="Requested By", value=item['requester'].mention, inline=True)
    embed.add_field(name="Duration",     value=f"`{format_duration(item['duration'])}`", inline=True)
    embed.add_field(name="Author",       value=f"`{item['uploader']}`", inline=True)

    view = NowPlayingView()
    now_playing_msg = await text_channel.send(
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions.none()
    )

# ─── VIEWS ────────────────────────────────────────────────────────────────
class NowPlayingView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="🔀 Shuffle", style=discord.ButtonStyle.primary, custom_id="shuffle")
    async def shuffle(self, inter, btn):
        if music_queue:
            random.shuffle(music_queue)
            await inter.response.send_message("🔀 Queue shuffled.", ephemeral=True)
        else:
            await inter.response.send_message("📭 Queue is empty.", ephemeral=True)

    @button(label="⏪ Back", style=discord.ButtonStyle.primary, custom_id="previous")
    async def previous(self, inter, btn):
        if music_history:
            prev = music_history.pop(0)
            music_queue.insert(0, prev)
            voice_client.stop()
            await inter.response.defer()
        else:
            await inter.response.send_message("🚫 No previous track.", ephemeral=True)

    @button(label="⏸ Pause", style=discord.ButtonStyle.primary, custom_id="pauseplay")
    async def pauseplay(self, inter, btn):
        if voice_client.is_playing():
            voice_client.pause()
            btn.label = "▶️ Play"
        elif voice_client.is_paused():
            voice_client.resume()
            btn.label = "⏸ Pause"
        elif music_queue:
            await _play_next()
            btn.label = "⏸ Pause"
        else:
            return await inter.response.send_message("🚫 Nothing to play.", ephemeral=True)
        await inter.response.edit_message(view=self)

    @button(label="⏭ Next", style=discord.ButtonStyle.primary, custom_id="skip")
    async def skip(self, inter, btn):
        if voice_client.is_playing():
            voice_client.stop()
            await inter.response.defer()
        else:
            await inter.response.send_message("🚫 Nothing is playing.", ephemeral=True)

    @button(label="⏹ Stop", style=discord.ButtonStyle.danger, custom_id="stop")
    async def stop(self, inter, btn):
        clear_all()
        await inter.response.send_message("🛑 Stopped and cleared queue.", ephemeral=True)

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

# ─── EMBED BUILDER ─────────────────────────────────────────────────────────
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
    for i, item in enumerate(music_queue[start:start+10], start=1+start):
        title = (item['title'][:61] + "…") if len(item['title'])>61 else item['title']
        e.add_field(
            name=f"{i}. [{title}]({item['webpage_url']})",
            value=f"`{format_duration(item['duration'])}`",
            inline=False
        )
    return e

# ─── SLASH COMMANDS ───────────────────────────────────────────────────────
@bot.tree.command(name="join", description="Join your voice channel (or move me there)")
async def join(inter: discord.Interaction):
    await inter.response.defer(thinking=True)
    if await ensure_voice(inter):
        await inter.followup.send("✅ Joined your voice channel.", ephemeral=True)

@bot.tree.command(name="play", description="Add a song to the queue")
@app_commands.describe(query="YouTube URL or search terms")
async def play(inter: discord.Interaction, query: str):
    await inter.response.defer(thinking=True)
    if not await ensure_voice(inter):
        return

    key = query if url_re.match(query) else query.lower()
    if USE_CACHE and key in track_cache:
        info = track_cache[key].copy()
        info['requester'] = inter.user
    else:
        search_str = query if url_re.match(query) else f"ytsearch1:{query}"
        result = await asyncio.to_thread(ydl.extract_info, search_str, False)
        if 'entries' in result:
            result = result['entries'][0]
        info = {
            'url':         result['url'],
            'webpage_url': result['webpage_url'],
            'title':       result['title'],
            'duration':    result['duration'],
            'uploader':    result.get('uploader',''),
            'requester':   inter.user
        }
        if USE_CACHE:
            to_cache = info.copy()
            to_cache.pop('requester', None)
            track_cache[key] = to_cache

    was_empty = len(music_queue) == 0
    music_queue.append(info)
    if was_empty and not voice_client.is_playing() and not voice_client.is_paused():
        await _play_next()

    embed = discord.Embed(
        title=f"Song Added to Queue #{len(music_queue)}",
        description=f"[{info['title']}]({info['webpage_url']}) [`{format_duration(info['duration'])}`]",
        color=discord.Color.blue()
    )
    embed.set_author(name=inter.user.display_name, icon_url=inter.user.avatar.url)
    await inter.followup.send(embed=embed)

@bot.tree.command(name="next", description="Add a song next in queue")
@app_commands.describe(query="YouTube URL or search terms")
async def play_next_cmd(inter: discord.Interaction, query: str):
    await inter.response.defer(thinking=True)
    if not await ensure_voice(inter):
        return

    key = query if url_re.match(query) else query.lower()
    if USE_CACHE and key in track_cache:
        info = track_cache[key].copy()
        info['requester'] = inter.user
    else:
        search_str = query if url_re.match(query) else f"ytsearch1:{query}"
        result = await asyncio.to_thread(ydl.extract_info, search_str, False)
        if 'entries' in result:
            result = result['entries'][0]
        info = {
            'url': result['url'],
            'webpage_url': result['webpage_url'],
            'title': result['title'],
            'duration': result['duration'],
            'uploader': result.get('uploader',''),
            'requester': inter.user
        }
        if USE_CACHE:
            to_cache = info.copy()
            to_cache.pop('requester', None)
            track_cache[key] = to_cache

    music_queue.insert(0, info)

    e = discord.Embed(
        title="Song Added to Queue #1",
        description=f"[{info['title']}]({info['webpage_url']}) [`{format_duration(info['duration'])}`]",
        color=discord.Color.blue()
    )
    e.set_author(name=inter.user.display_name, icon_url=inter.user.avatar.url)
    await inter.followup.send(embed=e)

    if not voice_client.is_playing() and not voice_client.is_paused():
        await _play_next()

@bot.tree.command(name="skip", description="Skip the current song")
async def skip(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if not voice_client.is_playing():
        return await inter.followup.send("⏭ Nothing is playing.", ephemeral=True)
    voice_client.stop()
    await inter.followup.send("⏭ Skipped.", ephemeral=True)

@bot.tree.command(name="previous", description="Play the previous song")
async def previous(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    if not music_history:
        return await inter.followup.send("🚫 No previous track.", ephemeral=True)
    prev = music_history.pop(0)
    music_queue.insert(0, prev)
    voice_client.stop()
    await inter.followup.send("⏮ Playing previous track.", ephemeral=True)

@bot.tree.command(name="pauseplay", description="Toggle pause/play")
async def pauseplay(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if voice_client.is_playing():
        voice_client.pause()
        return await inter.followup.send("⏸ Paused.", ephemeral=True)
    if voice_client.is_paused():
        voice_client.resume()
        return await inter.followup.send("▶ Resumed.", ephemeral=True)
    if music_queue:
        await _play_next()
        return await inter.followup.send("▶ Started playing.", ephemeral=True)
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
    await inter.followup.send("🔀 Queue shuffled.", ephemeral=True)

@bot.tree.command(name="remove", description="Remove a song by its position in the queue")
@app_commands.describe(position="Position (1‑based)")
async def remove_cmd(inter: discord.Interaction, position: int):
    await inter.response.defer(ephemeral=True)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    if position < 1 or position > len(music_queue):
        return await inter.followup.send("🚫 Invalid position.", ephemeral=True)
    removed = music_queue.pop(position-1)
    embed = discord.Embed(title="Removed",
                          description=f"[{removed['title']}]({removed['webpage_url']})",
                          color=discord.Color.blue())
    await inter.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop_cmd(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    if not voice_client or not voice_client.is_connected():
        return await inter.followup.send("🚫 I'm not in a voice channel.", ephemeral=True)
    clear_all()
    await inter.followup.send("🛑 Stopped and cleared queue.", ephemeral=True)

# ─── EVENTS ───────────────────────────────────────────────────────────────
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