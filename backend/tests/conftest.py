"""Global test configuration and AppLocker defensive compatibility layer."""

import os
import sys
import types
import uuid

# Keep tests offline: never load a developer's local backend/.env at import time.
os.environ.setdefault("NESTOCK_SKIP_DOTENV", "1")

# Windows AppLocker / WDAC defensive fallback:
# If C-extension _uuid_utils.pyd is blocked or missing,
# bridge to Python standard library uuid module.
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
        "SafeUUID",
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

    # Register both C-extension backend and top-level module wrappers
    sys.modules["uuid_utils"] = mod  # type: ignore[assignment]
    sys.modules["uuid_utils._uuid_utils"] = mod
    sys.modules["uuid_utils.compat"] = mod  # type: ignore[assignment]

# Windows defensive fallback for zoneinfo when tzdata package is missing:
try:
    import zoneinfo

    try:
        zoneinfo.ZoneInfo("Asia/Taipei")
    except (zoneinfo.ZoneInfoNotFoundError, ModuleNotFoundError):
        _orig_zoneinfo = zoneinfo.ZoneInfo

        class _FallbackZoneInfo:
            def __new__(cls, key: str):
                try:
                    return _orig_zoneinfo(key)
                except (zoneinfo.ZoneInfoNotFoundError, ModuleNotFoundError):
                    if key in ("Asia/Taipei", "ROC"):
                        from datetime import timedelta, timezone

                        return timezone(timedelta(hours=8), name=key)
                    raise

        zoneinfo.ZoneInfo = _FallbackZoneInfo  # type: ignore[misc]
except ImportError:
    pass
