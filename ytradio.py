"""YouTube Music radio tanpa henti: ytmusicapi (rekomendasi) + mpv (audio saja).

Fitur:
- logika radio (antrian, dedupe, backoff, ganti lagu, sleep timer, blokir) di kelas Radio
- bisa diuji tanpa mpv dan tanpa jaringan
- main() cuma wiring: parse argumen, jalankan mpv, sambungkan event, tick per putaran
- MpvPlayer sebagai context manager: mpv + 2 koneksi IPC dijamin dimatikan saat keluar

Pakai:  python ytradio.py "judul lagu / artis"     atau   python ytradio.py <link>
Opsi:   python ytradio.py --help
"""

import argparse
import atexit
import dataclasses
import datetime
import functools
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
from collections.abc import Callable
from typing import Any, Self, TextIO

from ytmusicapi import YTMusic

IS_WINDOWS = sys.platform == "win32"

# nama unik per proses: dua jendela YT Radio tidak saling mengendalikan player yang lain
if IS_WINDOWS:
    PIPE = rf"\\.\pipe\ytradio-{os.getpid()}"
else:
    PIPE = os.path.join(tempfile.gettempdir(), f"ytradio-{os.getpid()}.sock")

REFILL_WHEN_LEFT = 5  # ambil rekomendasi baru kalau sisa antrian <= ini
# lagu yang sudah diputar yang disisakan di playlist mpv (untuk tombol prev)
KEEP_PLAYED = 20
KEEP_QUEUED = 200  # videoId terakhir yang disimpan sebagai sumber rekomendasi cadangan
KEEP_HISTORY = 5000  # baris terakhir riwayat yang disimpan (biar file tidak membengkak)
HISTORY_TRIM_BYTES = 512 * 1024  # file riwayat di-trim hanya kalau sudah lewat ini
SLEEP_STEPS = [15, 30, 60, 90]  # menit, diputar bergiliran oleh tombol t
BACKOFF_START = 15  # detik, jeda refill pertama setelah gagal
BACKOFF_MAX = 300  # detik, batas jeda refill
STATE_EVERY = 5  # detik, sehowoften posisi lagu disimpan
MPV_CANDIDATES = [
    r"C:\Program Files\MPV Player\mpv.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\mpv\mpv.exe"),
]
WATCH_PROPS = [
    "pause",
    "volume",
    "mute",
    "af",
    "playlist-pos",
    "playlist-count",
    "path",
    "media-title",
    "duration",
    "idle-active",
]
UPDATE_HINT = (
    "winget upgrade yt-dlp.yt-dlp" if IS_WINDOWS else "pip install --upgrade yt-dlp"
)

HOME = os.path.expanduser("~")
STATE_FILE = os.path.join(HOME, ".ytradio.json")
BLOCKED_FILE = os.path.join(HOME, ".ytradio-blocked.json")
FAVORITES_FILE = os.path.join(HOME, ".ytradio-favorites.txt")
HISTORY_FILE = os.path.join(HOME, ".ytradio-history.txt")

HELP = (
    "Kontrol: spasi=pause  n/>=next  p/<=prev  ←/→=mundur/maju 10 dtk  "
    "↑/↓ atau 0/9=volume  m=mute  a=normalisasi on/off  u=lagu berikutnya  "
    "l=suka  x=blokir artis  t=sleep timer  s=ganti lagu awal  q=keluar"
)

# Normalisasi volume antar lagu (dynaudnorm: jendela ~15 dtk, halus, tidak "memompa")
NORM_FILTER = "@norm:lavfi=[dynaudnorm=f=500:g=31:p=0.9:m=10]"

# tombol biasa dan tombol panah
KEYS = {
    " ": ("cycle", "pause"),
    "n": ("playlist-next",),
    ">": ("playlist-next",),
    ".": ("playlist-next",),
    "p": ("playlist-prev",),
    "<": ("playlist-prev",),
    ",": ("playlist-prev",),
    "0": ("add", "volume", 5),
    "+": ("add", "volume", 5),
    "=": ("add", "volume", 5),
    "9": ("add", "volume", -5),
    "-": ("add", "volume", -5),
    "m": ("cycle", "mute"),
    # yang di bawah ditangani sendiri oleh key_loop/Radio, bukan perintah mpv
    "a": ("norm",),
    "u": ("upcoming",),
    "s": ("station",),
    "l": ("like",),
    "x": ("block",),
    "t": ("sleep",),
    "q": ("quit",),
}
ARROWS_WIN = {
    "M": ("seek", 10),
    "K": ("seek", -10),
    "H": ("add", "volume", 5),
    "P": ("add", "volume", -5),
}
ARROWS_UNIX = {
    "A": ("add", "volume", 5),  # Atas
    "B": ("add", "volume", -5),  # Bawah
    "C": ("seek", 10),  # Kanan
    "D": ("seek", -10),  # Kiri
}


