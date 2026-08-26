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


class ComputeRouterFull(gl.Contract):
    """
    Full GPU-job router with cross-contract reads from ProviderOracle and
    genuine validator re-reasoning (not just semantic pattern-matching).
    Designed for mainnet where cross-contract calls and a second LLM pass
    per validator are affordable. See compute_router.py for the
    testnet-friendly version that avoids the re-reasoning timeout risk.
    """

    owner: Address
    provider_oracle_addr: Address
    job_history: DynArray[str]
    escrow: TreeMap[str, str]

    @gl.public.write
    def set_owner(self, expected_owner: Address):
        if self.owner == Address(b'\x00' * 20):
            assert gl.message.sender_account == expected_owner, "Sender must match expected owner"
            self.owner = expected_owner
        else:
            assert gl.message.sender_account == self.owner, "Owner already set"

    def _only_owner(self):
        assert gl.message.sender_account == self.owner, "Only owner"

    @gl.public.write
    def set_oracle(self, oracle_addr: Address):
        self._only_owner()
        self.provider_oracle_addr = oracle_addr

    @gl.public.write
    def route_job(self, job_spec_json: str, priorities_json: str) -> str:
        assert len(job_spec_json) < 4096
        assert len(priorities_json) < 1000
        assert self.provider_oracle_addr != Address(b'\x00' * 20), "Oracle not set"

        job = json.loads(job_spec_json)
        priorities = json.loads(priorities_json)
        cost_p = min(10, max(0, int(priorities.get("cost", 5))))
        speed_p = min(10, max(0, int(priorities.get("speed", 5))))
        rel_p = min(10, max(0, int(priorities.get("reliability", 5))))

        # DETERMINISTIC: read live provider data + hard-filter on VRAM
        oracle = gl.get_contract_at(self.provider_oracle_addr)
        providers_str = oracle.view().get_all_providers()
        all_providers = json.loads(providers_str)
        vram_needed = int(job.get("vram_needed_gb", 0))
        providers = {
            pid: p for pid, p in all_providers.items()
            if int(p.get("vram_gb", 0)) >= vram_needed
        }
        assert len(providers) > 0, "No provider meets VRAM constraint"

        priorities_str = (
            "cost=" + str(cost_p) + "/10, speed=" + str(speed_p)
            + "/10, reliability=" + str(rel_p) + "/10"
        )
        job_str = "needs " + str(vram_needed) + "GB VRAM, ~" + str(job.get("est_hours", "?")) + "hrs"
        candidates_str = json.dumps(providers)

        def leader_fn():
            return gl.nondet.exec_prompt(
                "Pick the best GPU provider for this job.\n"
                "Job: " + job_str + "\n"
                "Candidates: " + candidates_str + "\n"
                "Priorities: " + priorities_str + "\n"
                "Respond ONLY with valid JSON, no markdown: "
                "{\"provider\":\"XX\",\"reasoning\":\"one sentence, referencing cost/speed/reliability tradeoff\"}",
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
                if len(data["reasoning"]) < 10:
                    return False

                # Genuine re-reasoning: a second, independent LLM call per
                # validator assesses defensibility rather than just
                # keyword-matching the leader's output. Costlier — this is
                # the mainnet path; testnet uses semantic checks instead
                # (see compute_router.py) because this times out under load.
                assessment = gl.nondet.exec_prompt(
                    "A router chose provider " + data["provider"]
                    + " for job: " + job_str + "\n"
                    "Candidates were: " + candidates_str + "\n"
                    "Priorities: " + priorities_str + "\n"
                    "Reasoning given: \"" + data["reasoning"] + "\"\n"
                    "Is this a defensible choice? Reply ONLY \"YES\" or \"NO\" then one sentence."
                )
                return assessment.strip().upper().startswith("YES")
            except Exception:
                return False

        routing = parse_result(gl.vm.run_nondet_unsafe(leader_fn, validator_fn))
        chosen = routing.get("provider", list(providers.keys())[0])
        record = {
            "provider": chosen,
            "reasoning": routing.get("reasoning", ""),
            "priorities": {"cost": cost_p, "speed": speed_p, "reliability": rel_p},
            "provider_data": providers.get(chosen, {}),
            "job_spec": job,
        }
        self.job_history.append(json.dumps(record))
        return json.dumps(record)

    @gl.public.write
    def fund_escrow(self, job_id: str, provider_id: str, amount_str: str):
        assert len(job_id) < 64
        self.escrow[job_id] = json.dumps({
            "provider": provider_id, "amount": amount_str, "status": "locked"
        })

    @gl.public.write
    def resolve_completion(self, job_id: str, evidence_json: str) -> str:
        assert job_id in self.escrow
        assert len(evidence_json) < 8192
        record = json.loads(self.escrow[job_id])

        def leader_fn():
            return gl.nondet.exec_prompt(
                "Job completion evidence: " + evidence_json + "\n"
                "Escrow record: " + json.dumps(record) + "\n"
                "Did the provider defensibly complete the job as specified? "
                "Respond ONLY with valid JSON: {\"completed\": true/false, \"reasoning\":\"why\"}",
                response_format="json",
            )

        def validator_fn(leader_result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            try:
                data = parse_result(leader_result.calldata)
                return "completed" in data and len(data.get("reasoning", "")) > 10
            except Exception:
                return False

        verdict = parse_result(gl.vm.run_nondet_unsafe(leader_fn, validator_fn))
        record["status"] = "released" if verdict.get("completed") else "disputed"
        record["verdict_reasoning"] = verdict.get("reasoning", "")
        self.escrow[job_id] = json.dumps(record)
        return json.dumps(record)

    @gl.public.view
    def get_history(self) -> str:
        records = []
        start = max(0, len(self.job_history) - 50)
        for i in range(start, len(self.job_history)):
            records.append(json.loads(self.job_history[i]))
        return json.dumps(records)

    @gl.public.view
    def get_history_count(self) -> u32:
        return u32(len(self.job_history))

    @gl.public.view
    def get_escrow_status(self, job_id: str) -> str:
        return self.escrow.get(job_id, "{}")
