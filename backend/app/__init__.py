"""Nestock backend."""

import sys
import types
import uuid as _std_uuid

# [DEFENSE_SHIM] 預防 Windows Smart App Control 阻斷 uuid_utils C-Extension
try:
    import uuid_utils  # noqa: F401
except ImportError:
    _mod = types.ModuleType("uuid_utils")
    _mod.UUID, _mod.SafeUUID = _std_uuid.UUID, getattr(_std_uuid, "SafeUUID", object)
    _mod.getnode, _mod.uuid1, _mod.uuid3, _mod.uuid4, _mod.uuid5 = (
        _std_uuid.getnode,
        _std_uuid.uuid1,
        _std_uuid.uuid3,
        _std_uuid.uuid4,
        _std_uuid.uuid5,
    )
    _mod.uuid6 = getattr(_std_uuid, "uuid6", _std_uuid.uuid1)
    _mod.uuid7 = getattr(_std_uuid, "uuid7", _std_uuid.uuid4)
    _mod.uuid8 = getattr(_std_uuid, "uuid8", _std_uuid.uuid4)
    _mod._uuid4_int = lambda: _std_uuid.uuid4().int
    _mod._uuid7_int = lambda: _mod.uuid7().int
    _mod.__version__ = "0.19.0"
    _mod.MAX = _std_uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    _mod.NIL = _std_uuid.UUID("00000000-0000-0000-0000-000000000000")
    for _n in [
        "NAMESPACE_DNS",
        "NAMESPACE_OID",
        "NAMESPACE_URL",
        "NAMESPACE_X500",
        "RESERVED_FUTURE",
        "RESERVED_MICROSOFT",
        "RESERVED_NCS",
        "RFC_4122",
    ]:
        setattr(_mod, _n, getattr(_std_uuid, _n, None))
    _mod.reseed = lambda: None
    sys.modules["uuid_utils"] = _mod

    _compat = types.ModuleType("uuid_utils.compat")
    for _a in [
        "uuid1",
        "uuid3",
        "uuid4",
        "uuid5",
        "uuid7",
        "UUID",
        "SafeUUID",
        "getnode",
        "NIL",
        "MAX",
        "__version__",
    ]:
        setattr(_compat, _a, getattr(_mod, _a, None))
    sys.modules["uuid_utils.compat"] = _compat
