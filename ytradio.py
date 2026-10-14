"""YouTube Music radio tanpa henti: ytmusicapi (rekomendasi) + mpv (audio saja).

Pakai:  python ytradio.py "judul lagu / artis"   atau   python ytradio.py <link lagu / playlist YouTube>
Opsi:   python ytradio.py --help
Kontrol (di jendela ini): lihat HELP di bawah.
"""
import argparse
import atexit
import datetime
import json
import os
import queue
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time

from ytmusicapi import YTMusic

IS_WINDOWS = sys.platform == "win32"

# nama unik per proses: dua jendela YT Radio tidak saling mengendalikan player yang lain
if IS_WINDOWS:
    PIPE = rf"\\.\pipe\ytradio-{os.getpid()}"
else:
    PIPE = os.path.join(tempfile.gettempdir(), f"ytradio-{os.getpid()}.sock")

REFILL_WHEN_LEFT = 5   # ambil rekomendasi baru kalau sisa antrian <= ini
KEEP_PLAYED = 20       # lagu yang sudah diputar yang disisakan di playlist mpv (untuk tombol prev)
KEEP_QUEUED = 200      # videoId terakhir yang disimpan sebagai sumber rekomendasi cadangan
SLEEP_STEPS = [15, 30, 60, 90]  # menit, diputar bergiliran oleh tombol t
MPV_CANDIDATES = [
    r"C:\Program Files\MPV Player\mpv.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\mpv\mpv.exe"),
]

HOME = os.path.expanduser("~")
STATE_FILE = os.path.join(HOME, ".ytradio.json")
BLOCKED_FILE = os.path.join(HOME, ".ytradio-blocked.json")
FAVORITES_FILE = os.path.join(HOME, ".ytradio-favorites.txt")
HISTORY_FILE = os.path.join(HOME, ".ytradio-history.txt")


def prefer_ipv4():
    """Prioritaskan IPv4 untuk menghindari timeout jaringan IPv6 yang lambat/bermasalah."""
    orig = socket.getaddrinfo

    def getaddrinfo_ipv4(*args, **kwargs):
        responses = orig(*args, **kwargs)
        ipv4 = [r for r in responses if r[0] == socket.AF_INET]
        return ipv4 if ipv4 else responses
    socket.getaddrinfo = getaddrinfo_ipv4


def cleanup_socket():
    if not IS_WINDOWS and os.path.exists(PIPE):
        try:
            os.unlink(PIPE)
        except OSError:
            pass


def refresh_path():
    """Ambil PATH terbaru dari registry di Windows (install winget baru belum masuk ke shell lama)."""
    if not IS_WINDOWS:
        return
    import winreg
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for root, key in [(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")]:
        try:
            with winreg.OpenKey(root, key) as k:
                parts += os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]).split(os.pathsep)
        except OSError:
            pass
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(p for p in parts if p))


def find_mpv():
    candidates = [shutil.which("mpv")]
    if IS_WINDOWS:
        candidates.extend(MPV_CANDIDATES)
    for p in candidates:
        if p and os.path.exists(p):
            return p
    install_msg = "winget install shinchiro.mpv" if IS_WINDOWS else "sudo apt install mpv"
    sys.exit(f"mpv tidak ditemukan. Install: {install_msg}")


def find_ytdlp():
    exe = "yt-dlp.exe" if IS_WINDOWS else "yt-dlp"
    candidates = []
    if not IS_WINDOWS:
        candidates.append(os.path.expanduser("~/.local/bin/yt-dlp"))
    candidates.append(shutil.which("yt-dlp"))
    # hasil `pip install yt-dlp` yang folder Scripts/bin-nya belum masuk PATH
    for scheme in (sysconfig.get_default_scheme(), f"{os.name}_user"):
        try:
            candidates.append(os.path.join(sysconfig.get_path("scripts", scheme), exe))
        except KeyError:
            pass
    for p in candidates:
        if p and os.path.exists(p):
            return p
    install_msg = "winget install yt-dlp.yt-dlp" if IS_WINDOWS else "pip install --upgrade yt-dlp"
    sys.exit(f"yt-dlp tidak ditemukan. Install: {install_msg}")


