"""Test ytradio: kelas Radio & friends, tanpa mpv dan tanpa jaringan.

Jalankan: python -m unittest -v test_ytradio
"""

import io
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import ytradio as yr

# videoId asli selalu 11 karakter; regex parser jugaRequire 11 karakter
SEED = "seedseedseed"
V1 = "aaaaaaaaaaa"
V2 = "bbbbbbbbbbb"
V3 = "ccccccccccc"
V4 = "ddddddddddd"
BAD = "badbadbadb1"
GOOD = "goodgoodgoo"


def track(title, *artists, vid=V1):
    return {"title": title, "videoId": vid, "artists": [{"name": a} for a in artists]}


def url(vid):
    return f"https://music.youtube.com/watch?v={vid}"


class FakeIpc:
    """Mpv palsu: mencatat perintah, membalas dari dict `props`."""

    def __init__(self, props=None, command_list=None):
        self.calls = []
        self.props = props or {}
        self.command_list = (
            command_list
            if command_list is not None
            else [{"name": "loadfile", "args": [{"name": "index"}]}]
        )
        self.fail_command = None  # nama perintah yang harus melempar
        self.fail_prop = None  # properti yang harus melempar saat di-get

    def cmd(self, *args):
        self.calls.append(args)
        if self.fail_command and args[0] == self.fail_command:
            raise OSError("koneksi mpv putus")
        if args[0] == "get_property":
            p = args[1]
            if self.fail_prop and p == self.fail_prop:
                raise OSError(f"get {p} gagal")
            if p == "command-list":
                return self.command_list
            if p == "time-pos":
                return 30.0
            return self.props.get(p)
        return None

    def names(self):
        return [c[0] for c in self.calls]

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)

    def of(self, name):
        return [c for c in self.calls if c[0] == name]


class FakeYT:
    """ytmusicapi palsu: `tracks` untuk search/playlist, `radio_tracks` untuk radio."""

    def __init__(self, tracks=None, radio_tracks=None):
        self.tracks = tracks if tracks is not None else []
        self.radio_tracks = radio_tracks if radio_tracks is not None else self.tracks
        self.fail = None
        self.queries = []

    def search(self, query, filter=None, limit=None):
        self.queries.append(query)
        return [self.tracks[0]] if self.tracks else []

    def get_watch_playlist(self, videoId=None, playlistId=None, radio=True, limit=30):
        if self.fail:
            raise RuntimeError(self.fail)
        src = self.radio_tracks if radio else self.tracks
        return {"tracks": [dict(t) for t in src]}


