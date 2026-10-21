"""Dedicated v3 transport: one HTTP attempt, public pinned TLS, bounded reads."""
from contextlib import contextmanager
from decimal import Decimal
import http.client
import ipaddress
import io
import json
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from .asset_provider import AssetJobError

JSON_LIMIT = 1024 * 1024


class TripoError(AssetJobError):
    def __init__(self, code, *, status=None):
        super().__init__(code, "Tripo action refused; original attempt is not replayed")
        self.status = status


class Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def remaining(self):
        seconds = self.end - time.monotonic()
        if seconds <= 0:
            raise TripoError("tripo_timeout")
        return seconds


def public_target(url, resolver):
    if (not isinstance(url, str) or len(url) > 16384
            or any(ord(c) < 33 or ord(c) == 127 for c in url)):
        raise TripoError("tripo_unsafe_url")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port not in {None, 443}):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9.:-]{1,253}", host) or host.endswith("."):
            raise ValueError
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            raise ValueError
        answers = resolver(host)
        if not isinstance(answers, (list, tuple)) or not 1 <= len(answers) <= 64:
            raise ValueError
        addresses = [ipaddress.ip_address(answer) for answer in answers]
        if not all(address.is_global for address in addresses):
            raise ValueError
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        return host, [str(address) for address in addresses], path
    except (ValueError, UnicodeError, OSError):
        raise TripoError("tripo_unsafe_url") from None


def system_addresses(host, deadline):
    # getaddrinfo itself has no timeout. The bounded caller does not wait for
    # a stuck daemon lookup; no unbounded executor shutdown or second lookup.
    result = queue.Queue(maxsize=1)
    def lookup():
        try:
            result.put([row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)])
        except OSError:
            result.put(None)
    threading.Thread(target=lookup, daemon=True).start()
    try:
        addresses = result.get(timeout=deadline.remaining())
    except queue.Empty:
        raise TripoError("tripo_timeout") from None
    if addresses is None:
        raise TripoError("tripo_dns_failed")
    return addresses


def pinned_connection(host, address, timeout):
    deadline = Deadline(timeout)
    connection = http.client.HTTPSConnection(host, 443, timeout=timeout)
    sock = socket.create_connection((address, 443), timeout=deadline.remaining())
    try:
        sock.settimeout(deadline.remaining())
        connection.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    except Exception:
        sock.close()
        raise
    return connection


class TimedBody(io.BytesIO):
    def __init__(self, body, sock, deadline):
        super().__init__(body)
        self.sock, self.deadline = sock, deadline

    def read(self, size=-1):
        self.sock.settimeout(self.deadline.remaining())
        return super().read(size)


class TimedResponse:
    def __init__(self, response, sock, deadline):
        self.response, self.sock, self.deadline = response, sock, deadline
        self.status, self.headers = response.status, response.headers

    def read(self, size):
        if self.response.isclosed():
            return b""
        self.sock.settimeout(self.deadline.remaining())
        return self.response.read1(size)


class PinnedTransport:
    def __init__(self, *, resolver=None, connector=pinned_connection):
        # These are trusted host constructor dependencies, never profile/env
        # switches. Tests use a loopback connector with synthetic public DNS.
        self.resolver, self.connector = resolver, connector

    @contextmanager
    def open(self, method, url, *, headers=None, body=None, timeout=60):
        deadline = Deadline(timeout)
        host, addresses, path = public_target(url, self.resolver or (lambda value: system_addresses(value, deadline)))
        connection = None
        try:
            connection = self.connector(host, addresses[0], deadline.remaining())
            sock = connection.sock
            if sock is None:
                raise TripoError("tripo_connection_failed")
            sock.settimeout(deadline.remaining())
            request_headers = {"Host": host, **(headers or {})}
            if body is not None:
                request_headers["Content-Length"] = str(len(body))
                body = TimedBody(body, sock, deadline)
            connection.request(method, path, body=body, headers=request_headers)
            sock.settimeout(deadline.remaining())
            response = connection.getresponse()
            try:
                yield TimedResponse(response, sock, deadline)
            finally:
                response.close()
        except TripoError:
            raise
        except (OSError, http.client.HTTPException):
            raise TripoError("tripo_connection_failed") from None
        finally:
            if connection is not None:
                connection.close()


def chunks(response, limit):
    length = response.headers.get("Content-Length")
    if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > limit):
        raise TripoError("tripo_size_limit")
    total = 0
    while True:
        block = response.read(min(65536, limit + 1 - total))
        if not block:
            break
        total += len(block)
        if total > limit:
            raise TripoError("tripo_size_limit")
        yield block
    if length is not None and total != int(length):
        raise TripoError("tripo_truncated_response")


def json_body(response):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        raw = b"".join(chunks(response, JSON_LIMIT))
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_float=Decimal,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        if not isinstance(document, dict):
            raise ValueError
        return document
    except (ValueError, UnicodeError, RecursionError):
        raise TripoError("tripo_protocol") from None