class Mpv:
    """Klien JSON IPC minimal ke mpv lewat named pipe (Windows) atau unix domain socket (Linux)."""

    def __init__(self, path, proc=None):
        self.sock = None
        for _ in range(50):
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(f"mpv berhenti saat start (exit code {proc.returncode})")
            try:
                if IS_WINDOWS:
                    self.f = open(path, "r+b", buffering=0)
                else:
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(path)
                    self.f = self.sock.makefile("rwb", buffering=0)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Tidak bisa terhubung ke mpv")
        self.buf = b""
        self.rid = 0
        self.lock = threading.Lock()
        self.on_event = None  # dipanggil untuk event mpv (end-file, property-change, ...)

    def close(self):
        try:
            if hasattr(self, "f") and self.f:
                self.f.close()
        except Exception:
            pass
        if getattr(self, "sock", None):
            try:
                self.sock.close()
            except Exception:
                pass

    def _readline(self):
        while b"\n" not in self.buf:
            chunk = self.f.read(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, *args):
        with self.lock:
            self.rid += 1
            msg = {"command": list(args), "request_id": self.rid}
            self.f.write((json.dumps(msg) + "\n").encode())
            while True:
                r = self._readline()
                if r.get("request_id") == self.rid:
                    return r.get("data")
                if "event" in r and self.on_event:
                    self.on_event(r)

    def watch(self, props, on_event):
        """Jadikan koneksi ini khusus event: observe props, lalu baca event terus di thread sendiri.

        Koneksi terpisah dari koneksi perintah karena di Windows baca dan tulis bersamaan pada
        satu handle pipe sinkron saling menunggu (deadlock).
        """
        self.on_event = on_event
        for i, p in enumerate(props, 1):
            self.cmd("observe_property", i, p)
        threading.Thread(target=self._event_loop, daemon=True).start()

    def _event_loop(self):
        try:
            while True:
                r = self._readline()
                if "event" in r:
                    self.on_event(r)
        except (EOFError, OSError, ValueError):
            pass


def video_id_from(text):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})", text)
    return m.group(1) if m else None


def playlist_id_from(text):
    m = re.search(r"[?&]list=([\w-]+)", text)
    return m.group(1) if m else None


def track_artists(t):
    return [a["name"] for a in t.get("artists") or [] if a.get("name")]


def track_label(t):
    artists = ", ".join(track_artists(t))
    return f"{artists} - {t['title']}" if artists else t["title"]


# keterangan versi di judul: (Official Video), [Lyrics], (Remastered 2011), ...
_TITLE_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*(official|video|audio|lyric|lirik|visuali[sz]er|remaster|m/?v\b|hd|4k)[^\)\]]*[\)\]]",
    re.I)


def track_key(t):
    """Kunci dedupe: lagu yang sama dengan videoId berbeda (MV, lyric video, audio) dianggap satu."""
    title = _TITLE_NOISE.sub("", t.get("title") or "")
    title = re.sub(r"\s+-\s+.*remaster.*$", "", title, flags=re.I)
    artists = track_artists(t)
    return re.sub(r"\W+", " ", f"{artists[0] if artists else ''} {title}".lower()).strip()


HELP = ("Kontrol: spasi=pause  n/>=next  p/<=prev  ←/→=mundur/maju 10 dtk  "
        "↑/↓ atau 0/9=volume  m=mute  a=normalisasi on/off  u=lagu berikutnya  "
        "l=suka  x=blokir artis  t=sleep timer  s=ganti lagu awal  q=keluar")

# Normalisasi volume antar lagu (dynaudnorm: jendela ~15 dtk, halus, tidak "memompa")
NORM_FILTER = "@norm:lavfi=[dynaudnorm=f=500:g=31:p=0.9:m=10]"

