#!/usr/bin/env bash
# Builds Airlog for Linux or macOS, for the machine's own CPU (x86_64 or arm64).
#
#   ./build_unix.sh                 full build: downloads ffmpeg, builds, self-tests, packages
#   FFMPEG_DIR=/path ./build_unix.sh   use ffmpeg/ffprobe(/ffplay) you already have (static builds)
#   ./build_unix.sh --no-deb        Linux: skip the .deb
#
# Linux  -> dist/Airlog/Airlog, dist/Airlog-<ver>-linux-<arch>.tar.gz, dist/airlog_<ver>_<debarch>.deb
# macOS  -> dist/Airlog.app,    dist/Airlog-<ver>-macos-<arch>.dmg
#
# Needs Python 3.10+ with tkinter (Linux: apt install python3-venv python3-tk binutils;
# macOS: python.org Python or brew install python-tk), curl and unzip/xz.
set -euo pipefail
cd "$(dirname "$0")"

MAKE_DEB=1
for a in "$@"; do
  case "$a" in
    --no-deb) MAKE_DEB=0 ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done

step() { printf '\n\033[33m==> %s\033[0m\n' "$*"; }

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' airlog/__init__.py)
OS=$(uname -s)
case "$(uname -m)" in
  x86_64|amd64)  ARCH=x86_64; MR_ARCH=amd64; BTBN=linux64;      DEB_ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64;  MR_ARCH=arm64; BTBN=linuxarm64;   DEB_ARCH=arm64 ;;
  *) echo "Unsupported CPU $(uname -m)"; exit 1 ;;
esac
case "$OS" in
  Linux)  PLAT=linux ;;
  Darwin) PLAT=macos ;;
  *) echo "Unsupported OS $OS (use build_windows.ps1 on Windows)"; exit 1 ;;
esac
echo "Airlog $VERSION for $PLAT-$ARCH"

# ---------------------------------------------------------------- Python + PyInstaller
step "Python build environment"
PY=""
for cand in "${PYTHON:-}" python3.12 python3.13 python3.11 python3.10 python3; do
  [[ -n "$cand" ]] && command -v "$cand" >/dev/null || continue
  if "$cand" -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then PY=$cand; break; fi
done
[[ -n "$PY" ]] || { echo "Need Python 3.10+ with tkinter (Linux: apt install python3-tk python3-venv; macOS: brew install python-tk)"; exit 1; }
"$PY" --version
VENV=.build/venv-$PLAT-$ARCH
[[ -x "$VENV/bin/python" ]] || "$PY" -m venv "$VENV"
VPY="$VENV/bin/python"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet "pyinstaller>=6.6" "flask>=2.2" waitress shazamio

# ---------------------------------------------------------------- ffmpeg
step "ffmpeg"
FFDIR=.build/ffmpeg-$PLAT-$ARCH
FF_SOURCE=""
if [[ -n "${FFMPEG_DIR:-}" ]]; then
  rm -rf "$FFDIR"; mkdir -p "$FFDIR"
  for t in ffmpeg ffprobe ffplay; do [[ -x "$FFMPEG_DIR/$t" ]] && cp "$FFMPEG_DIR/$t" "$FFDIR/"; done
  FF_SOURCE="$FFMPEG_DIR"
