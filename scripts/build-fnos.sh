#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_DIR="${MICAST_FNOS_STAGE_DIR:-${ROOT_DIR}/build/fnos/micast}"
OUTPUT_DIR="${MICAST_FNOS_OUTPUT_DIR:-${ROOT_DIR}/dist/fnos}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PLATFORM="${1:-${MICAST_FNOS_PLATFORM:-x86}}"

case "$PLATFORM" in
  x86|arm) ;;
  *)
    echo "不支持的 fnOS 平台：$PLATFORM（可选：x86、arm）" >&2
    exit 1
    ;;
esac

# The vendor directory ships cp312 wheels; manifest's install_dep_apps must
# promise the same Python or the package installs but fails to import.
CONFIGURED_PYTHON_VERSION="3.12"
CONFIGURED_ABI="cp312"
MANIFEST_PYTHON="$(
  sed -n 's/^install_dep_apps=python\([0-9]\)\.\{0,1\}\([0-9][0-9]*\).*/\1.\2/p' \
    "$ROOT_DIR/packaging/fnos/manifest" | head -n 1
)"
EXPECTED_ABI="cp${MANIFEST_PYTHON/./}"
if [ "$EXPECTED_ABI" != "$CONFIGURED_ABI" ]; then
  echo "manifest install_dep_apps 要求 Python ${MANIFEST_PYTHON:-?}，但打包脚本固定为" \
       "${CONFIGURED_PYTHON_VERSION}/${CONFIGURED_ABI}；请同步 scripts/build-fnos.* 的" \
       "--python-version/--abi 后再构建" >&2
  exit 1
fi

# Release tag、Python __version__ 与 manifest version 必须一致，否则 fnOS
# 应用市场显示的版本会与实际代码不符。
APP_VERSION="$(
  sed -n 's/^[[:space:]]*__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$ROOT_DIR/micast/__init__.py" | head -n 1
)"
MANIFEST_VERSION="$(sed -n 's/^version=//p' "$ROOT_DIR/packaging/fnos/manifest" | tr -d '\r' | head -n 1)"
if [ -z "$APP_VERSION" ] || [ -z "$MANIFEST_VERSION" ]; then
  echo "无法从 micast/__init__.py 或 packaging/fnos/manifest 解析版本号" >&2
  exit 1
fi
if [ "$APP_VERSION" != "$MANIFEST_VERSION" ]; then
  echo "版本不一致：micast/__init__.py 为 ${APP_VERSION}，manifest 为 ${MANIFEST_VERSION}；请同步后再构建" >&2
  exit 1
fi

command -v fnpack >/dev/null 2>&1 || {
  echo "fnpack 未安装或不在 PATH 中" >&2
  exit 1
}
command -v npm >/dev/null 2>&1 || {
  echo "npm 未安装或不在 PATH 中" >&2
  exit 1
}

rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/app/vendor" "$STAGE_DIR/app/web" "$STAGE_DIR/app/ui/images" "$OUTPUT_DIR"
cp -a "$ROOT_DIR/packaging/fnos/." "$STAGE_DIR/"
# Normalize executable scripts even when the checkout was made on Windows.
find "$STAGE_DIR/cmd" -type f -exec sed -i 's/\r$//' {} +
if [ -f "$STAGE_DIR/app/airplay2-runtime/run-shairport" ]; then
  sed -i 's/\r$//' "$STAGE_DIR/app/airplay2-runtime/run-shairport"
  sh -n "$STAGE_DIR/app/airplay2-runtime/run-shairport"
  chmod 0755 "$STAGE_DIR/app/airplay2-runtime/run-shairport"
fi
cp "$ROOT_DIR/micast/installation.py" "$STAGE_DIR/cmd/port-preflight.py"
cp "$ROOT_DIR/micast/ports.py" "$STAGE_DIR/cmd/ports.py"
cp "$ROOT_DIR/micast/config_store.py" "$STAGE_DIR/cmd/config_store.py"