# tombol biasa dan tombol panah
KEYS = {
    " ": ("cycle", "pause"),
    "n": ("playlist-next",), ">": ("playlist-next",), ".": ("playlist-next",),
    "p": ("playlist-prev",), "<": ("playlist-prev",), ",": ("playlist-prev",),
    "0": ("add", "volume", 5), "+": ("add", "volume", 5), "=": ("add", "volume", 5),
    "9": ("add", "volume", -5), "-": ("add", "volume", -5),
    "m": ("cycle", "mute"),
    # yang di bawah ditangani sendiri oleh key_loop/main, bukan perintah mpv
    "a": ("norm",),
    "u": ("upcoming",),
    "s": ("station",),
    "l": ("like",),
    "x": ("block",),
    "t": ("sleep",),
    "q": ("quit",),
}
ARROWS_WIN = {
    "M": ("seek", 10), "K": ("seek", -10),
    "H": ("add", "volume", 5), "P": ("add", "volume", -5),
}
ARROWS_UNIX = {
    "A": ("add", "volume", 5),   # Atas
    "B": ("add", "volume", -5),  # Bawah
    "C": ("seek", 10),           # Kanan
    "D": ("seek", -10),          # Kiri
}

_old_term_settings = None


def restore_terminal():
    global _old_term_settings
    if _old_term_settings is not None and sys.stdin.isatty():
        try:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _old_term_settings)
        except Exception:
            pass
        _old_term_settings = None


atexit.register(restore_terminal)


def read_key():
    if IS_WINDOWS:
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return ARROWS_WIN.get(msvcrt.getwch())
        return KEYS.get(ch.lower())
    else:
        import select
        if not sys.stdin.isatty():
            time.sleep(0.5)
            return None
        # os.read, bukan sys.stdin.read: buffer TextIOWrapper menelan sisa urutan
        # escape tombol panah sehingga select() berikutnya tidak melihatnya
        fd = sys.stdin.fileno()
        b = os.read(fd, 1)
        if not b:
            return ("quit",)
        if b == b"\x1b":
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                seq = os.read(fd, 2)
                if len(seq) == 2 and seq[:1] in (b"[", b"O"):
                    return ARROWS_UNIX.get(chr(seq[1]))
            return None
        return KEYS.get(chr(b[0]).lower())


def set_cbreak():
    """Linux: baca tombol satu per satu tanpa Enter."""
    if not IS_WINDOWS and sys.stdin.isatty():
        import termios, tty
        global _old_term_settings
        try:
            _old_term_settings = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            pass


def fmt_time(sec):
    if sec is None:
        return "-:--"
    sec = int(sec)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


