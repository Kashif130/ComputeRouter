"""Tests for ComputeRouterFull (the mainnet/Studio-style version) plus its
required wiring to ProviderOracle: owner/oracle initialization gating,
escrow funding, authorization, settlement, and replay protection.

This file was previously missing entirely — tests/test_compute_router.py
and tests/test_escrow_settlement.py only exercise the standalone
compute_router.py (which keeps its own provider registry). ComputeRouterFull
is the contract actually deployed by deploy/deploy-compute-studionet.mjs and
used by the "GenLayer Studio" tab in the frontend, so it needs its own
coverage of the same funding/authorization/settlement/replay guarantees,
plus the owner->oracle initialization sequence that's unique to it.

Run with: pytest tests/test_compute_router_full.py -v
Requires: pip install genlayer-test

NOTE ON CROSS-CONTRACT WIRING: set_oracle() takes the oracle's on-chain
Address. This suite assumes the object returned by direct_deploy(...) has
an `.address` attribute holding that Address (mirroring how direct_alice /
direct_bob are used elsewhere in this suite as ready-to-use Address values).
If your installed genlayer-test version exposes a deployed contract's
address under a different attribute (e.g. `.contract_address`), adjust the
`.address` accesses below — the assertions themselves don't depend on it.

NOTE ON THE VALUE FIXTURE: as in test_escrow_settlement.py, these tests set
`direct_vm.value = <int>` before a payable call. Adjust if your installed
genlayer-test version attaches value differently.
"""
import json
import pytest


def _register_providers_on_oracle(oracle, payout_a, payout_b):
    """Registers two providers on the oracle, each with a distinct payout
    wallet — so tests can tell "the depositor got refunded" apart from
    "the provider's actual registered wallet got paid" (not a caller-
    supplied string)."""
    oracle.register_provider(
        "vastA100",
        json.dumps({"gpu_type": "A100", "vram_gb": 80, "cost_per_hr": 1.10}),
        payout_a,
    )
    oracle.register_provider(
        "ioT4",
        json.dumps({"gpu_type": "T4", "vram_gb": 16, "cost_per_hr": 0.19}),
        payout_b,
    )


def _deploy_wired(direct_vm, direct_deploy, owner, provider_a_payout, provider_b_payout):
    """Deploys ProviderOracle + ComputeRouterFull and fully wires them:
    oracle owner set, providers registered, router owner set, oracle
    pointed at. Mirrors deploy/deploy-compute-studionet.mjs's own sequence
    exactly (oracle first, router set_owner, then set_oracle)."""
    direct_vm.sender = owner
    oracle = direct_deploy("contracts/provider_oracle.py")
    oracle.set_owner()
    _register_providers_on_oracle(oracle, provider_a_payout, provider_b_payout)

    router = direct_deploy("contracts/compute_router_full.py")
    router.set_owner(owner)
    router.set_oracle(oracle.address)
    return oracle, router


def _mock_routing_llm(direct_vm, provider="vastA100", reasoning="Cost is acceptable and reliability is strong for this workload."):
    """ComputeRouterFull's route_job makes TWO exec_prompt calls per
    validator pass (leader pick, then an independent re-reasoning
    'assessment' call) — both need mocking, unlike the single-call
    semantic-check version in compute_router.py."""
    direct_vm.mock_llm(
        r".*Pick the best GPU provider.*",
        json.dumps({"provider": provider, "reasoning": reasoning}),
    )
    direct_vm.mock_llm(
        r".*[Ii]s this a defensible choice.*",
        "YES, the reasoning reflects the stated priorities and hard constraints.",
    )


def _route_a_job(direct_vm, router, depositor, vram_needed_gb=24, provider="vastA100"):
    direct_vm.sender = depositor
    _mock_routing_llm(direct_vm, provider=provider)
    result = json.loads(router.route_job(
        json.dumps({"vram_needed_gb": vram_needed_gb, "est_hours": 2}),
        json.dumps({"cost": 5, "speed": 5, "reliability": 8}),
    ))
    direct_vm.clear_mocks()
    return result["job_id"], result["provider"]


# ─── Owner / oracle initialization gating ──────────────────────────────────

