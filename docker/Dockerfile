# MiCast controller image. AirPlay receivers run in isolated managed containers.

# The web bundle is architecture-independent. Build it on the runner's native
# platform so an arm64 image build never runs npm under QEMU.
FROM --platform=$BUILDPLATFORM node:20-alpine AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Runtime: slim (glibc) rather than alpine — zxing-cpp ships no musllinux
# wheels and would try a source build (no cmake/toolchain in the image).
# No ffmpeg needed: transcoding is in-process via PyAV (av wheels bundle
# libavcodec/libavfilter); curl is only for the compose healthcheck.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=web-build /web/dist /app/web/dist
RUN chmod +x /app/scripts/*.sh

# Persist config outside container if mounted
VOLUME ["/app/config"]

EXPOSE 42300 42400

CMD ["/app/scripts/start.sh"]
