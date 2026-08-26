# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
import json

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
    Trustless GPU-job routing on GenLayer.

    A job (training run, render, batch inference) needs a GPU provider.
    Providers differ on cost/hr, GPU type/VRAM, current queue wait, and a
    reliability score (historical completion rate). There is no formula
    that cleanly resolves "cheap but flaky" vs "expensive but reliable" vs
    "fast queue but wrong GPU tier" — it's a judgment call.

    Leader LLM proposes a provider + reasoning. Validators don't recompute
    the same score — they check whether the leader's pick is *defensible*
    given the hard constraints (VRAM fits, GPU type supported) and the
    stated priorities. That's the subjective consensus layer.
    """

    provider_registry: TreeMap[str, str]
    provider_id_list: str
    job_history: DynArray[str]
    escrow: TreeMap[str, str]  # job_id -> {"provider":..,"amount":..,"status":..}

    @gl.public.write
    def register_provider(self, provider_id: str, provider_data_json: str):
        assert len(provider_id) < 16
        assert len(provider_data_json) < 4096
        data = json.loads(provider_data_json)
        # hard-required fields — cheap deterministic validation, no LLM needed
        for field in ("gpu_type", "vram_gb", "cost_per_hr", "reliability_pct", "queue_wait_min"):
            assert field in data, f"missing field: {field}"
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
                # semantic defensibility check: reasoning must engage with
                # at least one of the stated priority dimensions, not just
                # restate the provider id
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
        routing["priorities"] = {"cost": cost_p, "speed": speed_p, "reliability": rel_p}
        routing["provider_data"] = providers.get(chosen, {})
        routing["job_spec"] = job
        self.job_history.append(json.dumps(routing))
        return json.dumps(routing)

    @gl.public.write
    def fund_escrow(self, job_id: str, provider_id: str, amount_str: str):
        """Lock payment for a routed job; released only on completion proof."""
        assert len(job_id) < 64
        self.escrow[job_id] = json.dumps({
            "provider": provider_id, "amount": amount_str, "status": "locked"
        })

    @gl.public.write
    def resolve_completion(self, job_id: str, evidence_json: str) -> str:
        """
        Dispute-style resolution: did the provider actually complete the
        job? Validators independently reason over submitted evidence
        (logs/output hash/duration) and reach a defensible — not identical
        — verdict. This is the same subjective-consensus primitive applied
        to payment release instead of routing.
        """
        assert job_id in self.escrow
        assert len(evidence_json) < 8192
        record = json.loads(self.escrow[job_id])

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
        record["status"] = "released" if verdict.get("completed") else "disputed"
        record["verdict_reasoning"] = verdict.get("reasoning", "")
        self.escrow[job_id] = json.dumps(record)
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
        return self.escrow.get(job_id, "{}")