def test_deploys_with_zero_owner_and_no_oracle(direct_vm, direct_deploy, direct_alice):
    router = direct_deploy("contracts/compute_router_full.py")
    direct_vm.sender = direct_alice
    assert int(router.get_history_count()) == 0


def test_set_owner_first_call_requires_matching_sender(direct_vm, direct_deploy, direct_alice, direct_bob):
    """The very first set_owner call must have sender == expected_owner —
    alice can't set bob as owner on bob's behalf."""
    router = direct_deploy("contracts/compute_router_full.py")
    direct_vm.sender = direct_alice
    with pytest.raises(Exception):
        router.set_owner(direct_bob)


def test_set_owner_cannot_be_changed_once_set(direct_vm, direct_deploy, direct_alice, direct_bob):
    router = direct_deploy("contracts/compute_router_full.py")
    direct_vm.sender = direct_alice
    router.set_owner(direct_alice)

    # Even alice can't call set_owner again with a different target, and
    # bob (not the owner) certainly can't reassign ownership either.
    with pytest.raises(Exception):
        router.set_owner(direct_bob)
    direct_vm.sender = direct_bob
    with pytest.raises(Exception):
        router.set_owner(direct_bob)


def test_set_oracle_requires_owner(direct_vm, direct_deploy, direct_alice, direct_bob):
    router = direct_deploy("contracts/compute_router_full.py")
    direct_vm.sender = direct_alice
    router.set_owner(direct_alice)

    direct_vm.sender = direct_bob
    with pytest.raises(Exception):
        router.set_oracle(direct_bob)


def test_route_job_rejects_before_oracle_configured(direct_vm, direct_deploy, direct_alice):
    """route_job must refuse to run at all until set_oracle has been
    called — there's no valid provider data source otherwise."""
    router = direct_deploy("contracts/compute_router_full.py")
    direct_vm.sender = direct_alice
    router.set_owner(direct_alice)
    with pytest.raises(Exception):
        router.route_job(
            json.dumps({"vram_needed_gb": 24, "est_hours": 2}),
            json.dumps({"cost": 5, "speed": 5, "reliability": 5}),
        )


# ─── Routing reads live from ProviderOracle ────────────────────────────────

def test_route_job_reads_providers_from_oracle(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_alice, direct_bob)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)
    assert provider == "vastA100"
    assert job_id == "job-0"


def test_route_job_filters_hard_vram_constraint_via_oracle(direct_vm, direct_deploy, direct_alice, direct_bob):
    """A job needing more VRAM than the T4 provides must never be
    offered to it — filtered using oracle data before the LLM sees it."""
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_alice, direct_bob)
    direct_vm.sender = direct_alice
    _mock_routing_llm(direct_vm, provider="vastA100")
    result = json.loads(router.route_job(
        json.dumps({"vram_needed_gb": 40, "est_hours": 2}),
        json.dumps({"cost": 5, "speed": 5, "reliability": 5}),
    ))
    direct_vm.clear_mocks()
    assert result["provider"] == "vastA100"
    assert result["provider_data"]["vram_gb"] >= 40


def test_route_job_rejects_when_no_provider_meets_vram(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_alice, direct_bob)
    direct_vm.sender = direct_alice
    with pytest.raises(Exception):
        router.route_job(
            json.dumps({"vram_needed_gb": 999, "est_hours": 1}),
            json.dumps({"cost": 5, "speed": 5, "reliability": 5}),
        )


# ─── Funding: escrow must actually bind to a real routed job ──────────────

def test_fund_escrow_requires_routed_job(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_alice, direct_bob)
    direct_vm.sender = direct_alice
    direct_vm.value = 1000
    with pytest.raises(Exception):
        router.fund_escrow("job-does-not-exist", "vastA100")


def test_fund_escrow_requires_matching_provider(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_alice, direct_bob)
    job_id, chosen = _route_a_job(direct_vm, router, direct_alice)
    wrong_provider = "ioT4" if chosen != "ioT4" else "vastA100"

    direct_vm.sender = direct_alice
    direct_vm.value = 1000
    with pytest.raises(Exception):
        router.fund_escrow(job_id, wrong_provider)


