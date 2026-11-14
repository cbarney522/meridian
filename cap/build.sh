#!/bin/bash
# Build meridian_<version>.upgx with the Impinj ETK's cap_gen.sh.
#   cap/build.sh                      (ETK defaults to /opt/10.3.0_Octane_Embedded_Development_Tools)
#   ETK_DIR=/path/to/etk cap/build.sh
#   SIGN_KEY_DIR=/path/to/keys cap/build.sh   (optional: passes -k to cap_gen.sh)
#   SKIP_TESTS=1 cap/build.sh
#
# Assembles the CAP in cap/build/cap/ from:
#   cap/files/     start, input_manifest.json, app.env, rw_dir/README.md
#   app/           the Meridian server, core modules, role/menu definitions and web UI
#   python/        a bundled aarch64 CPython (the R700 FW 10.x OS has none)
set -euo pipefail
trap 'echo "build.sh FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
cd "$(dirname "$0")"
REPO="$(cd .. && pwd)"
ETK_DIR="${ETK_DIR:-/opt/10.3.0_Octane_Embedded_Development_Tools}"   # FW 10.x needs ETK >= 10.3.0
CAP_GEN="$ETK_DIR/cap_gen.sh"
[ -x "$CAP_GEN" ] || { echo "cap_gen.sh not found at $CAP_GEN (set ETK_DIR)"; exit 1; }

VERSION="$(sed -n 's/^[[:space:]]*Version[[:space:]]*=[[:space:]]*//p' cap_description.in | tr -d '[:space:]')"
OUT="meridian_${VERSION}.upgx"
STAGE=build/cap

# ---- checks before packaging
python3 -m py_compile "$REPO"/app/server.py "$REPO"/app/core/*.py
python3 -c "import json,sys; [json.load(open(p)) for p in sys.argv[1:]]" \
  "$REPO"/app/defs/roles.json "$REPO"/app/defs/menu.json files/input_manifest.json
bash -n files/start
if [ -z "${SKIP_TESTS:-}" ]; then
  (cd "$REPO" && python3 -m unittest discover -s tests -q)
fi
find "$REPO/app" -name __pycache__ -prune -exec rm -rf {} +

# ---- stage
rm -rf build && mkdir -p "$STAGE"
cp -a files/. "$STAGE/"
cp -a "$REPO/app" "$STAGE/app"
echo "$VERSION" > "$STAGE/app/VERSION"
chmod +x "$STAGE/start" "$STAGE/app/server.py"

# ---- bundled Python runtime (downloaded, verified, pruned once; cached between builds)
. ./python-runtime.conf
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/meridian"
mkdir -p "$CACHE"
TARBALL="$CACHE/$PY_ASSET"
if [ ! -f "$TARBALL" ]; then
  echo "Downloading Python runtime $PY_ASSET ..."
  curl -fSL --retry 3 -o "$TARBALL.part" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_RELEASE/${PY_ASSET//+/%2B}"
  mv "$TARBALL.part" "$TARBALL"
fi
echo "$PY_SHA256  $TARBALL" | sha256sum -c --quiet - || { echo "Python runtime checksum mismatch; delete $TARBALL and retry"; exit 1; }
PYDIR="$CACHE/${PY_ASSET%.tar.gz}"
if [ ! -x "$PYDIR/python/bin/python3" ]; then
  rm -rf "$PYDIR" && mkdir -p "$PYDIR"
  tar -xzf "$TARBALL" -C "$PYDIR"
  # Prune what a headless server never uses (~88 MB -> ~45 MB)
  L="$PYDIR/python/lib/python3.12"
  rm -rf "$PYDIR"/python/include "$PYDIR"/python/share "$PYDIR"/python/lib/pkgconfig \
         "$PYDIR"/python/lib/tcl* "$PYDIR"/python/lib/tk* "$PYDIR"/python/lib/itcl* "$PYDIR"/python/lib/thread* \
         "$PYDIR"/python/lib/libtcl* \
         "$L"/idlelib "$L"/tkinter "$L"/turtledemo "$L"/turtle.py "$L"/ensurepip "$L"/lib2to3 \
         "$L"/test "$L"/unittest/test "$L"/site-packages/pip* "$L"/config-3.12-* \
         "$L"/lib-dynload/_tkinter*
fi
cp -a "$PYDIR/python" "$STAGE/python"
python3 -c "import sys; h=open(sys.argv[1],'rb').read(20); sys.exit(0 if h[:4]==b'\x7fELF' and int.from_bytes(h[18:20],'little')==183 else 1)" \
  "$(readlink -f "$STAGE/python/bin/python3")" || { echo "bundled python is not aarch64"; exit 1; }
# Python needs libgcc_s.so.1. The reader very likely has it; as insurance, bundle the ETK
# toolchain's aarch64 copy next to libpython (found via Python's RPATH $ORIGIN/../lib).
GCC_S="$(find "$ETK_DIR" -name libgcc_s.so.1 -path '*aarch64*' 2>/dev/null | head -1 || true)"
if [ -n "$GCC_S" ]; then cp -L "$GCC_S" "$STAGE/python/lib/" && echo "Bundled libgcc_s from $GCC_S"
else echo "Note: no aarch64 libgcc_s.so.1 in the ETK; relying on the reader's copy"; fi
echo "Bundled Python: $(du -sh "$STAGE/python" | cut -f1)"

# ---- time-zone database (TZif files are architecture-independent), used if the reader has none
if [ -d /usr/share/zoneinfo/America ]; then
  mkdir -p "$STAGE/zoneinfo"
  cp -aL /usr/share/zoneinfo/. "$STAGE/zoneinfo/"
  rm -rf "$STAGE/zoneinfo/posix" "$STAGE/zoneinfo/right" "$STAGE/zoneinfo"/*.tab "$STAGE/zoneinfo"/leap* \
         "$STAGE/zoneinfo"/posixrules "$STAGE/zoneinfo"/tzdata.zi "$STAGE/zoneinfo"/SECURITY 2>/dev/null || true
  echo "Bundled time-zone database: $(du -sh "$STAGE/zoneinfo" | cut -f1)"
else
  echo "Note: no /usr/share/zoneinfo on this build machine (sudo apt install tzdata); only UTC if the reader lacks one"
fi

# ---- fallback TLS cert, used only if the reader has no openssl
mkdir -p "$STAGE/app/tls"
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
  -keyout "$STAGE/app/tls/key.pem" -out "$STAGE/app/tls/cert.pem" -subj "/CN=meridian" 2>/dev/null
chmod 600 "$STAGE/app/tls/key.pem"

echo "Staged CAP: $(du -sh "$STAGE" | cut -f1)"
ARGS=(-d cap_description.in -o "$OUT")
[ -n "${SIGN_KEY_DIR:-}" ] && ARGS+=(-k "$SIGN_KEY_DIR")
"$CAP_GEN" "${ARGS[@]}"
ls -l "$OUT"
echo "Upload cap/$OUT in the reader's web UI, reboot the reader, then browse to https://<reader>:8443/"
