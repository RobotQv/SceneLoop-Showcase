"""First-start project setup. Existing .env files are never opened or changed."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import stat

_DEFAULT_ENV = (b"# SceneLoop local environment. Never commit this file.\n"
                b"# Add model configurations and encrypted API keys in Settings.\n"
                b"# Optional environment credentials and BLENDER_PATH can be set here.\n")
_MAX_TEMPLATE_BYTES = 65536


def _windows_private_file(path):
    """Create a new file with a protected DACL granting only this process user.

    Unlike chmod(0600), this applies Windows access control before any bytes are
    written. OS administrators can still take ownership using their privileges.
    """
    import msvcrt

    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x00000800)
    advapi = ctypes.WinDLL("advapi32.dll", use_last_error=True, winmode=0x00000800)
    pointer = ctypes.c_void_p
    dword = ctypes.c_uint32

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", dword), ("lpSecurityDescriptor", pointer),
                    ("bInheritHandle", ctypes.c_int)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("Sid", pointer), ("Attributes", dword)]

    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = pointer
    kernel.CloseHandle.argtypes = [pointer]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, dword, dword,
                                  ctypes.POINTER(SecurityAttributes), dword, dword, pointer]
    kernel.CreateFileW.restype = pointer
    advapi.OpenProcessToken.argtypes = [pointer, dword, ctypes.POINTER(pointer)]
    advapi.OpenProcessToken.restype = ctypes.c_int
    advapi.GetTokenInformation.argtypes = [pointer, ctypes.c_int, pointer,
                                         dword, ctypes.POINTER(dword)]
    advapi.GetTokenInformation.restype = ctypes.c_int
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi.ConvertSidToStringSidW.restype = ctypes.c_int
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p, dword, ctypes.POINTER(pointer), ctypes.POINTER(dword)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int

    error_message = "无法创建仅当前系统用户可读写的 .env，请检查项目目录权限。"
    token = pointer()
    sid_string = ctypes.c_wchar_p()
    descriptor = pointer()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise OSError(error_message)
        length = dword()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not 0 < length.value <= 65536:
            raise OSError(error_message)
        user_buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, user_buffer, length, ctypes.byref(length)):
            raise OSError(error_message)
        sid = ctypes.cast(user_buffer, ctypes.POINTER(TokenUser)).contents.Sid
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
            raise OSError(error_message)
        sddl = "D:P(A;;FA;;;" + sid_string.value + ")"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise OSError(error_message)
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, 0)
        # GENERIC_WRITE, no sharing, CREATE_NEW, FILE_ATTRIBUTE_NORMAL.
        handle = kernel.CreateFileW(str(path), 0x40000000, 0, ctypes.byref(attributes), 1, 0x80, None)
        if handle == pointer(-1).value:
            if ctypes.get_last_error() in (80, 183):
                raise FileExistsError(".env already exists")
            raise OSError(error_message)
        try:
            descriptor_number = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        try:
            return os.fdopen(descriptor_number, "wb")
        except BaseException:
            os.close(descriptor_number)
            raise
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if sid_string:
            kernel.LocalFree(ctypes.cast(sid_string, pointer))
        if token.value:
            kernel.CloseHandle(token)


def _create_private_file(path):
    if os.name == "nt":
        return _windows_private_file(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        return os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise


def _template_bytes(project_root):
    template = project_root / ".env.example"
    try:
        info = template.lstat()
    except FileNotFoundError:
        return _DEFAULT_ENV
    # Do not follow links or devices when seeding a private local file.
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        return _DEFAULT_ENV
    if info.st_size > _MAX_TEMPLATE_BYTES:
        raise OSError(".env.example 过大，无法初始化 .env。")
    with template.open("rb") as stream:
        contents = stream.read(_MAX_TEMPLATE_BYTES + 1)
    if len(contents) > _MAX_TEMPLATE_BYTES:
        raise OSError(".env.example 过大，无法初始化 .env。")
    return contents


def ensure_env(project_root: Path) -> bool:
    """Return True only when .env was created; never inspect an existing .env.

    Call after the service owns its lock and listening port. Missing templates
    use comments only. A concurrent creator wins; its file is left untouched.
    New POSIX files use 0600; Windows files use a user-only DACL.
    """
    project_root = Path(project_root)
    target = project_root / ".env"
    if os.path.lexists(target):
        return False
    contents = _template_bytes(project_root)
    try:
        stream = _create_private_file(target)
    except FileExistsError:
        return False
    with stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    return True
