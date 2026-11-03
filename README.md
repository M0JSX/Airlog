# Airlog

A radio station logger. It records the station continuously into clock-aligned
files (one per hour by default) **and** keeps a rolling buffer you can grab from
seconds after something airs, so you can cut a clip for use later in the same
hour instead of waiting for the hourly file to close.

The recorder runs as a background **service** on Windows, Linux or macOS. Anyone
on the network opens `http://<recorder>:8080/`, selects audio on the live
waveform and downloads it to their own machine to edit. Opening the **Airlog**
app on the recorder itself shows the same UI in its own window.

| | Download (from the GitHub release / Actions run) | The service |
|---|---|---|
| **Windows** 10/11 x64 | `Airlog-Setup-<ver>.exe` or the portable zip | Windows service, from boot |
| **Linux** x86_64 / arm64 | `airlog_<ver>_<arch>.deb` or `Airlog-<ver>-linux-<arch>.tar.gz` | systemd unit, from boot |
| **macOS** 11+ Apple Silicon | `Airlog-<ver>-macos-arm64.dmg` | launchd agent, while you're logged in |

Every build bundles ffmpeg, so the machine needs nothing else. There's also a
classic all-in-one desktop window (`--classic`) for quick use on one machine.

## Install

### Windows

Run `Airlog-Setup-<ver>.exe` (or unzip the portable build and run `Airlog.exe`,
which offers to install the service).

#### Windows service

The recorder runs as the **Airlog Recorder** service, so it records from boot
whether or not anyone has the UI open or is signed in, and Windows restarts it if
it crashes.

- **`Airlog.exe`** opens the UI in an Edge app window. If the service isn't
  installed it offers to install it; if it's stopped it starts it. Closing the
  window doesn't affect recording. Other PCs on the network can use
  `http://<this-pc>:8080/`.
- **`Airlog.exe --install-service`** (asks for admin) copies the app to
  `C:\Program Files\Airlog`, registers and starts the service, allows the web
  port through the firewall on private/domain networks and adds Start-menu and
  desktop shortcuts. On first install it copies the desktop app's settings.
  Run it again from a new build to upgrade.
- **`Airlog.exe --uninstall-service`** stops and removes the service, the firewall
  rule and the shortcuts. Settings and recordings are kept.
- **`Airlog.exe --classic`** runs the old all-in-one desktop window. Don't record
  with it while the service is running.

