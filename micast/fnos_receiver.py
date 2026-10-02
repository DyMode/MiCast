"""Single owner for the bundled fnOS receiver and its fixed port contract."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from micast.ports import AIRPLAY2_RECEIVER_PORT, reserve_tcp


def owns_listener(pid, port, proc_root=Path("/proc")):
    """Match listening socket inodes to this child, never another service."""
    owned = set()
    for fd in (proc_root / str(pid) / "fd").iterdir():
        try:
            link = os.readlink(fd)
        except FileNotFoundError:
            continue
        if link.startswith("socket:["):
            owned.add(link[8:-1])
    for family in ("tcp", "tcp6"):
        path = proc_root / str(pid) / "net" / family
        if not path.exists():
            continue
        for row in path.read_text().splitlines()[1:]:
            columns = row.split()
            if len(columns) >= 10 and columns[3] == "0A":
                if int(columns[1].rsplit(":", 1)[1], 16) == port and columns[9] in owned:
                    return True
    return False


def probe_listener(port):
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            pass
    return False


def await_listener(receiver, clock, port, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if clock.poll() is not None:
            return "clock_exited"
        if receiver.poll() is not None:
            return "receiver_exited"
        try:
            if owns_listener(receiver.pid, port) and probe_listener(port):
                return "ready"
        except FileNotFoundError:
            if receiver.poll() is not None:
                return "receiver_exited"
        except PermissionError as error:
            raise RuntimeError("无法确认 AirPlay 2 接收进程的监听端口归属，请检查进程权限") from error
        time.sleep(0.1)
    return "listener_timeout"


def stop_process(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def configuration(port, name, callback, python, receiver_id):
    def quoted(value):
        return json.dumps(str(value), ensure_ascii=False)
    return f"""general = {{
 name = {quoted(name)}; port = {port}; output_backend = "stdout";
 service_type = "airplay2"; ignore_volume_control = "yes";
 audio_decoded_buffer_desired_length_in_seconds = 1.5;
 audio_backend_buffer_desired_length_in_seconds = 0.35;
 run_this_when_volume_is_set = {quoted(f"{python} {callback} volume {receiver_id} ")};
}};
sessioncontrol = {{
 allow_session_interruption = "yes";
 session_timeout = 0;
 run_this_before_play_begins = {quoted(f"{python} {callback} start {receiver_id}")};
 run_this_after_play_ends = {quoted(f"{python} {callback} stop {receiver_id}")};
}};
diagnostics = {{ log_verbosity = 1; }};
stdout = {{ output_rate = 48000; output_format = "S16_LE"; output_channels = 2; }};
"""


def supervise():
    runtime = Path(os.environ["MICAST_AIRPLAY2_RUNTIME"])
    data = Path(os.environ["MICAST_DATA_DIR"])
    logs = Path(os.environ.get("MICAST_LOG_DIR", str(data)))
    logs.mkdir(parents=True, exist_ok=True)
    import platform
    loader_name = "ld-musl-aarch64.so.1" if platform.machine() in {"aarch64", "arm64"} else "ld-musl-x86_64.so.1"
    loader = runtime / "lib" / loader_name
    libraries = ":".join(str(runtime / p) for p in
                         ("usr-lib/pulseaudio", "usr-lib", "usr-local-lib"))
    prefix = [str(loader), "--library-path", libraries]
    preferred = AIRPLAY2_RECEIVER_PORT
    ready = Path(os.environ.get("MICAST_AIRPLAY2_READY_FILE", str(data / "airplay2-ready.json")))
    report = logs / "airplay2-startup.json"
    config = data / "shairport-sync.conf"
    attempts = []
    clock = receiver = None
    readers = []
    ready.unlink(missing_ok=True)
    try:
        try:
            with reserve_tcp(preferred, strict=True):
                pass
        except RuntimeError as error:
            raise RuntimeError(
                f"AirPlay 2 固定 TCP 端口 {preferred} 被占用或无法绑定；"
                "仅此功能暂不可用，释放端口后可在设置中重新启动"
            ) from error
        with (logs / "nqptp.log").open("ab") as log:
            clock = subprocess.Popen(prefix + [str(runtime / "bin/nqptp")],
                                     stdout=log, stderr=log)
        (data / "nqptp.pid").write_text(str(clock.pid))
        time.sleep(0.7)
        if clock.poll() is not None:
            raise RuntimeError("NQPTP 时钟服务启动失败，请查看 nqptp.log（权限或时钟端口）")
        port = preferred
        config.write_text(configuration(port, os.environ.get("MICAST_AIRPLAY_NAME", "MiCast"),
                          runtime / "callback.py", os.environ["PYTHON_BIN"],
                          os.environ.get("MICAST_DEVICE_ID", "airplay2")), encoding="utf-8")
        tail = deque(maxlen=40)
        receiver = subprocess.Popen([sys.executable, "-m", "micast.fnos_receiver", "--exec-receiver"] + prefix + [str(runtime / "bin/shairport-sync"),
                                   "-c", str(config), "-p", str(port)], stderr=subprocess.PIPE)
        (data / "shairport-sync.pid").write_text(str(receiver.pid))

        def drain(stream, messages):
            with (logs / "shairport-startup.log").open("ab") as log:
                while chunk := stream.read1(4096):
                    log.write(chunk)
                    log.flush()
                    messages.append(chunk)
            stream.close()

        reader = threading.Thread(target=drain, args=(receiver.stderr, tail), daemon=True)
        reader.start()
        readers.append(reader)
        state = await_listener(receiver, clock, port)
        attempts.append({"port": port, "state": state, "pid": receiver.pid})
        write_json(report, {"state": state, "preferred": preferred, "attempts": attempts,
                            "fixed_port": True})
        if state == "ready":
            write_json(ready, {"port": port, "pid": receiver.pid})
            while receiver.poll() is None:
                if clock.poll() is not None:
                    raise RuntimeError("NQPTP 时钟服务运行中退出")
                time.sleep(0.2)
            raise RuntimeError(f"AirPlay 2 接收进程退出，退出码 {receiver.returncode}")
        stop_process(receiver)
        reader.join(timeout=2)
        detail = b"".join(tail).decode(errors="replace").lower()
        if state == "clock_exited":
            raise RuntimeError("NQPTP 时钟服务退出，请查看 nqptp.log")
        if any(word in detail for word in ("permission denied", "error loading shared",
                                          "syntax error", "configuration error")):
            raise RuntimeError("AirPlay 2 权限、运行库或配置错误，请查看 shairport-startup.log")
        raise RuntimeError(f"AirPlay 2 固定 TCP 端口 {port} 未就绪（{state}），请检查接收器日志后重新启动；不会自动更换端口")
    except BaseException as error:
        write_json(report, {
            "state": "failed", "preferred": preferred,
            "attempts": attempts, "detail": str(error), "fixed_port": True,
        })
        raise
    finally:
        stop_process(receiver)
        stop_process(clock)
        for reader in readers:
            reader.join(timeout=2)
        for path in (ready, data / "nqptp.pid", data / "shairport-sync.pid"):
            path.unlink(missing_ok=True)


def main():
    import signal

    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        supervise()
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--exec-receiver":
        # Only NQPTP needs privileged ports. Suppress the loader's file caps
        # for Shairport so the parent can inspect its sockets as the app user.
        # Run this in a fresh interpreter, not preexec_fn in a threaded parent.
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise OSError(ctypes.get_errno(), "无法隔离接收器权限")
        os.execv(sys.argv[2], sys.argv[2:])
    sys.exit(main())