# --------------------------------------------------------------------------- environment


def warn(msg: str) -> None:
    """Pesan ke stderr (tidak lewat Ui, supaya tetap terlihat walau console sibuk)."""
    print(f"[ytradio] {msg}", file=sys.stderr, flush=True)


def prefer_ipv4() -> None:
    """Prioritaskan IPv4 untuk menghindari timeout jaringan IPv6 yang lambat/bermasalah."""
    orig = socket.getaddrinfo

    def getaddrinfo_ipv4(*args, **kwargs):
        responses = orig(*args, **kwargs)
        ipv4 = [r for r in responses if r[0] == socket.AF_INET]
        return ipv4 if ipv4 else responses

    socket.getaddrinfo = getaddrinfo_ipv4


def cleanup_socket() -> None:
    if not IS_WINDOWS and os.path.exists(PIPE):
        try:
            os.unlink(PIPE)
        except OSError:
            pass


def refresh_path() -> None:
    """Ambil PATH terbaru dari registry di Windows (install winget baru belum masuk ke shell lama)."""
    if not IS_WINDOWS:
        return
    import winreg

    parts = os.environ.get("PATH", "").split(os.pathsep)
    for root, key in [
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ]:
        try:
            with winreg.OpenKey(root, key) as k:
                parts += os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]).split(
                    os.pathsep
                )
        except OSError:
            pass
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(p for p in parts if p))


def find_mpv() -> str:
    candidates = [shutil.which("mpv")]
    if IS_WINDOWS:
        candidates.extend(MPV_CANDIDATES)
    for p in candidates:
        if p and os.path.exists(p):
            return p
    install_msg = (
        "winget install shinchiro.mpv" if IS_WINDOWS else "sudo apt install mpv"
    )
    sys.exit(f"mpv tidak ditemukan. Install: {install_msg}")


def find_ytdlp() -> str:
    exe = "yt-dlp.exe" if IS_WINDOWS else "yt-dlp"
    candidates: list[str | None] = []
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
    install_msg = (
        "winget install yt-dlp.yt-dlp" if IS_WINDOWS else "pip install --upgrade yt-dlp"
    )
    sys.exit(f"yt-dlp tidak ditemukan. Install: {install_msg}")


def install_signal_handlers() -> None:
    """Ctrl+C / SIGTERM harus keluar bersih (mpv dimatikan, socket dihapus)."""
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------- storage


def int_or(value: Any, default: int) -> int:
    """int() yang aman untuk data dari file status yang mungkin diedit manual."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_json(path: str, default: Any) -> Any:
    """Baca JSON; file di home boleh diedit manual, jadi tipe yang tidak sesuai
    `default` diperlakukan sama seperti file rusak."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def atomic_write(path: str, write: Callable[[TextIO], None]) -> None:
    """Tulis lewat file sementara lalu replace, supaya file tidak pernah separuh jadi.

    Nama sementara pakai pid: beberapa jendela YT Radio boleh jalan bersamaan dan
    file di home dipakai bersama, jadi dua proses tidak boleh menulis tmp yang sama."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            write(f)
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        # tmp yang gagal ditulis (atau tidak sempat jadi dipublish) dibuang,
        # kalau tidak file sampingan menumpuk di folder home
        try:
            os.unlink(tmp)
        except OSError:
            pass


def save_json(path: str, data: Any) -> None:
    atomic_write(path, lambda f: json.dump(data, f, ensure_ascii=False))


def trim_lines(path: str, keep: int) -> None:
    """Sisakan `keep` baris terakhir; dipakai buat file yang append terus (riwayat)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (
        OSError,
        ValueError,
    ):  # ValueError: isi file bukan UTF-8 (mis. diedit manual)
        return
    if len(lines) <= keep:
        return
    atomic_write(path, lambda f: f.writelines(lines[-keep:]))


