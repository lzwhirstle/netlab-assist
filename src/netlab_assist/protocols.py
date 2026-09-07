from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import os
import platform
import socket
import socketserver
import statistics
import struct
import subprocess
import threading
import time
from typing import Any
from urllib import error as urlerror, request

from .config import MAX_DURATION, MAX_MULTICAST_RATE_MBPS, MAX_STREAMS
from .jobs import JobContext, utc_now


BUFFER = b"N" * (128 * 1024)
DIRECT_HTTP_OPENER = request.build_opener(request.ProxyHandler({}))


def clamp_int(value: Any, low: int, high: int, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not low <= parsed <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return parsed


def clamp_float(value: Any, low: float, high: float, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(parsed) or not low <= parsed <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return parsed


def clean_host(value: Any) -> str:
    host = str(value or "").strip()
    if not host or len(host) > 253 or "://" in host or any(ch.isspace() for ch in host):
        raise ValueError("target must be an IP address or hostname")
    return host.strip("[]")


def local_ipv4_addresses() -> list[str]:
    addresses = {"127.0.0.1"}
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(item[4][0])
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))
        addresses.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(addresses, key=lambda value: (value.startswith("127."), value))


def route_ipv4_for(target: str, port: int) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, port))
        return sock.getsockname()[0]
    finally:
        sock.close()


def _read_line(sock: socket.socket, limit: int = 2048) -> bytes:
    data = bytearray()
    while len(data) < limit:
        part = sock.recv(1)
        if not part:
            break
        data.extend(part)
        if part == b"\n":
            break
    if len(data) >= limit:
        raise ValueError("handshake too long")
    return bytes(data)


class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ThroughputSinkHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.settimeout(5)
        try:
            line = _read_line(sock)
            prefix, raw = line.split(b" ", 1)
            if prefix != b"NLA1":
                return
            hello = json.loads(raw.decode("utf-8"))
            if not secrets_equal(str(hello.get("token", "")), self.server.access_code):
                sock.sendall(b"DENY\n")
                return
            sock.sendall(b"OK\n")
            sock.settimeout(3)
            idle_deadline = time.monotonic() + 10
            while True:
                try:
                    chunk = sock.recv(256 * 1024)
                except socket.timeout:
                    if time.monotonic() >= idle_deadline:
                        break
                    continue
                if not chunk:
                    break
                idle_deadline = time.monotonic() + 10
        except (OSError, ValueError, json.JSONDecodeError):
            return


class TupleEchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.settimeout(5)
        try:
            line = _read_line(sock)
            prefix, raw = line.split(b" ", 1)
            if prefix != b"NLAE":
                return
            hello = json.loads(raw.decode("utf-8"))
            if not secrets_equal(str(hello.get("token", "")), self.server.access_code):
                sock.sendall(b"{\"error\":\"access denied\"}\n")
                return
            payload = {
                "observed_peer": f"{self.client_address[0]}:{self.client_address[1]}",
                "server_local": f"{sock.getsockname()[0]}:{sock.getsockname()[1]}",
                "server_time": utc_now(),
            }
            sock.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        except (OSError, ValueError, json.JSONDecodeError):
            return


def secrets_equal(left: str, right: str) -> bool:
    # Constant-time behavior for the short per-run access code.
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def start_sink(port: int, access_code: str, handler: type[socketserver.BaseRequestHandler]) -> ReusableThreadingTCPServer:
    server = ReusableThreadingTCPServer(("0.0.0.0", port), handler)
    server.access_code = access_code
    threading.Thread(target=server.serve_forever, name=f"sink-{port}", daemon=True).start()
    return server


