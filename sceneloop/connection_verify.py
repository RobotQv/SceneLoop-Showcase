"""Explicit, read-only connection checks. Never invokes a generation provider.

The outer daemon wait bounds DNS, TLS and slow HTTP headers by one deadline;
the same deadline also guards every request/body read. Transport dependencies
are trusted constructor arguments, never options accepted from a profile.
"""
from decimal import Decimal
import http.client
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import threading
from urllib.parse import urlsplit, urlunsplit

from .tripo_transport import Deadline, TimedResponse, TripoError, chunks, public_target

CHECK_TIMEOUT = 20
BODY_LIMIT = 256 * 1024
REASONS = {
    "readonly_authenticated": "authenticated",
    "network_reachable": "reachable", "local_paths_present": "reachable",
    "unsupported_readonly_check": "inconclusive", "check_timeout": "inconclusive",
    "invalid_check_response": "inconclusive",
    **dict.fromkeys(("authentication_rejected", "access_denied", "rate_limited", "service_error",
                     "dns_failed", "tls_failed", "connection_failed", "unsafe_target",
                     "local_path_missing", "missing_credential"), "failed"),
}


def conclusion(code, http_status=None):
    return {"status": REASONS[code], "code": code, "http_status": http_status}


def safe_conclusion(value):
    """Only an explicit complete result can authenticate; discard arbitrary data."""
    if (type(value) is not dict or set(value) != {"status", "code", "http_status"}
            or type(value.get("code")) is not str or value["code"] not in REASONS
            or type(value.get("status")) is not str or value["status"] != REASONS[value["code"]]
            or (value["http_status"] is not None
                and (type(value["http_status"]) is not int or not 100 <= value["http_status"] <= 599))
            or (value["status"] == "authenticated" and value["http_status"] != 200)):
        return conclusion("invalid_check_response")
    expected = {"authentication_rejected": 401, "access_denied": 403, "rate_limited": 429}
    if value["code"] in expected and value["http_status"] != expected[value["code"]]:
        return conclusion("invalid_check_response")
    return dict(value)


class _Failure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _Resources:
    def __init__(self):
        self.lock = threading.Lock()
        self.sockets = []
        self.cancelled = False

    def add(self, sock):
        with self.lock:
            if self.cancelled:
                sock.close()
                raise _Failure("check_timeout")
            self.sockets.append(sock)

    def close(self):
        with self.lock:
            self.cancelled = True
            sockets, self.sockets = self.sockets, []
        # Interrupt slow header/body reads without waiting for a daemon join.
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def _connect(host, address, port, scheme, deadline, resources):
    # Numeric IP only: the checked host is not resolved again. SNI/certificate
    # verification still uses the original host. No proxy or redirect handler.
    sock = socket.create_connection((address, port), timeout=deadline.remaining())
    resources.add(sock)
    if scheme == "https":
        sock.settimeout(deadline.remaining())
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        resources.add(sock)
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    connection = cls(host, port, timeout=deadline.remaining())
    connection.sock = sock
    return connection


def _addresses(host):
    return [row[4][0] for row in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


def _credential(profile):
    name = profile.get("credential_env", "")
    mode = profile.get("credential_mode") or ("env" if name else "none")
    if mode in {"stored", "env"} and "_resolved_credential" in profile:
        key = profile.get("_resolved_credential")
    elif mode == "env" and isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
        key = os.environ.get(name)
    else:
        key = None
    if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 4096
                            or any(not 33 <= ord(c) <= 126 for c in key)):
        return None
    return key


def _models(document, official):
    if (not isinstance(document, dict) or document.get("object") != "list"
            or not isinstance(document.get("data"), list) or not 1 <= len(document["data"]) <= 2048):
        return False
    for item in document["data"]:
        if not isinstance(item, dict):
            return False
        identifier = item.get("id")
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 256
                or any(ord(c) < 32 for c in identifier)):
            return False
        if official and (item.get("object") != "model" or not isinstance(item.get("owned_by"), str)
                         or not item["owned_by"].strip() or len(item["owned_by"]) > 256):
            return False
        if official == "openai" and (type(item.get("created")) is not int or item["created"] < 0):
            return False
    return True


def _balance(document):
    if not isinstance(document, dict) or type(document.get("code")) is not int or document["code"] != 0:
        return False
    data = document.get("data")
    if not isinstance(data, dict):
        return False
    return all(type(data.get(key)) in {int, Decimal} and Decimal(data[key]).is_finite()
               for key in ("balance", "frozen"))


def _json(response):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    raw = b"".join(chunks(response, BODY_LIMIT))
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_float=Decimal,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


