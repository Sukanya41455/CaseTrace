"""Allowlisted, redacted evidence persistence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import BusinessOutcome, Failure, RunEvent, RunResult, Success
from .surface import Surface

_EVENT_FIELDS = (
    "event_id",
    "seq",
    "run_id",
    "timestamp",
    "kind",
    "actor",
    "summary",
    "step_id",
    "observation_id",
    "evidence_refs",
    "provider_response_id",
    "model_id",
    "model_call_count",
    "browser_id",
    "context_id",
    "page_id",
    "ownership_generation",
    "previous_owner",
    "current_owner",
)
_SNAPSHOT_FIELDS = (
    "observation_id",
    "vendor",
    "app_version",
    "surface_features",
    "controls",
    "state",
    "frames",
    "screen",
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passphrase|token|secret|authorization|cookie)\s*[:=]\s*[^\s,;]+"
)
_URL = re.compile(r"https?://[^\s<>\"]+")
_TRACEBACK = re.compile(r"(?is)\btraceback\b.*$")
_PROVIDER_BODY = re.compile(r"(?is)\b(provider(?: response| error)? body)\s*[:=].*$")


def check_evidence(root: Path) -> list[str]:
    """Check hashes and required run links; this does not authenticate the provider."""
    problems: list[str] = []

    def local_path(base: Path, value: str) -> Path:
        path = (base / value).resolve()
        if not path.is_relative_to(base.resolve()) or path == base.resolve():
            raise ValueError("unsafe evidence path")
        return path

    def read(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    def run_checks(folder: Path, mode: str | None = None):
        manifest = read(folder / "manifest.json")
        paths = set()
        for item in manifest["files"]:
            path = local_path(folder, item["path"])
            paths.add(item["path"])
            if not path.is_file():
                problems.append(f"missing evidence file: {item['path']}")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                problems.append(f"file digest mismatch: {item['path']}")
        events = []
        if "events.jsonl" in paths:
            events = [
                json.loads(line)
                for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line
            ]
            if [event["seq"] for event in events] != list(range(len(events))):
                problems.append("event sequence is not contiguous")
        if mode == "replay" or manifest.get("mode") == "replay":
            if any(
                event.get("model_call_count", 0) or event.get("provider_response_id")
                for event in events
            ):
                problems.append("replay contains model calls")
        return manifest, events

    try:
        manifest = read(root / "manifest.json")
        if "runs" not in manifest:
            run_checks(root)
            return problems
        runs = manifest["runs"]
        discovered_events = []
        handoff_events = []
        for name, relative in runs.items():
            folder = local_path(root, relative)
            run, events = run_checks(folder, "discovery" if name == "discovery" else "replay")
            if name == "discovery":
                discovered_events = events
            if name == "replay-handoff":
                handoff_events = events
            if run.get("artifact_digest") != manifest.get("artifact_digest"):
                problems.append(f"artifact digest mismatch: {name}")
            if run.get("binding_digest") != manifest.get("binding_digest"):
                problems.append(f"binding digest mismatch: {name}")
        if not any(
            e.get("provider_response_id") and e.get("model_id") and e.get("model_call_count", 0) > 0
            for e in discovered_events
        ):
            problems.append("missing genuine discovery evidence")
        owners = [e.get("current_owner") for e in handoff_events if e.get("kind") == "ownership"]
        manual = any(
            e.get("actor") == "human_operator" and e.get("kind") == "action" for e in handoff_events
        )
        if (
            not manual
            or "human_operator" not in owners
            or "automation" not in owners[owners.index("human_operator") + 1 :]
        ):
            problems.append("missing genuine human handoff evidence")
        if handoff_events:
            for key in ("browser_id", "context_id", "page_id"):
                identities = {e.get(key) for e in handoff_events}
                if len(identities) != 1 or None in identities:
                    problems.append(f"handoff changed or omitted {key}")
        for required in ("replay-posted", "replay-not-found", "replay-checkpoint-failure"):
            if required not in runs:
                problems.append(f"missing required run: {required}")
        if not manifest.get("different_replay_inputs"):
            problems.append("missing different-input replay evidence")
        artifact_path = local_path(root, manifest.get("capability", "capability.json"))
        from .contracts import Capability
        from .integrity import capability_digest

        capability = Capability.model_validate(read(artifact_path))
        if capability_digest(capability) != manifest.get("artifact_digest"):
            problems.append("capability digest mismatch")
        if not capability.validation:
            problems.append("capability has no validation evidence")
    except ValueError as error:
        problems.append(
            "unsafe evidence path"
            if str(error) == "unsafe evidence path"
            else "invalid evidence data"
        )
    except (OSError, KeyError, TypeError):
        problems.append("missing or malformed evidence manifest")
    return problems


class EvidenceWriter:
    def __init__(
        self,
        root: str | Path,
        run_id: str,
        *,
        sensitive_values: Iterable[object] = (),
    ) -> None:
        self.run_id = run_id
        self.run_dir = Path(root) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.result_path = self.run_dir / "result.json"
        self.manifest_path = self.run_dir / "manifest.json"
        self._next_seq = 0
        values = (str(value) for value in sensitive_values if value is not None)
        self._sensitive_values = tuple(
            sorted({value for value in values if value}, key=len, reverse=True)
        )
        self._capture_count = 0

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def emit(self, event: RunEvent) -> None:
        if event.run_id != self.run_id:
            raise ValueError("event run does not match evidence run")
        seq = getattr(event, "seq", None)
        if seq != self._next_seq:
            raise ValueError(f"event sequence must be {self._next_seq}")
        dumped = event.model_dump(mode="json")
        projected = {
            field: self._redact(dumped[field])
            for field in _EVENT_FIELDS
            if field in dumped and dumped[field] is not None
        }
        with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(projected, sort_keys=True, separators=(",", ":")) + "\n")
        self._next_seq += 1

    async def capture(self, surface: Surface) -> dict[str, str]:
        captured = await surface.capture()
        raw_snapshot = captured.get("snapshot", captured)
        if not isinstance(raw_snapshot, Mapping):
            raise ValueError("surface capture did not provide a sanitized snapshot")
        snapshot = {
            field: self._redact(raw_snapshot[field])
            for field in _SNAPSHOT_FIELDS
            if field in raw_snapshot
        }
        capture_id = self._capture_count
        self._capture_count += 1
        snapshot_path = self.run_dir / f"snapshot-{capture_id:03d}.json"
        snapshot_path.write_text(
            json.dumps(snapshot, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        result = {"snapshot_path": str(snapshot_path)}
        image = captured.get("masked_screenshot")
        if image is not None:
            if not captured.get("fully_masked") or not isinstance(image, bytes):
                raise ValueError("screenshots require complete recognized sensitive-region masking")
            screenshot_path = self.run_dir / f"screenshot-{capture_id:03d}.png"
            screenshot_path.write_bytes(image)
            result["screenshot_path"] = str(screenshot_path)
        return result

    def finish(self, result: RunResult) -> None:
        if result.run_id != self.run_id:
            raise ValueError("result run does not match evidence run")
        projection = self._result_projection(result)
        self.result_path.write_text(
            json.dumps(projection, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        files = []
        for path in sorted(self.run_dir.iterdir()):
            if path == self.manifest_path or not path.is_file():
                continue
            files.append(
                {
                    "path": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        manifest = {
            "run_id": self.run_id,
            "artifact_digest": result.artifact_digest,
            "binding_digest": result.binding_digest,
            "files": files,
        }
        self.manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )

    def _result_projection(self, result: RunResult) -> dict[str, Any]:
        common: dict[str, Any] = {
            "kind": result.kind,
            "run_id": result.run_id,
            "artifact_digest": result.artifact_digest,
            "binding_digest": result.binding_digest,
            "evidence_refs": self._redact(result.evidence_refs),
        }
        if isinstance(result, Success):
            return common | {
                "observed_at": result.observed_at.isoformat(),
                "payment": {"status": result.payment.status.value},
            }
        if isinstance(result, BusinessOutcome):
            return common | {
                "code": result.code.value,
                "coverage": result.coverage.model_dump(mode="json"),
                "candidates": self._redact(
                    [item.model_dump(mode="json") for item in result.candidates]
                ),
            }
        assert isinstance(result, Failure)
        return common | {
            "code": result.code.value,
            "current_step": result.current_step,
            "expected_condition": self._redact(result.expected_condition),
            "observed_condition": self._redact(result.observed_condition),
            "attempt_count": result.attempt_count,
        }

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, Mapping):
            return {str(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(item) for item in value]
        return value

    def _redact_text(self, value: str) -> str:
        redacted = value
        for sensitive in self._sensitive_values:
            redacted = redacted.replace(sensitive, "[REDACTED]")
        redacted = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
        redacted = _PROVIDER_BODY.sub("provider body=[REDACTED]", redacted)
        redacted = _TRACEBACK.sub("traceback [REDACTED]", redacted)

        def strip_query(match: re.Match[str]) -> str:
            parsed = urlsplit(match.group(0))
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

        return _URL.sub(strip_query, redacted)
