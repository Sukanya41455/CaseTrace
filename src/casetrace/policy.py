"""Parsed origin, route, and reviewed-control authorization."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from .contracts import (
    ClickStep,
    FailureCode,
    FillStep,
    NavigateStep,
    PolicyConfig,
    PolicyRoute,
    ReadStep,
    RunStopped,
    Step,
)
from .surface import ResolvedTarget

_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _origin(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        return None
    if parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    default_port = 80 if parsed.scheme == "http" else 443
    suffix = "" if port in {None, default_port} else f":{port}"
    return f"{parsed.scheme}://{parsed.hostname.lower()}{suffix}"


def _route_pattern(route: PolicyRoute) -> re.Pattern[str]:
    pieces: list[str] = []
    cursor = 0
    for match in _PLACEHOLDER.finditer(route.path):
        pieces.append(re.escape(route.path[cursor : match.start()]))
        pieces.append(r"[A-Za-z0-9._-]+")
        cursor = match.end()
    pieces.append(re.escape(route.path[cursor:]))
    return re.compile("^" + "".join(pieces) + "$", re.ASCII)


class Policy:
    def __init__(self, config: PolicyConfig, *, tenant_origin: str) -> None:
        normalized_tenant = _origin(tenant_origin)
        if normalized_tenant is None:
            raise ValueError("tenant origin must be an absolute HTTP(S) origin")
        allowed: set[str] = set()
        for configured in config.allowed_origins:
            candidate = tenant_origin if configured == "{tenant_origin}" else configured
            normalized = _origin(candidate)
            if normalized is None:
                raise ValueError("policy contains an invalid allowed origin")
            allowed.add(normalized)
        if normalized_tenant not in allowed:
            raise ValueError("tenant origin is absent from the policy allowlist")
        self.config = config
        self.tenant_origin = normalized_tenant
        self._allowed_origins = frozenset(allowed)
        self._routes = tuple((route, _route_pattern(route)) for route in config.allowed_routes)

    @classmethod
    def from_file(cls, path: str | Path, *, tenant_origin: str) -> Policy:
        import json

        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        policy_data = raw.get("policy", raw)
        return cls(PolicyConfig.model_validate(policy_data), tenant_origin=tenant_origin)

    def request_allowed(self, url: str, method: str, resource_type: str) -> bool:
        parsed = urlsplit(url)
        request_origin = _origin(f"{parsed.scheme}://{parsed.netloc}")
        if request_origin not in self._allowed_origins or parsed.fragment:
            return False
        raw_path = parsed.path or "/"
        path = unquote(raw_path)
        if (
            "\\" in path
            or "//" in path
            or any(part in {".", ".."} for part in path.split("/"))
            or "%2f" in raw_path.lower()
            or "%5c" in raw_path.lower()
        ):
            return False
        upper_method = method.upper()
        return any(
            upper_method in route.methods and pattern.fullmatch(path)
            for route, pattern in self._routes
        )

    def authorize_navigation(
        self, url: str, method: str = "GET", *, step_id: str | None = None
    ) -> None:
        if not self.request_allowed(url, method, "document"):
            self._deny(step_id, "reviewed origin and route", "request denied")

    def authorize(self, step: Step, resolved_target: ResolvedTarget | None = None) -> None:
        if isinstance(step, NavigateStep):
            return
        if resolved_target is None:
            self._deny(step.step_id, "resolved reviewed target", "target missing")
        assert resolved_target is not None
        if resolved_target.count != 1:
            code = (
                FailureCode.LOCATOR_AMBIGUOUS
                if resolved_target.count > 1
                else FailureCode.CHECKPOINT_FAILED
            )
            raise RunStopped(code, step.step_id, "exactly one target", "target count mismatch")
        if not self.request_allowed(resolved_target.page_url, "GET", "document"):
            self._deny(step.step_id, "reviewed page route", "page route denied")
        if isinstance(step, (FillStep, ClickStep)):
            meaning = resolved_target.control_meaning
            if (
                meaning in self.config.denied_control_meanings
                or meaning not in self.config.allowed_control_meanings
            ):
                self._deny(step.step_id, "reviewed inquiry control", "control meaning denied")
            if resolved_target.form_action:
                method = resolved_target.form_method or "GET"
                if not self.request_allowed(resolved_target.form_action, method, "document"):
                    self._deny(step.step_id, "reviewed form destination", "form destination denied")
        elif not isinstance(step, ReadStep):
            self._deny(step.step_id, "supported primitive", "operation denied")

    def absolute_url(self, value: str, base_url: str | None = None) -> str:
        return urljoin(base_url or self.tenant_origin + "/", value)

    @staticmethod
    def _deny(step_id: str | None, expected: str, observed: str) -> None:
        raise RunStopped(FailureCode.POLICY_DENIED, step_id, expected, observed)
