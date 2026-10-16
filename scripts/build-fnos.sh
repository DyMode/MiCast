#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_DIR="${MICAST_FNOS_STAGE_DIR:-${ROOT_DIR}/build/fnos/micast}"
OUTPUT_DIR="${MICAST_FNOS_OUTPUT_DIR:-${ROOT_DIR}/dist/fnos}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" "$ROOT_DIR/scripts/sync-legal.py" --check
"$PYTHON_BIN" "$ROOT_DIR/scripts/check-fnos-package.py" \
  --package "$ROOT_DIR/packaging/fnos" --repo-root "$ROOT_DIR"
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
# Python-based packaging checks can leave host-version bytecode beside cmd
# helpers. Drop it before text normalization and do not ship it in the FPK.
find "$STAGE_DIR/cmd" -type d -name '__pycache__' -prune -exec rm -rf {} +
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
cp -a "$ROOT_DIR/licenses" "$STAGE_DIR/app/"
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
install_vendor() {
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
}

# The package index intermittently drops connections ("Could not find a version
# that satisfies the requirement ..."), which aborted whole builds. Retry a few
# times before giving up, keeping the failure loud.
VENDOR_ATTEMPTS="${MICAST_FNOS_VENDOR_ATTEMPTS:-3}"
attempt=1
until install_vendor; do
  if [ "$attempt" -ge "$VENDOR_ATTEMPTS" ]; then
    echo "vendor 依赖下载失败（已尝试 ${attempt} 次）；请检查网络或包索引后重试" >&2
    exit 1
  fi
  echo "vendor 依赖下载第 ${attempt} 次失败，20 秒后重试" >&2
  attempt=$((attempt + 1))
  sleep 20
done

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

# fnpack prints "Packing failed" on content problems but still exits 0, which
# once left a stale .fpk in dist/ looking like a fresh build. Validate the
# staged tree first, then require both the success marker and a package that is
# newer than this build started.
"$PYTHON_BIN" "$ROOT_DIR/scripts/check-fnos-package.py" --package "$STAGE_DIR" --staged

PACK_STARTED="$(date +%s)"
# Keep the log out of the staging tree: fnpack packs the directory it runs in.
PACK_LOG="$(mktemp -t fnpack-build.XXXXXX.log)"
(
  cd "$STAGE_DIR"
  fnpack build
) 2>&1 | tee "$PACK_LOG"
PACKED="$STAGE_DIR/micast.fpk"
if [ ! -f "$PACKED" ]; then
  echo "fnpack 未产出 ${PACKED}；fnpack 输出见上方日志" >&2
  exit 1
fi
if ! grep -q "Packing successfully" "$PACK_LOG"; then
  echo "fnpack 报告打包失败（退出码可能仍为 0）；fnpack 输出见上方日志" >&2
  exit 1
fi
if [ "$(date -r "$PACKED" +%s)" -lt "$PACK_STARTED" ]; then
  echo "${PACKED} 早于本次打包开始时间，说明产物未真正重建" >&2
  exit 1
fi

cp "$PACKED" "$OUTPUT_DIR/"

# Rename to the versioned artifact name, matching the Windows build script.
VERSION="$(sed -n 's/^version=//p' "$STAGE_DIR/manifest" | tr -d '\r')"
PLATFORM="$(sed -n 's/^platform=//p' "$STAGE_DIR/manifest" | tr -d '\r')"
ARTIFACT="$OUTPUT_DIR/micast-$PLATFORM-$VERSION.fpk"
if [ -f "$OUTPUT_DIR/micast.fpk" ]; then
  mv "$OUTPUT_DIR/micast.fpk" "$ARTIFACT"
fi
if [ ! -f "$ARTIFACT" ]; then
  echo "未找到最终的 fnOS 包：${ARTIFACT}" >&2
  exit 1
fi
echo "fnOS package: $ARTIFACT ($(du -h "$ARTIFACT" | cut -f1))"