class ConnectionVerifier:
    def __init__(self, *, resolver=None, connector=None, timeout=CHECK_TIMEOUT):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= CHECK_TIMEOUT:
            raise ValueError("Invalid connection check timeout")
        self.resolver = resolver or _addresses
        self.connector = connector or _connect
        self.timeout = timeout

    def verify(self, profile):
        deadline, resources, results = Deadline(self.timeout), _Resources(), queue.Queue(maxsize=1)
        def work():
            try:
                value = self._check(dict(profile), deadline, resources)
                deadline.remaining()
            except _Failure as exc:
                value = conclusion(exc.code)
            except TripoError as exc:
                value = conclusion("check_timeout" if exc.code == "tripo_timeout" else
                                   "unsafe_target" if exc.code == "tripo_unsafe_url" else "invalid_check_response")
            except (TimeoutError, socket.timeout):
                value = conclusion("check_timeout")
            except socket.gaierror:
                value = conclusion("dns_failed")
            except ssl.SSLError:
                value = conclusion("tls_failed")
            except (ValueError, UnicodeError, RecursionError, http.client.IncompleteRead):
                value = conclusion("invalid_check_response")
            except http.client.HTTPException:
                value = conclusion("invalid_check_response")
            except OSError:
                value = conclusion("connection_failed")
            except Exception:
                value = conclusion("invalid_check_response")
            finally:
                resources.close()
            results.put(value)
        threading.Thread(target=work, daemon=True).start()
        try:
            return safe_conclusion(results.get(timeout=deadline.remaining()))
        except (queue.Empty, TripoError):
            resources.close()
            return conclusion("check_timeout")

    def _target(self, base, deadline):
        if (not isinstance(base, str) or not 1 <= len(base) <= 2048 or "\\" in base
                or any(ord(c) < 33 or ord(c) == 127 for c in base)):
            raise _Failure("unsafe_target")
        try:
            parsed = urlsplit(base)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment or "?" in base or "#" in base):
                raise ValueError
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not 1 <= port <= 65535:
                raise ValueError
            host = parsed.hostname.encode("idna").decode("ascii").lower()
            if not re.fullmatch(r"[a-z0-9.:-]{1,253}", host) or host.endswith("."):
                raise ValueError
            local = host == "localhost"
            try:
                local = local or ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
            if parsed.scheme == "http" and not local:
                raise ValueError
        except (ValueError, UnicodeError):
            raise _Failure("unsafe_target") from None
        deadline.remaining()
        try:
            addresses = self.resolver(host)
        except TimeoutError:
            raise _Failure("check_timeout") from None
        except OSError:
            raise _Failure("dns_failed") from None
        deadline.remaining()  # A late, uncancellable DNS lookup cannot start IO.
        if parsed.scheme == "http":
            try:
                if (not isinstance(addresses, (list, tuple)) or not 1 <= len(addresses) <= 64
                        or not all(ipaddress.ip_address(ip).is_loopback for ip in addresses)):
                    raise ValueError
            except ValueError:
                raise _Failure("unsafe_target") from None
        else:
            # Reuse the audited public-target guard, retaining the configured port
            # for the real connection. Validation never does a second DNS lookup.
            authority = "[" + host + "]" if ":" in host else host
            public_target(urlunsplit(("https", authority, parsed.path, "", "")), lambda _: addresses)
        return parsed, host, port, str(addresses[0])

    def _check(self, profile, deadline, resources):
        kind, provider = profile.get("kind"), profile.get("provider_id")
        if kind == "asset3d":
            if provider == "hunyuan" and profile.get("mode") == "local_process":
                try:
                    exists = Path(profile.get("python_path", "")).is_file() and Path(profile.get("model_dir", "")).is_dir()
                except (OSError, ValueError):
                    exists = False
                return conclusion("local_paths_present" if exists else "local_path_missing")
            if provider != "tripo" or profile.get("mode") != "cloud_api":
                return conclusion("unsupported_readonly_check")
        elif kind not in {"brain", "image"}:
            return conclusion("unsupported_readonly_check")
        base = profile.get("base_url", "").rstrip("/")
        # Explicit capability table: a provider ID alone conveys no trust.
        official = None
        if provider == "tripo":
            if base not in {"https://openapi.tripo3d.ai/v3", "https://openapi.tripo3d.com/v3"}:
                return conclusion("unsafe_target")
            official = "tripo"
        elif provider == "openai" and base == "https://api.openai.com/v1":
            official = "openai"
        elif provider == "deepseek" and base in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
            official = "deepseek"
        readonly_get = bool(official) or provider == "openai_compatible"
        key = _credential(profile) if readonly_get else None
        if official and key is None:
            return conclusion("missing_credential")
        parsed, host, port, address = self._target(base, deadline)
        connection = self.connector(host, address, port, parsed.scheme, deadline, resources)
        try:
            deadline.remaining()
            if connection.sock is None:
                raise _Failure("connection_failed")
            if not readonly_get:
                return conclusion("network_reachable")
            path = (parsed.path.rstrip("/") or "") + ("/account/balance" if official == "tripo" else "/models")
            headers = {"Host": parsed.netloc, "Accept": "application/json", "Connection": "close"}
            if key:
                headers["Authorization"] = "Bearer " + key
            connection.sock.settimeout(deadline.remaining())
            connection.request("GET", path, headers=headers)
            sock = connection.sock
            sock.settimeout(deadline.remaining())
            response = connection.getresponse()
            try:
                status = response.status
                if 300 <= status <= 399:
                    return conclusion("unsafe_target", status)
                errors = {401: "authentication_rejected", 403: "access_denied", 429: "rate_limited",
                          404: "unsupported_readonly_check", 405: "unsupported_readonly_check"}
                if status != 200:
                    return conclusion(errors.get(status, "service_error"), status)
                try:
                    document = _json(TimedResponse(response, sock, deadline))
                    valid = _balance(document) if official == "tripo" else _models(document, official)
                except TripoError as exc:
                    if exc.code == "tripo_timeout":
                        raise
                    valid = False
                except (ValueError, UnicodeError, RecursionError, http.client.IncompleteRead):
                    valid = False
                return conclusion("readonly_authenticated" if valid and official else
                                  "network_reachable" if valid else "invalid_check_response", status)
            finally:
                response.close()
        finally:
            connection.close()


def verify_connection(profile):
    return ConnectionVerifier().verify(profile)
