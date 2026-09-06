from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event, Thread
from urllib.parse import urlsplit

import pytest

import hugin.adapters.hh_browser as hh_browser
from hugin.adapters.hh_browser import _HhHttpProxy


@contextmanager
def running_proxy() -> Iterator[tuple[_HhHttpProxy, int]]:
    proxy = _HhHttpProxy("127.0.0.1")
    proxy.start()
    try:
        port = urlsplit(proxy.pac_url).port
        assert port is not None
        yield proxy, port
    finally:
        proxy.stop()


def receive_all(connection: socket.socket) -> bytes:
    parts = []
    while part := connection.recv(4096):
        parts.append(part)
    return b"".join(parts)


def test_proxy_configuration_is_local_and_restart_uses_a_working_listener() -> None:
    proxy = _HhHttpProxy("127.0.0.1")
    with pytest.raises(RuntimeError):
        _ = proxy.pac_url
    for _ in range(2):
        proxy.start()
        try:
            url = urlsplit(proxy.pac_url)
            assert url.hostname == "127.0.0.1" and url.port is not None
            proxy.start()
            with socket.create_connection((url.hostname, url.port), timeout=3) as client:
                client.sendall(b"GET /proxy.pac HTTP/1.1\r\nHost: localhost\r\n\r\n")
                headers, body = receive_all(client).split(b"\r\n\r\n", 1)
            assert headers.startswith(b"HTTP/1.1 200 OK")
            assert f"Content-Length: {len(body)}".encode() in headers
            assert b"Cache-Control: no-store" in headers
            assert b'host === "hh.ru"' in body
            assert b'return "DIRECT"' in body
        finally:
            proxy.stop()
        with pytest.raises(RuntimeError):
            _ = proxy.pac_url


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"CONNECT hh.ru.example.org:443 HTTP/1.1\r\n\r\n",
        b"CONNECT hh.ru:80 HTTP/1.1\r\n\r\n",
        b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n",
        b"GET https://hh.ru/ HTTP/1.1\r\n\r\n",
        b"CONNECT hh.ru:443\r\n\r\n",
    ],
)
def test_proxy_rejects_wrong_hosts_ports_and_methods_before_resolution(
    request_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved: list[str] = []

    def resolve(_self: _HhHttpProxy, host: str) -> str:
        resolved.append(host)
        raise AssertionError("Rejected request must not reach DNS")

    monkeypatch.setattr(_HhHttpProxy, "_resolve_host", resolve)
    with (
        running_proxy() as (_, port),
        socket.create_connection(("127.0.0.1", port), timeout=3) as client,
    ):
        client.sendall(request_bytes)
        assert receive_all(client).startswith(b"HTTP/1.1 403 Forbidden")
    assert resolved == []


def test_proxy_transfers_both_directions_and_releases_live_connections_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[bytes] = []
    finished = Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as upstream:
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        upstream.settimeout(3)
        port = upstream.getsockname()[1]

        def serve() -> None:
            try:
                connection, _ = upstream.accept()
                with connection:
                    connection.settimeout(3)
                    received.append(connection.recv(4096))
                    connection.sendall(b"confirmed locally")
                    received.append(connection.recv(4096))
            finally:
                finished.set()

        monkeypatch.setattr(hh_browser, "_HH_HTTPS_PORT", port)
        monkeypatch.setattr(_HhHttpProxy, "_resolve_host", lambda _self, _host: "127.0.0.1")
        thread = Thread(target=serve, daemon=True)
        thread.start()
        with running_proxy() as (proxy, proxy_port):
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=3) as client:
                client.sendall(f"CONNECT hh.ru:{port} HTTP/1.1\r\n\r\n".encode())
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    response.extend(client.recv(4096))
                assert response.startswith(b"HTTP/1.1 200 Connection Established")
                client.sendall(b"local evidence")
                assert client.recv(4096) == b"confirmed locally"
                proxy.stop()
                assert client.recv(4096) == b""
            assert finished.wait(3)
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert received == [b"local evidence", b""]


def test_proxy_rejects_oversized_header_without_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _HhHttpProxy, "_resolve_host", lambda _self, _host: pytest.fail("Unexpected resolution")
    )
    with (
        running_proxy() as (_, port),
        socket.create_connection(("127.0.0.1", port), timeout=3) as client,
    ):
        client.sendall(b"CONNECT hh.ru:443 HTTP/1.1\r\nX-Padding: " + b"x" * 20000)
        try:
            result = client.recv(4096)
        except ConnectionResetError:
            result = b""
        assert result == b""
