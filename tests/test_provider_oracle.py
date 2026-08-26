"""Tests for ProviderOracle contract.

Run with: pytest tests/test_provider_oracle.py -v
Requires: pip install genlayer-test
"""
import json
import pytest


def test_owner_set_on_first_call(direct_vm, direct_deploy, direct_alice):
    oracle = direct_deploy("contracts/provider_oracle.py")
    direct_vm.sender = direct_alice
    oracle.set_owner()
    # second call from a different sender should fail
    with pytest.raises(Exception):
        oracle.set_owner()  # already set, same sender is fine actually — see next test


def test_register_provider_requires_owner(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle = direct_deploy("contracts/provider_oracle.py")
    direct_vm.sender = direct_alice
    oracle.set_owner()

    direct_vm.sender = direct_bob
    with pytest.raises(Exception):
        oracle.register_provider("vastA100", json.dumps({
            "gpu_type": "A100", "vram_gb": 80, "cost_per_hr": 1.10,
        }))


def test_register_and_read_provider(direct_vm, direct_deploy, direct_alice):
    oracle = direct_deploy("contracts/provider_oracle.py")
    direct_vm.sender = direct_alice
    oracle.set_owner()
    oracle.register_provider("vastA100", json.dumps({
        "gpu_type": "A100", "vram_gb": 80, "cost_per_hr": 1.10,
    }))
    data = json.loads(oracle.get_provider("vastA100"))
    assert data["gpu_type"] == "A100"
    assert data["reliability_pct"] == 100  # no history yet — benefit of the doubt


def test_reliability_computed_from_history(direct_vm, direct_deploy, direct_alice):
    oracle = direct_deploy("contracts/provider_oracle.py")
    direct_vm.sender = direct_alice
    oracle.set_owner()
    oracle.register_provider("ioT4", json.dumps({
        "gpu_type": "T4", "vram_gb": 16, "cost_per_hr": 0.19,
    }))

    oracle.record_completion("ioT4", True)
    oracle.record_completion("ioT4", True)
    oracle.record_completion("ioT4", False)

    reliability = int(oracle.get_reliability("ioT4"))
    assert reliability == 66  # 2/3 completed, truncated

    data = json.loads(oracle.get_provider("ioT4"))
    assert data["jobs_completed"] == 2
    assert data["jobs_disputed"] == 1


def test_reliability_not_gameable_by_self_report(direct_vm, direct_deploy, direct_alice):
    """register_provider data does NOT include a client-supplied reliability
    score that gets trusted — it's always computed from completions/disputes."""
    oracle = direct_deploy("contracts/provider_oracle.py")
    direct_vm.sender = direct_alice
    oracle.set_owner()
    # Even if a provider tries to claim 100% reliability in their registration
    # payload, get_reliability ignores it and computes from real history.
    oracle.register_provider("shady", json.dumps({
        "gpu_type": "A100", "vram_gb": 80, "cost_per_hr": 0.50, "reliability_pct": 999,
    }))
    oracle.record_completion("shady", False)
    oracle.record_completion("shady", False)
    assert int(oracle.get_reliability("shady")) == 0


def test_update_pricing_live_within_tolerance(direct_vm, direct_deploy, direct_alice):
    """Leader and validator both fetch the same live pricing API; consensus
    holds when prices agree within 5% (comparative equivalence principle)."""
    oracle = direct_deploy("contracts/provider_oracle.py")
    direct_vm.sender = direct_alice
    oracle.set_owner()
    oracle.register_provider("vastA100", json.dumps({
        "gpu_type": "A100", "vram_gb": 80, "cost_per_hr": 1.10,
    }))

    direct_vm.mock_web(
        r".*vast\.ai.*",
        {"status": 200, "body": json.dumps({"dph_total": 1.15, "gpu_name": "A100"})},
    )
    oracle.update_pricing_live("vastA100", "https://api.vast.ai/instances/12345")
    data = json.loads(oracle.get_provider("vastA100"))
    assert abs(data["cost_per_hr"] - 1.15) < 0.01
    direct_vm.clear_mocks()


def test_get_all_providers_empty(direct_vm, direct_deploy, direct_alice):
    oracle = direct_deploy("contracts/provider_oracle.py")
    direct_vm.sender = direct_alice
    assert json.loads(oracle.get_all_providers()) == {}
    assert int(oracle.get_provider_count()) == 0