class Ui:
    """Output console: baris status yang diperbarui di tempat (\\r) + pesan biasa di atasnya."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = ""
        self.prompting = False    # True selama input() tombol s, jangan timpa prompt
        self.wake = threading.Event()  # minta loop utama segera perbarui status
        self.labels = {}          # videoId -> "Artis - Judul"
        self.artists = {}         # videoId -> ["Artis", ...]

    def _clear(self):
        if self.status:
            print("\r" + " " * len(self.status) + "\r", end="")

    def say(self, msg):
        with self.lock:
            self._clear()
            print(msg, flush=True)
            if self.status and not self.prompting:
                print(self.status, end="", flush=True)

    def set_status(self, text):
        # status yang lebih lebar dari terminal akan wrap dan \r tidak bisa menimpanya lagi
        text = text[:max(20, shutil.get_terminal_size().columns - 1)]
        with self.lock:
            if self.prompting:
                return
            pad = max(0, len(self.status) - len(text))
            print("\r" + text + " " * pad + "\b" * pad, end="", flush=True)
            self.status = text

    def begin_prompt(self):
        with self.lock:
            self._clear()
            self.status = ""
            self.prompting = True

    def end_prompt(self):
        with self.lock:
            self.prompting = False
        self.wake.set()


def norm_enabled(af):
    return any(f.get("label") == "norm" and f.get("enabled", True) for f in af or [])


def show_upcoming(ipc, ui, n=5):
    pos = ipc.cmd("get_property", "playlist-pos")
    count = ipc.cmd("get_property", "playlist-count") or 0
    if pos is None or pos < 0:
        return
    lines = ["Berikutnya:"]
    for i in range(pos + 1, min(pos + 1 + n, count)):
        fn = ipc.cmd("get_property", f"playlist/{i}/filename") or ""
        lines.append(f"  {i - pos}. {ui.labels.get(video_id_from(fn), fn)}")
    if len(lines) == 1:
        lines.append("  (sedang mengambil rekomendasi...)")
    ui.say("\n".join(lines))


def key_loop(ipc, stations, ui, actions):
    """Baca tombol dari console dan teruskan ke mpv (mpv sendiri tidak baca console).

    actions: nama perintah -> fungsi untuk perintah yang ditangani main (like, block, sleep).
    """
    set_cbreak()
    try:
        while True:
            cmd = read_key()
            if not cmd:
                continue
            try:
                if cmd[0] == "station":
                    ipc.cmd("set_property", "pause", True)
                    ui.begin_prompt()
                    restore_terminal()
                    try:
                        q = input("\n⏹ stop. Lagu / artis / link baru (Enter = lanjut yang tadi): ").strip()
                    except EOFError:
                        q = ""
                    set_cbreak()
                    if q:
                        stations.put(q)  # loop utama yang mengganti antrian
                    else:
                        ipc.cmd("set_property", "pause", False)
                    ui.end_prompt()
                    continue
                if cmd[0] == "norm":
                    if norm_enabled(ipc.cmd("get_property", "af")):
                        ipc.cmd("af", "remove", "@norm")
                    else:
                        ipc.cmd("af", "add", NORM_FILTER)
                    continue
                if cmd[0] == "upcoming":
                    show_upcoming(ipc, ui)
                    continue
                if cmd[0] in actions:
                    actions[cmd[0]]()
                    continue
                ipc.cmd(*cmd)
                if cmd[0] == "quit":
                    return
            except (EOFError, OSError):
                return
    finally:
        restore_terminal()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def append_line(path, line):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def resolve_seed(yt, query, say=None):
    """Sumber radio dari query: link playlist -> {"playlistId"[, "videoId"]}, link lagu -> {"videoId"},
    selain itu lagu teratas hasil pencarian. None kalau tidak ketemu."""
    vid, pid = video_id_from(query), playlist_id_from(query)
    if pid:
        return {"playlistId": pid, **({"videoId": vid} if vid else {})}
    if vid:
        return {"videoId": vid}
    results = yt.search(query, filter="songs", limit=1)
    if not results:
        return None
    msg = f"Mulai dari: {track_label(results[0])}"
    if say:
        say(msg)
    else:
        print(msg, flush=True)
    return {"videoId": results[0]["videoId"]}


def parse_args(argv):
    p = argparse.ArgumentParser(prog="ytradio", description="YouTube Music radio tanpa henti (audio lewat mpv).")
    p.add_argument("query", nargs="*", help="judul lagu / artis, link lagu, atau link playlist "
                                           "(kosong = tanya, Enter = lanjut lagu terakhir)")
    p.add_argument("--volume", type=int, metavar="0-100", help="volume awal (default: terakhir dipakai, atau 80)")
    p.add_argument("--no-norm", action="store_true", help="mulai dengan normalisasi volume mati")
    p.add_argument("--no-ipv4", action="store_true", help="jangan paksa IPv4 (kalau IPv6 kamu lancar)")
    p.add_argument("--sleep", type=int, metavar="MENIT", help="berhenti otomatis setelah sekian menit")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    except (AttributeError, ValueError):
        pass
    if not args.no_ipv4:
        prefer_ipv4()

    last = load_json(STATE_FILE, {})
    query = " ".join(args.query).strip()
    if not query:
        hint = f" (Enter = lanjut: {last['title']})" if last.get("video_id") else ""
        query = input(f"Lagu / artis / link awal{hint}: ").strip()
        if not query and not last.get("video_id"):
            return

    refresh_path()
    yt = YTMusic()
    resume_at = None
    if query:
        try:
            seed = resolve_seed(yt, query)
        except Exception as e:  # jaringan putus dsb.
            sys.exit(f"Gagal mencari lagu: {e}")
        if not seed:
            sys.exit("Lagu tidak ditemukan.")
    else:
        seed = {"videoId": last["video_id"]}
        resume_at = last.get("time") or None
        print(f"Lanjut: {last.get('title', last['video_id'])} dari {fmt_time(resume_at or 0)}", flush=True)

    volume = args.volume if args.volume is not None else last.get("volume", 80)
    cleanup_socket()
    mpv = subprocess.Popen([
        find_mpv(),
        "--idle=yes",
        "--no-video",
        "--force-window=no",
        "--ytdl-format=bestaudio/best",
        f"--script-opts=ytdl_hook-ytdl_path={find_ytdlp()}",
        *([] if args.no_ipv4 else ["--ytdl-raw-options=force-ipv4="]),
        f"--input-ipc-server={PIPE}",
        "--msg-level=all=no",  # error ditampilkan sendiri lewat event IPC (1 baris)
        "--prefetch-playlist=yes",
        f"--volume={max(0, min(100, volume))}",
        *([] if args.no_norm else [f"--af={NORM_FILTER}"]),
        "--no-input-terminal",
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ipc = evc = None
    ui = Ui()
    try:
        ipc = Mpv(PIPE, mpv)
        ipc.cmd("disable_event", "all")  # event dibaca lewat koneksi kedua (evc)
        print(HELP, flush=True)

        fails = 0
        update_hint = "winget upgrade yt-dlp.yt-dlp" if IS_WINDOWS else "pip install --upgrade yt-dlp"
        props = {}  # nilai terbaru properti mpv yang di-observe, diisi thread event

        def on_event(ev):
            nonlocal fails
            name = ev.get("event")
            if name == "property-change":
                props[ev.get("name")] = ev.get("data")
                ui.wake.set()
            elif name == "end-file" and ev.get("reason") == "error":
                fails += 1
                err = ev.get("file_error")
                ui.say(f"  ✗ lagu gagal diputar{f' ({err})' if err else ''}, dilewati")
                if fails == 3:
                    ui.say(f"  [3 lagu berturut-turut gagal. Cek internet, atau update yt-dlp: "
                           f"{update_hint}]")
            elif name == "file-loaded":
                fails = 0

        evc = Mpv(PIPE, mpv)
        evc.watch(["pause", "volume", "mute", "af", "playlist-pos", "playlist-count",
                   "path", "media-title", "duration", "idle-active"], on_event)

        # Deteksi apakah perintah 'loadfile' pada mpv ini mendukung argumen 'index'
        loadfile_has_index = False
        for cmd_info in ipc.cmd("get_property", "command-list") or []:
            if cmd_info.get("name") == "loadfile":
                loadfile_has_index = any(a.get("name") == "index" for a in cmd_info.get("args", []))
                break

        qlock = threading.Lock()  # melindungi seen/queued/seed/gen antara loop utama dan thread refill
        seen = set()  # videoId dan track_key yang sudah pernah masuk antrian
        queued = []   # videoId dalam urutan antrian mpv (KEEP_QUEUED terakhir)
        gen = 0       # naik tiap ganti lagu awal; hasil refill generasi lama dibuang
        start_at = resume_at  # posisi lanjut, dipakai sekali untuk lagu awal
        refilling = threading.Event()
        retry_at = 0.0  # jangan refill sebelum waktu ini (backoff saat gagal/buntu)
        backoff = 0
        blocked = {a.lower() for a in load_json(BLOCKED_FILE, [])}  # nama artis yang diblokir
        sleep_at = time.monotonic() + args.sleep * 60 if args.sleep else None
        sleep_idx = -1  # posisi di SLEEP_STEPS untuk tombol t

        def is_blocked(artists):
            return any(a.lower() in blocked for a in artists)

        def refill(src, g):
            """Tambah lagu dari src (radio sebuah lagu, atau isi playlist) ke antrian.
            Mengembalikan jumlah lagu baru, atau None kalau generasi sudah basi."""
            nonlocal start_at
            is_playlist = "playlistId" in src
            data = yt.get_watch_playlist(**src, radio=not is_playlist, limit=100 if is_playlist else 30)
            added = 0
            with qlock:
                if g != gen:
                    return None
                for t in data.get("tracks", []):
                    vid = t.get("videoId")
                    key = track_key(t)
                    artists = track_artists(t)
                    if not vid or vid in seen or key in seen or is_blocked(artists):
                        continue
                    seen.update((vid, key))
                    queued.append(vid)
                    title = track_label(t)
                    ui.labels[vid] = title
                    ui.artists[vid] = artists
                    url = f"https://music.youtube.com/watch?v={vid}"
                    # %N%value = escape mpv untuk nilai berisi karakter apa pun (N = panjang byte)
                    opt = f"force-media-title=%{len(title.encode())}%{title}"
                    if start_at and vid == src.get("videoId"):
                        opt += f",start={int(start_at)}"  # lanjut dari posisi terakhir
                        start_at = None
                    if loadfile_has_index:
                        ipc.cmd("loadfile", url, "append-play", -1, opt)
                    else:
                        ipc.cmd("loadfile", url, "append-play", opt)
                    added += 1
                del queued[:-KEEP_QUEUED]
            return added

        def refill_worker(g, current_vid):
            """Jalan di thread terpisah supaya loop utama (status, tombol s) tidak tersendat."""
            nonlocal retry_at, backoff
            try:
                with qlock:
                    if g != gen:
                        return
                    if queued:
                        # radio terakhir buntu → coba lagu yang sedang main, lalu beberapa lagu acak
                        vids = [queued[-1], current_vid] + random.sample(queued[:-1], min(3, len(queued) - 1))
                        cands = [{"videoId": v} for v in dict.fromkeys(v for v in vids if v)]
                    else:
                        cands = [seed]
                try:
                    for src in cands:
                        added = refill(src, g)
                        if added is None:
                            return
                        if added:
                            backoff = 0
                            return
                    msg = "rekomendasi habis"
                except Exception as e:  # jaringan putus dsb.
                    msg = f"gagal ambil rekomendasi: {e}"
                backoff = min(backoff * 2 or 15, 300)
                retry_at = time.monotonic() + backoff
                ui.say(f"[{msg}, coba lagi dalam {backoff} dtk]")
            finally:
                refilling.clear()

        def current_vid():
            return video_id_from(props.get("path") or "")

        def like():
            vid = current_vid()
            if vid:
                append_line(FAVORITES_FILE, f"{ui.labels.get(vid, vid)}\thttps://music.youtube.com/watch?v={vid}")
                ui.say(f"  ♥ disimpan ke {FAVORITES_FILE}")

        def block():
            vid = current_vid()
            artists = ui.artists.get(vid)
            if not artists:
                ui.say("  [artis lagu ini tidak diketahui]")
                return
            blocked.add(artists[0].lower())
            save_json(BLOCKED_FILE, sorted(blocked))
            # buang lagu artis ini yang sudah terlanjur ada di antrian
            pos = ipc.cmd("get_property", "playlist-pos") or 0
            count = ipc.cmd("get_property", "playlist-count") or 0
            removed = 0
            for i in range(count - 1, pos, -1):
                v = video_id_from(ipc.cmd("get_property", f"playlist/{i}/filename") or "")
                if is_blocked(ui.artists.get(v, [])):
                    ipc.cmd("playlist-remove", i)
                    removed += 1
            ui.say(f"  🚫 {artists[0]} diblokir, {removed} lagu dibuang dari antrian "
                   f"(buka blokir: edit {BLOCKED_FILE})")
            ipc.cmd("playlist-next")

        def cycle_sleep():
            nonlocal sleep_at, sleep_idx
            sleep_idx = (sleep_idx + 1) % (len(SLEEP_STEPS) + 1)
            if sleep_idx == len(SLEEP_STEPS):
                sleep_at = None
                ui.say("  💤 sleep timer mati")
            else:
                sleep_at = time.monotonic() + SLEEP_STEPS[sleep_idx] * 60
                ui.say(f"  💤 berhenti dalam {SLEEP_STEPS[sleep_idx]} menit (t lagi = ganti/mati)")
            ui.wake.set()

        stations = queue.Queue()  # query baru dari tombol s
        actions = {"like": like, "block": block, "sleep": cycle_sleep}
        threading.Thread(target=key_loop, args=(ipc, stations, ui, actions), daemon=True).start()

        shown = None  # path lagu yang terakhir diumumkan
        title = None
        last_save = 0
        while mpv.poll() is None:
            try:
                new_query = stations.get_nowait()
            except queue.Empty:
                new_query = None
            if new_query:
                try:
                    new_seed = resolve_seed(yt, new_query, ui.say)
                except Exception as e:
                    ui.say(f"[gagal mencari: {e}]")
                    new_seed = None
                if new_seed:
                    with qlock:
                        gen += 1  # refill yang masih berjalan untuk antrian lama dibuang
                        seed = new_seed
                        start_at = None
                        seen.clear()
                        queued.clear()
                        ipc.cmd("stop")  # kosongkan antrian lama
                    ipc.cmd("set_property", "pause", False)
                    retry_at = 0.0
                    backoff = 0  # antrian baru diisi refill di bawah pada putaran berikutnya
                else:
                    ui.say("Lagu tidak ditemukan, lanjut yang tadi. Tekan s untuk coba lagi.")
                    ipc.cmd("set_property", "pause", False)
                continue

            if sleep_at and time.monotonic() >= sleep_at:
                ui.say("\n💤 sleep timer habis, sampai jumpa")
                ipc.cmd("quit")
                break

            pos = props.get("playlist-pos")
            playing = pos is not None and pos >= 0
            path = props.get("path")
            if not path:
                # reset hanya lewat event mpv (bukan saat ganti lagu awal): props bisa masih
                # memegang path lama sesaat setelah stop dan lagu lama akan diumumkan ulang
                shown = None
            elif playing and path != shown:
                shown = path
                vid = video_id_from(path)
                title = ui.labels.get(vid) or props.get("media-title") or path
                ui.say(f"\n♪ {title}")
                append_line(HISTORY_FILE, f"{datetime.datetime.now():%Y-%m-%d %H:%M}\t{title}\t{path}")
                last_save = 0  # simpan segera saat lagu berganti
                # buang lagu lama dari playlist mpv supaya tidak membengkak saat jalan berjam-jam
                for _ in range(pos - KEEP_PLAYED):
                    ipc.cmd("playlist-remove", 0)

            if playing:
                t_pos = ipc.cmd("get_property", "time-pos")
                vol = props.get("volume") or 0
                icon = "⏸" if props.get("pause") else "▶"
                extra = ""
                if props.get("mute"):
                    extra += " · mute"
                if not norm_enabled(props.get("af")):
                    extra += " · normalisasi off"
                if sleep_at:
                    extra += f" · tidur {fmt_time(max(0, sleep_at - time.monotonic()))}"
                ui.set_status(f"  {icon} {fmt_time(t_pos)} / {fmt_time(props.get('duration'))}"
                              f" · vol {vol:.0f}%{extra}")

                if time.monotonic() - last_save >= 5:
                    vid = video_id_from(path or "")
                    if vid:
                        save_json(STATE_FILE, {"video_id": vid, "title": title, "time": t_pos or 0,
                                               "volume": round(vol)})
                    last_save = time.monotonic()

            count = props.get("playlist-count") or 0
            if playing:
                left = count - pos - 1
            elif props.get("idle-active"):
                left = 0  # antrian kosong/habis (awal, ganti lagu, semua gagal) → isi
            else:
                left = count  # lagu pertama masih dimuat
            if (left <= REFILL_WHEN_LEFT and not refilling.is_set()
                    and time.monotonic() >= retry_at):
                refilling.set()
                threading.Thread(target=refill_worker, args=(gen, current_vid() if playing else None),
                                 daemon=True).start()
            ui.wake.wait(1)
            ui.wake.clear()
    except (EOFError, OSError, KeyboardInterrupt):
        pass
    except RuntimeError as e:  # mpv gagal start / tidak bisa dihubungi
        print(f"\n{e}", flush=True)
    finally:
        restore_terminal()
        print(flush=True)  # pindah dari baris status
        if mpv.poll() is None:
            mpv.terminate()
        for conn in (ipc, evc):
            if conn:
                conn.close()
        cleanup_socket()


if __name__ == "__main__":
    main()
