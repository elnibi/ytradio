# YT Radio

Radio YouTube Music tanpa henti di terminal: mulai dari satu lagu, artis, atau playlist, lalu
rekomendasi YouTube Music terus mengisi antrian. Audio saja lewat mpv, tanpa browser.

## Install

```
pip install -r requirements.txt
```

Butuh juga **mpv** dan **yt-dlp**:

| OS      | mpv                            | yt-dlp                          |
|---------|--------------------------------|---------------------------------|
| Windows | `winget install shinchiro.mpv` | `winget install yt-dlp.yt-dlp`  |
| Linux   | `sudo apt install mpv`         | `pip install --upgrade yt-dlp`  |

## Pakai

```
ytradio "coldplay yellow"                 # cari lagu, lalu radio dari situ
ytradio https://youtu.be/xxxxxxxxxxx      # link lagu
ytradio "https://youtube.com/playlist?list=..."   # putar playlist, lalu lanjut radio
ytradio                                   # tanya; Enter = lanjut lagu terakhir dari posisinya
ytradio --sleep 30 --volume 60            # opsi lain: python ytradio.py --help
```

(`ytradio.bat` di Windows, `ytradio.sh` di Linux.)

Opsi: `--volume N`, `--no-norm` (normalisasi volume mati), `--no-ipv4` (jangan paksa IPv4),
`--sleep MENIT`.

## Kontrol

| Tombol            | Fungsi                                    |
|-------------------|-------------------------------------------|
| spasi             | pause / lanjut                            |
| `n` `>` / `p` `<` | lagu berikutnya / sebelumnya              |
| ← / →             | mundur / maju 10 detik                    |
| ↑ ↓ atau `0` `9`  | volume                                    |
| `m`               | mute                                      |
| `a`               | normalisasi volume antar lagu on/off      |
| `u`               | lihat 5 lagu berikutnya                   |
| `l`               | suka: simpan lagu ke daftar favorit       |
| `x`               | blokir artis lagu ini dan lewati          |
| `t`               | sleep timer 15 / 30 / 60 / 90 menit / mati |
| `s`               | ganti lagu / artis / link awal            |
| `q`               | keluar                                    |

Tombol media keyboard juga bisa: di Windows mpv otomatis muncul di kontrol media sistem; di Linux
pasang skrip [mpv-mpris](https://github.com/hoyon/mpv-mpris).

## File di folder home

| File                     | Isi                                                     |
|--------------------------|---------------------------------------------------------|
| `.ytradio.json`          | lagu, posisi, dan volume terakhir (untuk lanjut)        |
| `.ytradio-favorites.txt` | lagu yang di-`l`                                        |
| `.ytradio-history.txt`   | riwayat putar                                           |
| `.ytradio-blocked.json`  | artis yang di-`x`; hapus namanya dari sini untuk buka blokir |

## Test

```
python -m unittest -v
```
