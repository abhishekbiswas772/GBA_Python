#!/usr/bin/env bash
# Package a `flet build linux` output folder as a .deb.
#   packaging/linux/build-deb.sh <bundle dir> <version> <out dir>
# Installs to /opt/retropy, with /usr/bin/retropy, a desktop entry and an icon.
set -euo pipefail

bundle="$1"
version="$2"
out="$3"
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"

# the app binary: the executable at the top of the bundle
exe="$(find "$bundle" -maxdepth 1 -type f -perm -u+x | head -n 1)"
if [ -z "$exe" ]; then
  echo "no executable found in $bundle" >&2
  exit 1
fi
exe_name="$(basename "$exe")"

# Debian versions can't contain '-' unless there is a revision: 1.2.3-rc1 -> 1.2.3~rc1
deb_version="${version//-/\~}"
pkg="$(mktemp -d)"
trap 'rm -rf "$pkg"' EXIT

mkdir -p "$pkg/opt/retropy" "$pkg/usr/bin" "$pkg/usr/share/applications" \
         "$pkg/usr/share/icons/hicolor/256x256/apps" "$pkg/usr/share/doc/retropy" "$pkg/DEBIAN"
cp -a "$bundle/." "$pkg/opt/retropy/"
ln -s "/opt/retropy/$exe_name" "$pkg/usr/bin/retropy"
cp "$root/assets/icon.png" "$pkg/usr/share/icons/hicolor/256x256/apps/retropy.png"
cp "$root/LICENSE" "$pkg/usr/share/doc/retropy/copyright"

cat > "$pkg/usr/share/applications/retropy.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=RetroPy
GenericName=Retro console emulator
Comment=Game Boy, Game Boy Color, GBA, NES and (experimental) PlayStation
Exec=/opt/retropy/$exe_name %f
Icon=retropy
Terminal=false
Categories=Game;Emulator;
Keywords=gameboy;gba;nes;playstation;emulator;
EOF

size_kb="$(du -sk "$pkg/opt" | cut -f1)"
cat > "$pkg/DEBIAN/control" <<EOF
Package: retropy
Version: $deb_version
Section: games
Priority: optional
Architecture: amd64
Installed-Size: $size_kb
Depends: libgtk-3-0 | libgtk-3-0t64, libgstreamer1.0-0, libgstreamer-plugins-base1.0-0
Maintainer: Abhishek Biswas <noreply@github.com>
Homepage: https://github.com/abhishekbiswas772/RetroPy
Description: Retro console emulators written from scratch
 Game Boy, Game Boy Color, Game Boy Advance and NES cores in pure Python,
 an experimental PlayStation core in Rust, and a Flet desktop app with
 save states, rewind and MP4 recording.
EOF

mkdir -p "$out"
deb="$out/retropy_${deb_version}_amd64.deb"
fakeroot dpkg-deb --build --root-owner-group "$pkg" "$deb" >/dev/null 2>&1 \
  || dpkg-deb --build --root-owner-group "$pkg" "$deb"
echo "$deb"