def test_fund_escrow_requires_nonzero_value(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_alice, direct_bob)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with pytest.raises(Exception):
        router.fund_escrow(job_id, provider)


def test_fund_escrow_locks_real_value(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_alice, direct_bob)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    router.fund_escrow(job_id, provider)

    status = json.loads(router.get_escrow_status(job_id))
    assert status["status"] == "locked"
    assert status["amount"] == "5000"
    assert status["provider"] == provider


def test_fund_escrow_rejects_double_funding(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_alice, direct_bob)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 1000
    router.fund_escrow(job_id, provider)

    direct_vm.value = 1000
    with pytest.raises(Exception):
        router.fund_escrow(job_id, provider)


# ─── Authorization: payout address comes from the oracle, not the caller ──

def test_resolve_completion_rejects_unrelated_caller(direct_vm, direct_deploy, direct_alice, direct_bob):
    """A caller who is neither the depositor nor the provider's
    oracle-registered payout address cannot trigger resolution. Both
    providers' payouts are registered to alice here (who is also the
    depositor), so bob has no stake in this job at all."""
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_alice, direct_alice)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 1000
    router.fund_escrow(job_id, provider)

    direct_vm.sender = direct_bob
    direct_vm.value = 0
    with pytest.raises(Exception):
        router.resolve_completion(job_id, json.dumps({"log_summary": "done"}))


def test_resolve_completion_allows_depositor(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_bob, direct_bob)
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


def test_resolve_completion_allows_oracle_registered_payout_address(direct_vm, direct_deploy, direct_alice, direct_bob):
    """The provider's payout address is read LIVE from the oracle — not
    supplied by the caller — so that address (bob) must also be allowed
    to trigger resolution, even though bob never called fund_escrow."""
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_bob, direct_bob)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 1000
    router.fund_escrow(job_id, provider)

    direct_vm.sender = direct_bob  # the provider's registered payout wallet
    direct_vm.value = 0
    direct_vm.mock_llm(
        r".*Did the provider defensibly complete.*",
        json.dumps({"completed": True, "reasoning": "Evidence matches the job spec within the estimated window."}),
    )
    result = json.loads(router.resolve_completion(job_id, json.dumps({"log_summary": "done"})))
    assert result["status"] == "released"
    direct_vm.clear_mocks()


# ─── Settlement: correct transfer target, read live from the oracle ───────

def test_settlement_releases_to_provider_on_completion(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_bob, direct_bob)
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


def test_settlement_refunds_depositor_on_non_completion(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_bob, direct_bob)
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


def test_resolve_completion_succeeds_even_if_oracle_relay_is_unauthorized(direct_vm, direct_deploy, direct_alice, direct_bob):
    """resolve_completion's best-effort relay to oracle.record_completion()
    is wrapped in try/except specifically because the router usually isn't
    the oracle's owner — that relay failing must NEVER roll back the
    settlement (transfer + status flip) that already happened above it."""
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_bob, direct_bob)
    job_id, provider = _route_a_job(direct_vm, router, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 1500
    router.fund_escrow(job_id, provider)

    direct_vm.value = 0
    direct_vm.mock_llm(
        r".*Did the provider defensibly complete.*",
        json.dumps({"completed": True, "reasoning": "Evidence matches the job spec within the estimated window."}),
    )
    # Must not raise, regardless of whether the oracle relay itself is
    # authorized on this deployment.
    result = json.loads(router.resolve_completion(job_id, json.dumps({"output_hash": "0xabc"})))
    direct_vm.clear_mocks()
    assert result["status"] == "released"


# ─── Replay protection: a resolved job can never be resolved again ────────

def test_resolve_completion_cannot_be_replayed(direct_vm, direct_deploy, direct_alice, direct_bob):
    oracle, router = _deploy_wired(direct_vm, direct_deploy, direct_alice, direct_bob, direct_bob)
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

    status = json.loads(router.get_escrow_status(job_id))
    assert status["status"] == "released"


def test_empty_history(direct_vm, direct_deploy, direct_alice):
    router = direct_deploy("contracts/compute_router_full.py")
    direct_vm.sender = direct_alice
    assert int(router.get_history_count()) == 0
    assert json.loads(router.get_history()) == []
  