def run_throughput_sender(
    target: str,
    port: int,
    token: str,
    duration: int,
    streams: int,
    context: JobContext | None = None,
    label: str = "forward",
) -> dict[str, Any]:
    target = clean_host(target)
    duration = clamp_int(duration, 1, MAX_DURATION, "duration")
    streams = clamp_int(streams, 1, MAX_STREAMS, "streams")
    token = str(token or "").strip().upper()
    if not token:
        raise ValueError("peer access code is required")

    start_event = threading.Event()
    ready = threading.Condition()
    state = {"ready": 0, "bytes": 0}
    errors: list[str] = []
    lock = threading.Lock()
    sockets: list[socket.socket] = []
    common: dict[str, float] = {}

    def worker(index: int) -> None:
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((target, port), timeout=5)
            sock.settimeout(5)
            hello = {"token": token, "stream": index, "label": label}
            sock.sendall(b"NLA1 " + json.dumps(hello).encode("utf-8") + b"\n")
            if _read_line(sock) != b"OK\n":
                raise PermissionError("peer rejected the access code")
            with ready:
                sockets.append(sock)
                state["ready"] += 1
                ready.notify_all()
            start_event.wait(8)
            sock.settimeout(2)
            while time.monotonic() < common["deadline"]:
                if context and context.cancelled:
                    break
                sent = sock.send(BUFFER)
                with lock:
                    state["bytes"] += sent
        except Exception as exc:
            with lock:
                errors.append(f"stream {index + 1}: {type(exc).__name__}: {exc}")
            with ready:
                state["ready"] += 1
                ready.notify_all()
        finally:
            if sock:
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                sock.close()

    workers = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(streams)]
    for thread in workers:
        thread.start()

    with ready:
        ready.wait_for(lambda: state["ready"] >= streams, timeout=8)
    if not sockets:
        start_event.set()
        for thread in workers:
            thread.join(timeout=1)
        raise ConnectionError("no stream reached the peer: " + "; ".join(errors))

    started = time.monotonic()
    common["deadline"] = started + duration
    start_event.set()
    samples: list[dict[str, Any]] = []
    previous_bytes = 0
    previous_time = started
    while time.monotonic() < common["deadline"]:
        if context and context.cancelled:
            break
        time.sleep(min(0.5, max(0.01, common["deadline"] - time.monotonic())))
        now = time.monotonic()
        with lock:
            total = state["bytes"]
        delta_time = max(0.001, now - previous_time)
        mbps = (total - previous_bytes) * 8 / delta_time / 1_000_000
        sample = {"second": round(now - started, 3), "mbps": round(mbps, 3), "bytes": total, "direction": label}
        samples.append(sample)
        if context:
            context.update(progress=min(1.0, (now - started) / duration), sample=sample)
        previous_bytes = total
        previous_time = now

    for thread in workers:
        thread.join(timeout=4)
    finished = time.monotonic()
    with lock:
        total_bytes = state["bytes"]
    elapsed = max(0.001, finished - started)
    rates = [sample["mbps"] for sample in samples]
    return {
        "direction": label,
        "target": target,
        "port": port,
        "streams_requested": streams,
        "streams_connected": len(sockets),
        "duration_requested_s": duration,
        "elapsed_s": round(elapsed, 3),
        "bytes": total_bytes,
        "average_mbps": round(total_bytes * 8 / elapsed / 1_000_000, 3),
        "minimum_sample_mbps": round(min(rates), 3) if rates else 0.0,
        "maximum_sample_mbps": round(max(rates), 3) if rates else 0.0,
        "samples": samples,
        "errors": errors,
        "cancelled": bool(context and context.cancelled),
        "measured_at": utc_now(),
    }


def remote_throughput(
    peer: str,
    ui_port: int,
    peer_token: str,
    sink_token: str,
    sink_port: int,
    duration: int,
    streams: int,
) -> dict[str, Any]:
    return remote_peer_call(
        peer,
        ui_port,
        peer_token,
        "/api/peer/throughput",
        {
            "sink_token": sink_token,
            "sink_port": sink_port,
            "duration": duration,
            "streams": streams,
        },
        timeout=duration + 15,
    )


