"""Tests for ComputeRouter's escrow: funding (real value), authorization,
settlement, and replay protection.

Run with: pytest tests/test_escrow_settlement.py -v
Requires: pip install genlayer-test

NOTE ON THE VALUE FIXTURE: these tests set `direct_vm.value = <int>` before
a payable call, mirroring the existing `direct_vm.sender = <account>`
pattern used throughout this suite. If your installed genlayer-test
version exposes attaching value differently (e.g. a context manager or a
kwarg on the call itself), adjust the `direct_vm.value = ...` lines below
to match — the assertions themselves (funded status, transfer targets,
replay rejection) don't depend on how value is attached.
"""
import json
import pytest


def _register_providers(contract):
    contract.register_provider(
        "vastA100",
        json.dumps({
            "gpu_type": "A100", "vram_gb": 80, "cost_per_hr": 1.10,
            "reliability_pct": 97, "queue_wait_min": 2,
        }),
    )
    contract.register_provider(
        "ioT4",
        json.dumps({
            "gpu_type": "T4", "vram_gb": 16, "cost_per_hr": 0.19,
            "reliability_pct": 91, "queue_wait_min": 12,
        }),
    )


def _route_a_job(direct_vm, router, depositor):
    """Routes a job and returns its job_id + provider, so escrow tests
    fund something the contract actually recognizes as routed."""
    direct_vm.sender = depositor
    direct_vm.mock_llm(
        r".*Pick the best GPU provider.*",
        json.dumps({
            "provider": "vastA100",
            "reasoning": "Cost is acceptable and reliability is high with a short queue wait.",
        }),
    )
    result = json.loads(router.route_job(
        json.dumps({"vram_needed_gb": 24, "est_hours": 2}),
        json.dumps({"cost": 5, "speed": 5, "reliability": 8}),
    ))
    direct_vm.clear_mocks()
    return result["job_id"], result["provider"]


# ─── Funding: escrow must actually bind to a real routed job ──────────────

def test_fund_escrow_requires_routed_job(direct_vm, direct_deploy, direct_alice):
    """fund_escrow rejects a job_id that route_job never produced — escrow
    can't be opened against a job/provider pair that doesn't exist."""
    router = direct_deploy("contracts/compute_router.py")
    direct_vm.sender = direct_alice
    _register_providers(router)

    direct_vm.value = 1000
    with pytest.raises(Exception):
        router.fund_escrow("job-does-not-exist", "vastA100")


def test_fund_escrow_requires_matching_provider(direct_vm, direct_deploy, direct_alice):
    """The provider_id passed to fund_escrow must match what route_job
    actually chose for that job_id — you can't fund a different provider."""
    router = direct_deploy("contracts/compute_router.py")
    direct_vm.sender = direct_alice
    _register_providers(router)
    job_id, chosen_provider = _route_a_job(direct_vm, router, direct_alice)
    wrong_provider = "ioT4" if chosen_provider != "ioT4" else "vastA100"

    direct_vm.sender = direct_alice
    direct_vm.value = 1000
    with pytest.raises(Exception):
        router.fund_escrow(job_id, wrong_provider)


def test_fund_escrow_requires_nonzero_value(direct_vm, direct_deploy, direct_alice):
    """fund_escrow is payable and must reject a zero-value call — the
    escrowed amount comes from real attached GEN, not a claimed number."""
    router = direct_deploy("contracts/compute_router.py")
    direct_vm.sender = direct_alice
    _register_providers(router)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with pytest.raises(Exception):
        router.fund_escrow(job_id, provider)


def test_fund_escrow_locks_real_value(direct_vm, direct_deploy, direct_alice):
    """A successful fund_escrow records the depositor and the exact
    attached amount, with status 'locked'."""
    router = direct_deploy("contracts/compute_router.py")
    direct_vm.sender = direct_alice
    _register_providers(router)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    router.fund_escrow(job_id, provider)

    status = json.loads(router.get_escrow_status(job_id))
    assert status["status"] == "locked"
    assert status["amount"] == "5000"
    assert status["provider"] == provider


def test_fund_escrow_rejects_double_funding(direct_vm, direct_deploy, direct_alice):
    """The same job_id cannot be funded twice."""
    router = direct_deploy("contracts/compute_router.py")
    direct_vm.sender = direct_alice
    _register_providers(router)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 1000
    router.fund_escrow(job_id, provider)

    direct_vm.value = 1000
    with pytest.raises(Exception):
        router.fund_escrow(job_id, provider)


