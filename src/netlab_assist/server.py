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
    remote_peer_call,
    remote_throughput,
    route_ipv4_for,
    run_throughput_sender,
    secrets_equal,
    start_sink,
    tcp_probe,
    throughput_sink_probe,
)


WEB_ROOT = Path(__file__).resolve().parent / "web"
REQUIRED_WEB_FILES = ("index.html", "app.js", "styles.css")
PEER_ENDPOINTS = {
    "/api/peer/status",
    "/api/peer/throughput",
    "/api/peer/multicast/start",
    "/api/peer/job",
    "/api/peer/job/cancel",
}


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
            if parsed.path not in PEER_ENDPOINTS and not self._is_local():
                raise PermissionError("local test controls are available only from this computer")
            body = self._body()
            if parsed.path == "/api/throughput":
                self._start_throughput(body)
            elif parsed.path == "/api/peer/check":
                self._peer_check(body)
            elif parsed.path == "/api/peer/status":
                self._peer_status()
            elif parsed.path == "/api/peer/throughput":
                self._run_peer_throughput(body)
            elif parsed.path == "/api/peer/multicast/start":
                self._run_peer_multicast(body)
            elif parsed.path == "/api/peer/job":
                self._run_peer_job(body)
            elif parsed.path == "/api/peer/job/cancel":
                self._run_peer_job_cancel(body)
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
            elif parsed.path == "/api/multicast/pair":
                self._start_paired_multicast(body)
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
        peer_status = remote_peer_call(target, peer_ui_port, peer_token, "/api/peer/status")
        peer_throughput_port = int(peer_status.get("throughput_port", body.get("peer_throughput_port", settings.throughput_port)))

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

    def _require_peer_token(self) -> None:
        if not secrets_equal(self.headers.get("X-Lab-Token", ""), self.state.settings.access_code):
            raise PermissionError("invalid peer access code")

    def _peer_status(self) -> None:
        self._require_peer_token()
        settings = self.state.settings
        self._json({
            "app": "NetLab Assist",
            "version": __version__,
            "hostname": socket.gethostname(),
            "local_ips": local_ipv4_addresses(),
            "ui_port": settings.ui_port,
            "throughput_port": settings.throughput_port,
            "echo_port": settings.echo_port,
            "uptime_s": round(time.monotonic() - self.state.started_monotonic, 1),
        })

    @staticmethod
    def _check_result(name: str, operation: Callable[[], Any]) -> dict[str, Any]:
        started = time.monotonic()
        try:
            detail = operation()
            return {
                "name": name,
                "ok": True,
                "classification": "ready",
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "detail": detail,
            }
        except PermissionError as exc:
            classification = "invalid_code"
            message = str(exc)
        except ConnectionRefusedError as exc:
            classification = "refused"
            message = str(exc)
        except (TimeoutError, socket.timeout) as exc:
            classification = "timeout"
            message = str(exc) or "timeout"
        except ConnectionError as exc:
            classification = "incompatible" if "incompatible" in str(exc).lower() else "network_error"
            message = str(exc)
        except OSError as exc:
            classification = "network_error"
            message = str(exc)
        except Exception as exc:
            classification = "peer_error"
            message = str(exc)
        return {
            "name": name,
            "ok": False,
            "classification": classification,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "error": message,
        }

    def _peer_check(self, body: dict[str, Any]) -> None:
        target = clean_host(body.get("target"))
        token = str(body.get("peer_token") or "").strip().upper()
        ui_port = int(body.get("peer_ui_port", self.state.settings.ui_port))
        control = self._check_result(
            "control",
            lambda: remote_peer_call(target, ui_port, token, "/api/peer/status", timeout=4),
        )
        if not control["ok"]:
            self._json({
                "ready": False,
                "target": target,
                "checks": {"control": control},
                "classification": control["classification"],
            })
            return
        peer = control["detail"]
        throughput_port = int(peer["throughput_port"])
        echo_port = int(peer["echo_port"])
        throughput = self._check_result(
            "throughput",
            lambda: throughput_sink_probe(target, throughput_port, token),
        )
        echo = self._check_result(
            "echo",
            lambda: nat_tuple_probe(target, echo_port, token),
        )
        checks = {"control": control, "throughput": throughput, "echo": echo}
        route_ip = None
        if throughput.get("ok"):
            route_ip = str(throughput.get("detail", {}).get("local", "")).rsplit(":", 1)[0] or None
        self._json({
            "ready": all(item["ok"] for item in checks.values()),
            "target": target,
            "peer": peer,
            "checks": checks,
            "route_ip": route_ip,
            "classification": "ready" if all(item["ok"] for item in checks.values()) else "service_blocked",
        })

    def _run_peer_throughput(self, body: dict[str, Any]) -> None:
        self._require_peer_token()
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

    def _run_peer_multicast(self, body: dict[str, Any]) -> None:
        self._require_peer_token()
        role = str(body.get("role", "receiver")).lower()
        group = body.get("group", "235.0.0.10")
        port = int(body.get("port", 5000))
        duration = int(body.get("duration", 30))
        interface_ip = route_ipv4_for(self.client_address[0], self.state.settings.ui_port)
        if role == "sender":
            rate = float(body.get("rate_mbps", 5))
            job_id = self.state.jobs.start(
                "peer_multicast_sender",
                lambda context: multicast_sender(context, group, port, rate, duration, interface_ip=interface_ip),
            )
        elif role == "receiver":
            job_id = self.state.jobs.start(
                "peer_multicast_receiver",
                lambda context: multicast_receiver(context, group, port, duration, interface_ip=interface_ip),
            )
        else:
            raise ValueError("multicast role must be sender or receiver")
        self._json({"job_id": job_id, "status": "queued"}, HTTPStatus.ACCEPTED)

    def _run_peer_job(self, body: dict[str, Any]) -> None:
        self._require_peer_token()
        job_id = str(body.get("job_id") or "")
        job = self.state.jobs.get(job_id)
        if not job:
            self._error(HTTPStatus.NOT_FOUND, "job not found")
        else:
            self._json(job)

    def _run_peer_job_cancel(self, body: dict[str, Any]) -> None:
        self._require_peer_token()
        job_id = str(body.get("job_id") or "")
        if not self.state.jobs.cancel(job_id):
            self._error(HTTPStatus.NOT_FOUND, "job not found")
        else:
            self._json({"ok": True, "job_id": job_id})

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

    def _start_paired_multicast(self, body: dict[str, Any]) -> None:
        target = clean_host(body.get("target"))
        token = str(body.get("peer_token") or "").strip().upper()
        ui_port = int(body.get("peer_ui_port", self.state.settings.ui_port))
        direction = str(body.get("direction", "forward")).lower()
        if direction not in {"forward", "reverse"}:
            raise ValueError("direction must be forward or reverse")
        group = body.get("group", "235.0.0.10")
        port = int(body.get("port", 5000))
        duration = int(body.get("duration", 30))
        rate = float(body.get("rate_mbps", 5))
        remote_peer_call(target, ui_port, token, "/api/peer/status", timeout=4)
        local_interface_ip = route_ipv4_for(target, ui_port)

        def wait_peer_job(job_id: str, context: JobContext) -> dict[str, Any]:
            deadline = time.monotonic() + duration + 12
            while time.monotonic() < deadline:
                if context.cancelled:
                    remote_peer_call(
                        target, ui_port, token, "/api/peer/job/cancel", {"job_id": job_id}, timeout=4,
                    )
                job = remote_peer_call(
                    target, ui_port, token, "/api/peer/job", {"job_id": job_id}, timeout=4,
                )
                if job.get("status") in {"completed", "failed", "cancelled"}:
                    if job.get("status") == "failed":
                        raise ConnectionError(f"peer multicast task failed: {job.get('error') or 'unknown error'}")
                    return job
                time.sleep(0.25)
            raise TimeoutError("peer multicast task did not finish in time")

        def task(context: JobContext) -> dict[str, Any]:
            remote_job_id = ""
            local_result: dict[str, Any] = {}
            local_error: list[Exception] = []
            remote_role = "receiver" if direction == "forward" else "sender"
            local_role = "sender" if direction == "forward" else "receiver"
            remote_duration = duration + 1 if remote_role == "receiver" else duration
            local_duration = duration + 1 if local_role == "receiver" else duration
            try:
                if direction == "forward":
                    started = remote_peer_call(
                        target,
                        ui_port,
                        token,
                        "/api/peer/multicast/start",
                        {"role": remote_role, "group": group, "port": port, "duration": remote_duration, "rate_mbps": rate},
                        timeout=4,
                    )
                    remote_job_id = str(started["job_id"])
                    time.sleep(0.45)
                    local_result = multicast_sender(
                        context, group, port, rate, local_duration, interface_ip=local_interface_ip,
                    )
                    remote_job = wait_peer_job(remote_job_id, context)
                else:
                    def receive_local() -> None:
                        try:
                            local_result.update(multicast_receiver(
                                context, group, port, local_duration, interface_ip=local_interface_ip,
                            ))
                        except Exception as exc:
                            local_error.append(exc)

                    receiver_thread = threading.Thread(target=receive_local, daemon=True)
                    receiver_thread.start()
                    time.sleep(0.45)
                    started = remote_peer_call(
                        target,
                        ui_port,
                        token,
                        "/api/peer/multicast/start",
                        {"role": remote_role, "group": group, "port": port, "duration": remote_duration, "rate_mbps": rate},
                        timeout=4,
                    )
                    remote_job_id = str(started["job_id"])
                    remote_job = wait_peer_job(remote_job_id, context)
                    receiver_thread.join(duration + 5)
                    if local_error:
                        raise local_error[0]
                    if receiver_thread.is_alive():
                        raise TimeoutError("local multicast receiver did not finish in time")
                remote_result = remote_job.get("result") or {}
                receiver_result = remote_result if remote_role == "receiver" else local_result
                sender_result = local_result if local_role == "sender" else remote_result
                return {
                    "mode": direction,
                    "peer": target,
                    "local_role": local_role,
                    "peer_role": remote_role,
                    "sender": sender_result,
                    "receiver": receiver_result,
                    "average_mbps": receiver_result.get("average_mbps"),
                    "packets": receiver_result.get("packets"),
                    "loss_percent": receiver_result.get("loss_percent"),
                    "out_of_order": receiver_result.get("out_of_order"),
                    "measured_at": utc_now(),
                    "evidence_note": "The peer receiver is started automatically before the sender; only the control side is clicked.",
                }
            finally:
                if context.cancelled and remote_job_id:
                    try:
                        remote_peer_call(
                            target, ui_port, token, "/api/peer/job/cancel", {"job_id": remote_job_id}, timeout=4,
                        )
                    except Exception:
                        pass

        job_id = self.state.jobs.start("paired_multicast", task)
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