def remote_peer_call(
    peer: str,
    ui_port: int,
    peer_token: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    peer = clean_host(peer)
    ui_port = clamp_int(ui_port, 1024, 65535, "peer UI port")
    peer_token = str(peer_token or "").strip().upper()
    if not peer_token:
        raise ValueError("peer access code is required")
    payload = json.dumps(body or {}).encode("utf-8")
    req = request.Request(
        f"http://{peer}:{ui_port}{path}",
        data=payload,
        headers={"Content-Type": "application/json", "X-Lab-Token": peer_token},
        method="POST",
    )
    try:
        with DIRECT_HTTP_OPENER.open(req, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        status_code = exc.code
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            message = str(payload.get("error") or f"peer returned HTTP {status_code}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            message = f"peer returned HTTP {status_code}"
        finally:
            exc.close()
        if status_code == 403 and "local test controls" in message.lower():
            raise ConnectionError("peer version is incompatible; upgrade both computers") from exc
        if status_code == 403:
            raise PermissionError("peer rejected the access code") from exc
        if status_code == 404:
            raise ConnectionError("peer version is incompatible; upgrade both computers") from exc
        raise ConnectionError(message) from exc
    except urlerror.URLError as exc:
        reason = exc.reason
        reason_errno = getattr(reason, "errno", None)
        if isinstance(reason, ConnectionRefusedError) or reason_errno in {61, 111, 10061}:
            raise ConnectionRefusedError("peer control port refused the connection") from exc
        if isinstance(reason, (socket.timeout, TimeoutError)) or reason_errno in {60, 110, 10060}:
            raise TimeoutError("peer control port timed out") from exc
        raise ConnectionError(f"peer control connection failed: {reason}") from exc
    except socket.timeout as exc:
        raise TimeoutError("peer control port timed out") from exc
    except json.JSONDecodeError as exc:
        raise ConnectionError("peer returned an invalid response") from exc
    if not isinstance(parsed, dict):
        raise ConnectionError("peer returned an invalid response")
    return parsed


def throughput_sink_probe(target: str, port: int, token: str, timeout: float = 4.0) -> dict[str, Any]:
    target = clean_host(target)
    port = clamp_int(port, 1024, 65535, "throughput port")
    started = time.monotonic()
    with socket.create_connection((target, port), timeout=timeout) as sock:
        local = f"{sock.getsockname()[0]}:{sock.getsockname()[1]}"
        remote = f"{sock.getpeername()[0]}:{sock.getpeername()[1]}"
        sock.settimeout(timeout)
        hello = {"token": str(token or "").strip().upper(), "stream": 0, "label": "readiness"}
        sock.sendall(b"NLA1 " + json.dumps(hello).encode("utf-8") + b"\n")
        response = _read_line(sock)
    if response == b"DENY\n":
        raise PermissionError("peer rejected the access code")
    if response != b"OK\n":
        raise ConnectionError("peer throughput service returned an invalid response")
    return {
        "ok": True,
        "local": local,
        "remote": remote,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }


def nat_tuple_probe(target: str, port: int, token: str, timeout: float = 5.0) -> dict[str, Any]:
    target = clean_host(target)
    started = time.monotonic()
    with socket.create_connection((target, port), timeout=timeout) as sock:
        local = f"{sock.getsockname()[0]}:{sock.getsockname()[1]}"
        destination = f"{sock.getpeername()[0]}:{sock.getpeername()[1]}"
        hello = {"token": str(token or "").strip().upper()}
        sock.sendall(b"NLAE " + json.dumps(hello).encode("utf-8") + b"\n")
        raw = _read_line(sock, limit=4096)
    result = json.loads(raw.decode("utf-8"))
    if "error" in result:
        raise PermissionError(result["error"])
    result.update({
        "client_local": local,
        "client_destination": destination,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    })
    return result


def tcp_probe(target: str, port: int, timeout: float = 2.0) -> dict[str, Any]:
    target = clean_host(target)
    port = clamp_int(port, 1, 65535, "port")
    timeout = clamp_float(timeout, 0.2, 10.0, "timeout")
    started = time.monotonic()
    try:
        with socket.create_connection((target, port), timeout=timeout) as sock:
            local = f"{sock.getsockname()[0]}:{sock.getsockname()[1]}"
            remote = f"{sock.getpeername()[0]}:{sock.getpeername()[1]}"
        return {"ok": True, "classification": "connected", "local": local, "remote": remote, "error": None,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
    except ConnectionRefusedError as exc:
        classification = "refused"
        error = str(exc)
    except socket.timeout as exc:
        classification = "timeout"
        error = str(exc) or "timeout"
    except socket.gaierror as exc:
        classification = "dns_error"
        error = str(exc)
    except OSError as exc:
        classification = "network_error"
        error = str(exc)
    return {"ok": False, "classification": classification, "error": error,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}


def ping_probe(target: str, count: int = 3, timeout: float = 2.0) -> dict[str, Any]:
    target = clean_host(target)
    count = clamp_int(count, 1, 10, "count")
    timeout = clamp_float(timeout, 0.2, 10.0, "timeout")
    system = platform.system()
    if system == "Windows":
        command = ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), target]
    elif system == "Darwin":
        command = ["ping", "-c", str(count), "-W", str(int(timeout * 1000)), target]
    else:
        command = ["ping", "-c", str(count), "-W", str(max(1, math.ceil(timeout))), target]
    started = time.monotonic()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=(count * timeout) + 5, check=False)
        output = (completed.stdout + completed.stderr).strip()
        return_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        raw = (exc.stdout or "") + (exc.stderr or "")
        output = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
        output = (output + "\nping process timed out").strip()
        return_code = -1
    return {
        "ok": return_code == 0,
        "classification": "replied" if return_code == 0 else "no_reply",
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "return_code": return_code,
        "output": output[-4000:],
    }


def _encode_dns_name(name: str) -> bytes:
    name = name.strip().rstrip(".")
    if not name or len(name) > 253:
        raise ValueError("invalid DNS name")
    encoded = bytearray()
    for label in name.split("."):
        raw = label.encode("idna")
        if not 1 <= len(raw) <= 63:
            raise ValueError("invalid DNS label")
        encoded.append(len(raw))
        encoded.extend(raw)
    encoded.append(0)
    return bytes(encoded)


def _read_dns_name(data: bytes, offset: int, depth: int = 0) -> tuple[str, int]:
    if depth > 20:
        raise ValueError("DNS compression loop")
    labels: list[str] = []
    consumed = offset
    jumped = False
    while True:
        if offset >= len(data):
            raise ValueError("truncated DNS name")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            suffix, _ = _read_dns_name(data, pointer, depth + 1)
            if suffix:
                labels.append(suffix)
            if not jumped:
                consumed = offset + 2
            jumped = True
            break
        offset += 1
        if length == 0:
            if not jumped:
                consumed = offset
            break
        if offset + length > len(data):
            raise ValueError("truncated DNS label")
        labels.append(data[offset:offset + length].decode("ascii", errors="replace"))
        offset += length
        if not jumped:
            consumed = offset
    return ".".join(labels), consumed


def dns_query(server: str, name: str, qtype: str = "A", transport: str = "udp", txid: int | None = None,
              timeout: float = 4.0, port: int = 53) -> dict[str, Any]:
    server = clean_host(server)
    qtype = str(qtype).upper()
    type_code = {"A": 1, "AAAA": 28}.get(qtype)
    if not type_code:
        raise ValueError("qtype must be A or AAAA")
    transport = str(transport).lower()
    if transport not in {"udp", "tcp"}:
        raise ValueError("transport must be udp or tcp")
    if txid is None:
        txid = int.from_bytes(os.urandom(2), "big")
    txid = clamp_int(txid, 0, 65535, "transaction ID")
    port = clamp_int(port, 1, 65535, "DNS port")
    message = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0) + _encode_dns_name(name) + struct.pack("!HH", type_code, 1)
    family, socktype, proto, _, sockaddr = socket.getaddrinfo(server, port, 0, socket.SOCK_DGRAM if transport == "udp" else socket.SOCK_STREAM)[0]
    started = time.monotonic()
    with socket.socket(family, socktype, proto) as sock:
        sock.settimeout(timeout)
        if transport == "udp":
            sock.connect(sockaddr)
            source = sock.getsockname()
            sock.send(message)
            response = sock.recv(65535)
        else:
            sock.connect(sockaddr)
            source = sock.getsockname()
            sock.sendall(struct.pack("!H", len(message)) + message)
            length = struct.unpack("!H", _recv_exact(sock, 2))[0]
            response = _recv_exact(sock, length)
    elapsed = (time.monotonic() - started) * 1000
    parsed = parse_dns_response(response)
    parsed.update({
        "server": server,
        "server_port": port,
        "name": name.rstrip("."),
        "qtype": qtype,
        "transport": transport.upper(),
        "query_transaction_id": f"0x{txid:04x}",
        "source_ip": source[0],
        "source_port": source[1],
        "query_bytes": len(message) + (2 if transport == "tcp" else 0),
        "response_bytes": len(response) + (2 if transport == "tcp" else 0),
        "elapsed_ms": round(elapsed, 3),
        "measured_at": utc_now(),
    })
    return parsed


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("connection closed before the DNS response completed")
        data.extend(chunk)
    return bytes(data)


