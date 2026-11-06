#!/usr/bin/env bash
# Build a static, LGPL FFmpeg (plus ffprobe and ffplay) for macOS from source.
#
#   packaging/build_ffmpeg_macos.sh <output dir>
#
# Prebuilt macOS FFmpeg downloads are GPL builds (they include x264/x265); Airlog needs none of the
# GPL parts, so this builds just what it uses, with LGPL or permissive libraries only:
#   LAME (LGPL), libogg/libvorbis/opus (BSD), SRT (MPL-2.0), OpenSSL 3 (Apache-2.0), SDL2 (zlib, for ffplay).
# OpenSSL 3's Apache licence needs LGPL v3, hence --enable-version3: the result is LGPL v3 or later.
# Takes ~10 minutes on an Apple Silicon CI runner; build_unix.sh caches the result.
set -euo pipefail

OUT=${1:?usage: build_ffmpeg_macos.sh <output dir>}
FFMPEG_V=7.1.1
LAME_V=3.100
OGG_V=1.3.5
VORBIS_V=1.3.7
OPUS_V=1.5.2
OPENSSL_V=3.0.16
SRT_V=1.5.4
SDL_V=2.30.11

ROOT=$(pwd)/.build/ffbuild
PREFIX=$ROOT/prefix
SRC=$ROOT/src
mkdir -p "$PREFIX" "$SRC" "$OUT"
OUT=$(cd "$OUT" && pwd)          # absolute: the build cds into the source folders
export MACOSX_DEPLOYMENT_TARGET=11.0
export PKG_CONFIG_PATH=$PREFIX/lib/pkgconfig PKG_CONFIG_LIBDIR=$PREFIX/lib/pkgconfig
export CFLAGS="-O2 -I$PREFIX/include" LDFLAGS="-L$PREFIX/lib"
JOBS=$(sysctl -n hw.ncpu)
command -v pkg-config >/dev/null || brew install pkgconf
command -v cmake >/dev/null || brew install cmake

fetch() {   # dir name, url [mirror url...]: download (retrying, then mirrors) and unpack into $SRC/<dir>
  local dir=$1; shift
  [[ -d "$SRC/$dir" ]] && return
  for url in "$@"; do
    echo "  $url"
    if curl -fsSL --retry 4 --retry-all-errors --retry-delay 5 -o "$SRC/dl" "$url"; then
      mkdir -p "$SRC/$dir"
      tar -xf "$SRC/dl" -C "$SRC/$dir" --strip-components 1
      rm -f "$SRC/dl"
      return
    fi
  done
  echo "Couldn't download $dir"; exit 1
}

echo "Fetching sources"
fetch ffmpeg "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_V.tar.xz" \
      "https://github.com/FFmpeg/FFmpeg/archive/refs/tags/n$FFMPEG_V.tar.gz"
fetch lame "https://downloads.sourceforge.net/project/lame/lame/$LAME_V/lame-$LAME_V.tar.gz"
fetch ogg "https://downloads.xiph.org/releases/ogg/libogg-$OGG_V.tar.xz" \
      "https://github.com/xiph/ogg/releases/download/v$OGG_V/libogg-$OGG_V.tar.xz"
fetch vorbis "https://downloads.xiph.org/releases/vorbis/libvorbis-$VORBIS_V.tar.xz"
fetch opus "https://downloads.xiph.org/releases/opus/opus-$OPUS_V.tar.gz" \
      "https://github.com/xiph/opus/releases/download/v$OPUS_V/opus-$OPUS_V.tar.gz"
fetch openssl "https://github.com/openssl/openssl/releases/download/openssl-$OPENSSL_V/openssl-$OPENSSL_V.tar.gz"
fetch srt "https://github.com/Haivision/srt/archive/refs/tags/v$SRT_V.tar.gz"
fetch sdl "https://github.com/libsdl-org/SDL/releases/download/release-$SDL_V/SDL2-$SDL_V.tar.gz"

autobuild() {   # dir, extra configure args...
  local d=$1; shift
  echo "Building $d"
  (cd "$SRC/$d" && ./configure --prefix="$PREFIX" --disable-shared --enable-static "$@" >/dev/null \
     && make -j"$JOBS" >/dev/null && make install >/dev/null)
}

# LAME 3.100 lists a symbol that no longer exists; the macOS linker refuses it.
sed -i '' '/lame_init_old/d' "$SRC/lame/include/libmp3lame.sym"
autobuild lame --disable-frontend --disable-decoder
autobuild ogg
# libvorbis 1.3.7's configure adds -force_cpusubtype_ALL, which arm64 clang rejects.
sed -i '' 's/-force_cpusubtype_ALL//g' "$SRC/vorbis/configure"
autobuild vorbis --with-ogg="$PREFIX" --disable-examples --disable-docs
autobuild opus --disable-doc --disable-extra-programs

echo "Building openssl"
(cd "$SRC/openssl" && ./Configure darwin64-arm64-cc no-shared no-tests --prefix="$PREFIX" --libdir=lib >/dev/null \
   && make -j"$JOBS" >/dev/null && make install_sw >/dev/null)   # install_sw: no docs

echo "Building srt"
(cd "$SRC/srt" && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PREFIX" \
   -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DENABLE_SHARED=OFF -DENABLE_STATIC=ON -DENABLE_APPS=OFF \
   -DUSE_ENCLIB=openssl -DOPENSSL_ROOT_DIR="$PREFIX" -DOPENSSL_USE_STATIC_LIBS=ON >/dev/null \
   && cmake --build build -j"$JOBS" >/dev/null && cmake --install build >/dev/null)

echo "Building SDL2"
(cd "$SRC/sdl" && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PREFIX" \
   -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DSDL_SHARED=OFF -DSDL_STATIC=ON -DSDL_TEST=OFF >/dev/null \
   && cmake --build build -j"$JOBS" >/dev/null && cmake --install build >/dev/null)

echo "Building ffmpeg $FFMPEG_V (LGPL)"
cd "$SRC/ffmpeg"
./configure --prefix="$PREFIX" --pkg-config-flags=--static \
  --extra-cflags="-I$PREFIX/include" --extra-ldflags="-L$PREFIX/lib" --extra-libs="-lc++" \
  --disable-shared --enable-static --enable-version3 \
  --disable-doc --disable-debug --disable-xlib --disable-libxcb --disable-sndio --disable-lzma \
  --enable-libmp3lame --enable-libopus --enable-libvorbis --enable-libsrt --enable-openssl --enable-sdl2 \
  > "$ROOT/configure.log" || { tail -40 ffbuild/config.log; exit 1; }
grep -q "^License: LGPL version 3 or later" "$ROOT/configure.log" \
  || { echo "ffmpeg didn't configure as LGPL v3:"; grep -i license "$ROOT/configure.log"; exit 1; }
make -j"$JOBS" >/dev/null
cp ffmpeg ffprobe ffplay "$OUT/"
cp COPYING.LGPLv3 "$OUT/LICENSE.txt"
echo "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_V.tar.xz (built by packaging/build_ffmpeg_macos.sh)" > "$OUT/SOURCE"
for t in ffmpeg ffprobe ffplay; do
  if otool -L "$OUT/$t" | tail -n +2 | grep -v -E "^\s+(/usr/lib/|/System/Library/)"; then
    echo "$t links to a library outside macOS: it wouldn't run on another Mac"; exit 1
  fi
done
"$OUT/ffmpeg" -hide_banner -version | head -1
