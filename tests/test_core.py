from __future__ import annotations

import json
from pathlib import Path
import socket
import struct
import sys
import threading
import time
import unittest
from urllib import request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from netlab_assist.config import Settings
from netlab_assist.jobs import JobManager
from netlab_assist.protocols import (
    TupleEchoHandler,
    ThroughputSinkHandler,
    dns_query,
    nat_tuple_probe,
    parse_dns_response,
    run_throughput_sender,
    start_sink,
)
from netlab_assist.server import create_servers


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConfigTests(unittest.TestCase):
    def test_random_access_code_and_port_validation(self) -> None:
        settings = Settings(ui_port=20001, throughput_port=20002, echo_port=20003).normalized()
        self.assertEqual(len(settings.access_code), 6)
        self.assertTrue(settings.access_code.isdigit())
        with self.assertRaises(ValueError):
            Settings(ui_port=80).normalized()


class JobTests(unittest.TestCase):
    def test_job_lifecycle(self) -> None:
        manager = JobManager()
        job_id = manager.start("unit", lambda context: {"value": 7})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = manager.get(job_id)
            if job and job["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(manager.get(job_id)["result"]["value"], 7)


class ProtocolTests(unittest.TestCase):
    def test_dns_a_response_parser(self) -> None:
        qname = b"\x03www\x07example\x03com\x00"
        question = qname + struct.pack("!HH", 1, 1)
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("192.0.2.10")
        packet = struct.pack("!HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0) + question + answer
        parsed = parse_dns_response(packet)
        self.assertEqual(parsed["response_transaction_id"], "0x1234")
        self.assertEqual(parsed["answers"][0]["value"], "192.0.2.10")
        self.assertEqual(parsed["rcode"], 0)

    def test_udp_dns_query_against_local_fixture(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]

        def reply() -> None:
            query, address = server.recvfrom(4096)
            txid = query[:2]
            response = (
                txid
                + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
                + query[12:]
                + b"\xc0\x0c"
                + struct.pack("!HHIH", 1, 1, 60, 4)
                + socket.inet_aton("192.0.2.25")
            )
            server.sendto(response, address)

        thread = threading.Thread(target=reply, daemon=True)
        thread.start()
        try:
            result = dns_query("127.0.0.1", "lab.example", txid=0x2468, port=port)
            self.assertEqual(result["server_port"], port)
            self.assertEqual(result["query_transaction_id"], "0x2468")
            self.assertEqual(result["answers"][0]["value"], "192.0.2.25")
        finally:
            server.close()
            thread.join(timeout=1)

    def test_authenticated_tuple_echo(self) -> None:
        port = free_port()
        server = start_sink(port, "654321", TupleEchoHandler)
        try:
            result = nat_tuple_probe("127.0.0.1", port, "654321")
            self.assertTrue(result["observed_peer"].startswith("127.0.0.1:"))
            self.assertEqual(result["server_local"], f"127.0.0.1:{port}")
            with self.assertRaises(PermissionError):
                nat_tuple_probe("127.0.0.1", port, "000000")
        finally:
            server.shutdown()
            server.server_close()

    def test_authenticated_tcp_throughput(self) -> None:
        port = free_port()
        server = start_sink(port, "123456", ThroughputSinkHandler)
        try:
            result = run_throughput_sender("127.0.0.1", port, "123456", duration=1, streams=2)
            self.assertEqual(result["streams_connected"], 2)
            self.assertGreater(result["bytes"], 0)
            self.assertGreater(result["average_mbps"], 0)
            self.assertTrue(result["samples"])
        finally:
            server.shutdown()
            server.server_close()


class APITests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            bind="127.0.0.1", ui_port=free_port(), throughput_port=free_port(), echo_port=free_port(),
            access_code="112233", open_browser=False,
        ).normalized()
        self.web, self.throughput, self.echo = create_servers(self.settings)
        self.thread = threading.Thread(target=self.web.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.web.shutdown(); self.throughput.shutdown(); self.echo.shutdown()
        self.web.server_close(); self.throughput.server_close(); self.echo.server_close()

    def get_json(self, path: str) -> dict:
        with request.urlopen(f"http://127.0.0.1:{self.settings.ui_port}{path}", timeout=3) as response:
            return json.loads(response.read())

    def post_json(self, path: str, body: dict) -> dict:
        req = request.Request(
            f"http://127.0.0.1:{self.settings.ui_port}{path}",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST",
        )
        with request.urlopen(req, timeout=3) as response:
            return json.loads(response.read())

    def test_status_static_and_probe(self) -> None:
        status = self.get_json("/api/status")
        self.assertEqual(status["access_code"], "112233")
        self.assertEqual(status["version"], "0.1.0")
        with request.urlopen(f"http://127.0.0.1:{self.settings.ui_port}/", timeout=3) as response:
            self.assertIn(b"NetLab Assist", response.read())
        probe = self.post_json("/api/probe", {"target": "127.0.0.1", "kind": "tcp", "port": self.settings.echo_port})
        self.assertTrue(probe["ok"])

    def test_bidirectional_throughput_job(self) -> None:
        started = self.post_json("/api/throughput", {
            "target": "127.0.0.1",
            "peer_token": self.settings.access_code,
            "direction": "bidirectional",
            "duration": 1,
            "streams": 1,
            "peer_ui_port": self.settings.ui_port,
            "peer_throughput_port": self.settings.throughput_port,
        })
        deadline = time.monotonic() + 12
        job = None
        while time.monotonic() < deadline:
            job = self.get_json(f"/api/jobs/{started['job_id']}")
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertIn("forward", job["result"]["results"])
        self.assertIn("reverse", job["result"]["results"])
        self.assertGreater(job["result"]["aggregate_average_mbps"], 0)


if __name__ == "__main__":
    unittest.main()