def parse_dns_response(data: bytes) -> dict[str, Any]:
    if len(data) < 12:
        raise ValueError("truncated DNS response")
    txid, flags, questions, answers, authority, additional = struct.unpack("!HHHHHH", data[:12])
    offset = 12
    for _ in range(questions):
        _, offset = _read_dns_name(data, offset)
        offset += 4
        if offset > len(data):
            raise ValueError("truncated DNS question")
    records: list[dict[str, Any]] = []
    for _ in range(answers):
        record_name, offset = _read_dns_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError("truncated DNS answer")
        record_type, record_class, ttl, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        raw = data[offset:offset + length]
        offset += length
        value: str
        if record_type == 1 and len(raw) == 4:
            value = socket.inet_ntop(socket.AF_INET, raw)
        elif record_type == 28 and len(raw) == 16:
            value = socket.inet_ntop(socket.AF_INET6, raw)
        elif record_type in {2, 5, 12}:
            value, _ = _read_dns_name(data, offset - length)
        else:
            value = raw.hex()
        records.append({"name": record_name, "type": record_type, "class": record_class, "ttl": ttl, "value": value})
    return {
        "response_transaction_id": f"0x{txid:04x}",
        "rcode": flags & 0x000F,
        "truncated": bool(flags & 0x0200),
        "recursive_available": bool(flags & 0x0080),
        "question_count": questions,
        "answer_count": answers,
        "authority_count": authority,
        "additional_count": additional,
        "answers": records,
    }


