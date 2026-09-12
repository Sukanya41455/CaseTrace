from datetime import UTC, datetime

from casetrace.contracts import Capability, ScenarioResult, TenantBindings, ValidationReport
from casetrace.integrity import binding_digest, capability_digest
from tests.unit.test_contracts import _artifact


def tenant(**changes):
    return TenantBindings(
        **(
            {
                "tenant_id": "test-bank",
                "vendor": "CaseTrace Fixture Bank",
                "origin": "http://127.0.0.1:8000",
                "app_version": "2026.09",
                "timezone": "America/Chicago",
            }
            | changes
        )
    )


def test_validation_sealing_does_not_change_executable_digest():
    capability = Capability.model_validate(_artifact())
    digest = capability_digest(capability)
    sealed = capability.model_copy(
        update={
            "validation": [
                ValidationReport(
                    artifact_digest=digest,
                    binding_digest=binding_digest(tenant()),
                    scenario_results=[
                        ScenarioResult(scenario="test_fixture", passed=True, evidence_refs=[])
                    ],
                    validated_at=datetime.now(UTC),
                )
            ]
        }
    )
    assert capability_digest(sealed) == digest


def test_executable_edit_invalidates_saved_digest():
    capability = Capability.model_validate(_artifact())
    original = capability_digest(capability)
    capability.limits.max_ui_actions -= 1
    assert capability_digest(capability) != original


def test_json_object_key_order_does_not_change_digest():
    payload = _artifact()
    reordered = dict(reversed(list(payload.items())))
    assert capability_digest(Capability.model_validate(payload)) == capability_digest(
        Capability.model_validate(reordered)
    )


def test_binding_origin_and_privacy_changes_require_revalidation():
    original = binding_digest(tenant())
    assert binding_digest(tenant(origin="http://127.0.0.1:8002")) != original
    assert binding_digest(tenant(privacy_mappings={"account_id": "masked"})) != original
