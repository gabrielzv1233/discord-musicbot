import re
import random
import threading
import time
import logging
import tkinter as tk
from yt_dlp import YoutubeDL
import vlc

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)8s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger()

ydl     = YoutubeDL({'format':'bestaudio','quiet':True,'noplaylist':True})
url_re  = re.compile(r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/')

player        = vlc.Instance().media_player_new()
player.audio_set_volume(80)
queue, history = [], []
current        = None

search_queue, search_lock = [], threading.Lock()

root            = tk.Tk()
queue_window    = None
queue_listbox   = None
url_entry       = None
status_label    = None

def fetch_track(q):
    log.debug(f"Searching for: {q}")
    try:
        if url_re.match(q):
            info = ydl.extract_info(q, download=False)
        else:
            res     = ydl.extract_info(f"ytsearch1:{q}", download=False)
            entries = res.get('entries') or []
            if not entries:
                log.warning(f"No results for {q!r}")
                return None
            info    = entries[0]
        if 'entries' in info:
            info = info['entries'][0]
        return {'url': info['url'], 'title': info.get('title','<untitled>')}
    except Exception:
        log.exception(f"Failed to fetch: {q}")
        return None

def play_track(track):
    global current
    if not track or not track.get('url'):
        log.warning("Invalid track, skipping")
        return
    if current:
        history.insert(0, current)
        if len(history) > 10:
            history.pop()
    current = track
    media = vlc.Instance().media_new(track['url'])
    player.set_media(media)
    player.event_manager().event_attach(
        vlc.EventType.MediaPlayerEndReached,
        lambda e: play_next()
    )
    player.play()
    status_label.config(text=f"now playing:\n:{track['title']}")
    log.info(f"Now playing: {track['title']}")

def play_next():
    global queue
    log.debug("play_next called")
    while queue:
        item = queue.pop(0)
        refresh_queue_list()
        if item.get('url'):
            play_track(item)
            return
    log.debug("Queue empty")

def play_prev():
    if player.get_time() / 1000 <= 5 and history:
        play_track(history.pop(0))
    else:
        player.set_time(0)

def toggle_play():
    if player.is_playing():
        player.pause(); log.debug("Paused")
    else:
        player.play();  log.debug("Resumed")

def add(q, front=False):
    status_label.config(text=f"Searching: {q}")
    placeholder = {'url':None, 'title':f"Loading: {q}", 'query':q, 'loading':True}
    if front:
        queue.insert(0, placeholder)
    else:
        queue.append(placeholder)
    refresh_queue_list()
    with search_lock:
        search_queue.append((q, front))

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

def replace_placeholder(q, track):
    for i, item in enumerate(queue):
        if item.get('loading') and item.get('query') == q:
            new_item = track or {'url':None, 'title':f"Error loading: {q}", 'query':q}
            queue[i] = new_item
            status_label.config(text=f"Added to queue: {new_item['title']}")
            log.debug(f"Replaced placeholder for {q}")
            break
    else:
        log.warning(f"No placeholder found for {q}")
        return
    refresh_queue_list()
    if current is None:
        play_next()

def refresh_queue_list():
    if queue_listbox and queue_listbox.winfo_exists():
        queue_listbox.delete(0, tk.END)
        for t in queue:
            queue_listbox.insert(tk.END, t['title'])

def open_queue_window():
    global queue_window, queue_listbox
    if queue_window and queue_window.winfo_exists():
        queue_window.lift()
        return
    queue_window = tk.Toplevel(root)
    queue_window.title("Queue")
    queue_listbox = tk.Listbox(queue_window, width=50)
    queue_listbox.pack(padx=5, pady=5)
    tk.Button(queue_window, text="Remove Selected", command=lambda: (
        queue.pop(queue_listbox.curselection()[0]) if queue_listbox.curselection() else None,
        refresh_queue_list()
    )).pack(side=tk.LEFT, padx=5)
    tk.Button(queue_window, text="Shuffle Queue", command=lambda: (
        random.shuffle(queue),
        refresh_queue_list()
    )).pack(side=tk.LEFT)
    refresh_queue_list()

def on_add():
    q = url_entry.get().strip()
    if q:
        add(q)

def on_play_next():
    q = url_entry.get().strip()
    if q:
        add(q, True)

def on_prev():   play_prev()
def on_toggle(): toggle_play()
def on_next():   play_next()

root.title("YouTube Audio Player")

url_entry = tk.Entry(root, width=40)
url_entry.grid(row=0, column=0, columnspan=5, padx=5, pady=5)
url_entry.bind("<Return>", lambda e: on_add())
url_entry.focus_set()

tk.Button(root, text="Add to Queue",  command=on_add).grid(row=1, column=0)
tk.Button(root, text="Prev ◀",        command=on_prev).grid(row=1, column=1)
tk.Button(root, text="Play/Pause",    command=on_toggle).grid(row=1, column=2)
tk.Button(root, text="Next ▶",        command=on_next).grid(row=1, column=3)
tk.Button(root, text="Play Next",     command=on_play_next).grid(row=1, column=4)

volume_frame = tk.Frame(root)
volume_frame.grid(row=2, column=0, columnspan=5, pady=5)

volume_slider = tk.Scale(
    volume_frame,
    from_=0,
    to=100,
    orient=tk.HORIZONTAL,
    command=lambda v: (
        player.audio_set_volume(int(v)),
        volume_value_label.config(text=f"{v}%")
    )
)
volume_slider.set(80)
volume_slider.pack(side=tk.LEFT)

volume_value_label = tk.Label(volume_frame, text="80%")
volume_value_label.pack(side=tk.LEFT, padx=10)

tk.Button(root, text="Manage Queue",  command=open_queue_window).grid(row=3, column=0, columnspan=5, pady=5)

status_label = tk.Label(root, text="No track playing", wraplength=300)
status_label.grid(row=4, column=0, columnspan=5, pady=5)

threading.Thread(target=process_search_queue, daemon=True).start()

root.mainloop()