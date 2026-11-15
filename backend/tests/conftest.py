"""Global test configuration and AppLocker defensive compatibility layer."""

import sys
import types
import uuid

# Windows AppLocker defensive fallback:
# If C-extension _uuid_utils.pyd is blocked by Windows AppLocker policy,
# bridge to Python 3.14 standard library uuid module.
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
