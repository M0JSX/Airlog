# Airlog

A radio station logger. It records the station continuously into clock-aligned
files (one per hour by default) **and** keeps a rolling buffer you can grab from
seconds after something airs, so you can cut a clip for use later in the same
hour instead of waiting for the hourly file to close.

Ways to run it, all sharing the same recording engine:

- **Recorder + browser UI (recommended):** `airlog.server` runs headless on a
  Raspberry Pi 4 as a systemd service. Anyone on the network opens
  `http://<pi>:8080/`, selects audio on the live waveform and downloads it to
  their own machine to edit.
- **Windows service:** the same recorder as a Windows service, with `Airlog.exe`
  opening the browser UI in its own window. See [Windows service](#windows-service).
- **Desktop app:** `python -m airlog` runs recorder and UI together on one
  Windows/macOS/Linux machine.

## Raspberry Pi 4 setup

Pi OS Bookworm (64-bit, Lite is fine). Copy this folder to the Pi, then:

```bash
cd airlog
sudo ./deploy/install-pi.sh
```

This installs ffmpeg, Flask and waitress, copies the app to `/opt/airlog`, writes
`~/.config/airlog/config.json`, and starts the `airlog` service. Open the URL it
prints, go to **Settings**, and set the source.

Options: `sudo DATA_DIR=/mnt/ssd/Airlog PORT=8080 ./deploy/install-pi.sh`

```bash
journalctl -u airlog -f          # logs
sudo systemctl restart airlog    # restart
```

### Storage on the Pi

The grab buffer is raw PCM, about **690 MB per hour** at 48 kHz stereo, written
continuously. That much constant writing will wear out an SD card, so:

- On a Pi with 4 GB+ RAM the installer puts the buffer in RAM
  (`/dev/shm/airlog`, 60 minutes). It's lost on reboot, but the hourly files
  aren't.
- Better still, plug in a USB SSD and point **Settings → Buffer folder** and the
  archive/grabs folders (or `DATA_DIR`) at it, then raise the buffer length.

### Sound card on the Pi

The Pi has no line input, so use a USB audio interface. In Settings choose
**Sound card input**, driver **alsa**, press **Refresh**, and pick the
`plughw:X,Y` entry. `plughw` lets ALSA convert to 48 kHz stereo if the interface
runs at another rate. Set the record level with `alsamixer`. The installer adds
the service user to the `audio` group.

### Clock

The Pi has no real-time clock, so it relies on NTP. The service waits for time
sync at boot, so file names and grab times are right after a power cut. If the
browser's own clock disagrees with the recorder, the UI shows the recorder's
time and warns you.

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
  in Audition. **Save on recorder** stores the clip in the Pi's grabs folder
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
- **Restarts:** the grab buffer survives a service restart, but not a reboot if
  it's kept in RAM.

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
without a console.

### Building the Windows .exe

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

**GitHub:** push the project to a GitHub repo and
`.github/workflows/build-windows.yml` does the same on a Windows runner. Download
the zip and installer from the run's *Artifacts*. Pushing a tag like `v1.0.0`
attaches them to a release.

The exe isn't code-signed, so Windows SmartScreen will warn on first run
(*More info → Run anyway*).

### Windows service

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

## Running the server elsewhere

Any Linux, macOS or Windows box works:

```bash
pip install flask waitress
python3 -m airlog.server --config ./config.json --port 8080
```

Note that `--config` creates the file if it doesn't exist.

## Where things go

| | Default |
|---|---|
| Hourly files | `~/Airlog/Archive/YYYY-MM-DD/Station_YYYY-MM-DD_HH-00.mp3` |
| Grabs saved on recorder | `~/Airlog/Grabs/` |
| Grab buffer | `~/Airlog/.spool` (Pi installer: `/dev/shm/airlog`) |
| Settings | `~/.config/airlog/config.json` · `%APPDATA%\Airlog\` · `~/Library/Application Support/Airlog/` · Windows service: `%ProgramData%\Airlog\` |

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
| `GET/POST /api/config` | settings |
| `POST /api/record {action: start\|stop}` | control |

For example, to get the last two minutes as an MP3 from a shell:

```bash
now=$(date +%s); curl -o clip.mp3 "http://pi:8080/api/clip?from=$((now-120))&to=$now&fmt=mp3"
```

## Test

`python tests/test_engine.py` fakes the clock (inside the test only) to 8 s
before the hour, records a tone with a simulated dropout, and checks the split,
the gap fill and a grab.
