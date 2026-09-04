from __future__ import annotations

from dataclasses import dataclass
import secrets


@dataclass(frozen=True)
class Settings:
    bind: str = "0.0.0.0"
    ui_port: int = 18080
    throughput_port: int = 18881
    echo_port: int = 18882
    access_code: str = ""
    open_browser: bool = True

    def normalized(self) -> "Settings":
        code = self.access_code.strip().upper() or f"{secrets.randbelow(1_000_000):06d}"
        return Settings(
            bind=self.bind,
            ui_port=_port(self.ui_port),
            throughput_port=_port(self.throughput_port),
            echo_port=_port(self.echo_port),
            access_code=code,
            open_browser=self.open_browser,
        )


def _port(value: int) -> int:
    value = int(value)
    if not 1024 <= value <= 65535:
        raise ValueError("ports must be between 1024 and 65535")
    return value


MAX_DURATION = 120
MAX_STREAMS = 16
MAX_MULTICAST_RATE_MBPS = 50.0
MAX_REQUEST_BYTES = 64 * 1024
