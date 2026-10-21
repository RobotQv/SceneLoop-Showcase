"""OS-bound encryption for API credentials; never stores an encryption key.

Windows DPAPI binds ciphertext to the Windows logon user. A Codex login change
under that same OS user does not affect it. Other platforms deliberately fail
closed until an OS keychain backend is implemented; environment keys still work.
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import hmac
import os


MAX_KEY_LENGTH = 4096
_CIPHER_PREFIX = "dpapi-user-v1:"
_MAX_CIPHER_BYTES = 16384
_MAX_CIPHER_TEXT = len(_CIPHER_PREFIX) + 4 * ((_MAX_CIPHER_BYTES + 2) // 3)
_PAYLOAD_MAGIC = b"SceneLoop API key v1\0"
_MAX_PAYLOAD = len(_PAYLOAD_MAGIC) + 32 + MAX_KEY_LENGTH
_UI_FORBIDDEN = 0x01


class CredentialError(Exception):
    """Safe public error: callers must not replace it with native diagnostics."""

    def __init__(self, message, code="credential_storage_error", status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def validate_api_key(api_key):
    """Accept bearer-token text only, without silently trimming its contents."""
    if not isinstance(api_key, str) or not 1 <= len(api_key) <= MAX_KEY_LENGTH:
        raise CredentialError("密钥必须为 1 到 4096 个可见 ASCII 字符。", "invalid_api_key")
    if any(ord(character) < 33 or ord(character) > 126 for character in api_key):
        raise CredentialError("密钥不能包含空格、换行或非 ASCII 字符。", "invalid_api_key")
    return api_key


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _WindowsDPAPI:
    def __init__(self):
        # Resolve built-in DLLs through System32, never through the project path.
        crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True, winmode=0x00000800)
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x00000800)
        blob_pointer = ctypes.POINTER(_DataBlob)
        self._protect = crypt32.CryptProtectData
        self._protect.argtypes = [blob_pointer, ctypes.c_wchar_p, blob_pointer,
                                  ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_pointer]
        self._protect.restype = ctypes.c_int
        self._unprotect = crypt32.CryptUnprotectData
        self._unprotect.argtypes = [blob_pointer, ctypes.POINTER(ctypes.c_wchar_p), blob_pointer,
                                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_pointer]
        self._unprotect.restype = ctypes.c_int
        self._free = kernel32.LocalFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = ctypes.c_void_p

    def transform(self, data, *, decrypt=False):
        """The only native crypto boundary. No native error or input is logged."""
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        result = _DataBlob()
        operation = self._unprotect if decrypt else self._protect
        maximum = _MAX_PAYLOAD if decrypt else _MAX_CIPHER_BYTES
        try:
            # No machine-wide flag, optional entropy, reserved value, prompt, or
            # description output. Windows owns its keys and checks the DPAPI MAC.
            ok = operation(ctypes.byref(source), None, None, None, None,
                           _UI_FORBIDDEN, ctypes.byref(result))
            if not ok or not result.pbData or not 0 < result.cbData <= maximum:
                raise CredentialError("无法处理本机加密密钥，请在当前系统用户下重新填写。")
            return ctypes.string_at(result.pbData, result.cbData)
        except (OSError, ValueError, ctypes.ArgumentError):
            raise CredentialError("无法处理本机加密密钥，请在当前系统用户下重新填写。") from None
        finally:
            ctypes.memset(buffer, 0, len(buffer))
            if result.pbData:
                # These mutable native buffers can be wiped; Python strings and
                # bytes cannot promise complete process-memory erasure.
                ctypes.memset(result.pbData, 0, result.cbData)
                self._free(ctypes.cast(result.pbData, ctypes.c_void_p))


class SecretProtector:
    """Small injectable interface used by the configuration repository."""

    def __init__(self):
        self._native = None
        self._reason = "当前系统尚未接入系统密钥存储，请使用环境变量方式。"
        if os.name == "nt":
            try:
                self._native = _WindowsDPAPI()
            except (OSError, AttributeError):
                self._reason = "Windows 密钥保护不可用，请使用环境变量方式。"

    def status(self):
        if self._native is not None:
            return {"available": True, "backend": "windows-dpapi",
                    "label": "Windows 当前用户加密", "reason": ""}
        return {"available": False, "backend": "unavailable",
                "label": "加密密钥保存不可用", "reason": self._reason}

    def _require_native(self):
        if self._native is None:
            raise CredentialError(self._reason, "credential_storage_unavailable", 503)
        return self._native

    def protect(self, api_key):
        key = validate_api_key(api_key).encode("ascii")
        native = self._require_native()
        # Internal checksum/version validation catches malformed decrypted
        # output too. It is inside DPAPI, not a replacement encryption scheme.
        payload = _PAYLOAD_MAGIC + hashlib.sha256(key).digest() + key
        protected = native.transform(payload)
        if not protected or len(protected) > _MAX_CIPHER_BYTES:
            raise CredentialError("无法保存本机加密密钥。")
        return _CIPHER_PREFIX + base64.b64encode(protected).decode("ascii")

    def unprotect(self, ciphertext):
        invalid = "保存的加密密钥无效，请重新填写。"
        if (not isinstance(ciphertext, str) or len(ciphertext) > _MAX_CIPHER_TEXT
                or not ciphertext.startswith(_CIPHER_PREFIX)):
            raise CredentialError(invalid)
        try:
            encoded = ciphertext[len(_CIPHER_PREFIX):]
            protected = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise CredentialError(invalid) from None
        if (not protected or len(protected) > _MAX_CIPHER_BYTES
                or base64.b64encode(protected).decode("ascii") != encoded):
            raise CredentialError(invalid)
        payload = self._require_native().transform(protected, decrypt=True)
        offset = len(_PAYLOAD_MAGIC)
        if (not payload.startswith(_PAYLOAD_MAGIC)
                or not offset + 32 < len(payload) <= _MAX_PAYLOAD):
            raise CredentialError(invalid)
        checksum = payload[offset:offset + 32]
        key = payload[offset + 32:]
        if not hmac.compare_digest(checksum, hashlib.sha256(key).digest()):
            raise CredentialError(invalid)
        try:
            return validate_api_key(key.decode("ascii"))
        except (UnicodeError, CredentialError):
            raise CredentialError(invalid) from None
