"""Require external HTTP transports to be mocked while allowing loopback integration tests."""
import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    def allow_loopback(host):
        if host == 'localhost':
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise AssertionError('External network access blocked. Mock the transport in the test.')

    resolve = socket.getaddrinfo

    def guarded_lookup(host, *args, **kwargs):
        if host is not None:
            allow_loopback(host)
        return resolve(host, *args, **kwargs)

    def guard(method):
        def guarded_connection(sock, address):
            if isinstance(address, tuple):
                allow_loopback(address[0])
            return method(sock, address)
        return guarded_connection

    monkeypatch.setattr(socket, 'getaddrinfo', guarded_lookup)
    monkeypatch.setattr(socket.socket, 'connect', guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', guard(socket.socket.connect_ex))
