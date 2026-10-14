# YT Radio

Radio YouTube Music tanpa henti di terminal: mulai dari satu lagu, artis, atau playlist, lalu
rekomendasi YouTube Music terus mengisi antrian. Audio saja lewat mpv, tanpa browser.

## Install

Butuh Python 3.10 atau lebih baru.

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

## Kalau ada masalah

| Gejala                                         | Coba                                                         |
|------------------------------------------------|--------------------------------------------------------------|
| `✗ lagu gagal diputar` berturut-turut          | cek internet, lalu update yt-dlp (`winget upgrade yt-dlp.yt-dlp` / `pip install -U yt-dlp`) |
| `[rekomendasi habis, coba lagi dalam N dtk]`   | biarkan saja (dicoba ulang otomatis), atau `s` untuk ganti lagu awal |
| koneksi lambat / timeout                       | kalau IPv6 kamu justru lancar, jalankan dengan `--no-ipv4`   |
| `mpv tidak ditemukan` / `yt-dlp tidak ditemukan` | install sesuai tabel di atas, lalu buka terminal baru      |
| `mpv berhenti saat start`                      | update mpv                                                   |
| huruf/emoji berantakan di Windows              | pakai Windows Terminal; `ytradio.bat` sudah set UTF-8 (`chcp 65001`) |

Beberapa jendela YT Radio bisa jalan bersamaan tanpa saling mengganggu; file di folder home
dipakai bersama, jadi "lanjut lagu terakhir" mengikuti jendela yang terakhir menyimpan.

## Cara kerja

- Lagu awal dicari lewat [ytmusicapi](https://github.com/sigma67/ytmusicapi), lalu antrian diisi dari
  radio YouTube Music lagu itu. Kalau sisa antrian tinggal 5 lagu, rekomendasi baru diambil di
  thread terpisah dari lagu terakhir di antrian (cadangan: lagu yang sedang main, lalu lagu acak dari
  antrian). Kalau buntu atau gagal, dicoba lagi dengan jeda 15 dtk, berlipat sampai maks. 5 menit.
- Lagu yang sama tidak diputar ulang, termasuk versi lain dengan videoId berbeda (MV, lyric video,
  remaster).
- mpv diputar tanpa jendela dan dikendalikan lewat JSON IPC: satu koneksi untuk perintah, satu untuk
  event (status diperbarui begitu ada perubahan). Playlist mpv hanya menyimpan 20 lagu yang sudah
  lewat supaya tetap ringan saat jalan berjam-jam.
- Volume antar lagu disamakan dengan filter `dynaudnorm` (tombol `a`).

## Test

```
python -m unittest -v
```