def append_line(path: str, line: str, keep: int = 0) -> None:
    """keep=0: tanpa batas. keep=N: sisakan N baris terakhir (kalau file sudah besar)."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if keep and os.path.getsize(path) >= HISTORY_TRIM_BYTES:
            trim_lines(path, keep)
    except OSError:
        pass


# --------------------------------------------------------------------------- tracks


def video_id_from(text: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})", text)
    return m.group(1) if m else None


def playlist_id_from(text: str) -> str | None:
    m = re.search(r"[?&]list=([\w-]+)", text)
    return m.group(1) if m else None


def track_artists(t: dict) -> list[str]:
    return [a["name"] for a in t.get("artists") or [] if a.get("name")]


def track_label(t: dict) -> str:
    artists = ", ".join(track_artists(t))
    return f"{artists} - {t['title']}" if artists else t["title"]


# keterangan versi di judul: (Official Video), [Lyrics], (Remastered 2011), ...
_TITLE_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*(official|video|audio|lyric|lirik|visuali[sz]er|remaster|m/?v\b|hd|4k)[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
# kredit kolaborasi, di judul ("Yellow (feat. X)") maupun di nama artis
_FEATURED = re.compile(
    r"\s*[\(\[]?\s*\b(?:feat|ft|featuring|ftd)\b\.?\s.*$", re.IGNORECASE
)


def track_key(t: dict) -> str:
    """Kunci dedupe: lagu yang sama dengan videoId berbeda (MV, lyric video, audio) dianggap satu."""
    title = _TITLE_NOISE.sub("", t.get("title") or "")
    title = re.sub(r"\s+-\s+.*remaster.*$", "", title, flags=re.IGNORECASE)
    title = _FEATURED.sub("", title).strip(" -–([")
    artists = track_artists(t)
    first = _FEATURED.sub("", artists[0]).strip() if artists else ""
    return re.sub(r"\W+", " ", f"{first} {title}".lower()).strip()


def resolve_seed(
    yt: YTMusic, query: str, say: Callable[[str], None] | None = None
) -> dict | None:
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


class TrackQueue:
    """Antrian lagu: dedupe (videoId + track_key), batas memori, dan guard generasi.

    Tidak tahu apa itu mpv. `add` menerima fungsi enqueue untuk tiap lagu baru dan
    memanggilnya di bawah lock, jadi penggantian lagu awal tetap atomik terhadap
    refill yang sedang jalan.
    """

    def __init__(self, keep=KEEP_QUEUED):
        self.keep = keep
        self.lock = threading.Lock()
        self.ids = set()  # videoId yang sudah masuk antrian
        self.keys = set()  # track_key yang sudah masuk (versi lain dari lagu yang sama)
        self.vids = []  # videoId dalam urutan antrian, self.keep terakhir saja
        self.gen = 0  # naik tiap ganti lagu awal; hasil refill generasi lama dibuang

    def reset(self, do_reset=None):
        """Ganti lagu awal: naikkan generasi dan kosongkan antrian. do_reset (mis. mpv
        "stop") dijalankan di bawah lock supaya tidak ada lagu lama yang ditambahkan
        setelah antrian dikosongkan."""
        with self.lock:
            self.gen += 1
            self.ids.clear()
            self.keys.clear()
            self.vids.clear()
            if do_reset:
                do_reset()
            return self.gen

    def add(self, gen, tracks, is_blocked, enqueue):
        """Masukkan tracks (hasil ytmusicapi) yang belum pernah masuk antrian.

        Balikan: list videoId yang ditambahkan, atau None kalau `gen` sudah basi.
        """
        with self.lock:
            if gen != self.gen:
                return None
            added = []
            for t in tracks:
                vid, artists = t.get("videoId"), track_artists(t)
                key = track_key(t)
                if (
                    not vid
                    or vid in self.ids
                    or key in self.keys
                    or is_blocked(artists)
                ):
                    continue
                self.ids.add(vid)
                self.keys.add(key)
                self.vids.append(vid)
                enqueue(vid, track_label(t), artists)
                added.append(vid)
            del self.vids[: -self.keep]
            return added

    def candidates(self, gen, current_vid, seed):
        """Sumber refill cadangan kalau radio terakhir buntu: lagu terakhir di antrian,
        lagu yang sedang main, lalu beberapa lagu acak. None kalau `gen` sudah basi."""
        with self.lock:
            if gen != self.gen:
                return None
            if not self.vids:
                return [seed]
            vids = [self.vids[-1], current_vid] + random.sample(
                self.vids[:-1], min(3, len(self.vids) - 1)
            )
            return [{"videoId": v} for v in dict.fromkeys(v for v in vids if v)]


# --------------------------------------------------------------------------- terminal


def fmt_time(sec: float | None) -> str:
    if sec is None:
        return "-:--"
    sec = int(sec)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


_old_term_settings = None


def restore_terminal() -> None:
    global _old_term_settings
    if _old_term_settings is not None and sys.stdin.isatty():
        try:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _old_term_settings)
        except Exception:
            pass
        _old_term_settings = None


atexit.register(restore_terminal)


def set_cbreak() -> None:
    """Linux: baca tombol satu per satu tanpa Enter."""
    # dicek lewat sys.platform langsung (bukan IS_WINDOWS) supaya type checker
    # tahu isi blok ini hanya dipakai di Linux
    if sys.platform == "win32" or not sys.stdin.isatty():
        return
    import termios
    import tty

    global _old_term_settings
    try:
        _old_term_settings = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
    except Exception:
        pass


def read_key() -> tuple | None:
    if IS_WINDOWS:
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return ARROWS_WIN.get(msvcrt.getwch())
        return KEYS.get(ch.lower())
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


class Ui:
    """Output console: baris status yang diperbarui di tempat (\\r) + pesan biasa di atasnya."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = ""
        self.prompting = False  # True selama input() tombol s, jangan timpa prompt
        self.wake = threading.Event()  # minta loop utama segera perbarui status
        self.labels = {}  # videoId -> "Artis - Judul"
        self.artists = {}  # videoId -> ["Artis", ...]

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
        width = shutil.get_terminal_size().columns - 1
        if width > 0:
            text = text[:width]
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


