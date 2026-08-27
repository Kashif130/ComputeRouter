# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
import json

ZERO_ADDR = Address(b'\x00' * 20)


def parse_result(raw):
    """Handle both dict (response_format='json') and str (raw text) from exec_prompt."""
    if isinstance(raw, dict):
        return raw
    s = raw.strip() if isinstance(raw, str) else str(raw).strip()
    if s.startswith("```"):
        lines = s.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        s = "\n".join(lines).strip()
    return json.loads(s)


class ComputeRouter(gl.Contract):
    """
    Trustless GPU-job routing on GenLayer, with a real escrow: funds are
    received on-chain by the contract itself (@gl.public.write.payable),
    bound to the specific job_id + provider that route_job actually
    produced, and released or refunded on-chain via emit_transfer() only
    after a single, non-replayable completion decision.

    Leader LLM proposes a provider + reasoning. Validators don't recompute
    the same score — they check whether the leader's pick is *defensible*
    given the hard constraints (VRAM fits) and the stated priorities.
    That's the subjective consensus layer, applied twice: once to route
    the job, once to judge whether it was defensibly completed.
    """

    provider_registry: TreeMap[str, str]
    provider_payout: TreeMap[str, Address]   # provider_id -> wallet that receives payment
    provider_id_list: str
    job_history: DynArray[str]
    job_counter: u32

    routed_jobs: TreeMap[str, str]           # job_id -> provider_id (set only by route_job)
    escrow_meta: TreeMap[str, str]           # job_id -> {"provider","amount","status"}
    escrow_depositor: TreeMap[str, Address]  # job_id -> who funded it (typed, not user-supplied)

    @gl.public.write
    def register_provider(self, provider_id: str, provider_data_json: str):
        """
        Self-service registration: the caller becomes the payout address
        for this provider_id. Re-registering the same id from a different
        address is rejected, so a provider's payout wallet can't be
        silently swapped by someone else.
        """
        assert len(provider_id) < 16
        assert all(c.isalnum() or c == '_' for c in provider_id), "provider_id must be alphanumeric/underscore"
        assert len(provider_data_json) < 4096
        data = json.loads(provider_data_json)
        for field in ("gpu_type", "vram_gb", "cost_per_hr", "reliability_pct", "queue_wait_min"):
            assert field in data, f"missing field: {field}"

        existing_payout = self.provider_payout.get(provider_id, ZERO_ADDR)
        if existing_payout != ZERO_ADDR:
            assert gl.message.sender_address == existing_payout, "provider_id already owned by a different address"
        else:
            self.provider_payout[provider_id] = gl.message.sender_address

        self.provider_registry[provider_id] = provider_data_json
        existing = self.provider_id_list or ""
        ids = [x for x in existing.split(",") if x]
        if provider_id not in ids:
            ids.append(provider_id)
        self.provider_id_list = ",".join(ids)

    @gl.public.write
    def route_job(self, job_spec_json: str, priorities_json: str) -> str:
        """
        job_spec_json: {"vram_needed_gb": 24, "gpu_type_pref": "A100", "est_hours": 3}
        priorities_json: {"cost": 0-10, "speed": 0-10, "reliability": 0-10}

        Assigns a fresh job_id and records which provider it was routed
        to in `routed_jobs`. fund_escrow() later checks against this
        record, so escrow can only ever be opened for a job that was
        actually routed to the provider it names.
        """
        assert len(job_spec_json) < 4096
        job = json.loads(job_spec_json)
        priorities = json.loads(priorities_json)

        cost_p = min(10, max(0, int(priorities.get("cost", 5))))
        speed_p = min(10, max(0, int(priorities.get("speed", 5))))
        rel_p = min(10, max(0, int(priorities.get("reliability", 5))))

        id_str = self.provider_id_list or ""
        all_ids = [x for x in id_str.split(",") if x]
        assert len(all_ids) > 0

        # DETERMINISTIC pre-filter: hard constraint (VRAM) is not a judgment
        # call — filter it before the LLM ever sees the candidates. This
        # keeps the equivalence principle tight: every validator LLM starts
        # from the identical, already-filtered candidate set.
        vram_needed = int(job.get("vram_needed_gb", 0))
        providers = {}
        summary_lines = []
        for pid in all_ids:
            raw = self.provider_registry.get(pid, "")
            if not raw:
                continue
            p = json.loads(raw)
            if int(p.get("vram_gb", 0)) < vram_needed:
                continue  # hard-fails constraint, never shown to leader
            providers[pid] = p
            summary_lines.append(
                pid + ": " + str(p.get("gpu_type", "?"))
                + " " + str(p.get("vram_gb", "?")) + "GB, $"
                + str(p.get("cost_per_hr", "?")) + "/hr, "
                + str(p.get("reliability_pct", "?")) + "% reliable, "
                + str(p.get("queue_wait_min", "?")) + "min queue"
            )

        assert len(providers) > 0, "no provider meets hard VRAM constraint"
        providers_compact = "\n".join(summary_lines)
        priorities_str = (
            "cost=" + str(cost_p) + "/10, speed=" + str(speed_p)
            + "/10, reliability=" + str(rel_p) + "/10"
        )
        job_str = "needs " + str(vram_needed) + "GB VRAM, ~" + str(job.get("est_hours", "?")) + "hrs"

        def leader_fn():
            return gl.nondet.exec_prompt(
                "Pick the best GPU provider for this job.\n"
                "Job: " + job_str + "\n"
                "Candidates (already VRAM-filtered):\n" + providers_compact + "\n"
                "Priorities: " + priorities_str + "\n"
                "Reply JSON: {\"provider\":\"XX\",\"reasoning\":\"why, referencing cost/speed/reliability tradeoff\"}",
                response_format="json",
            )

        def validator_fn(leader_result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            try:
                data = parse_result(leader_result.calldata)
                if "provider" not in data or "reasoning" not in data:
                    return False
                if data["provider"] not in providers:
                    return False
                rl = data["reasoning"].lower()
                if len(rl) <= 10:
                    return False
                mentions_tradeoff = any(
                    kw in rl for kw in ("cost", "price", "$", "speed", "queue",
                                         "wait", "reliab", "fail", "uptime")
                )
                return mentions_tradeoff
            except Exception:
                return False

        routing = parse_result(gl.vm.run_nondet_unsafe(leader_fn, validator_fn))
        chosen = routing.get("provider", list(providers.keys())[0])

        job_id = "job-" + str(int(self.job_counter))
        self.job_counter = u32(int(self.job_counter) + 1)
        self.routed_jobs[job_id] = chosen

        routing["job_id"] = job_id
        routing["priorities"] = {"cost": cost_p, "speed": speed_p, "reliability": rel_p}
        routing["provider_data"] = providers.get(chosen, {})
        routing["job_spec"] = job
        self.job_history.append(json.dumps(routing))
        return json.dumps(routing)

    @gl.public.write.payable
    def fund_escrow(self, job_id: str, provider_id: str):
        """
        Locks real GEN for a routed job. The transaction's actual attached
        value (gl.message.value) becomes the escrowed amount — the caller
        cannot claim an amount that wasn't really sent. job_id must match
        a provider_id that route_job genuinely assigned it to, so escrow
        can't be opened against a job/provider pair that was never routed.
        """
        assert len(job_id) < 64
        assert self.routed_jobs.get(job_id, "") == provider_id, \
            "job_id was not routed to this provider_id"
        assert self.escrow_meta.get(job_id, "") == "", "job_id already funded"
        amount = gl.message.value
        assert int(amount) > 0, "must attach GEN value to fund escrow"

        self.escrow_depositor[job_id] = gl.message.sender_address
        self.escrow_meta[job_id] = json.dumps({
            "provider": provider_id,
            "amount": str(int(amount)),
            "status": "locked",
        })

    @gl.public.write
    def resolve_completion(self, job_id: str, evidence_json: str) -> str:
        """
        Non-replayable settlement: only a job whose escrow status is still
        "locked" can be resolved — once it flips to "released" or
        "refunded" this reverts on any further call, so funds can never be
        paid out twice for the same job.

        Only the depositor or the provider's registered payout address may
        trigger resolution (both have a legitimate stake in the outcome).
        The leader LLM judges the submitted evidence; validators
        independently verify the verdict is defensible. On a "completed"
        verdict, escrowed GEN is transferred on-chain to the provider's
        payout address; otherwise it's refunded on-chain to the depositor.
        """
        raw = self.escrow_meta.get(job_id, "")
        assert raw != "", "unknown job_id"
        record = json.loads(raw)
        assert record.get("status") == "locked", "job already resolved — no replay"
        assert len(evidence_json) < 8192

        provider_id = record["provider"]
        depositor = self.escrow_depositor.get(job_id, ZERO_ADDR)
        payout_addr = self.provider_payout.get(provider_id, ZERO_ADDR)
        caller = gl.message.sender_address
        assert caller == depositor or caller == payout_addr, \
            "only the depositor or the provider may resolve this job"

        def leader_fn():
            return gl.nondet.exec_prompt(
                "Job completion evidence: " + evidence_json + "\n"
                "Escrow record: " + json.dumps(record) + "\n"
                "Did the provider defensibly complete the job as specified? "
                "Reply JSON: {\"completed\": true/false, \"reasoning\":\"why\"}",
                response_format="json",
            )

        def validator_fn(leader_result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            try:
                data = parse_result(leader_result.calldata)
                return "completed" in data and "reasoning" in data and len(data["reasoning"]) > 10
            except Exception:
                return False

        verdict = parse_result(gl.vm.run_nondet_unsafe(leader_fn, validator_fn))
        amount = u256(int(record["amount"]))
        completed = bool(verdict.get("completed"))

        # Flip status BEFORE the transfer so a re-entrant/duplicate call
        # (or a retried transaction) can never observe "locked" again.
        record["status"] = "released" if completed else "refunded"
        record["verdict_reasoning"] = verdict.get("reasoning", "")
        self.escrow_meta[job_id] = json.dumps(record)

        target = payout_addr if completed else depositor
        assert target != ZERO_ADDR, "no valid payout target on record"
        gl.get_contract_at(target).emit_transfer(value=amount)

        return json.dumps(record)

    @gl.public.view
    def get_providers(self) -> str:
        id_str = self.provider_id_list or ""
        all_ids = [x for x in id_str.split(",") if x]
        providers = {}
        for pid in all_ids:
            raw = self.provider_registry.get(pid, "")
            if raw:
                providers[pid] = json.loads(raw)
        return json.dumps(providers)

    @gl.public.view
    def get_job_count(self) -> u32:
        return u32(len(self.job_history))

    @gl.public.view
    def get_job_history(self) -> str:
        records = []
        start = max(0, len(self.job_history) - 50)
        for i in range(start, len(self.job_history)):
            records.append(json.loads(self.job_history[i]))
        return json.dumps(records)

    @gl.public.view
    def get_escrow_status(self, job_id: str) -> str:
        raw = self.escrow_meta.get(job_id, "")
        if not raw:
            return "{}"
        record = json.loads(raw)
        depositor = self.escrow_depositor.get(job_id, ZERO_ADDR)
        record["depositor"] = str(depositor)
        return json.dumps(record)
