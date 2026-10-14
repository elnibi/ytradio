"""Unit test fungsi murni ytradio (tanpa jaringan/mpv). Jalankan: python -m unittest -v"""
import unittest

import ytradio as yr


class FakeYT:
    def __init__(self, results):
        self.results = results
        self.queries = []

    def search(self, query, filter=None, limit=None):
        self.queries.append(query)
        return self.results


def track(title, *artists, vid="abcdefghijk"):
    return {"title": title, "videoId": vid, "artists": [{"name": a} for a in artists]}


class TestParsing(unittest.TestCase):
    def test_video_id_from(self):
        self.assertEqual(yr.video_id_from("https://music.youtube.com/watch?v=dQw4w9WgXcQ&si=x"), "dQw4w9WgXcQ")
        self.assertEqual(yr.video_id_from("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(yr.video_id_from("https://youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertIsNone(yr.video_id_from("coldplay yellow"))

    def test_playlist_id_from(self):
        self.assertEqual(yr.playlist_id_from("https://youtube.com/playlist?list=PLabc_123-x"), "PLabc_123-x")
        self.assertEqual(yr.playlist_id_from("https://youtube.com/watch?v=dQw4w9WgXcQ&list=RDxyz"), "RDxyz")
        self.assertIsNone(yr.playlist_id_from("https://youtu.be/dQw4w9WgXcQ"))

    def test_fmt_time(self):
        self.assertEqual(yr.fmt_time(None), "-:--")
        self.assertEqual(yr.fmt_time(65.9), "1:05")
        self.assertEqual(yr.fmt_time(3725), "1:02:05")

    def test_parse_args(self):
        a = yr.parse_args(["coldplay", "yellow", "--volume", "50", "--sleep", "30", "--no-norm"])
        self.assertEqual(a.query, ["coldplay", "yellow"])
        self.assertEqual((a.volume, a.sleep, a.no_norm, a.no_ipv4), (50, 30, True, False))


class TestTracks(unittest.TestCase):
    def test_track_label(self):
        self.assertEqual(yr.track_label(track("Yellow", "Coldplay")), "Coldplay - Yellow")
        self.assertEqual(yr.track_label(track("Yellow")), "Yellow")
        self.assertEqual(yr.track_label(track("Lagu", "A", "B")), "A, B - Lagu")

    def test_track_key_versions_same(self):
        base = yr.track_key(track("Yellow", "Coldplay"))
        for title in ["Yellow (Official Video)", "Yellow [Lyrics]", "YELLOW (Remastered 2011)",
                      "Yellow - Remastered 2011", "Yellow (Official Audio)"]:
            self.assertEqual(yr.track_key(track(title, "Coldplay")), base, title)

    def test_track_key_different(self):
        self.assertNotEqual(yr.track_key(track("Yellow", "Coldplay")), yr.track_key(track("Fix You", "Coldplay")))
        self.assertNotEqual(yr.track_key(track("Yellow", "Coldplay")), yr.track_key(track("Yellow", "Other")))
        # judul non-latin tidak boleh jadi kunci kosong
        self.assertTrue(yr.track_key(track("夜に駆ける", "YOASOBI")).endswith("夜に駆ける"))

    def test_norm_enabled(self):
        self.assertTrue(yr.norm_enabled([{"label": "norm", "name": "lavfi"}]))
        self.assertFalse(yr.norm_enabled([{"label": "norm", "enabled": False}]))
        self.assertFalse(yr.norm_enabled(None))


class TestResolveSeed(unittest.TestCase):
    def test_link_lagu(self):
        self.assertEqual(yr.resolve_seed(None, "https://youtu.be/dQw4w9WgXcQ"), {"videoId": "dQw4w9WgXcQ"})

    def test_link_playlist(self):
        self.assertEqual(yr.resolve_seed(None, "https://youtube.com/playlist?list=PLx"), {"playlistId": "PLx"})
        self.assertEqual(yr.resolve_seed(None, "https://youtube.com/watch?v=dQw4w9WgXcQ&list=PLx"),
                         {"playlistId": "PLx", "videoId": "dQw4w9WgXcQ"})

    def test_search(self):
        said = []
        yt = FakeYT([track("Yellow", "Coldplay", vid="yKNxeF4KMsY")])
        self.assertEqual(yr.resolve_seed(yt, "coldplay yellow", said.append), {"videoId": "yKNxeF4KMsY"})
        self.assertEqual(said, ["Mulai dari: Coldplay - Yellow"])

    def test_search_kosong(self):
        self.assertIsNone(yr.resolve_seed(FakeYT([]), "zzzz"))


if __name__ == "__main__":
    unittest.main()