def key_loop(ipc, stations, ui, actions):
    """Baca tombol dari console dan teruskan ke mpv (mpv sendiri tidak baca console).

    actions: nama perintah -> fungsi untuk perintah yang ditangani Radio (like, block, sleep).
    """
    set_cbreak()
    try:
        while True:
            try:
                cmd = read_key()
            except (EOFError, OSError):
                return
            if not cmd:
                continue
            try:
                if cmd[0] == "station":
                    ipc.cmd("set_property", "pause", True)
                    ui.begin_prompt()
                    restore_terminal()
                    try:
                        q = input(
                            "\n⏹ stop. Lagu / artis / link baru (Enter = lanjut yang tadi): "
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        q = ""  # Ctrl+C di prompt = batal, lanjut lagu tadi
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
                    actions["upcoming"]()
                    continue
                if cmd[0] in actions:
                    actions[cmd[0]]()
                    continue
                ipc.cmd(*cmd)
                if cmd[0] == "quit":
                    return
            except (EOFError, OSError):
                return
            except Exception as e:  # jangan sampai thread mati diam-diam
                ui.say(f"[tombol gagal: {e}]")
    finally:
        restore_terminal()


def norm_enabled(af: list | None) -> bool:
    return any(f.get("label") == "norm" and f.get("enabled", True) for f in af or [])


# --------------------------------------------------------------------------- mpv IPC


class Mpv:
    """Klien JSON IPC minimal ke mpv lewat named pipe (Windows) atau unix socket (Linux)."""

    def __init__(self, path: str, proc: subprocess.Popen | None = None) -> None:
        self.sock: socket.socket | None = None
        self.f: Any = None
        for _ in range(50):
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(
                    f"mpv berhenti saat start (exit code {proc.returncode})"
                )
            try:
                # sys.platform langsung, bukan IS_WINDOWS, supaya type checker tahu
                # cabang unix socket hanya hidup di Linux
                if sys.platform == "win32":
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
        # dipanggil untuk event mpv (end-file, property-change, ...)
        self.on_event = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self, timeout: float = 1.0) -> None:
        """Tutup koneksi lewat thread terpisah: di Windows, CloseHandle() menunggu
        I/O yang masih pending di thread pembaca (event), yang hanya selesai kalau
        mpv mengirim data atau mati. Kalau mpv masih hidup dan idle, menutupnya
        langsung akan menggantungkan aplikasi saat exit."""

        def _close_file():
            try:
                if self.f:
                    self.f.close()
            except Exception:
                pass

        t = threading.Thread(target=_close_file, daemon=True)
        t.start()
        t.join(timeout)
        sock = self.sock
        if sock:
            try:
                sock.close()
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
                    self._safe_event(r)

    def _safe_event(self, ev):
        try:
            self.on_event(ev)
        except Exception as e:  # handler salah tidak boleh mematikan thread
            warn(f"event handler error: {e}")

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
                    self._safe_event(r)
        except (EOFError, OSError, ValueError):
            pass


def detect_loadfile_index(ipc: Any) -> bool:
    """mpv >= 0.38 punya argumen 'index' di loadfile; di bawah itu opsinya harus jadi
    argumen ketiga. Bentuk yang dipakai: loadfile <url> <flags> <index> <options>."""
    for c in ipc.cmd("get_property", "command-list") or []:
        if c.get("name") == "loadfile":
            return any(a.get("name") == "index" for a in c.get("args", []))
    return False