The service runs as LocalSystem, so its settings are machine-wide in
`%ProgramData%\Airlog\config.json` (edit them in the UI's Settings), with its log
in `%ProgramData%\Airlog\logs\service.log`. Recordings stay where the settings
point; by default that's `%USERPROFILE%\Airlog` of whoever installed it. Set
**Settings → Password** if the network isn't trusted.

### Linux

Built on Debian 12, so it runs on Debian 12+, Raspberry Pi OS 12+ (64-bit),
Ubuntu 23.04+ and anything of similar age or newer.

**Debian, Ubuntu, Raspberry Pi OS** (the service is set up for the user running `sudo`):

```bash
sudo apt install ./airlog_1.3.0_amd64.deb      # or _arm64.deb
```

**Any distro** (tarball):

```bash
tar -xzf Airlog-1.3.0-linux-x86_64.tar.gz
sudo ./Airlog/Airlog --install-service          # copies to /opt/airlog, adds the airlog command and menu entry
```

Either way you get the `airlog.service` systemd unit. It records from boot,
whether or not anyone is logged in, and restarts if it stops. It runs as your
user (in the `audio` group), so recordings land in `~/Airlog` by default.
Opening **Airlog** from the menu (or running `airlog`) shows the UI; if the
service isn't installed yet it offers to install it.

```bash
journalctl -u airlog -f                 # logs
sudo systemctl restart airlog           # restart
sudo airlog --uninstall-service         # remove the service (settings and recordings are kept)
sudo apt remove airlog                  # .deb: removes the service and the app
```

- Settings: `/var/lib/airlog/config.json` (edit them in the UI's Settings).
- The installer opens the web port in ufw or firewalld if either is active.
- Machines without a real-time clock (e.g. a Raspberry Pi) rely on NTP; the
  service waits for time sync at boot so file names and grab times are right
  after a power cut.
- Use `--user NAME` with `--install-service` to run the recorder as another account.

### macOS

Open the `.dmg`, drag **Airlog** to Applications and open it. It offers to
install the recorder as a login agent, which starts when you log in and is
restarted if it stops. No admin password is needed.

- The app isn't signed by Apple, so the first time macOS blocks it: open
  **System Settings → Privacy & Security** and click **Open Anyway**, or run
  `xattr -dr com.apple.quarantine /Applications/Airlog.app`.
- The agent runs in your login session because that's where macOS allows
  microphone access. The first sound-card capture asks for permission; if you
  missed it, enable Airlog under **Privacy & Security → Microphone**.
- For unattended recording from boot, turn on automatic login (**Users &
  Groups**) and stop the Mac sleeping (**Energy** / **Battery** settings, or
  `sudo pmset -a sleep 0`).
- Settings: `~/Library/Application Support/Airlog/service.json`. Log:
  `~/Library/Logs/Airlog/service.log`.
- Remove it with `/Applications/Airlog.app/Contents/MacOS/Airlog --uninstall-service`
  (settings and recordings are kept), then drag the app to the Bin.

### Sound card input

In **Settings** choose **Sound card input**, press **Refresh** and pick the device.

- **Linux:** driver **alsa** (the service can't reach a desktop PulseAudio/PipeWire
  session). Pick a `plughw:X,Y` entry: `plughw` lets ALSA convert to 48 kHz
  stereo if the interface runs at another rate. Set the record level with
  `alsamixer`.
- **macOS:** driver **avfoundation**.
- **Windows:** driver **dshow**.

### Storage

The grab buffer is raw PCM, about **690 MB per hour** at 48 kHz stereo, written
continuously. On a machine booting from an SD card (e.g. a Raspberry Pi), point
**Settings → Buffer folder** and the archive/grabs folders at a USB SSD so the
card doesn't wear out.

## Using the browser UI

- **Waveform:** shows the last 1 minute to 2 hours, following live.
  - Drag to select, drag an edge to adjust, Shift-click to extend.
  - Double-click selects the whole hour.
  - Wheel scrolls; Ctrl+wheel (or trackpad pinch) zooms.
  - **Zoom to selection** helps with fine edits.
- **Mark In (F2)** when an item starts and **Mark Out (F3)** when it ends.
  - **Download (F4)** fetches the selection.
  - **In → now (F5)** takes everything from the in-point up to live.
- **Nudge** buttons move In or Out by ±0.1 s or ±1 s. You can also type exact
  times (`HH:MM:SS.s`, or `MM:SS` in the current hour).
- **Preview (P)** plays the selection in the browser, with a playhead on the
  waveform.
- **Quick grab** buttons download the last 30 s, 1, 2, 5, 10 or 15 min
  immediately.
- **Download** goes straight to your machine as WAV, MP3 or FLAC, ready to open
  in Audition. **Save on recorder** stores the clip in the recorder's grabs folder
  instead, listed under *Saved grabs*.
- **Hourly archive** lists the finished hourly files to play or download.
- **Settings** edits everything (source, formats, folders, buffer, password).
  Saving restarts the recorder, and the gap of a second or two is filled with
  silence.

Several people can use the UI at once; each browser has its own selection.

**Access:** set **Settings → Web password** to require a login (HTTP Basic).
Leave it blank only on a trusted studio LAN. For access from outside the
building, put it behind a VPN (e.g. WireGuard) or nginx with HTTPS. Don't
port-forward it.

## What the recorder does

- **Source:** an internet stream (Icecast/Shoutcast/HLS/.pls/.m3u), a sound card
  input, or any custom ffmpeg input (SRT, RTP, a test tone…).
- **Hourly archive:** MP3/FLAC/WAV/OGG files split exactly on the clock (60, 30
  or 15 minutes). The split is sample-accurate, because Airlog cuts the PCM
  itself.
- **Source delay:** streams arrive several seconds behind air. Set
  **Source delay** and timestamps and file splits shift back, so the pips land
  at the top of the file.
- **Resilient:** it reconnects automatically after a stream drops and fills the
  gap with silence, keeping times aligned. A watchdog restarts a stalled source,
  and there's a silence alert.
- **Restarts:** the grab buffer survives a service restart and a reboot.
- **Song identification:** every 30 s (configurable) Airlog sends a 12-second
  sample of the audio to Shazam. The current song, with its cover art, appears at
  the top of the page. Each song is labelled on the timeline (click a label to
  select the song), and the **Songs** tab lists what played. It works for any
  source, including a sound card. See below.

### Song identification

This uses [shazamio](https://github.com/shazamio/ShazamIO), an *unofficial*
client for Shazam's private app API. It needs no account or key. Shazam doesn't
support it, so it can stop working without notice, and heavy use may be
rate-limited. When Shazam can't be reached, the page says so and Airlog retries
with a back-off. Recording is never affected.

- Shazam reports how far into the song the sample was, so each song's start time
  is estimated from that. Treat the times as a guide, accurate to a second or
  two, and check the edges before you download a song.
- Speech, jingles and ads don't match. After two checks in a row with no match,
  the current song is closed.
- Only the 12-second samples are sent, and only while recording. Turn it off
  under **Settings → Song identification**.
- The song list is kept in `tracks.json` in the grab buffer folder, holding the
  last 1,000 songs.
- The Windows, Linux and macOS builds include it. From source, run
  `pip install shazamio`.

## Desktop app (single machine)

```bash
# Windows
winget install Python.Python.3.12 ; winget install Gyan.FFmpeg ; py -m airlog
# macOS
brew install python-tk ffmpeg && python3 -m airlog
# Linux
sudo apt install python3-tk ffmpeg && python3 -m airlog
```

It has the same features, plus *Open in editor after grab* (set the editor path
in Settings → Grabs). On Windows, double-clicking `run_airlog.pyw` launches it
without a console. The packaged builds open it with `--classic`; don't record
with it while the service is running.

## Building

### Windows

On any Windows 10/11 PC with Python 3.10+ (`winget install Python.Python.3.12`),
double-click **`build_windows.bat`** (or run `.\build_windows.ps1`). It:

1. creates a private build environment and installs PyInstaller;
2. downloads ffmpeg (gyan.dev "essentials", falling back to BtbN) and bundles
   `ffmpeg.exe`, `ffprobe.exe` and `ffplay.exe`, so the PC needs nothing else;
3. builds `dist\Airlog\Airlog.exe` with the Airlog icon and version info;
4. **self-tests the exe** (`Airlog.exe --selftest`): Tk, the bundled ffmpeg,
   and a real 4-second capture, archive file and grab;
5. writes `dist\Airlog-<version>-win64.zip` (portable, unzip and run);
6. if Inno Setup is installed (`winget install JRSoftware.InnoSetup`), also
   writes `dist\Airlog-Setup-<version>.exe`, an admin installer that puts Airlog
   in Program Files and installs the service.

The exe isn't code-signed, so Windows SmartScreen will warn on first run
(*More info → Run anyway*).

### Linux and macOS

On the machine type you're building for (the build is for its own CPU), run
**`./build_unix.sh`**. It needs Python 3.10+ with tkinter, `curl` and `unzip`
(Linux also `xz-utils`, `binutils`, `xvfb` for a headless self-test, and
`dpkg-dev` for the .deb). It:

1. creates a private build environment and installs PyInstaller;
2. downloads a static ffmpeg (Linux: BtbN, falling back to Martin Riedl's
   builds; macOS: Martin Riedl's builds) and bundles it, or uses
   `FFMPEG_DIR=/path/to/static/ffmpeg ./build_unix.sh`;
3. builds `dist/Airlog/Airlog` (Linux) or `dist/Airlog.app` (macOS);
4. **self-tests the build** the same way as the Windows exe;
5. writes `dist/Airlog-<version>-linux-<arch>.tar.gz` and
   `dist/airlog_<version>_<arch>.deb` (skip it with `--no-deb`), or
   `dist/Airlog-<version>-macos-<arch>.dmg`.

A Linux build runs on distros with the same or a newer glibc than the build
machine, which is why CI builds on Debian 12.

### GitHub Actions

`.github/workflows/build.yml` builds Windows x64, Linux x86_64 and arm64, and
macOS Apple Silicon on every push and pull request. Download the packages from
the run's *Artifacts*. Pushing a tag like `v1.3.0` attaches them all to a
release.

## Running from source

The recorder and web UI run anywhere Python 3.10+ and ffmpeg do:

```bash
pip install flask waitress            # plus shazamio for song identification
python3 -m airlog.server --config ./config.json --port 8080
```

Note that `--config` creates the file if it doesn't exist. On Linux and macOS,
`python3 -m airlog --install-service` installs the service from a source
checkout too (it runs `python3 -m airlog --service` from that folder, so leave
the checkout where it is).

## Where things go

| | Default |
|---|---|
| Hourly files | `~/Airlog/Archive/YYYY-MM-DD/Station_YYYY-MM-DD_HH-00.mp3` |
| Grabs saved on recorder | `~/Airlog/Grabs/` |
| Grab buffer | `~/Airlog/.spool` |
| Settings (service) | Windows: `%ProgramData%\Airlog\config.json` · Linux: `/var/lib/airlog/config.json` · macOS: `~/Library/Application Support/Airlog/service.json` |
| Settings (classic window) | Windows: `%APPDATA%\Airlog\` · Linux: `~/.config/airlog/` · macOS: `~/Library/Application Support/Airlog/config.json` |

File names use strftime codes plus `{station}`; a `/` creates sub-folders. The
time in an archive name is that file's start, so the first file after a start is
a partial (e.g. `12-17`) and every file after that starts on the boundary.

## HTTP API

The browser UI uses these; they're handy for scripting too.

| | |
|---|---|
| `GET /api/status` | state, live time, levels, current file |
| `GET /api/peaks?from=&to=&n=` | waveform buckets (0–255) |
| `GET /api/clip?from=&to=&fmt=wav\|mp3\|flac[&download=1]` | stream a clip (epoch seconds) |
| `POST /api/grab {from,to,fmt}` | save a clip on the recorder |
| `GET /api/grabs`, `DELETE /api/grabs/<path>` | saved grabs |
| `GET /api/archive[?date=YYYY-MM-DD]` | hourly files |
| `GET /api/tracks[?limit=200]` | identified songs, newest first (`now_playing` is also in `/api/status`) |
| `GET/POST /api/config` | settings |
| `POST /api/record {action: start\|stop}` | control |

For example, to get the last two minutes as an MP3 from a shell:

```bash
now=$(date +%s); curl -o clip.mp3 "http://recorder:8080/api/clip?from=$((now-120))&to=$now&fmt=mp3"
```

## Test

`python tests/test_engine.py` fakes the clock (inside the test only) to 8 s
before the hour, records a tone with a simulated dropout, and checks the split,
the gap fill and a grab. `python tests/test_nowplaying.py` checks the
song-change logic against canned Shazam answers (no network).
