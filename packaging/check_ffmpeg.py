"""Check the FFmpeg a build bundles: LGPL (not GPL or nonfree), and has everything Airlog uses.
Then write the licence notice that ships with the build.

    python packaging/check_ffmpeg.py <ffmpeg> <windows|linux|macos> <where it came from> <notice file>

Fails the build (exit 1) if anything is missing or the licence is wrong.
"""
import re
import subprocess
import sys
from pathlib import Path

NEED = {
    # outputs (Icecast/Shoutcast/SRT/UDP/RTP), archive and grabs
    "encoders": ["libmp3lame", "aac", "libopus", "libvorbis", "mp2", "flac", "pcm_s16le", "pcm_s16be", "s302m"],
    "muxers": ["mp3", "adts", "ogg", "mpegts", "rtp", "wav", "flac", "s16le"],
    # stream sources and outputs
    "protocols": ["srt", "https", "tls", "udp", "rtp", "tcp", "pipe"],
    # per-output processing (compressor, limiter, multiband) and the source pipeline
    "filters": ["acompressor", "alimiter", "acrossover", "amix", "aresample", "volume"],
}
DEVICES = {"windows": ["dshow"], "macos": ["avfoundation"], "linux": []}       # sound card capture
DEVICES_NICE = {"linux": ["alsa", "pulse"]}                                    # warned about, not required


def run(ff: str, *args: str) -> str:
    r = subprocess.run([ff, "-hide_banner", *args], capture_output=True, text=True, errors="replace")
    return r.stdout + r.stderr


def names(text: str) -> set[str]:
    """Names from an ffmpeg listing: "<flags> <name> <description>" lines (-encoders, -muxers, -filters,
    -devices) or one bare name per line (-protocols)."""
    out = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[A-Z.|]+", parts[0]):
            out.add(parts[1])
        elif len(parts) == 1 and not parts[0].endswith(":"):
            out.add(parts[0])
    return out


def main() -> int:
    ff, plat, source, notice = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])
    problems = []
    version = run(ff, "-version")
    first = version.splitlines()[0] if version else "?"
    conf = next((l for l in version.splitlines() if l.startswith("configuration:")), "")
    lic = run(ff, "-L")
    if "--enable-gpl" in conf or "--enable-nonfree" in conf:
        problems.append("this FFmpeg is a GPL/nonfree build; Airlog bundles LGPL builds only")
    if "Lesser General Public License" not in lic:
        problems.append("ffmpeg -L doesn't report the LGPL")
    v3 = "--enable-version3" in conf
    for what, need in NEED.items():
        have = names(run(ff, f"-{what}"))
        missing = [n for n in need if n not in have]
        if missing:
            problems.append(f"missing {what}: {', '.join(missing)}")
    devs = names(run(ff, "-devices"))
    missing = [d for d in DEVICES[plat] if d not in devs]
    if missing:
        problems.append(f"missing input devices: {', '.join(missing)} (sound card capture)")
    for d in DEVICES_NICE.get(plat, []):
        if d not in devs:
            how = "Airlog reads ALSA cards through PortAudio instead" if d == "alsa" else f"capture via {d} won't work"
            print(f"  note: no '{d}' input device in this build ({how})")
    if problems:
        print("FFmpeg check failed for " + ff + ":\n  " + "\n  ".join(problems))
        return 1

    # Exact source: a release tarball, or the git commit in a snapshot version like N-12345-gabcdef0-20261105.
    m = re.search(r"version (n?\d+\.\d+(?:\.\d+)?)\b", first)
    g = re.search(r"-g([0-9a-f]{7,40})\b", first)
    if m and not g:
        src = f"https://ffmpeg.org/releases/ffmpeg-{m.group(1).lstrip('n')}.tar.xz"
    elif g:
        src = f"https://github.com/FFmpeg/FFmpeg/tree/{g.group(1)}"
    else:
        src = "https://ffmpeg.org/download.html"
    notice.parent.mkdir(parents=True, exist_ok=True)
    notice.write_text(
        f"Airlog bundles FFmpeg (https://ffmpeg.org), {first}\n"
        f"Built from: {source}\n\n"
        f"This FFmpeg is licensed under the GNU Lesser General Public License, version {'3' if v3 else '2.1'} "
        f"or later (see ffmpeg-LICENSE.txt). Airlog runs it as a separate program.\n"
        f"Source code for this exact version: {src}\n"
        f"Its configuration:\n  {conf.removeprefix('configuration:').strip()}\n", encoding="utf-8")
    print(f"  FFmpeg OK: LGPL v{'3' if v3 else '2.1'}+, everything Airlog uses is there ({first})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
