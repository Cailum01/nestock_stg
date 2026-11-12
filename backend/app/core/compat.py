"""[CORE_COMPAT] 執行期環境向下相容與系統原則防禦層.

================================================================================
【防禦性架構設計宣告 / SYSTEM COMPATIBILITY LAYER】
- 當運行於啟用 Windows 應用程式控制原則 (AppLocker / WDAC) 的受限作業系統時，
  未簽署之 C 語言擴充模組 (_uuid_utils.pyd) 會被作業系統阻斷 (os error 4551)。
- 本模組在應用程式啟動首要時刻攔截此異常，並自動平滑橋接至 Python 3.14 標準庫
  原生提供之安全 uuid 引擎，確保 LangChain / LangGraph 與主進程無阻礙正常運作。
================================================================================
"""

import sys
import types
import uuid

# Windows AppLocker defensive fallback for uuid_utils C-extension:
try:
    import uuid_utils  # noqa: F401
except ImportError:
    mod = types.ModuleType("uuid_utils._uuid_utils")
    attrs = [
        "MAX",
        "NAMESPACE_DNS",
        "NAMESPACE_OID",
        "NAMESPACE_URL",
        "NAMESPACE_X500",
        "NIL",
        "RESERVED_FUTURE",
        "RESERVED_MICROSOFT",
        "RESERVED_NCS",
        "RFC_4122",
        "UUID",
        "getnode",
        "uuid1",
        "uuid3",
        "uuid4",
        "uuid5",
        "uuid6",
        "uuid7",
        "uuid8",
    ]
    mod.__dict__.update({a: getattr(uuid, a, None) for a in attrs})
    mod.__version__ = "0.10.0"
    mod._uuid4_int = lambda *args, **kwargs: uuid.uuid4().int
    mod._uuid7_int = lambda *args, **kwargs: getattr(uuid, "uuid7", uuid.uuid4)().int
    mod.reseed = lambda *args, **kwargs: None
    sys.modules["uuid_utils._uuid_utils"] = mod