# ─── Authorization: only depositor or provider may resolve ────────────────

def test_resolve_completion_rejects_unrelated_caller(direct_vm, direct_deploy, direct_alice, direct_bob):
    """A third party with no stake in the job cannot trigger resolution."""
    router = direct_deploy("contracts/compute_router.py")
    direct_vm.sender = direct_alice
    _register_providers(router)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 1000
    router.fund_escrow(job_id, provider)

    # direct_bob is neither the depositor (alice) nor the provider's
    # registrant (also alice, since alice registered vastA100/ioT4) —
    # use bob to represent a fully unrelated caller.
    direct_vm.sender = direct_bob
    direct_vm.value = 0
    with pytest.raises(Exception):
        router.resolve_completion(job_id, json.dumps({"log_summary": "done"}))


def test_resolve_completion_allows_depositor(direct_vm, direct_deploy, direct_alice):
    """The depositor themself is always allowed to trigger resolution."""
    router = direct_deploy("contracts/compute_router.py")
    direct_vm.sender = direct_alice
    _register_providers(router)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 1000
    router.fund_escrow(job_id, provider)

    direct_vm.value = 0
    direct_vm.mock_llm(
        r".*Did the provider defensibly complete.*",
        json.dumps({"completed": True, "reasoning": "Logs and output hash both match the expected job spec."}),
    )
    result = json.loads(router.resolve_completion(job_id, json.dumps({"log_summary": "done"})))
    assert result["status"] == "released"
    direct_vm.clear_mocks()


# ─── Settlement: correct transfer target on release vs refund ─────────────

def test_settlement_releases_to_provider_on_completion(direct_vm, direct_deploy, direct_alice):
    router = direct_deploy("contracts/compute_router.py")
    direct_vm.sender = direct_alice
    _register_providers(router)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 2500
    router.fund_escrow(job_id, provider)

    direct_vm.value = 0
    direct_vm.mock_llm(
        r".*Did the provider defensibly complete.*",
        json.dumps({"completed": True, "reasoning": "Evidence matches the job spec and timing was within estimate."}),
    )
    result = json.loads(router.resolve_completion(job_id, json.dumps({"output_hash": "0xabc"})))
    direct_vm.clear_mocks()

    assert result["status"] == "released"
    status = json.loads(router.get_escrow_status(job_id))
    assert status["status"] == "released"


def test_settlement_refunds_depositor_on_non_completion(direct_vm, direct_deploy, direct_alice):
    router = direct_deploy("contracts/compute_router.py")
    direct_vm.sender = direct_alice
    _register_providers(router)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 2500
    router.fund_escrow(job_id, provider)

    direct_vm.value = 0
    direct_vm.mock_llm(
        r".*Did the provider defensibly complete.*",
        json.dumps({"completed": False, "reasoning": "No output hash was submitted and duration exceeds estimate by 4x."}),
    )
    result = json.loads(router.resolve_completion(job_id, json.dumps({"log_summary": "nothing"})))
    direct_vm.clear_mocks()

    assert result["status"] == "refunded"
    status = json.loads(router.get_escrow_status(job_id))
    assert status["status"] == "refunded"


# ─── Replay protection: a resolved job can never be resolved again ────────

def test_resolve_completion_cannot_be_replayed(direct_vm, direct_deploy, direct_alice):
    """Once a job settles (released or refunded), calling
    resolve_completion again — with the same or different evidence —
    must revert instead of re-triggering a transfer."""
    router = direct_deploy("contracts/compute_router.py")
    direct_vm.sender = direct_alice
    _register_providers(router)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 1000
    router.fund_escrow(job_id, provider)

    direct_vm.value = 0
    direct_vm.mock_llm(
        r".*Did the provider defensibly complete.*",
        json.dumps({"completed": True, "reasoning": "Evidence matches the job spec and timing was within estimate."}),
    )
    first = json.loads(router.resolve_completion(job_id, json.dumps({"output_hash": "0xabc"})))
    assert first["status"] == "released"
    direct_vm.clear_mocks()

    direct_vm.mock_llm(
        r".*Did the provider defensibly complete.*",
        json.dumps({"completed": True, "reasoning": "Trying to resolve this already-settled job a second time."}),
    )
    with pytest.raises(Exception):
        router.resolve_completion(job_id, json.dumps({"output_hash": "0xabc"}))
    direct_vm.clear_mocks()

    # Status must remain exactly as the first (and only valid) resolution left it.
    status = json.loads(router.get_escrow_status(job_id))
    assert status["status"] == "released"
  
