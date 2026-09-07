from __future__ import annotations

import ipaddress
from dataclasses import dataclass
import json
import mimetypes
from pathlib import Path
import platform
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from . import __version__
from .config import MAX_REQUEST_BYTES, Settings
from .jobs import JobContext, JobManager, utc_now
from .protocols import (
    TupleEchoHandler,
    ThroughputSinkHandler,
    clean_host,
    dns_query,
    local_ipv4_addresses,
    multicast_receiver,
    multicast_sender,
    nat_tuple_probe,
    ping_probe,
    reachability_monitor,
    remote_throughput,
    run_throughput_sender,
    secrets_equal,
    start_sink,
    tcp_probe,
)


WEB_ROOT = Path(__file__).resolve().parent / "web"
REQUIRED_WEB_FILES = ("index.html", "app.js", "styles.css")


@dataclass
class ApplicationState:
    settings: Settings
    jobs: JobManager
    started_monotonic: float
    started_at: str


class NetLabHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], state: ApplicationState):
        self.state = state
        super().__init__(address, handler)


class Handler(BaseHTTPRequestHandler):
    server_version = f"NetLabAssist/{__version__}"

    @property
    def state(self) -> ApplicationState:
        return self.server.state

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the local console useful without logging request bodies or access codes.
        print(f"[{self.log_date_time_string()}] {self.client_address[0]} {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not self._is_local():
            self._error(HTTPStatus.FORBIDDEN, "the UI and local results are available only from this computer")
            return
        if parsed.path == "/api/status":
            settings = self.state.settings
            self._json({
                "app": "NetLab Assist",
                "version": __version__,
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "local_ips": local_ipv4_addresses(),
                "access_code": settings.access_code,
                "ui_port": settings.ui_port,
                "throughput_port": settings.throughput_port,
                "echo_port": settings.echo_port,
                "started_at": self.state.started_at,
                "uptime_s": round(time.monotonic() - self.state.started_monotonic, 1),
                "jobs": self.state.jobs.summary(),
            })
            return
        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            job = self.state.jobs.get(job_id)
            if not job:
                self._error(HTTPStatus.NOT_FOUND, "job not found")
            else:
                self._json(job)
            return
        self._static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path != "/api/peer/throughput" and not self._is_local():
                raise PermissionError("local test controls are available only from this computer")
            body = self._body()
            if parsed.path == "/api/throughput":
                self._start_throughput(body)
            elif parsed.path == "/api/peer/throughput":
                self._run_peer_throughput(body)
            elif parsed.path == "/api/probe":
                self._probe(body)
            elif parsed.path == "/api/dns":
                self._json(dns_query(
                    body.get("server"), body.get("name"), body.get("qtype", "A"),
                    body.get("transport", "udp"), body.get("txid"), body.get("timeout", 4),
                    body.get("port", 53),
                ))
            elif parsed.path == "/api/nat":
                self._json(nat_tuple_probe(
                    body.get("target"), body.get("port", self.state.settings.echo_port), body.get("peer_token"),
                ))
            elif parsed.path == "/api/multicast/start":
                self._start_multicast(body)
            elif parsed.path == "/api/monitor/start":
                self._start_monitor(body)
            elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
                job_id = parsed.path.split("/")[3]
                if not self.state.jobs.cancel(job_id):
                    self._error(HTTPStatus.NOT_FOUND, "job not found")
                else:
                    self._json({"ok": True, "job_id": job_id})
            else:
                self._error(HTTPStatus.NOT_FOUND, "route not found")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        except Exception as exc:
            self._error(HTTPStatus.BAD_GATEWAY, f"{type(exc).__name__}: {exc}")

    def _start_throughput(self, body: dict[str, Any]) -> None:
        settings = self.state.settings
        target = clean_host(body.get("target"))
        peer_token = str(body.get("peer_token") or "").strip().upper()
        direction = str(body.get("direction", "forward")).lower()
        if direction not in {"forward", "reverse", "bidirectional"}:
            raise ValueError("direction must be forward, reverse, or bidirectional")
        duration = int(body.get("duration", 10))
        streams = int(body.get("streams", 1))
        peer_ui_port = int(body.get("peer_ui_port", settings.ui_port))
        peer_throughput_port = int(body.get("peer_throughput_port", settings.throughput_port))

        def task(context: JobContext) -> dict[str, Any]:
            results: dict[str, Any] = {}
            failures: list[str] = []

            def forward() -> None:
                try:
                    results["forward"] = run_throughput_sender(
                        target, peer_throughput_port, peer_token, duration, streams, context, "forward"
                    )
                except Exception as exc:
                    failures.append(f"forward: {type(exc).__name__}: {exc}")

            def reverse() -> None:
                try:
                    results["reverse"] = remote_throughput(
                        target, peer_ui_port, peer_token, settings.access_code,
                        settings.throughput_port, duration, streams,
                    )
                    for sample in results["reverse"].get("samples", []):
                        context.update(sample={**sample, "direction": "reverse"})
                except Exception as exc:
                    failures.append(f"reverse: {type(exc).__name__}: {exc}")

            selected: list[Callable[[], None]] = []
            if direction in {"forward", "bidirectional"}:
                selected.append(forward)
            if direction in {"reverse", "bidirectional"}:
                selected.append(reverse)
            threads = [threading.Thread(target=fn, daemon=True) for fn in selected]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(duration + 20)
            if failures and not results:
                raise ConnectionError("; ".join(failures))
            averages = [float(item.get("average_mbps", 0)) for item in results.values()]
            return {
                "mode": direction,
                "peer": target,
                "results": results,
                "aggregate_average_mbps": round(sum(averages), 3),
                "failures": failures,
                "measured_at": utc_now(),
                "evidence_note": "Aggregate is the sum of independent directions; evaluate each direction separately.",
            }

        job_id = self.state.jobs.start("throughput", task)
        self._json({"job_id": job_id, "status": "queued"}, HTTPStatus.ACCEPTED)

    def _run_peer_throughput(self, body: dict[str, Any]) -> None:
        if not secrets_equal(self.headers.get("X-Lab-Token", ""), self.state.settings.access_code):
            raise PermissionError("invalid peer access code")
        # Reflection safeguard: ignore an arbitrary target in the body and send only to the requesting host.
        target = self.client_address[0]
        result = run_throughput_sender(
            target,
            int(body.get("sink_port", self.state.settings.throughput_port)),
            str(body.get("sink_token", "")),
            int(body.get("duration", 10)),
            int(body.get("streams", 1)),
            None,
            "reverse",
        )
        self._json(result)

    def _probe(self, body: dict[str, Any]) -> None:
        kind = str(body.get("kind", "tcp")).lower()
        if kind == "tcp":
            result = tcp_probe(body.get("target"), body.get("port", 443), body.get("timeout", 2))
        elif kind == "ping":
            result = ping_probe(body.get("target"), body.get("count", 3), body.get("timeout", 2))
        else:
            raise ValueError("probe kind must be tcp or ping")
        result.update({"kind": kind, "target": body.get("target"), "tested_at": utc_now()})
        self._json(result)

    def _start_multicast(self, body: dict[str, Any]) -> None:
        role = str(body.get("role", "receiver")).lower()
        group = body.get("group", "235.0.0.10")
        port = int(body.get("port", 5000))
        duration = int(body.get("duration", 30))
        if role == "sender":
            rate = float(body.get("rate_mbps", 5))
            job_id = self.state.jobs.start(
                "multicast_sender",
                lambda context: multicast_sender(context, group, port, rate, duration),
            )
        elif role == "receiver":
            job_id = self.state.jobs.start(
                "multicast_receiver",
                lambda context: multicast_receiver(context, group, port, duration),
            )
        else:
            raise ValueError("multicast role must be sender or receiver")
        self._json({"job_id": job_id, "status": "queued"}, HTTPStatus.ACCEPTED)

    def _start_monitor(self, body: dict[str, Any]) -> None:
        job_id = self.state.jobs.start(
            "monitor",
            lambda context: reachability_monitor(
                context, body.get("target"), int(body.get("port", self.state.settings.echo_port)),
                float(body.get("interval", 0.5)), int(body.get("duration", 120)),
            ),
        )
        self._json({"job_id": job_id, "status": "queued"}, HTTPStatus.ACCEPTED)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if not 0 <= length <= MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _is_local(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        requested = (WEB_ROOT / relative).resolve()
        try:
            requested.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        if not requested.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        mime = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
        data = requested.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message, "status": int(status)}, status)


def create_servers(settings: Settings) -> tuple[NetLabHTTPServer, Any, Any]:
    missing = [name for name in REQUIRED_WEB_FILES if not (WEB_ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError(f"packaged web assets are missing: {', '.join(missing)}")
    settings = settings.normalized()
    state = ApplicationState(settings=settings, jobs=JobManager(), started_monotonic=time.monotonic(), started_at=utc_now())
    throughput_sink = start_sink(settings.throughput_port, settings.access_code, ThroughputSinkHandler)
    echo_sink = start_sink(settings.echo_port, settings.access_code, TupleEchoHandler)
    web_server = NetLabHTTPServer((settings.bind, settings.ui_port), Handler, state)
    return web_server, throughput_sink, echo_sink
