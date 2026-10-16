"""Identify this fnOS app's service processes without ptrace-protected cwd links."""

import argparse
import os
from pathlib import Path


def matches(argv, uid, username, app_uid):
    if not argv:
        return False
    # Match an argument pair, never a substring such as micast.fnos_receiver.
    if not any(a == "-m" and b == "micast" for a, b in zip(argv, argv[1:], strict=False)):
        return False
    executable = Path(argv[0]).name
    if executable in {"python", "python3", "python3.12"}:
        return uid == app_uid
    if executable == "runuser" and uid == 0:
        if not any(
            a in {"-u", "--user"} and b == username for a, b in zip(argv, argv[1:], strict=False)
        ):
            return False
        try:
            command = argv[argv.index("--") + 1 :]
        except ValueError:
            return False
        return matches(command, app_uid, username, app_uid)
    return False


def service_pids(proc, username, app_uid):
    result = []
    for entry in proc.iterdir():
        if not entry.name.isdecimal() or int(entry.name) <= 1:
            continue
        try:
            argv = [
                item.decode(errors="replace")
                for item in (entry / "cmdline").read_bytes().split(b"\0")
                if item
            ]
            uid_line = next(
                line
                for line in (entry / "status").read_text().splitlines()
                if line.startswith("Uid:")
            )
            uid = int(uid_line.split()[1])
        except (OSError, ValueError, StopIteration):
            continue
        if matches(argv, uid, username, app_uid):
            result.append(int(entry.name))
    return sorted(result)


if __name__ == "__main__":
    import pwd

    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    uid = pwd.getpwnam(args.username).pw_uid
    for pid in service_pids(Path("/proc"), args.username, uid):
        if pid != os.getpid():
            print(pid)