@dataclass
class MulticastPacket:
    session: int
    sequence: int
    sent_ns: int


def multicast_sender(context: JobContext, group: str, port: int, rate_mbps: float, duration: int,
                     packet_size: int = 1200, interface_ip: str | None = None) -> dict[str, Any]:
    address = ipaddress.ip_address(group)
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_multicast:
        raise ValueError("group must be an IPv4 multicast address")
    port = clamp_int(port, 1024, 65535, "port")
    rate_mbps = clamp_float(rate_mbps, 0.05, MAX_MULTICAST_RATE_MBPS, "rate")
    duration = clamp_int(duration, 1, 600, "duration")
    packet_size = clamp_int(packet_size, 128, 1400, "packet size")
    session = int.from_bytes(os.urandom(4), "big")
    header = struct.Struct("!4sIIQ")
    padding = b"M" * (packet_size - header.size)
    interval = packet_size * 8 / (rate_mbps * 1_000_000)
    interface_bytes = None
    if interface_ip:
        interface = ipaddress.ip_address(interface_ip)
        if not isinstance(interface, ipaddress.IPv4Address):
            raise ValueError("multicast interface must be an IPv4 address")
        interface_bytes = socket.inet_aton(str(interface))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 16)
    if interface_bytes:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, interface_bytes)
    started = time.monotonic()
    deadline = started + duration
    next_send = started
    sequence = 0
    bytes_sent = 0
    last_sample_time = started
    last_sample_bytes = 0
    try:
        while time.monotonic() < deadline and not context.cancelled:
            now = time.monotonic()
            if now < next_send:
                time.sleep(min(0.002, next_send - now))
                continue
            payload = header.pack(b"NLA1", session, sequence, time.time_ns()) + padding
            sock.sendto(payload, (str(address), port))
            sequence += 1
            bytes_sent += len(payload)
            next_send += interval
            if now - last_sample_time >= 1:
                sample_mbps = (bytes_sent - last_sample_bytes) * 8 / (now - last_sample_time) / 1_000_000
                context.update(progress=(now - started) / duration,
                               sample={"second": round(now - started, 3), "mbps": round(sample_mbps, 3), "packets": sequence})
                last_sample_time = now
                last_sample_bytes = bytes_sent
    finally:
        sock.close()
    elapsed = max(0.001, time.monotonic() - started)
    return {"role": "sender", "group": str(address), "port": port, "interface_ip": interface_ip, "session": session, "packets": sequence,
            "bytes": bytes_sent, "elapsed_s": round(elapsed, 3), "average_mbps": round(bytes_sent * 8 / elapsed / 1_000_000, 3),
            "cancelled": context.cancelled, "measured_at": utc_now()}


