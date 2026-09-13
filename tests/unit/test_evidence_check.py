import hashlib
import json

from casetrace import evidence


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_run_hashes_are_checked_and_paths_cannot_escape(tmp_path):
    write_json(tmp_path / "result.json", {"run_id": "sample"})
    digest = hashlib.sha256((tmp_path / "result.json").read_bytes()).hexdigest()
    write_json(
        tmp_path / "manifest.json",
        {
            "run_id": "sample",
            "files": [{"path": "result.json", "sha256": digest}],
        },
    )
    assert evidence.check_evidence(tmp_path) == []
    (tmp_path / "result.json").write_text("changed")
    assert "file digest mismatch: result.json" in evidence.check_evidence(tmp_path)
    write_json(
        tmp_path / "manifest.json",
        {
            "run_id": "sample",
            "files": [{"path": "../private.json", "sha256": digest}],
        },
    )
    assert "unsafe evidence path" in evidence.check_evidence(tmp_path)


def test_replay_with_model_calls_is_rejected(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"seq": 0, "model_call_count": 1}) + "\n")
    write_json(
        tmp_path / "manifest.json",
        {
            "run_id": "replay-posted",
            "mode": "replay",
            "files": [
                {
                    "path": "events.jsonl",
                    "sha256": hashlib.sha256(events.read_bytes()).hexdigest(),
                }
            ],
        },
    )
    assert "replay contains model calls" in evidence.check_evidence(tmp_path)


def test_delivery_requires_genuine_discovery_and_stable_handoff(tmp_path):
    write_json(tmp_path / "manifest.json", {"runs": {}})
    problems = evidence.check_evidence(tmp_path)
    assert "missing genuine discovery evidence" in problems
    assert "missing genuine human handoff evidence" in problems