class FakeUi(yr.Ui):
    """Ui yang tidak menulis ke console, cuma mengumpulkan pesan."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def say(self, msg):
        self.messages.append(msg)

    def set_status(self, text):
        self.status = text

    @property
    def said(self):
        return " | ".join(self.messages)


class TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ytradio-test-")

    def settings(self, **overrides):
        return yr.Settings(
            state_file=os.path.join(self.tmp, "state.json"),
            history_file=os.path.join(self.tmp, "history.txt"),
            log_file=os.path.join(self.tmp, "ytradio.log"),
            favorites_file=os.path.join(self.tmp, "fav.txt"),
            blocked_file=os.path.join(self.tmp, "blocked.json"),
            **overrides,
        )

    def radio(self, ipc=None, yt=None, ui=None, resume_at=None, **overrides):
        return yr.Radio(
            ipc if ipc is not None else FakeIpc(),
            yt if yt is not None else FakeYT(),
            ui if ui is not None else FakeUi(),
            {"videoId": SEED},
            self.settings(**overrides),
            resume_at=resume_at,
        )

    def playing(self, radio, vid=V1, pos=0, count=10, **extra):
        """Isi cache props seolah mpv sedang memutar `vid`."""
        radio.props.update(
            {
                "playlist-pos": pos,
                "playlist-count": count,
                "path": url(vid),
                "media-title": "judul",
                "duration": 210.0,
                "volume": 80.0,
                "pause": False,
                "af": [{"label": "norm"}],
                "idle-active": False,
            }
        )
        radio.props.update(extra)
        return radio

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()


# ------------------------------------------------------------------ parsing


class TestParsing(TestCase):
    def test_video_id(self):
        self.assertEqual(
            yr.video_id_from("https://music.youtube.com/watch?v=dQw4w9WgXcQ&si=x"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(
            yr.video_id_from("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ"
        )
        self.assertIsNone(yr.video_id_from("coldplay yellow"))

    def test_playlist_id(self):
        self.assertEqual(
            yr.playlist_id_from("https://youtube.com/watch?v=dQw4w9WgXcQ&list=RDxyz"),
            "RDxyz",
        )

    def test_fmt_time(self):
        self.assertEqual(yr.fmt_time(None), "-:--")
        self.assertEqual(yr.fmt_time(65.9), "1:05")
        self.assertEqual(yr.fmt_time(3725), "1:02:05")

    def test_parse_args(self):
        a = yr.parse_args(["coldplay", "yellow", "--volume", "50", "--sleep", "30"])
        self.assertEqual(a.query, ["coldplay", "yellow"])
        self.assertEqual(
            (a.volume, a.sleep, a.no_norm, a.no_ipv4), (50, 30, False, False)
        )

    def test_sleep_negatif_ditolak(self):
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            yr.parse_args(["--sleep", "-1"])

    def test_resolve_seed_pencarian(self):
        yt = FakeYT([track("Yellow", "Coldplay", vid="yKNxeF4KMsY")])
        said = []
        self.assertEqual(
            yr.resolve_seed(yt, "coldplay yellow", said.append),
            {"videoId": "yKNxeF4KMsY"},
        )
        self.assertEqual(said, ["Mulai dari: Coldplay - Yellow"])

    def test_resolve_seed_link(self):
        self.assertEqual(
            yr.resolve_seed(None, "https://youtu.be/dQw4w9WgXcQ"),
            {"videoId": "dQw4w9WgXcQ"},
        )
        self.assertEqual(
            yr.resolve_seed(None, "https://youtube.com/watch?v=dQw4w9WgXcQ&list=PLx"),
            {"playlistId": "PLx", "videoId": "dQw4w9WgXcQ"},
        )

    def test_track_key_feat(self):
        base = yr.track_key(track("Yellow", "Coldplay"))
        self.assertEqual(yr.track_key(track("Yellow (feat. X)", "Coldplay")), base)
        self.assertEqual(yr.track_key(track("Yellow", "Coldplay feat. X")), base)


# ------------------------------------------------------------------ TrackQueue


class TestTrackQueue(TestCase):
    def setUp(self):
        super().setUp()
        self.tq = yr.TrackQueue()

    def enqueue_recorder(self):
        got = []
        return got, lambda vid, title, artists: got.append(vid)

    def test_tambah_lagu_baru(self):
        got, enqueue = self.enqueue_recorder()
        res = self.tq.add(
            self.tq.gen,
            [track("Yellow", "Coldplay", vid=V1), track("Fix You", "Coldplay", vid=V2)],
            lambda a: False,
            enqueue,
        )
        self.assertEqual(res, [V1, V2])
        self.assertEqual(got, [V1, V2])

    def test_dedupe_id_dan_key(self):
        _, enqueue = self.enqueue_recorder()
        tracks = [
            track("Yellow", "Coldplay", vid=V1),
            track("Yellow", "Coldplay", vid=V2),  # key sama
            track("Yellow (Official Video)", "Coldplay", vid=V3),  # key sama
            track("Yellow", "Coldplay", vid=V1),  # id sama
        ]
        self.assertEqual(
            self.tq.add(self.tq.gen, tracks, lambda a: False, enqueue), [V1]
        )
        self.assertEqual(self.tq.add(self.tq.gen, tracks, lambda a: False, enqueue), [])

    def test_blokir_dilewati(self):
        res = self.tq.add(
            self.tq.gen,
            [track("X", "Bad", vid=V1), track("Y", "Good", vid=V2)],
            lambda a: "Bad" in a,
            lambda *a: None,
        )
        self.assertEqual(res, [V2])

    def test_videoId_kosong_dilewati(self):
        res = self.tq.add(
            self.tq.gen,
            [{"title": "Tanpa ID", "artists": []}],
            lambda a: False,
            lambda *a: None,
        )
        self.assertEqual(res, [])

    def test_reset_kosongkan_dan_naikkan_gen(self):
        _, enqueue = self.enqueue_recorder()
        self.tq.add(self.tq.gen, [track("X", "Y", vid=V1)], lambda a: False, enqueue)
        calls = []
        self.tq.reset(lambda: calls.append("stop"))
        self.assertEqual(calls, ["stop"])  # dijalankan di bawah lock
        self.assertEqual((self.tq.ids, self.tq.keys, self.tq.vids), (set(), set(), []))
        self.assertEqual(self.tq.gen, 1)

    def test_generasi_basi(self):
        _, enqueue = self.enqueue_recorder()
        stale = self.tq.gen
        self.tq.reset()
        self.assertIsNone(
            self.tq.add(stale, [track("X", "Y", vid=V1)], lambda a: False, enqueue)
        )

    def test_reset_boleh_tambah_lagu_sama_lagi(self):
        _, enqueue = self.enqueue_recorder()
        tracks = [track("X", "Y", vid=V1)]
        self.tq.add(self.tq.gen, tracks, lambda a: False, enqueue)
        self.tq.reset()
        self.assertEqual(
            self.tq.add(self.tq.gen, tracks, lambda a: False, enqueue), [V1]
        )

    def test_batas_memori(self):
        tq = yr.TrackQueue(keep=2)
        for i in range(5):
            tq.add(
                tq.gen,
                [track(f"L{i}", "A", vid=f"vid{i:08d}")],
                lambda a: False,
                lambda *a: None,
            )
        self.assertEqual(tq.vids, ["vid00000003", "vid00000004"])

    def test_kandidat_refill(self):
        for i in range(4):
            self.tq.add(
                self.tq.gen,
                [track(f"L{i}", "A", vid=f"vid{i:08d}")],
                lambda a: False,
                lambda *a: None,
            )
        cands = self.tq.candidates(self.tq.gen, "main", {"videoId": SEED})
        self.assertEqual(cands[0], {"videoId": "vid00000003"})  # terakhir di antrian
        self.assertEqual(cands[1], {"videoId": "main"})  # sedang main
        self.assertEqual(len(cands), 5)  # + 3 acak
        self.assertNotIn({"videoId": SEED}, cands)

    def test_kandidat_kosong_pakai_seed(self):
        self.assertEqual(
            self.tq.candidates(self.tq.gen, None, {"playlistId": "PLx"}),
            [{"playlistId": "PLx"}],
        )

    def test_kandidat_generasi_basi(self):
        stale = self.tq.gen
        self.tq.reset()
        self.assertIsNone(self.tq.candidates(stale, None, {"videoId": SEED}))


# ------------------------------------------------------------------ refill


class TestRefill(TestCase):
    def test_refill_memuat_dan_dedupe(self):
        yt = FakeYT(
            [
                track("Yellow", "Coldplay", vid=V1),
                track("Yellow (Official Video)", "Coldplay", vid=V2),  # duplikat
                track("Fix You", "Coldplay", vid=V3),
            ]
        )
        ipc = FakeIpc()
        radio = self.radio(ipc=ipc, yt=yt)
        self.assertEqual(radio.refill({"videoId": SEED}, radio.queue.gen), [V1, V3])
        self.assertEqual(ipc.count("loadfile"), 2)

    def test_loadfile_bentuk_lengkap(self):
        ipc = FakeIpc()
        radio = self.radio(ipc=ipc, yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.refill({"videoId": SEED}, radio.queue.gen)
        call = ipc.of("loadfile")[0]
        self.assertEqual(call[1], url(V1))
        self.assertEqual(call[2], "append-play")
        self.assertEqual(call[3], -1)  # argumen index (mpv >= 0.38)
        self.assertIn("force-media-title=", call[4])

    def test_loadfile_tanpa_index(self):
        ipc = FakeIpc()
        radio = self.radio(
            ipc=ipc,
            yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]),
            loadfile_has_index=False,
        )
        radio.refill({"videoId": SEED}, radio.queue.gen)
        call = ipc.of("loadfile")[0]
        self.assertEqual(len(call), 4)  # opsi jadi argumen ketiga
        self.assertIn("force-media-title=", call[3])

    def test_media_title_escaped_berkoma(self):
        radio = self.radio(yt=FakeYT([track("Yellow", "C, D", vid=V1)]))
        radio.refill({"videoId": SEED}, radio.queue.gen)
        opt = radio.ipc.of("loadfile")[0][4]
        # %N% dengan N = panjang byte UTF-8 dari "C, D - Yellow" = 13 byte ASCII
        self.assertIn("%13%C, D - Yellow", opt)

    def test_media_title_panjang_itu_byte_bukan_karakter(self):
        radio = self.radio(yt=FakeYT([track("夜に駆ける", "YOASOBI", vid=V1)]))
        radio.refill({"videoId": SEED}, radio.queue.gen)
        opt = radio.ipc.of("loadfile")[0][4]
        title = "YOASOBI - 夜に駆ける"  # 5 + 3 + 5x3 byte = 23 byte UTF-8
        self.assertIn(f"%{len(title.encode())}%{title}", opt)

    def test_start_lanjut_dipakai_sekali(self):
        radio = self.radio(
            yt=FakeYT(
                [
                    track("Yellow", "Coldplay", vid=V1),
                    track("Fix You", "Coldplay", vid=V2),
                ]
            ),
            resume_at=42.0,
        )
        radio.refill({"videoId": V1}, radio.queue.gen)
        opts = [c[4] for c in radio.ipc.of("loadfile")]
        self.assertIn("start=42", opts[0])
        self.assertNotIn("start=42", opts[1])
        self.assertIsNone(radio.start_at)  # dipakai sekali

    def test_start_hanya_untuk_lagu_seed(self):
        radio = self.radio(
            yt=FakeYT([track("Fix You", "Coldplay", vid=V2)]), resume_at=42.0
        )
        radio.refill({"videoId": V1}, radio.queue.gen)  # seed != lagu yang di-load
        self.assertNotIn("start=42", radio.ipc.of("loadfile")[0][4])

    def test_refill_generasi_basi_membuang(self):
        radio = self.radio(yt=FakeYT([track("X", "Y", vid=V1)]))
        radio.queue.reset()
        self.assertIsNone(radio.refill({"videoId": SEED}, 0))
        self.assertEqual(radio.ipc.count("loadfile"), 0)

    def test_labels_dan_artists_terisi(self):
        radio = self.radio(yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.refill({"videoId": SEED}, radio.queue.gen)
        self.assertEqual(radio.ui.labels[V1], "Coldplay - Yellow")
        self.assertEqual(radio.ui.artists[V1], ["Coldplay"])

    def test_playlist_batas_100_radio_30(self):
        seen = []

        class RecorderYT(FakeYT):
            def get_watch_playlist(
                self, videoId=None, playlistId=None, radio=True, limit=30
            ):
                seen.append((playlistId, radio, limit))
                return super().get_watch_playlist(
                    videoId, playlistId, radio=radio, limit=limit
                )

        yt = RecorderYT([track("X", "Y", vid=V1)])
        radio = self.radio(yt=yt)
        radio.refill({"videoId": SEED}, radio.queue.gen)
        radio.refill({"playlistId": "PLx"}, radio.queue.gen)
        self.assertEqual(seen[0], (None, True, 30))
        self.assertEqual(seen[1], ("PLx", False, 100))


# ------------------------------------------------------------------ backoff


class TestBackoff(TestCase):
    def refill_once(self, radio, at=0.0):
        with patch("ytradio.time.monotonic", return_value=at):
            radio.refill_worker(radio.queue.gen, None)
        self.assertFalse(radio.refilling.is_set())

    def test_gagal_jaringan_tambah_backoff(self):
        yt = FakeYT()
        yt.fail = "jaringan putus"
        radio = self.radio(yt=yt)
        self.refill_once(radio, at=1000.0)
        self.assertEqual(radio.backoff, yr.BACKOFF_START)
        self.assertEqual(radio.retry_at, 1000.0 + yr.BACKOFF_START)
        self.assertIn("gagal ambil rekomendasi", radio.ui.said)

    def test_rekomendasi_habis_tambah_backoff(self):
        radio = self.radio(yt=FakeYT([track("X", "Y", vid=V1)]))
        radio.queue.add(0, [track("X", "Y", vid=V1)], lambda a: False, lambda *a: None)
        self.refill_once(radio)
        self.assertIn("rekomendasi habis", radio.ui.said)
        self.assertEqual(radio.backoff, yr.BACKOFF_START)

    def test_backoff_naik_sampai_batas(self):
        radio = self.radio(yt=FakeYT())
        for expected in (15, 30, 60, 120, 240, yr.BACKOFF_MAX, yr.BACKOFF_MAX):
            self.refill_once(radio)
            self.assertEqual(radio.backoff, expected)

    def test_sukses_reset_backoff(self):
        radio = self.radio(yt=FakeYT([track("X", "Y", vid=V1)]))
        radio.backoff = 60
        self.refill_once(radio)
        self.assertEqual(radio.backoff, 0)

    def test_generasi_basi_tidak_backoff(self):
        radio = self.radio(yt=FakeYT([track("X", "Y", vid=V1)]))
        radio.queue.reset()
        radio.backoff = 0
        self.refill_once(radio)
        self.assertEqual(radio.backoff, 0)  # tidak ada yang dihitung
        self.assertEqual(radio.ui.messages, [])

    def test_maybe_refill_hormati_retry_at(self):
        radio = self.radio(yt=FakeYT([track("X", "Y", vid=V1)]))
        radio.retry_at = 999.0
        with patch("ytradio.time.monotonic", return_value=100.0):
            radio.maybe_refill(playing=False)
        self.assertFalse(radio.refilling.is_set())  # belum dared refill


# ------------------------------------------------------------------ tick


class TestTick(TestCase):
    def test_tick_false_saat_jalan(self):
        self.assertFalse(self.radio().tick())

    def test_tick_true_saat_sleep_habis(self):
        radio = self.radio()
        radio.sleep_at = 100.0
        self.playing(radio)
        with patch("ytradio.time.monotonic", return_value=500.0):
            self.assertTrue(radio.tick())
        self.assertIn("quit", radio.ipc.names())
        self.assertIn("sleep timer habis", radio.ui.said)

    def test_tick_false_saat_sleep_belum_habis(self):
        radio = self.radio()
        radio.sleep_at = 500.0
        self.playing(radio)
        with patch("ytradio.time.monotonic", return_value=100.0):
            self.assertFalse(radio.tick())
        self.assertNotIn("quit", radio.ipc.names())

    def test_umu_lagu_baru(self):
        radio = self.radio(yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.tick()  # refill, load V1
        self.playing(radio, V1)
        radio.tick()  # umumkan
        self.assertTrue(any("Coldplay - Yellow" in m for m in radio.ui.messages))
        self.assertEqual(radio.title, "Coldplay - Yellow")

    def test_tidak_umu_lagu_sama_dua_kali(self):
        radio = self.radio(yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.tick()
        self.playing(radio, V1)
        radio.tick()
        count = len(radio.ui.messages)
        radio.tick()  # path sama
        self.assertEqual(len(radio.ui.messages), count)

    def test_path_kosong_reset_shown(self):
        radio = self.radio(yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.tick()
        self.playing(radio, V1)
        radio.tick()
        radio.shown = "x"
        radio.props["path"] = None  # idle: props kosong
        radio.tick()
        self.assertIsNone(radio.shown)

    def test_state_tersimpan_saat_lagu_berganti(self):
        radio = self.radio(yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.tick()
        self.playing(radio, V1)
        radio.tick()
        state = json.loads(self.read(radio.settings.state_file))
        self.assertEqual(state["video_id"], V1)
        self.assertEqual(state["title"], "Coldplay - Yellow")
        self.assertEqual(state["volume"], 80)
        self.assertEqual(state["time"], 30.0)

    def test_state_dibatasi_setiap_5_detik(self):
        radio = self.radio(yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.tick()
        self.playing(radio, V1)
        radio.tick()
        saved = os.path.getmtime(radio.settings.state_file)
        with patch("ytradio.time.monotonic", return_value=time.monotonic() + 1):
            radio.tick()
        self.assertEqual(os.path.getmtime(radio.settings.state_file), saved)

    def test_buang_lagu_lama_dari_playlist(self):
        radio = self.radio(
            yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]), keep_played=3
        )
        radio.tick()
        self.playing(radio, V1, pos=7, count=8)
        radio.tick()
        self.assertEqual(radio.ipc.count("playlist-remove"), 4)  # 7 - 3

    def test_riwayat_ditulis(self):
        radio = self.radio(yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.tick()
        self.playing(radio, V1)
        radio.tick()
        self.assertIn("Coldplay - Yellow", self.read(radio.settings.history_file))

    def test_error_dilempar_ke_main(self):
        """tick melempar; main() yang menahan supaya radio tidak mati."""
        radio = self.radio(yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.tick()
        self.playing(radio, V1)
        radio.ipc.fail_prop = "time-pos"
        with self.assertRaises(OSError):
            radio.tick()


# ------------------------------------------------------------------ ganti lagu


class TestSwitchStation(TestCase):
    def test_antrian_lama_dibuang(self):
        ipc = FakeIpc()
        yt = FakeYT([track("Yellow", "Coldplay", vid=V1)])
        radio = self.radio(ipc=ipc, yt=yt)
        radio.tick()
        self.assertEqual(radio.queue.vids, [V1])

        yt.tracks = yt.radio_tracks = [track("Paradise", "Coldplay", vid=V2)]
        radio.switch_station("coldplay paradise")
        self.assertEqual(ipc.count("stop"), 1)
        self.assertEqual(radio.queue.vids, [])
        self.assertEqual(radio.queue.gen, 1)
        self.assertEqual(radio.seed, {"videoId": V2})
        self.assertEqual(radio.backoff, 0)
        self.assertIn(("set_property", "pause", False), ipc.calls)

    def test_start_at_dibuang(self):
        radio = self.radio(yt=FakeYT([track("X", "Y", vid=V2)]), resume_at=42.0)
        radio.switch_station("lagu baru")
        self.assertIsNone(radio.start_at)

    def test_gagal_cari_antrian_lama_utuh(self):
        radio = self.radio(yt=FakeYT([track("X", "Y", vid=V1)]))
        radio.tick()

        class BrokenYT(FakeYT):
            def search(self, query, filter=None, limit=None):
                raise RuntimeError("offline")

        radio.yt = BrokenYT([track("X", "Y", vid=V1)])
        radio.switch_station("apa saja")
        self.assertIn("gagal mencari", radio.ui.said)
        self.assertEqual(radio.queue.vids, [V1])

    def test_query_dari_tombol_s(self):
        radio = self.radio(yt=FakeYT([track("Paradise", "Coldplay", vid=V2)]))
        radio.stations.put("coldplay paradise")
        radio.tick()
        self.assertEqual(radio.seed, {"videoId": V2})

    def test_ganti_lagu_kosong_tidak_mengganggu(self):
        """Query tak ketemu: antrian lama harus tetap utuh dan tetap diputar."""
        ipc = FakeIpc()
        radio = self.radio(ipc=ipc, yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.tick()
        radio.yt = FakeYT([])
        radio.switch_station("entah lagu yang tidak ada")
        self.assertIn("tidak ditemukan", radio.ui.said.lower())
        self.assertEqual(radio.queue.vids, [V1])
        self.assertEqual(ipc.count("stop"), 0)
        self.assertIn(("set_property", "pause", False), ipc.calls)

    def test_refill_generasi_lama_dibuang(self):
        radio = self.radio(yt=FakeYT([track("X", "Y", vid=V2)]))
        radio.queue.add(0, [track("A", "B", vid=V1)], lambda a: False, lambda *a: None)
        radio.switch_station("lagu baru")
        self.assertIsNone(radio.refill({"videoId": V1}, 0))
        self.assertNotIn(V1, radio.queue.vids)


# ------------------------------------------------------------------ aksi tombol


class TestActions(TestCase):
    def test_like(self):
        radio = self.radio(yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.tick()
        self.playing(radio, V1)
        radio.like()
        line = self.read(radio.settings.favorites_file).strip()
        self.assertIn("Coldplay - Yellow", line)
        self.assertIn(url(V1), line)

    def test_like_tanpa_lagu_jalan(self):
        radio = self.radio()
        radio.like()  # tidak ada path -> diam, tidak error

    def test_block_simpan_dan_lanjut(self):
        radio = self.radio(yt=FakeYT([track("Bad", "BadArtis", vid=BAD)]))
        radio.tick()
        self.playing(radio, BAD)
        radio.block()
        self.assertIn("badartis", radio.blocked)
        self.assertIn("badartis", json.loads(self.read(radio.settings.blocked_file)))
        self.assertIn("playlist-next", radio.ipc.names())  # skip lagunya
        self.assertIn("diblokir", radio.ui.said)

    def test_block_membuang_karya_lain_di_antrian(self):
        """Blokir artis = buang lagunya yang masih di antrian, selain yang sedang main."""
        radio = self.radio(
            yt=FakeYT(
                [
                    track("Main", "GoodArtis", vid=GOOD),
                    track("Juga", "BadArtis", vid=BAD),
                    track("Sama", "BadArtis", vid=V2),
                    track("Awal", "GoodArtis", vid=V1),
                ]
            )
        )
        radio.tick()
        radio.ui.artists.update(
            {
                GOOD: ["GoodArtis"],
                BAD: ["BadArtis"],
                V2: ["BadArtis"],
                V1: ["GoodArtis"],
            }
        )
        # mpv: 0 = GoodArtis (main), 1 = BadArtis, 2 = BadArtis, 3 = GoodArtis
        self.playing(radio, GOOD, pos=0, count=4)
        radio.ipc.props = {
            "playlist-pos": 0,
            "playlist-count": 4,
            "playlist/1/filename": url(BAD),
            "playlist/2/filename": url(V2),
            "playlist/3/filename": url(V1),
        }
        radio.block()  # blokir GoodArtis
        self.assertIn("goodartis", radio.blocked)
        self.assertEqual(radio.ipc.of("playlist-remove"), [("playlist-remove", 3)])
        self.assertIn("1 lagu dibuang", radio.ui.said)

    def test_block_tidak_tahu_artis(self):
        radio = self.radio()
        self.playing(radio, V1)  # ui.artists belum diisi
        radio.block()
        self.assertIn("tidak diketahui", radio.ui.said)
        self.assertEqual(radio.blocked, set())

    def test_block_simpan_nama_kecil(self):
        radio = self.radio()
        radio.ui.artists[V1] = ["BadArtis"]
        radio.ui.labels[V1] = "BadArtis - X"
        self.playing(radio, V1)
        radio.block()
        self.assertEqual(
            json.loads(self.read(radio.settings.blocked_file)), ["badartis"]
        )

    def test_sleep_cycle_lima_langkah(self):
        radio = self.radio(sleep_steps=(5, 10))
        radio.cycle_sleep()
        self.assertIsNotNone(radio.sleep_at)
        radio.cycle_sleep()
        radio.cycle_sleep()  # habis semua langkah -> mati
        self.assertIsNone(radio.sleep_at)
        self.assertIn("mati", radio.ui.said)

    def test_sleep_langkah_berikutnya_naik(self):
        radio = self.radio(sleep_steps=(5, 10))
        radio.cycle_sleep()
        first = radio.sleep_at
        radio.cycle_sleep()
        self.assertGreater(radio.sleep_at, first)

    def test_sleep_dari_args(self):
        radio = self.radio(sleep_minutes=30)
        self.assertIsNotNone(radio.sleep_at)
        radio2 = self.radio(sleep_minutes=0)
        self.assertIsNone(radio2.sleep_at)  # 0 = mati

    def test_show_upcoming(self):
        radio = self.radio(yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.tick()
        radio.ipc.props = {
            "playlist-pos": 0,
            "playlist-count": 2,
            "playlist/1/filename": url(V1),
        }
        radio.show_upcoming()
        self.assertIn("Berikutnya", radio.ui.said)
        self.assertIn("Coldplay - Yellow", radio.ui.said)  # dari labels

    def test_show_upcoming_kosong(self):
        radio = self.radio()
        radio.ipc.props = {"playlist-pos": 0, "playlist-count": 1}
        radio.show_upcoming()
        self.assertIn("mengambil rekomendasi", radio.ui.said)

    def test_show_upcoming_saat_idle(self):
        radio = self.radio()
        radio.ipc.props = {"playlist-pos": -1, "playlist-count": 0}
        radio.show_upcoming()
        self.assertEqual(radio.ui.messages, [])  # diam

    def test_key_actions_lengkap(self):
        self.assertEqual(
            set(self.radio().key_actions()), {"like", "block", "sleep", "upcoming"}
        )

    def test_is_blocked_kasus_huruf(self):
        radio = self.radio()
        radio.blocked.add("badartis")  # yang disimpan selalu huruf kecil
        self.assertTrue(radio.is_blocked(["BadArtis"]))
        self.assertTrue(radio.is_blocked(["BadArtis"]))
        self.assertFalse(radio.is_blocked(["Other"]))


# ------------------------------------------------------------------ event mpv


class TestEvents(TestCase):
    def test_property_change_masuk_cache(self):
        radio = self.radio()
        radio.on_event({"event": "property-change", "name": "volume", "data": 55})
        self.assertEqual(radio.props["volume"], 55)

    def test_error_beruntun_memberi_pesan(self):
        radio = self.radio()
        radio.on_event({"event": "end-file", "reason": "error", "file_error": "404"})
        self.assertEqual(radio.fails, 1)
        self.assertIn("404", radio.ui.said)
        radio.on_event({"event": "end-file", "reason": "error"})
        radio.on_event({"event": "end-file", "reason": "error"})
        self.assertIn("3 lagu", radio.ui.said)
        self.assertIn(yr.UPDATE_HINT, radio.ui.said)

    def test_error_putar_dicatat_dengan_konteks_lagu(self):
        radio = self.radio(yt=FakeYT([track("Yellow", "Coldplay", vid=V1)]))
        radio.ui.labels[V1] = "Coldplay - Yellow"
        self.playing(radio, V1)
        radio.on_event({"event": "end-file", "reason": "error", "file_error": "403"})
        log = self.read(radio.settings.log_file)
        self.assertIn('"title": "Coldplay - Yellow"', log)
        self.assertIn(f'"video_id": "{V1}"', log)
        self.assertIn('"file_error": "403"', log)

    def test_file_loaded_reset_fails(self):
        radio = self.radio()
        radio.on_event({"event": "end-file", "reason": "error"})
        self.assertEqual(radio.fails, 1)
        radio.on_event({"event": "file-loaded"})
        self.assertEqual(radio.fails, 0)

    def test_end_file_bukan_error_diabaikan(self):
        radio = self.radio()
        radio.on_event({"event": "end-file", "reason": "eof"})
        self.assertEqual(radio.fails, 0)


# ------------------------------------------------------------------ mpv helper


class TestMpvHelpers(TestCase):
    def test_detect_loadfile_index(self):
        self.assertTrue(yr.detect_loadfile_index(FakeIpc()))
        self.assertFalse(
            yr.detect_loadfile_index(
                FakeIpc(command_list=[{"name": "loadfile", "args": []}])
            )
        )
        self.assertFalse(yr.detect_loadfile_index(FakeIpc(command_list=[])))

    def test_event_loop_sigal_saat_mpv_mati(self):
        """Thread event harus keluar bersih kalau mpv menutup koneksi."""

        class DeadMpv(yr.Mpv):
            def __init__(self):
                self.on_event = lambda ev: None

            def _readline(self):
                raise EOFError

        DeadMpv()._event_loop()  # tidak melempar

    def test_event_handler_error_tidak_membunuh_thread(self):
        seen = []

        class BrokenEvent:
            def __call__(self, ev):
                raise ValueError("handler rusak")

        m = object.__new__(yr.Mpv)
        m.on_event = BrokenEvent()
        with patch("ytradio.warn", seen.append):
            m._safe_event({"event": "property-change"})
        self.assertEqual(len(seen), 1)
        self.assertIn("handler rusak", seen[0])

    def test_close_tidak_gantung(self):
        """close() harus selesai dalam batas waktu walau ada reader yang blocked."""

        class BlockingFile:
            def close(self):
                time.sleep(30)  # tiru CloseHandle yang menunggu I/O pending

        m = object.__new__(yr.Mpv)
        m.sock = None
        m.f = BlockingFile()
        start = time.monotonic()
        m.close(timeout=0.3)
        self.assertLess(time.monotonic() - start, 5)


# ------------------------------------------------------------------ settings


class TestSettings(TestCase):
    def test_default_sama_dengan_konstanta(self):
        s = yr.Settings()
        self.assertEqual(s.refill_when_left, yr.REFILL_WHEN_LEFT)
        self.assertEqual(s.keep_played, yr.KEEP_PLAYED)
        self.assertEqual(s.keep_queued, yr.KEEP_QUEUED)
        self.assertEqual(s.sleep_steps, tuple(yr.SLEEP_STEPS))
        self.assertEqual(s.state_every, yr.STATE_EVERY)
        self.assertTrue(s.norm)
        self.assertTrue(s.loadfile_has_index)
        self.assertIsNone(s.sleep_minutes)


class TestHelpers(TestCase):
    def test_log_line_menambah_timestamp_dan_sumber(self):
        path = os.path.join(self.tmp, "ytradio.log")
        yr.log_line(path, "mpv", "ytdl_hook: gagal\n")
        line = self.read(path)
        self.assertIn("[mpv] ytdl_hook: gagal", line)
        self.assertRegex(line, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_int_or(self):
        self.assertEqual(yr.int_or(80, 0), 80)
        self.assertEqual(yr.int_or("55", 0), 55)
        self.assertEqual(yr.int_or(None, 80), 80)
        self.assertEqual(yr.int_or("bukan angka", 80), 80)

    def test_save_json_tulis_berkas(self):
        path = os.path.join(self.tmp, "s.json")
        yr.save_json(path, {"a": 1})
        self.assertEqual(json.loads(self.read(path)), {"a": 1})

    def test_load_json_file_rusak_jadi_default(self):
        path = os.path.join(self.tmp, "rusak.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{bukan json")
        self.assertEqual(yr.load_json(path, {"default": True}), {"default": True})

    def test_load_json_tidak_ada(self):
        self.assertEqual(yr.load_json(os.path.join(self.tmp, "nope.json"), 42), 42)

    def test_load_json_tipe_salah_jadi_default(self):
        # file di home bisa diedit manual jadi null / list / string; main() memakai
        # .get() langsung, jadi tipe yang meleset harus jatuh ke default
        for isi in ("null", "[1, 2]", '"teks"'):
            path = os.path.join(self.tmp, "salah.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(isi)
            self.assertEqual(yr.load_json(path, {"default": True}), {"default": True})
        # tipe yang cocok tetap dipakai (blocked = list of str)
        path = os.path.join(self.tmp, "benar.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('["artis1", "artis2"]')
        self.assertEqual(yr.load_json(path, []), ["artis1", "artis2"])
        self.assertEqual(yr.load_json(path, ["default"]), ["artis1", "artis2"])

    def test_append_line_batasi_baris(self):
        path = os.path.join(self.tmp, "h.txt")
        with patch.object(yr, "HISTORY_TRIM_BYTES", 1):
            for i in range(10):
                yr.append_line(path, f"baris {i}", keep=4)
        self.assertEqual(
            self.read(path).splitlines(),
            ["baris 6", "baris 7", "baris 8", "baris 9"],
        )

    def test_append_line_pertama_tertulis(self):
        path = os.path.join(self.tmp, "baru.txt")
        yr.append_line(path, "pertama", keep=4)
        self.assertEqual(self.read(path).splitlines(), ["pertama"])

    def test_append_line_bukan_utf8_tidak_lempar(self):
        # file riwayat dipakai bersama proses lain, bisa berisi byte non-UTF-8:
        # trim tidak boleh jadi error yang naik ke loop utama
        path = os.path.join(self.tmp, "campuran.txt")
        with open(path, "wb") as f:
            f.write(b"lama\xff\r\nbaru\r\n")
        with patch.object(yr, "HISTORY_TRIM_BYTES", 1):
            yr.append_line(path, "tambahan", keep=2)
        with open(path, encoding="utf-8", errors="replace") as f:
            isi = f.read()
        self.assertIn("baru", isi)
        self.assertIn("tambahan", isi)

    def test_trim_lines_tidak_ada(self):
        yr.trim_lines(os.path.join(self.tmp, "nope.txt"), 5)  # tidak melempar

    def test_ask_seed_state_kosong_atau_rusak(self):
        # .ytradio.json berisi null/list/string: ask_seed harus tetap jalan, bukan
        # traceback dari last.get() sebelum try/except main()
        with patch("builtins.input", return_value=""), patch("builtins.print"):
            kosong, _ = yr.ask_seed(FakeYT(), {})
            rusak, pos = yr.ask_seed(
                FakeYT(), {"video_id": V1, "title": "Yellow", "time": "bukan angka"}
            )
        self.assertIsNone(kosong)
        self.assertEqual(rusak, {"videoId": V1})
        self.assertIsNone(pos)  # time rusak -> 0 -> bukan lanjut

    def test_ask_seed_lanjut_dari_posisi(self):
        with patch("builtins.input", return_value=""), patch("builtins.print"):
            seed, resume_at = yr.ask_seed(
                FakeYT(), {"video_id": V1, "title": "Yellow", "time": 90.5}
            )
        self.assertEqual(seed, {"videoId": V1})
        self.assertEqual(resume_at, 90)  # int_or: detik pecahan dipotong untuk mpv

    def test_tmp_unik_per_proses(self):
        # dua jendela YT Radio boleh jalan bersamaan: nama tmp harus beda
        path = os.path.join(self.tmp, "state.json")
        seen = []

        def catat(f):
            seen.append(f.name)
            f.write("x")

        with patch.object(yr.os, "getpid", side_effect=[111, 222]):
            yr.atomic_write(path, catat)
            yr.atomic_write(path, catat)
        self.assertEqual(len(set(seen)), 2)

    def test_atomic_write_gagal_tidak_ketinggalan_tmp(self):
        path = os.path.join(self.tmp, "gagal.json")

        def ledak(f):
            f.write("x")
            raise RuntimeError("boom")

        with (
            self.assertRaises(RuntimeError),
            patch.object(yr.os, "getpid", return_value=7),
        ):
            yr.atomic_write(path, ledak)
        self.assertEqual(os.listdir(self.tmp), [])


if __name__ == "__main__":
    unittest.main()