def multicast_receiver(context: JobContext, group: str, port: int, duration: int,
                       interface_ip: str | None = None) -> dict[str, Any]:
    address = ipaddress.ip_address(group)
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_multicast:
        raise ValueError("group must be an IPv4 multicast address")
    port = clamp_int(port, 1024, 65535, "port")
    duration = clamp_int(duration, 1, 600, "duration")
    header = struct.Struct("!4sIIQ")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", port))
        if interface_ip:
            interface = ipaddress.ip_address(interface_ip)
            if not isinstance(interface, ipaddress.IPv4Address):
                raise ValueError("multicast interface must be an IPv4 address")
            membership_interface = str(interface)
        else:
            membership_interface = "0.0.0.0"
        membership = socket.inet_aton(str(address)) + socket.inet_aton(membership_interface)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.settimeout(0.25)
        started = time.monotonic()
        deadline = started + duration
        packets = 0
        bytes_received = 0
        lost = 0
        out_of_order = 0
        expected_by_session: dict[int, int] = {}
        delays_ms: list[float] = []
        last_sample_time = started
        last_sample_bytes = 0
        while time.monotonic() < deadline and not context.cancelled:
            try:
                payload, _ = sock.recvfrom(65535)
            except socket.timeout:
                payload = b""
            now = time.monotonic()
            if len(payload) >= header.size:
                magic, session, sequence, sent_ns = header.unpack(payload[:header.size])
                if magic == b"NLA1":
                    expected = expected_by_session.get(session, sequence)
                    if sequence > expected:
                        lost += sequence - expected
                    elif sequence < expected:
                        out_of_order += 1
                    expected_by_session[session] = max(expected, sequence + 1)
                    packets += 1
                    bytes_received += len(payload)
                    delay = max(0.0, (time.time_ns() - sent_ns) / 1_000_000)
                    if len(delays_ms) < 10000:
                        delays_ms.append(delay)
            if now - last_sample_time >= 1:
                sample_mbps = (bytes_received - last_sample_bytes) * 8 / (now - last_sample_time) / 1_000_000
                context.update(progress=(now - started) / duration,
                               sample={"second": round(now - started, 3), "mbps": round(sample_mbps, 3), "packets": packets, "lost": lost})
                last_sample_time = now
                last_sample_bytes = bytes_received
        elapsed = max(0.001, time.monotonic() - started)
        total_expected = packets + lost
        return {
            "role": "receiver", "group": str(address), "port": port, "interface_ip": interface_ip, "packets": packets, "lost": lost,
            "out_of_order": out_of_order, "loss_percent": round(lost * 100 / total_expected, 4) if total_expected else 0.0,
            "bytes": bytes_received, "elapsed_s": round(elapsed, 3),
            "average_mbps": round(bytes_received * 8 / elapsed / 1_000_000, 3),
            "average_delay_ms": round(statistics.fmean(delays_ms), 3) if delays_ms else None,
            "delay_jitter_ms": round(statistics.pstdev(delays_ms), 3) if len(delays_ms) > 1 else 0.0 if delays_ms else None,
            "sessions": sorted(expected_by_session), "cancelled": context.cancelled, "measured_at": utc_now(),
        }
    finally:
        sock.close()


def reachability_monitor(context: JobContext, target: str, port: int, interval: float, duration: int) -> dict[str, Any]:
    target = clean_host(target)
    port = clamp_int(port, 1, 65535, "port")
    interval = clamp_float(interval, 0.1, 10.0, "interval")
    duration = clamp_int(duration, 1, 1800, "duration")
    started = time.monotonic()
    deadline = started + duration
    checks = 0
    successes = 0
    transitions: list[dict[str, Any]] = []
    last_state: bool | None = None
    outage_started: float | None = None
    outages: list[float] = []
    while time.monotonic() < deadline and not context.cancelled:
        result = tcp_probe(target, port, timeout=min(2.0, max(0.2, interval)))
        now = time.monotonic()
        checks += 1
        successes += int(result["ok"])
        if result["ok"] != last_state:
            transitions.append({"second": round(now - started, 3), "up": result["ok"], "classification": result["classification"]})
            if not result["ok"]:
                outage_started = now
            elif outage_started is not None:
                outages.append(now - outage_started)
                outage_started = None
            last_state = result["ok"]
        context.update(progress=(now - started) / duration,
                       sample={"second": round(now - started, 3), "up": result["ok"], "elapsed_ms": result["elapsed_ms"]})
        time.sleep(interval)
    if outage_started is not None:
        outages.append(time.monotonic() - outage_started)
    return {
        "target": target, "port": port, "checks": checks, "successes": successes,
        "availability_percent": round(successes * 100 / checks, 3) if checks else 0.0,
        "transitions": transitions, "outages_s": [round(item, 3) for item in outages],
        "longest_outage_s": round(max(outages), 3) if outages else 0.0,
        "cancelled": context.cancelled, "measured_at": utc_now(),
    }