# The bundled AirPlay 2 receiver is an x86-64 native runtime. Keep it in the
# existing x86 package, but never ship unusable x86 binaries in the ARM FPK.
if [ "$PLATFORM" = "arm" ]; then
  rm -rf "$STAGE_DIR/app/airplay2-runtime"
fi

npm --prefix "$ROOT_DIR/web" ci
npm --prefix "$ROOT_DIR/web" run build

cp -a "$ROOT_DIR/micast" "$STAGE_DIR/app/"
# Keep the dist/ level: the app resolves the UI as <root>/web/dist, matching
# the PowerShell builder's layout.
cp -a "$ROOT_DIR/web/dist" "$STAGE_DIR/app/web/"

if [ "$PLATFORM" = "arm" ]; then
  WHEEL_PLATFORMS="--platform manylinux_2_28_aarch64 --platform manylinux_2_17_aarch64 --platform manylinux2014_aarch64"
else
  WHEEL_PLATFORMS="--platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64"
fi

# Always cross-download the target platform wheels, even for x86 on an x86
# host: a plain `pip install` on Windows/macOS would vendor that host's
# binaries into a Linux package. Never install the micast package itself into
# vendor either — the runtime resolves `python -m micast` from the app
# directory, and vendor comes first on PYTHONPATH, so a vendored micast would
# shadow the app source and run stale code.
# shellcheck disable=SC2086
"$PYTHON_BIN" -m pip install \
  --disable-pip-version-check \
  --no-compile \
  --only-binary=:all: \
  $WHEEL_PLATFORMS \
  --implementation cp \
  --python-version 3.12 \
  --abi cp312 \
  --target "$STAGE_DIR/app/vendor" \
  --requirement "$ROOT_DIR/requirements.txt"

sed -i "s/^platform=.*/platform=$PLATFORM/" "$STAGE_DIR/manifest"

cp "$ROOT_DIR/web/public/icons/fnos-64.png" "$STAGE_DIR/ICON.PNG"
cp "$ROOT_DIR/web/public/icons/fnos-256.png" "$STAGE_DIR/ICON_256.PNG"
cp "$ROOT_DIR/web/public/icons/fnos-64.png" "$STAGE_DIR/app/ui/images/icon_64.png"
cp "$ROOT_DIR/web/public/icons/fnos-256.png" "$STAGE_DIR/app/ui/images/icon_256.png"

find "$STAGE_DIR/cmd" -type f -exec chmod 0755 {} +
find "$STAGE_DIR/app" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$STAGE_DIR/app" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
find "$STAGE_DIR/app/vendor" -type d \( -name tests -o -name test -o -name __pycache__ \) -prune -exec rm -rf {} +
find "$STAGE_DIR/app/vendor" -type f -name '*.pyi' -delete

# miservice-fork 只在它的独立 CLI（cli.py）里用到 rich / mutagen / setuptools；
# MiCast 走的是 miaccount / minaservice 等库路径，不需要这些，剥掉省约 2MB。
for junk in rich pygments mutagen setuptools pkg_resources _distutils_hack; do
  rm -rf "$STAGE_DIR/app/vendor/$junk"
  rm -rf "$STAGE_DIR/app/vendor/${junk}-"*.dist-info "$STAGE_DIR/app/vendor/${junk/-/_}"-*.dist-info
done

(
  cd "$STAGE_DIR"
  fnpack build
)

find "$STAGE_DIR" -maxdepth 1 -type f -name '*.fpk' -exec cp {} "$OUTPUT_DIR/" \;

# Rename to the versioned artifact name, matching the Windows build script.
VERSION="$(sed -n 's/^version=//p' "$STAGE_DIR/manifest" | tr -d '\r')"
PLATFORM="$(sed -n 's/^platform=//p' "$STAGE_DIR/manifest" | tr -d '\r')"
if [ -f "$OUTPUT_DIR/micast.fpk" ]; then
  mv "$OUTPUT_DIR/micast.fpk" "$OUTPUT_DIR/micast-$PLATFORM-$VERSION.fpk"
fi
echo "fnOS package: $OUTPUT_DIR"
