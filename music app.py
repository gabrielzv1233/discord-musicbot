import re, random, threading, time
import logging
import tkinter as tk
from yt_dlp import YoutubeDL
import vlc

# ─── DEBUG LOGGING SETUP ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)8s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

# ─── YT‑DLP & URL CHECK ──────────────────────────────────────────────────────
ydl     = YoutubeDL({'format':'bestaudio','quiet':True,'noplaylist':True})
url_re  = re.compile(r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/')

# ─── VLC + MAIN QUEUE & HISTORY ───────────────────────────────────────────────
player   = vlc.Instance().media_player_new()
queue, history = [], []
current   = None

# ─── SEARCH QUEUE SYSTEM ──────────────────────────────────────────────────────
search_queue = []
search_lock  = threading.Lock()

# ─── UI REFS ─────────────────────────────────────────────────────────────────
root          = tk.Tk()
queue_window  = None
queue_listbox = None
url_entry     = None
status_label  = None

# ─── TRACK FETCH ─────────────────────────────────────────────────────────────
def fetch_track(q):
    log.debug(f"fetch_track START q={q!r}")
    try:
        if url_re.match(q):
            info = ydl.extract_info(q, download=False)
        else:
            res    = ydl.extract_info(f"ytsearch1:{q}", download=False)
            entries= res.get('entries') or []
            if not entries:
                log.warning(f"No results for {q!r}")
                return None
            info   = entries[0]
        if 'entries' in info:
            info = info['entries'][0]
        track = {'url': info['url'], 'title': info.get('title','')}
        log.debug(f"fetch_track DONE q={q!r} → {track['title']!r}")
        return track
    except Exception:
        log.exception(f"fetch_track FAILED q={q!r}")
        return None

# ─── PLAYBACK CONTROLS ────────────────────────────────────────────────────────
def play_track(track):
    global current
    log.debug(f"play_track {track!r}")
    if not track or not track.get('url'):
        log.warning("play_track skipped invalid track")
        return
    if current:
        history.insert(0, current)
        if len(history)>10: history.pop()
    current = track
    media = vlc.Instance().media_new(track['url'])
    player.set_media(media)
    player.event_manager().event_attach(
        vlc.EventType.MediaPlayerEndReached, lambda e: play_next())
    player.play()
    status_label.config(text=track['title'])
    log.info(f"Now playing: {track['title']}")

def play_next():
    global queue
    log.debug("play_next called")
    while queue:
        item = queue.pop(0)
        log.debug(f" play_next popped {item!r}")
        refresh_queue_list()
        if item.get('url'):
            play_track(item)
            return
        else:
            log.warning(f" skipping invalid: {item.get('query')!r}")
    log.debug(" play_next found nothing")

def play_prev():
    log.debug("play_prev called")
    if player.get_time()/1000 <=5 and history:
        play_track(history.pop(0))
    else:
        player.set_time(0)

def toggle_play():
    if player.is_playing():
        player.pause(); log.debug(" paused")
    else:
        player.play();  log.debug(" resumed")

# ─── QUEUE PLACEHOLDER & ADD ──────────────────────────────────────────────────
def add_placeholder(q, front=False):
    placeholder = {'url':None,'title':f"Loading: {q}",'query':q,'loading':True}
    idx = 0 if front else len(queue)
    if front: queue.insert(0, placeholder)
    else:     queue.append(placeholder)
    log.debug(f"Placeholder added idx={idx} q={q!r}")
    refresh_queue_list()
    return

def add(q, front=False):
    add_placeholder(q, front)
    with search_lock:
        search_queue.append((q, front))
        log.debug(f" search_queue append {(q,front)} (size={len(search_queue)})")

# ─── BACKGROUND SEARCH WORKER ─────────────────────────────────────────────────
def process_search_queue():
    log.debug("Search worker started")
    while True:
        job = None
        with search_lock:
            if search_queue:
                job = search_queue.pop(0)
        if not job:
            time.sleep(0.1)
            continue
        q, front = job
        track = fetch_track(q)
        root.after(0, replace_placeholder, q, track)

# ─── REPLACE PLACEHOLDER & AUTO‑PLAY ──────────────────────────────────────────
def replace_placeholder(q, track):
    global current
    log.debug(f"replace_placeholder q={q!r} track={track!r}")
    # find the first loading placeholder matching this query
    for i,item in enumerate(queue):
        if item.get('loading') and item.get('query') == q:
            new_item = track or {'url':None,'title':f"Error loading: {q}",'query':q}
            queue[i] = new_item
            log.debug(f" Replaced at queue[{i}] → {new_item['title']!r}")
            break
    else:
        log.warning(f" No placeholder found for {q!r}")
        return
    refresh_queue_list()
    # if nothing is playing yet, auto-start
    if current is None:
        log.debug(" Running play_next() after replace")
        play_next()

# ─── QUEUE WINDOW ─────────────────────────────────────────────────────────────
def open_queue_window():
    global queue_window, queue_listbox
    if queue_window and queue_window.winfo_exists():
        queue_window.lift(); return
    queue_window = tk.Toplevel(root)
    queue_window.title("Queue")
    queue_listbox = tk.Listbox(queue_window, width=50)
    queue_listbox.pack(padx=5, pady=5)

    def refresh():
        queue_listbox.delete(0, tk.END)
        for t in queue:
            queue_listbox.insert(tk.END, t['title'])
        log.debug(f"Queue window refreshed ({len(queue)} items)")

    def remove():
        sel = queue_listbox.curselection()
        if sel:
            rem = queue.pop(sel[0])
            log.info(f"Removed {rem!r}")
            refresh()

    tk.Button(queue_window, text="Remove Selected", command=remove).pack(side=tk.LEFT, padx=5)
    tk.Button(queue_window, text="Shuffle Queue", command=lambda:(random.shuffle(queue), refresh())).pack(side=tk.LEFT)
    refresh()

# ─── LIVE‑UPDATE LISTBOX ─────────────────────────────────────────────────────
def refresh_queue_list():
    if queue_listbox and queue_listbox.winfo_exists():
        queue_listbox.delete(0, tk.END)
        for t in queue:
            queue_listbox.insert(tk.END, t['title'])
        log.debug("Queue listbox live‑updated")

# ─── UI CALLBACKS ─────────────────────────────────────────────────────────────
def on_add():
    q = url_entry.get().strip()
    if not q:
        log.debug("on_add skipped: empty")
        return
    log.debug(f"on_add {q!r}")
    add(q)

def on_play_next():
    q = url_entry.get().strip()
    if not q:
        log.debug("on_play_next skipped: empty")
        return
    log.debug(f"on_play_next {q!r}")
    add(q, True)

def on_next():  log.debug("on_next");  play_next()
def on_prev():  log.debug("on_prev");  play_prev()
def on_toggle():log.debug("on_toggle");toggle_play()

# ─── GUI SETUP ────────────────────────────────────────────────────────────────
root.title("YouTube Audio Player")

url_entry = tk.Entry(root, width=40)
url_entry.grid(row=0, column=0, columnspan=5, padx=5, pady=5)
url_entry.bind("<Return>", lambda e: on_add())
url_entry.focus_set()

tk.Button(root, text="Add to Queue",  command=on_add).grid(row=1, column=0)
tk.Button(root, text="Play Next",     command=on_play_next).grid(row=1, column=1)
tk.Button(root, text="Prev ◀",        command=on_prev).grid(row=1, column=2)
tk.Button(root, text="Play/Pause",    command=on_toggle).grid(row=1, column=3)
tk.Button(root, text="Next ▶",        command=on_next).grid(row=1, column=4)
tk.Button(root, text="Manage Queue",  command=open_queue_window)\
  .grid(row=2, column=0, columnspan=5, pady=5)

status_label = tk.Label(root, text="No track playing", wraplength=300)
status_label.grid(row=3, column=0, columnspan=5, pady=5)

# ─── START WORKER THREAD ──────────────────────────────────────────────────────
threading.Thread(target=process_search_queue, daemon=True).start()

root.mainloop()
    