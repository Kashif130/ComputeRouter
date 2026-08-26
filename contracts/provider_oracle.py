# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
import json


class ProviderOracle(gl.Contract):
    """
    Registry + live pricing oracle for decentralized GPU providers.

    Owner-gated writes, capped provider count. Pulls live spot pricing from
    public GPU marketplace APIs (Vast.ai-style) so ComputeRouter always
    routes against real, current market data instead of stale hardcoded
    numbers.

    Reliability score is NOT scraped — it's computed onchain from
    ComputeRouter's own escrow history (completed vs disputed jobs), so it
    can't be gamed by a provider's own marketing claims.
    """

    owner: Address
    provider_registry: TreeMap[str, str]
    provider_ids: DynArray[str]
    completions: TreeMap[str, u32]
    disputes: TreeMap[str, u32]

    MAX_PROVIDERS = 100

    @gl.public.write
    def set_owner(self):
        if self.owner == Address(b'\x00' * 20):
            self.owner = gl.message.sender_account
        else:
            assert gl.message.sender_account == self.owner, "Owner already set"

    def _only_owner(self):
        assert gl.message.sender_account == self.owner, "Only owner"

    @gl.public.write
    def register_provider(self, provider_id: str, provider_data_json: str):
        """Register or update a GPU provider. Owner only."""
        self._only_owner()
        assert len(provider_id) < 16, "provider_id too long"
        assert all(c.isalnum() or c == '_' for c in provider_id), "provider_id must be alphanumeric"
        assert len(provider_data_json) < 4096, "provider data too large"
        data = json.loads(provider_data_json)
        for field in ("gpu_type", "vram_gb", "cost_per_hr"):
            assert field in data, f"{field} required"

        is_new = self.provider_registry.get(provider_id, "") == ""
        self.provider_registry[provider_id] = provider_data_json
        if is_new:
            assert len(self.provider_ids) < self.MAX_PROVIDERS, "provider cap reached"
            self.provider_ids.append(provider_id)

    @gl.public.write
    def update_pricing_live(self, provider_id: str, api_url: str):
        """
        Refresh a provider's spot price from a live marketplace API.
        Leader fetches; validators independently re-fetch and check the
        leader's reported price is within tolerance (comparative
        equivalence — the right choice for a numeric API value, unlike the
        judgment calls in ComputeRouter itself).
        """
        assert self.provider_registry.get(provider_id, "") != "", "Provider not registered"
        assert len(api_url) < 512

        def leader_fn():
            response = gl.nondet.web.get(api_url)
            data = json.loads(response.body.decode("utf-8"))
            # Expected shape: {"dph_total": 0.42, "gpu_name": "A100", ...}
            # (Vast.ai-style field naming)
            price = data.get("dph_total", data.get("cost_per_hr"))
            return {"cost_per_hr": float(price)}

        def validator_fn(leader_result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            try:
                my_response = gl.nondet.web.get(api_url)
                my_data = json.loads(my_response.body.decode("utf-8"))
                my_price = float(my_data.get("dph_total", my_data.get("cost_per_hr", 0)))
                leader_price = float(leader_result.calldata["cost_per_hr"])
                if my_price == 0:
                    return False
                # Comparative equivalence: 5% tolerance on live price feed
                return abs(leader_price - my_price) / my_price <= 0.05
            except Exception:
                return False

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        record = json.loads(self.provider_registry[provider_id])
        record["cost_per_hr"] = result["cost_per_hr"]
        self.provider_registry[provider_id] = json.dumps(record)

    @gl.public.write
    def record_completion(self, provider_id: str, completed: bool):
        """
        Called by ComputeRouter (or its owner-relay) after resolve_completion.
        Feeds the reliability score — real track record, not self-reported.
        """
        self._only_owner()
        assert self.provider_registry.get(provider_id, "") != "", "Provider not registered"
        if completed:
            self.completions[provider_id] = u32(int(self.completions.get(provider_id, u32(0))) + 1)
        else:
            self.disputes[provider_id] = u32(int(self.disputes.get(provider_id, u32(0))) + 1)

    @gl.public.view
    def get_reliability(self, provider_id: str) -> u32:
        """Reliability as a percentage (0-100), computed from job history."""
        done = int(self.completions.get(provider_id, u32(0)))
        disputed = int(self.disputes.get(provider_id, u32(0)))
        total = done + disputed
        if total == 0:
            return u32(100)  # no history yet — benefit of the doubt
        return u32(int((done / total) * 100))

    @gl.public.view
    def get_provider(self, provider_id: str) -> str:
        raw = self.provider_registry.get(provider_id, "")
        if not raw:
            return json.dumps({"error": "provider not found"})
        data = json.loads(raw)
        data["reliability_pct"] = int(self.get_reliability(provider_id))
        data["jobs_completed"] = int(self.completions.get(provider_id, u32(0)))
        data["jobs_disputed"] = int(self.disputes.get(provider_id, u32(0)))
        return json.dumps(data)

    @gl.public.view
    def get_all_providers(self) -> str:
        providers = {}
        for pid in self.provider_ids:
            raw = self.provider_registry.get(pid, "")
            if raw:
                data = json.loads(raw)
                data["reliability_pct"] = int(self.get_reliability(pid))
                data["jobs_completed"] = int(self.completions.get(pid, u32(0)))
                data["jobs_disputed"] = int(self.disputes.get(pid, u32(0)))
                providers[pid] = data
        return json.dumps(providers)

    @gl.public.view
    def get_provider_count(self) -> u32:
        return u32(len(self.provider_ids))
