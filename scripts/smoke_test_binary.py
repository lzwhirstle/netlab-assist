from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib import request


HTTP = request.build_opener(request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_test_binary.py PATH_TO_BINARY")
    executable = Path(sys.argv[1]).resolve()
    ports = [free_port() for _ in range(3)]
    process = subprocess.Popen(
        [
            str(executable),
            "--no-browser",
            "--bind", "127.0.0.1",
            "--port", str(ports[0]),
            "--throughput-port", str(ports[1]),
            "--echo-port", str(ports[2]),
            "--access-code", "112233",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise RuntimeError(f"packaged application exited early ({process.returncode}):\n{output}")
            try:
                with HTTP.open(f"http://127.0.0.1:{ports[0]}/", timeout=2) as response:
                    page = response.read()
                with HTTP.open(f"http://127.0.0.1:{ports[0]}/api/status", timeout=2) as response:
                    status = json.loads(response.read())
                if b"NetLab Assist" not in page:
                    raise AssertionError("home page does not contain the application name")
                if status.get("app") != "NetLab Assist":
                    raise AssertionError("status endpoint returned an unexpected application name")
                peer_request = request.Request(
                    f"http://127.0.0.1:{ports[0]}/api/peer/check",
                    data=json.dumps({
                        "target": "127.0.0.1",
                        "peer_token": "112233",
                        "peer_ui_port": ports[0],
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with HTTP.open(peer_request, timeout=5) as response:
                    peer = json.loads(response.read())
                if not peer.get("ready"):
                    raise AssertionError(f"packaged peer readiness failed: {peer}")
                print(f"Packaged smoke test passed: {executable.name} {status.get('version')}")
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.25)
        raise RuntimeError(f"packaged application did not become ready: {last_error}")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
