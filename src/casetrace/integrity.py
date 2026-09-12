"""Stable identities for executable artifacts and tenant bindings."""

import hashlib
import json

from casetrace.contracts import Capability, TenantBindings


def capability_digest(capability: Capability) -> str:
    return _digest(capability.model_dump(mode="json", exclude={"validation"}))


def binding_digest(bindings: TenantBindings) -> str:
    return _digest(bindings.model_dump(mode="json"))


def _digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
