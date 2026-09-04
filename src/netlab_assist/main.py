from __future__ import annotations

import argparse
import signal
import sys
import threading
import webbrowser

from . import __version__
from .config import Settings
from .server import create_servers


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="NetLab Assist lightweight laboratory node")
    result.add_argument("--bind", default="0.0.0.0", help="UI bind address (default: 0.0.0.0)")
    result.add_argument("--port", type=int, default=18080, help="UI and peer-control TCP port")
    result.add_argument("--throughput-port", type=int, default=18881, help="throughput sink TCP port")
    result.add_argument("--echo-port", type=int, default=18882, help="tuple-observation TCP port")
    result.add_argument("--access-code", default="", help="fixed peer access code; random when omitted")
    result.add_argument("--no-browser", action="store_true", help="do not open the local browser")
    return result


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    settings = Settings(
        bind=args.bind,
        ui_port=args.port,
        throughput_port=args.throughput_port,
        echo_port=args.echo_port,
        access_code=args.access_code,
        open_browser=not args.no_browser,
    ).normalized()
    try:
        web_server, throughput_sink, echo_sink = create_servers(settings)
    except OSError as exc:
        print(f"Unable to start NetLab Assist: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    stopping = threading.Event()

    def stop(*_: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=web_server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    url = f"http://127.0.0.1:{settings.ui_port}"
    print(f"NetLab Assist {__version__}")
    print(f"Local UI: {url}")
    print(f"Access code: {settings.access_code}")
    print("Use only on an authorized laboratory network. Press Ctrl+C to stop.")
    if settings.open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        web_server.serve_forever(poll_interval=0.25)
    finally:
        throughput_sink.shutdown()
        echo_sink.shutdown()
        throughput_sink.server_close()
        echo_sink.server_close()
        web_server.server_close()


if __name__ == "__main__":
    main()