elif [[ ! -x "$FFDIR/ffmpeg" ]]; then
  rm -rf "$FFDIR" .build/ff_dl; mkdir -p "$FFDIR" .build/ff_dl
  MR="https://ffmpeg.martin-riedl.de/redirect/latest/$PLAT/$MR_ARCH/release"
  fetch_mr() {   # Martin Riedl's static builds: one zip per tool
    for t in ffmpeg ffprobe ffplay; do
      if curl -fsSL -o ".build/ff_dl/$t.zip" "$MR/$t.zip"; then
        unzip -oq ".build/ff_dl/$t.zip" -d .build/ff_dl/x && mv ".build/ff_dl/x/$t" "$FFDIR/"
      elif [[ $t != ffplay ]]; then return 1; fi
    done
  }
  if [[ $PLAT == linux ]]; then
    URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-$BTBN-gpl.tar.xz"
    echo "Downloading $URL"
    if curl -fsSL -o .build/ff_dl/ff.tar.xz "$URL" && tar -xJf .build/ff_dl/ff.tar.xz -C .build/ff_dl; then
      cp .build/ff_dl/*/bin/ffmpeg .build/ff_dl/*/bin/ffprobe "$FFDIR/"
      cp .build/ff_dl/*/bin/ffplay "$FFDIR/" 2>/dev/null || true
      cp .build/ff_dl/*/LICENSE.txt "$FFDIR/LICENSE.txt" 2>/dev/null || true
      FF_SOURCE="$URL"
    else
      echo "  failed; trying $MR"; fetch_mr; FF_SOURCE="$MR"
    fi
  else
    echo "Downloading $MR/{ffmpeg,ffprobe,ffplay}.zip"
    fetch_mr || { echo "ffmpeg download failed. Set FFMPEG_DIR to a folder with static ffmpeg + ffprobe."; exit 1; }
    FF_SOURCE="$MR"
  fi
  echo "$FF_SOURCE" > "$FFDIR/SOURCE"
  rm -rf .build/ff_dl
fi
[[ -n "$FF_SOURCE" ]] || FF_SOURCE=$(cat "$FFDIR/SOURCE" 2>/dev/null || echo "a static FFmpeg build")
chmod +x "$FFDIR"/ff*
for t in ffmpeg ffprobe; do [[ -x "$FFDIR/$t" ]] || { echo "$t missing from $FFDIR"; exit 1; }; done
[[ -x "$FFDIR/ffplay" ]] || echo "  (no ffplay: preview in the classic desktop window won't be available)"
[[ $PLAT == macos ]] && xattr -dr com.apple.quarantine "$FFDIR" 2>/dev/null || true
"$FFDIR/ffmpeg" -hide_banner -version | head -1

