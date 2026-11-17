"""Keep secrets in the Windows Credential Manager instead of a plain text file.

Windows encrypts each entry for the signed-in Windows account; another account on the
same computer, or a copy of the disk without that account's key, cannot read it.
Standard library only (ctypes). On other systems `default()` returns None and the
caller falls back to environment variables or its config file.
"""
import json
import sys

TARGET = 'AmazonKing/SP-API'
MAX_BLOB = 2560  # CRED_MAX_CREDENTIAL_BLOB_SIZE


class WindowsVault:
    name = 'Windows 凭据管理器'

    def __init__(self, target=TARGET):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.target = ctypes, target

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                        ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD), ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
                        ('Persist', wintypes.DWORD), ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                        ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]
        self.CREDENTIAL = CREDENTIAL
        api = ctypes.WinDLL('advapi32', use_last_error=True)
        api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        api.CredReadW.restype = wintypes.BOOL
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        self.api = api

    def read(self):
        """The saved values, or {} when nothing is saved or the entry is unreadable."""
        ctypes = self.ctypes
        pointer = ctypes.POINTER(self.CREDENTIAL)()
        if not self.api.CredReadW(self.target, 1, 0, ctypes.byref(pointer)):
            return {}
        try:
            entry = pointer.contents
            raw = ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize)
        finally:
            self.api.CredFree(pointer)
        try:
            values = json.loads(raw.decode('utf-8'))
        except ValueError:
            return {}
        return {k: v for k, v in values.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(values, dict) else {}

    def write(self, values):
        ctypes = self.ctypes
        raw = json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(raw) > MAX_BLOB:
            raise ValueError('凭证内容过长，无法保存到系统凭据管理器')
        buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        entry = self.CREDENTIAL()
        entry.Type, entry.Persist = 1, 2  # generic credential, kept for this Windows account on this computer
        entry.TargetName, entry.UserName = self.target, 'SP-API'
        entry.Comment = 'Amazon SP-API credentials for the listing tool'
        entry.CredentialBlobSize, entry.CredentialBlob = len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        if not self.api.CredWriteW(ctypes.byref(entry), 0):
            raise ValueError('保存到系统凭据管理器失败（Windows 错误 %d）' % ctypes.get_last_error())

    def delete(self):
        if not self.api.CredDeleteW(self.target, 1, 0) and self.ctypes.get_last_error() != 1168:  # 1168 = not found
            raise ValueError('从系统凭据管理器删除失败（Windows 错误 %d）' % self.ctypes.get_last_error())


def default(target=TARGET):
    if sys.platform != 'win32':
        return None
    try:
        return WindowsVault(target)
    except (OSError, AttributeError):
        return None
