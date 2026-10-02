#!/bin/sh
set -eu

name="${MICAST_AIRPLAY_NAME:-MiCast}"
protocol="${MICAST_AIRPLAY_PROTOCOL:-auto}"
pcm_port="${MICAST_PCM_PORT:-42800}"
case "$protocol" in
  auto|classic|airplay2) ;;
  *) echo "Unsupported AirPlay protocol: $protocol" >&2; exit 2 ;;
esac

# Shairport resets its RTSP port after parsing config. Host-network deployments
# must reserve that fixed port; bridge deployments may map a different host port.
receiver_port="$(python3 -c '
import sys
sys.path.insert(0, "/app")
from ports import native_receiver_port, reserve_tcp
port = native_receiver_port(sys.argv[1])
with reserve_tcp(port, strict=True) as lease:
    print(lease.port)
' "$protocol")"
echo "AirPlay 接收端口: $receiver_port；PCM 端口: $pcm_port" >&2



cat >/etc/shairport-sync.conf <<EOF
general = {
  name = "$name";
  port = $receiver_port;
  interface = "eth0";
  output_backend = "stdout";
  service_type = "$protocol";
  ignore_volume_control = "yes";
  run_this_when_volume_is_set = "/app/scripts/receiver-volume.sh ";
  log_verbosity = 1;
};
sessioncontrol = {
  allow_session_interruption = "yes";
  session_timeout = ${MICAST_SESSION_TIMEOUT:-60};
  run_this_before_play_begins = "/app/scripts/receiver-session-start.sh";
  run_this_after_play_ends = "/app/scripts/receiver-session-stop.sh";
};
stdout = {
  output_rate = 48000;
  output_format = "S16_LE";
  output_channels = 2;
};
EOF

rm -f /run/dbus/dbus.pid /run/avahi-daemon/pid
dbus-uuidgen --ensure
dbus-daemon --system
avahi-daemon --daemonize --no-chroot

if [ "$protocol" != "classic" ]; then
  /usr/local/bin/nqptp >/dev/null 2>&1 &
  nqptp_pid=$!
  sleep 1
  if ! kill -0 "$nqptp_pid" 2>/dev/null; then
    echo "NQPTP 无法启动，请检查 319/320/9000 UDP 端口或权限。" >&2
    exit 1
  fi
fi

# Keep the PCM listener alive across MiCast restarts and short network drops.
# Without `fork`, socat exits when its first client disconnects while
# Shairport Sync keeps advertising the AirPlay service, leaving a visible but
# unusable receiver behind.
fifo=/run/micast-pcm.fifo
rm -f "$fifo"
mkfifo "$fifo"
cleanup() {
  [ -z "${shairport_pid:-}" ] || kill "$shairport_pid" 2>/dev/null || true
  [ -z "${pcm_pid:-}" ] || kill "$pcm_pid" 2>/dev/null || true
  [ -z "${nqptp_pid:-}" ] || kill "$nqptp_pid" 2>/dev/null || true
  rm -f "$fifo"
}
trap cleanup EXIT
trap 'exit 0' INT TERM
/usr/local/bin/shairport-sync -c /etc/shairport-sync.conf > "$fifo" &
shairport_pid=$!
socat -u - "TCP-LISTEN:${pcm_port},fork,reuseaddr,keepalive" < "$fifo" &
pcm_pid=$!
while kill -0 "$shairport_pid" 2>/dev/null && kill -0 "$pcm_pid" 2>/dev/null; do
  if [ -n "${nqptp_pid:-}" ] && ! kill -0 "$nqptp_pid" 2>/dev/null; then
    echo "时钟服务退出，停止该接收器，等待容器重试。" >&2
    exit 1
  fi
  sleep 1
done
echo "AirPlay 或 PCM 监听退出，停止该接收器，等待容器重试。" >&2
exit 1