# Licence notice, bundled through the spec
LICDIR=.build/licenses
rm -rf "$LICDIR"; mkdir -p "$LICDIR"
[[ -f "$FFDIR/LICENSE.txt" ]] && cp "$FFDIR/LICENSE.txt" "$LICDIR/ffmpeg-LICENSE.txt"
cat > "$LICDIR/ffmpeg-NOTICE.txt" <<EOF
Airlog bundles FFmpeg (https://ffmpeg.org) from $FF_SOURCE
FFmpeg is licensed under the GNU GPL (this build). Source: https://ffmpeg.org/download.html
EOF

# ---------------------------------------------------------------- PyInstaller
step "PyInstaller"
rm -rf dist/Airlog dist/Airlog.app
AIRLOG_FFMPEG_DIR="$PWD/$FFDIR" AIRLOG_LICENSE_DIR="$PWD/$LICDIR" \
  "$VPY" -m PyInstaller --noconfirm --clean --distpath dist --workpath ".build/pyi-$PLAT" packaging/airlog.spec

if [[ $PLAT == macos ]]; then
  APP_EXE=dist/Airlog.app/Contents/MacOS/Airlog
else
  APP_EXE=dist/Airlog/Airlog
  cp README.md dist/Airlog/README.md
fi

# ---------------------------------------------------------------- self-test the build
step "Self-test"
REPORT="$PWD/dist/selftest-$PLAT-$ARCH.txt"
rm -f "$REPORT"
RUN=()
if [[ $PLAT == linux && -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  command -v xvfb-run >/dev/null || { echo "No display for the Tk check: install xvfb (apt install xvfb xauth)"; exit 1; }
  RUN=(xvfb-run -a)
fi
${RUN[@]+"${RUN[@]}"} "$APP_EXE" --selftest "$REPORT" || true
[[ -f "$REPORT" ]] || { echo "Self-test wrote no report: the app failed to start"; exit 1; }
cat "$REPORT"
grep -q "RESULT  OK" "$REPORT" || { echo "Self-test failed (see above)"; exit 1; }

# ---------------------------------------------------------------- packages
if [[ $PLAT == linux ]]; then
  step "Tarball"
  TGZ="dist/Airlog-$VERSION-linux-$ARCH.tar.gz"
  tar -C dist -czf "$TGZ" Airlog
  echo "  $TGZ"

  if [[ $MAKE_DEB == 1 ]]; then
    step ".deb"
    if ! command -v dpkg-deb >/dev/null; then
      echo "  dpkg-deb not found - skipping (apt install dpkg-dev)"
    else
      PKG=.build/deb/airlog_${VERSION}_$DEB_ARCH
      rm -rf "$PKG"
      mkdir -p "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/bin" "$PKG/usr/share/applications" \
               "$PKG/usr/share/icons/hicolor/256x256/apps" "$PKG/usr/share/doc/airlog"
      cp -a dist/Airlog "$PKG/opt/airlog"
      ln -s /opt/airlog/Airlog "$PKG/usr/bin/airlog"
      cp airlog/assets/icon.png "$PKG/usr/share/icons/hicolor/256x256/apps/airlog.png"
      cp README.md "$PKG/usr/share/doc/airlog/README.md"
      cat > "$PKG/usr/share/applications/airlog.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Airlog
Comment=Radio station logger: hourly archive and instant grabs
Exec=/usr/bin/airlog
Icon=airlog
Terminal=false
Categories=AudioVideo;Audio;Recorder;
EOF
      SIZE=$(du -sk "$PKG" | cut -f1)
      cat > "$PKG/DEBIAN/control" <<EOF
Package: airlog
Version: $VERSION
Architecture: $DEB_ARCH
Maintainer: Jonathan (M0JSX) <jonathan@m0jsx.radio>
Installed-Size: $SIZE
Depends: libc6 (>= 2.36)
Recommends: alsa-utils
Section: sound
Priority: optional
Homepage: https://github.com/M0JSX/airlog
Description: Radio station logger with hourly archive and instant grabs
 Records a radio station (internet stream or sound card) into clock-aligned
 hourly files and keeps a rolling buffer you can grab from seconds after
 something airs, through a browser UI. Runs as a systemd service.
EOF
      cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
UNIT=/etc/systemd/system/airlog.service
if [ "$1" = configure ]; then
  if [ -f "$UNIT" ]; then
    # Upgrade: the unit already points at /opt/airlog; pick up the new build.
    systemctl daemon-reload || true
    systemctl try-restart airlog.service || true
  elif [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    # Fresh install with sudo: run the recorder as the user who installed it.
    /opt/airlog/Airlog --install-service --user "$SUDO_USER" \
      || echo "Airlog: the service didn't start; open Airlog from the menu or run: sudo airlog --install-service"
  else
    echo "Airlog installed. Open it from the menu (it offers to install the recorder service),"
    echo "or run:  sudo airlog --install-service --user <name>"
  fi
fi
exit 0
EOF
      cat > "$PKG/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] && [ -f /etc/systemd/system/airlog.service ]; then
  /opt/airlog/Airlog --uninstall-service || {
    systemctl disable --now airlog.service || true
    rm -f /etc/systemd/system/airlog.service
    systemctl daemon-reload || true
  }
fi
exit 0
EOF
      cat > "$PKG/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
# Recordings are never removed; purge only drops the service settings.
[ "$1" = purge ] && rm -rf /var/lib/airlog
exit 0
EOF
      chmod 0755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm" "$PKG/DEBIAN/postrm"
      DEB="dist/airlog_${VERSION}_$DEB_ARCH.deb"
      dpkg-deb --root-owner-group -Zxz --build "$PKG" "$DEB" >/dev/null
      echo "  $DEB"
    fi
  fi
else
  step "Disk image"
  DMG="dist/Airlog-$VERSION-macos-$ARCH.dmg"
  STAGE=.build/dmg
  rm -rf "$STAGE" "$DMG"; mkdir -p "$STAGE"
  ditto dist/Airlog.app "$STAGE/Airlog.app"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -quiet -volname "Airlog $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
  echo "  $DMG"
fi

step "Done"
ls -lh dist | grep -E 'Airlog-|airlog_|selftest' || true
