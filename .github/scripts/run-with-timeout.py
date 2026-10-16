"""Run a native test with a deadline on Windows, Linux and macOS."""
import os
import signal
import subprocess
import sys


def main():
    seconds = float(sys.argv[1])
    args = sys.argv[2:]
    if seconds <= 0 or not args:
        raise ValueError("Expected positive seconds followed by a command.")
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    process = subprocess.Popen(args, **options)
    try:
        status = process.wait(timeout=seconds)
        if status:
            print(f"Command exited with status {status} (0x{status & 0xffffffff:08X}): {args[0]}",
                  file=sys.stderr, flush=True)
        return status if status >= 0 else 128 - status
    except subprocess.TimeoutExpired:
        print(f"Timed out after {seconds:g}s: {args[0]}", file=sys.stderr, flush=True)
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait()
        return 124


if __name__ == "__main__":
    sys.exit(main())