class MpvPlayer:
    """Proses mpv + dua koneksi IPC (perintah & event), dijamin dimatikan saat keluar."""

    def __init__(
        self, volume: int = 80, norm: bool = True, force_ipv4: bool = True
    ) -> None:
        self.ipc: Mpv | None = None
        self.evc: Mpv | None = None
        self.proc = subprocess.Popen(
            [
                find_mpv(),
                # mpv dikendalikan dari sini: mpv.conf user boleh menimpa ytdl-format/af/volume
                "--no-config",
                "--idle=yes",
                "--no-video",
                "--force-window=no",
                "--ytdl-format=bestaudio/best",
                f"--script-opts=ytdl_hook-ytdl_path={find_ytdlp()}",
                # client web (default) sering diblok "Sign in to confirm you're not a bot";
                # web_music/mweb lolos. Pakai -append (1 opsi per argumen) karena nilainya berkoma.
                "--ytdl-raw-options-append=extractor-args=youtube:player_client=web_music,mweb",
                *([] if not force_ipv4 else ["--ytdl-raw-options-append=force-ipv4="]),
                f"--input-ipc-server={PIPE}",
                "--msg-level=all=no",  # error ditampilkan sendiri lewat event IPC (1 baris)
                "--prefetch-playlist=yes",
                f"--volume={max(0, min(100, volume))}",
                *([] if not norm else [f"--af={NORM_FILTER}"]),
                "--no-input-terminal",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def __enter__(self) -> Self:
        try:
            ipc = self.ipc = Mpv(PIPE, self.proc)
            ipc.cmd("disable_event", "all")  # event dibaca lewat koneksi kedua
            self.evc = Mpv(PIPE, self.proc)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        # mpv dimatikan dulu: begitu prosesnya gone, read yang tertunda di thread event
        # selesai dan handle pipe bisa ditutup tanpa nunggu.
        if self.proc.poll() is None:
            self.proc.terminate()
        for conn in (self.evc, self.ipc):
            if conn:
                conn.close()
        cleanup_socket()


# --------------------------------------------------------------------------- radio


@dataclasses.dataclass
class Settings:
    """Semua knob yang tadinya konstanta modul, supaya Radio bisa diuji dengan angka kecil."""

    refill_when_left: int = REFILL_WHEN_LEFT
    keep_played: int = KEEP_PLAYED
    keep_queued: int = KEEP_QUEUED
    sleep_steps: tuple = tuple(SLEEP_STEPS)
    sleep_minutes: int | None = None
    norm: bool = True  # True = filter dynaudnorm aktif (tombol a)
    loadfile_has_index: bool = True
    state_every: int = STATE_EVERY
    state_file: str = STATE_FILE
    history_file: str = HISTORY_FILE
    favorites_file: str = FAVORITES_FILE
    blocked_file: str = BLOCKED_FILE


class Radio:
    """Seluruh isi radio: antrian, baris status, aksi tombol, dan refill.

    Tidak menyentuh mpv langsung untuk apa pun selain lewat `ipc`; jadi seluruh kelas
    ini bisa diuji dengan mpv palsu. Loop utama cuma memanggil `tick()` berulang.
    """

    def __init__(self, ipc, yt, ui, seed, settings=None, resume_at=None, blocked=()):
        self.ipc, self.yt, self.ui = ipc, yt, ui
        self.settings = settings or Settings()
        self.seed = seed
        self.queue = TrackQueue(keep=self.settings.keep_queued)
        self.blocked = {a.lower() for a in blocked}
        self.props = {}  # cache properti mpv, diisi dari event
        self.fails = 0  # lagu gagal beruntun, buat pesan "cek yt-dlp"
        self.refilling = threading.Event()
        self.retry_at = 0.0  # jangan refill sebelum waktu ini (backoff)
        self.backoff = 0
        self.start_at = resume_at  # posisi lanjut, dipakai sekali untuk lagu awal
        self.sleep_at = (
            time.monotonic() + self.settings.sleep_minutes * 60
            if self.settings.sleep_minutes
            else None
        )
        self.sleep_idx = -1
        self.shown = None  # path lagu yang terakhir diumumkan
        self.title = None
        self.last_save = 0.0
        self.stations = queue.Queue()  # query baru dari tombol s

    # ------------------------------------------------------------------ event mpv

    def on_event(self, ev):
        """Handler event mpv (dipanggil dari thread event, bukan loop utama)."""
        name = ev.get("event")
        if name == "property-change":
            self.props[ev.get("name")] = ev.get("data")
            self.ui.wake.set()
        elif name == "end-file" and ev.get("reason") == "error":
            self.fails += 1
            err = ev.get("file_error")
            self.ui.say(f"  ✗ lagu gagal diputar{f' ({err})' if err else ''}, dilewati")
            if self.fails == 3:
                self.ui.say(
                    "  [3 lagu berturut-turut gagal. Cek internet, atau update yt-dlp: "
                    f"{UPDATE_HINT}]"
                )
        elif name == "file-loaded":
            self.fails = 0

    # ------------------------------------------------------------------ loop utama

    def tick(self):
        """Satu putaran loop utama. True kalau aplikasi harus berhenti."""
        query = self._take_station()
        if query is not None:
            self.switch_station(query)
            return False
        if self.sleep_expired():
            self.ui.say("\n💤 sleep timer habis, sampai jumpa")
            self.ipc.cmd("quit")
            return True
        playing = self.announce_track()
        if playing:
            self.refresh_playing()
        self.maybe_refill(playing)
        return False

    def _take_station(self):
        """Query baru dari tombol s, kalau ada. None = tidak ada permintaan baru."""
        try:
            return self.stations.get_nowait()
        except queue.Empty:
            return None

    def is_playing(self):
        pos = self.props.get("playlist-pos")
        return pos is not None and pos >= 0

    def current_vid(self):
        return video_id_from(self.props.get("path") or "")

    def announce_track(self):
        """Umumkan lagu yang baru mulai; buang lagu lama dari playlist mpv.
        True kalau sedang ada lagu main."""
        playing = self.is_playing()
        path = self.props.get("path")
        if not path:
            # reset hanya lewat event mpv (bukan saat ganti lagu awal): props bisa masih
            # memegang path lama sesaat setelah stop dan lagu lama akan diumumkan ulang
            self.shown = None
        elif playing and path != self.shown:
            self.shown = path
            vid = video_id_from(path)
            self.title = (
                self.ui.labels.get(vid) or self.props.get("media-title") or path
            )
            self.ui.say(f"\n♪ {self.title}")
            append_line(
                self.settings.history_file,
                f"{datetime.datetime.now().astimezone():%Y-%m-%d %H:%M}\t{self.title}\t{path}",
                keep=KEEP_HISTORY,
            )
            self.last_save = 0.0  # simpan segera saat lagu berganti
            # buang lagu lama dari playlist mpv supaya tidak membengkak saat jalan berjam-jam
            for _ in range(
                (self.props.get("playlist-pos") or 0) - self.settings.keep_played
            ):
                self.ipc.cmd("playlist-remove", 0)
        return playing

    def refresh_playing(self):
        """Baris status + penyimpanan state, selama ada lagu main."""
        t_pos = self.ipc.cmd("get_property", "time-pos")
        vol = self.props.get("volume") or 0
        icon = "⏸" if self.props.get("pause") else "▶"
        extra = ""
        if self.props.get("mute"):
            extra += " · mute"
        if not norm_enabled(self.props.get("af")):
            extra += " · normalisasi off"
        if self.sleep_at:
            extra += f" · tidur {fmt_time(max(0, self.sleep_at - time.monotonic()))}"
        self.ui.set_status(
            f"  {icon} {fmt_time(t_pos)} / {fmt_time(self.props.get('duration'))}"
            f" · vol {vol:.0f}%{extra}"
        )
        if time.monotonic() - self.last_save >= self.settings.state_every:
            vid = self.current_vid()
            if vid:
                save_json(
                    self.settings.state_file,
                    {
                        "video_id": vid,
                        "title": self.title,
                        "time": t_pos or 0,
                        "volume": round(vol),
                    },
                )
            self.last_save = time.monotonic()

    # ------------------------------------------------------------------ refill

    def maybe_refill(self, playing):
        """Jalankan refill di thread lain kalau sisa antrian menipis."""
        count = self.props.get("playlist-count") or 0
        if playing:
            left = count - (self.props.get("playlist-pos") or 0) - 1
        elif self.props.get("idle-active"):
            left = 0  # antrian kosong/habis (awal, ganti lagu, semua gagal) → isi
        else:
            left = count  # lagu pertama masih dimuat
        if (
            left <= self.settings.refill_when_left
            and not self.refilling.is_set()
            and time.monotonic() >= self.retry_at
        ):
            self.refilling.set()
            threading.Thread(
                target=self.refill_worker,
                args=(self.queue.gen, self.current_vid() if playing else None),
                daemon=True,
            ).start()

    def refill_worker(self, gen, current_vid):
        """Jalan di thread terpisah supaya loop utama (status, tombol s) tidak tersendat."""
        try:
            cands = self.queue.candidates(gen, current_vid, self.seed)
            if cands is None:
                return  # ganti lagu awal di tengah jalan, hasilnya dibuang
            try:
                for src in cands:
                    added = self.refill(src, gen)
                    if added is None:
                        return
                    if added:
                        self.backoff = 0
                        return
                msg = "rekomendasi habis"
            except Exception as e:  # jaringan putus dsb.
                msg = f"gagal ambil rekomendasi: {e}"
            self.backoff = min(self.backoff * 2 or BACKOFF_START, BACKOFF_MAX)
            self.retry_at = time.monotonic() + self.backoff
            self.ui.say(f"[{msg}, coba lagi dalam {self.backoff} dtk]")
        finally:
            self.refilling.clear()

    def refill(self, src, gen):
        """Tambah lagu dari src (radio sebuah lagu, atau isi playlist) ke antrian.
        Balikan: jumlah lagu baru, atau None kalau generasi sudah basi."""
        is_playlist = "playlistId" in src
        data = self.yt.get_watch_playlist(
            **src, radio=not is_playlist, limit=100 if is_playlist else 30
        )
        enqueue = functools.partial(self._enqueue, src_vid=src.get("videoId"))
        return self.queue.add(gen, data.get("tracks", []), self.is_blocked, enqueue)

    def _enqueue(self, vid, title, artists, src_vid=None):
        """Masukkan satu lagu ke playlist mpv (dipanggil TrackQueue.add di bawah lock)."""
        self.ui.labels[vid] = title
        self.ui.artists[vid] = artists
        url = f"https://music.youtube.com/watch?v={vid}"
        # %N%value = escape mpv untuk nilai berisi karakter apa pun (N = panjang byte)
        opt = f"force-media-title=%{len(title.encode())}%{title}"
        if self.start_at and vid == src_vid:
            opt += f",start={int(self.start_at)}"  # lanjut dari posisi terakhir
            self.start_at = None
        if self.settings.loadfile_has_index:
            self.ipc.cmd("loadfile", url, "append-play", -1, opt)
        else:
            self.ipc.cmd("loadfile", url, "append-play", opt)

    # ------------------------------------------------------------------ aksi tombol

    def key_actions(self):
        return {
            "like": self.like,
            "block": self.block,
            "sleep": self.cycle_sleep,
            "upcoming": self.show_upcoming,
        }

    def switch_station(self, query):
        """Ganti lagu awal (tombol s): antrian lama dibuang, seed baru dipakai."""
        try:
            new_seed = resolve_seed(self.yt, query, self.ui.say)
        except Exception as e:
            self.ui.say(f"[gagal mencari: {e}]")
            new_seed = None
        if not new_seed:
            self.ui.say(
                "Lagu tidak ditemukan, lanjut yang tadi. Tekan s untuk coba lagi."
            )
            self.ipc.cmd("set_property", "pause", False)
            return
        # reset dulu (antrian lama dibuang + refill lama jadi basi), baru ganti seed:
        # refill yang sedang jalan akan lihat gen beda dan membuang hasilnya
        self.queue.reset(lambda: self.ipc.cmd("stop"))
        self.seed = new_seed
        self.start_at = None
        self.ipc.cmd("set_property", "pause", False)
        self.retry_at = 0.0
        self.backoff = 0

    def like(self):
        vid = self.current_vid()
        if vid:
            append_line(
                self.settings.favorites_file,
                f"{self.ui.labels.get(vid, vid)}\thttps://music.youtube.com/watch?v={vid}",
            )
            self.ui.say(f"  ♥ disimpan ke {self.settings.favorites_file}")

    def is_blocked(self, artists):
        return any(a.lower() in self.blocked for a in artists)

    def block(self):
        """Blokir artis lagu ini, buang karyanya dari antrian, lalu skip."""
        vid = self.current_vid()
        artists = self.ui.artists.get(vid)
        if not artists:
            self.ui.say("  [artis lagu ini tidak diketahui]")
            return
        self.blocked.add(artists[0].lower())
        save_json(self.settings.blocked_file, sorted(self.blocked))
        pos = self.ipc.cmd("get_property", "playlist-pos") or 0
        count = self.ipc.cmd("get_property", "playlist-count") or 0
        removed = 0
        for i in range(count - 1, pos, -1):
            v = video_id_from(
                self.ipc.cmd("get_property", f"playlist/{i}/filename") or ""
            )
            if self.is_blocked(self.ui.artists.get(v, [])):
                self.ipc.cmd("playlist-remove", i)
                removed += 1
        self.ui.say(
            f"  🚫 {artists[0]} diblokir, {removed} lagu dibuang dari antrian "
            f"(buka blokir: edit {self.settings.blocked_file})"
        )
        self.ipc.cmd("playlist-next")

    def cycle_sleep(self):
        self.sleep_idx = (self.sleep_idx + 1) % (len(self.settings.sleep_steps) + 1)
        if self.sleep_idx == len(self.settings.sleep_steps):
            self.sleep_at = None
            self.ui.say("  💤 sleep timer mati")
        else:
            step = self.settings.sleep_steps[self.sleep_idx]
            self.sleep_at = time.monotonic() + step * 60
            self.ui.say(f"  💤 berhenti dalam {step} menit (t lagi = ganti/mati)")
        self.ui.wake.set()

    def sleep_expired(self):
        return bool(self.sleep_at) and time.monotonic() >= self.sleep_at

    def show_upcoming(self, n=5):
        pos = self.ipc.cmd("get_property", "playlist-pos")
        count = self.ipc.cmd("get_property", "playlist-count") or 0
        if pos is None or pos < 0:
            return
        lines = ["Berikutnya:"]
        for i in range(pos + 1, min(pos + 1 + n, count)):
            fn = self.ipc.cmd("get_property", f"playlist/{i}/filename") or ""
            lines.append(f"  {i - pos}. {self.ui.labels.get(video_id_from(fn), fn)}")
        if len(lines) == 1:
            lines.append("  (sedang mengambil rekomendasi...)")
        self.ui.say("\n".join(lines))


# --------------------------------------------------------------------------- main


def nonneg_int(text):
    """argparse type: bilangan bulat >= 0 (--sleep negatif harus ditolak)."""
    n = int(text)
    if n < 0:
        raise argparse.ArgumentTypeError("harus >= 0")
    return n


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="ytradio",
        description="YouTube Music radio tanpa henti (audio lewat mpv).",
    )
    p.add_argument(
        "query",
        nargs="*",
        help="judul lagu / artis, link lagu, atau link playlist "
        "(kosong = tanya, Enter = lanjut lagu terakhir)",
    )
    p.add_argument(
        "--volume",
        type=int,
        metavar="0-100",
        help="volume awal (default: terakhir dipakai, atau 80)",
    )
    p.add_argument(
        "--no-norm", action="store_true", help="mulai dengan normalisasi volume mati"
    )
    p.add_argument(
        "--no-ipv4",
        action="store_true",
        help="jangan paksa IPv4 (kalau IPv6 kamu lancar)",
    )
    p.add_argument(
        "--sleep",
        type=nonneg_int,
        metavar="MENIT",
        help="berhenti otomatis setelah sekian menit (0 = mati)",
    )
    return p.parse_args(argv)


def seed_from_query(yt: YTMusic, query: str) -> dict:
    """Query -> sumber radio. Berhenti dengan pesan kalau gagal atau tidak ketemu."""
    if not query or not query.strip():
        sys.exit("Query tidak boleh kosong.")
    try:
        seed = resolve_seed(yt, query)
    except Exception as e:  # jaringan putus dsb.
        sys.exit(f"Gagal mencari lagu: {e}")
    if not seed:
        sys.exit("Lagu tidak ditemukan.")
    return seed


def ask_seed(yt, last):
    """Tentukan lagu awal kalau query kosong: tanya user, atau lanjut lagu terakhir.

    Balikan (seed, resume_at); seed None kalau user batal dan tidak ada lagu terakhir.
    """
    hint = f" (Enter = lanjut: {last['title']})" if last.get("video_id") else ""
    query = input(f"Lagu / artis / link awal{hint}: ").strip()
    if query:
        return seed_from_query(yt, query), None
    if not last.get("video_id"):
        return None, None
    vid, resume_at = str(last["video_id"]), int_or(last.get("time"), 0)
    print(f"Lanjut: {last.get('title', vid)} dari {fmt_time(resume_at)}", flush=True)
    return {"videoId": vid}, resume_at or None


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    install_signal_handlers()
    if not args.no_ipv4:
        prefer_ipv4()

    refresh_path()
    last = load_json(STATE_FILE, {})
    yt = YTMusic()
    query = " ".join(args.query).strip()
    seed, resume_at = (
        (seed_from_query(yt, query), None) if query else ask_seed(yt, last)
    )
    if not seed:
        return
    # .ytradio.json bisa diedit manual: coerce angkanya supaya tidak crash
    volume = args.volume if args.volume is not None else int_or(last.get("volume"), 80)
    settings = Settings(sleep_minutes=args.sleep, norm=not args.no_norm)

    cleanup_socket()
    ui = Ui()
    try:
        with MpvPlayer(
            volume, norm=settings.norm, force_ipv4=not args.no_ipv4
        ) as player:
            settings.loadfile_has_index = detect_loadfile_index(player.ipc)
            blocked = load_json(BLOCKED_FILE, [])
            radio = Radio(
                player.ipc, yt, ui, seed, settings, resume_at=resume_at, blocked=blocked
            )
            player.evc.watch(WATCH_PROPS, radio.on_event)
            print(HELP, flush=True)
            threading.Thread(
                target=key_loop,
                args=(player.ipc, radio.stations, ui, radio.key_actions()),
                daemon=True,
            ).start()
            while player.proc.poll() is None:
                try:
                    if radio.tick():
                        break
                except (
                    Exception
                ) as e:  # radio harus tetap jalan walau satu putaran gagal
                    ui.say(f"[error: {e}]")
                    time.sleep(1)
                ui.wake.wait(1)
                ui.wake.clear()
    except (EOFError, OSError, KeyboardInterrupt):
        pass
    except RuntimeError as e:  # mpv gagal start / tidak bisa dihubungi
        print(f"\n{e}", flush=True)
    finally:
        restore_terminal()
        print(flush=True)  # pindah dari baris status


if __name__ == "__main__":
    main()
