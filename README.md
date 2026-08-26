# ComputeRouter

Trustless GPU-job routing on GenLayer. Validators independently reason about which decentralized compute provider should run your job — and reach subjective consensus on the tradeoff between cost, speed, and reliability. No formula resolves that tradeoff; only judgment does, and GenLayer makes the judgment verifiable.

## The Problem

Decentralized GPU marketplaces (Vast.ai, io.net, Lambda-style spot networks) already exist, but matching a job to a provider is either done by a centralized broker you have to trust, or left entirely to the user to shop around manually. There's no formula for "cheap but flaky" vs "expensive but instant" vs "fast queue but the wrong GPU tier" — it's a judgment call, and today nobody verifies it.

Worse: once a job is routed, there's no trustless way to confirm it actually *completed* before releasing payment. Today that's either a centralized escrow you trust, or no escrow at all.

ComputeRouter solves both with the same primitive: **leader proposes, validators check defensibility — not identical output, defensible output.** That's subjective consensus, applied twice — once to route the job, once to release the payment.

## How It Works

```
User submits job: needs 24GB VRAM, ~3hrs, priorities cost=8 speed=3 reliability=5
  |
  v
ComputeRouter (Intelligent Contract)
  |-- [DETERMINISTIC] Hard-filter providers by VRAM — a formula problem, not a judgment call
  |-- [NONDET] Leader LLM picks the best-fit provider from the filtered candidates
  |-- [CONSENSUS] Validators verify: is this choice defensible given the tradeoffs?
  |-- [DETERMINISTIC] Record routing decision + reasoning onchain
  |
  v
Job runs off-chain on the chosen provider
  |
  v
fund_escrow() locks payment  →  resolve_completion() submits evidence
  |-- [NONDET] Leader LLM judges: did the provider defensibly complete the job?
  |-- [CONSENSUS] Validators independently verify the verdict
  |-- [DETERMINISTIC] Escrow releases or moves to disputed
```

### Why the Hard/Soft Split Matters

WindfallRouter (the sibling project this pattern is drawn from) left every decision to the LLM. ComputeRouter separates the two kinds of question:

- **Hard constraint** (VRAM must fit) — resolved deterministically in Python *before* the LLM ever sees the candidate list. Every validator starts from the identical, pre-filtered set, which tightens the equivalence principle and removes an entire class of failure (LLM picking a provider that literally can't run the job).
- **Soft judgment** (cost vs speed vs reliability tradeoff) — left to the leader LLM, checked for defensibility by validators. This is the part with no formula.

### Four Providers, Three Tradeoffs

| Provider | GPU | VRAM | Cost/hr | Reliability | Queue |
|----------|-----|------|---------|-------------|-------|
| `vastA100` | A100 | 80GB | $1.10 | 97% | 2 min |
| `ioT4` | T4 | 16GB | $0.19 | 91% | 12 min |
| `lambdaH100` | H100 | 80GB | $2.49 | 99% | 0 min |
| `runpodA6000` | A6000 | 48GB | $0.79 | 94% | 5 min |

Users set three priority sliders (0–10 each): **cost**, **speed**, **reliability**. Different sliders produce different routing decisions for the same job — all verified by consensus, not by trusting a single broker.

### Reliability Is Earned, Not Claimed

`ProviderOracle.get_reliability()` is computed from real escrow history (`completions` vs `disputes`), not from a self-reported field in a provider's registration payload. A provider claiming 99.9% reliability in their own data gets ignored — the score comes from `resolve_completion()` outcomes.

## Contracts

| Contract | Purpose | Equivalence Principle |
|----------|---------|----------------------|
| `compute_router.py` | Testnet-friendly router with a built-in provider registry, escrow, and dispute resolution | Non-Comparative (semantic defensibility — reliable under testnet load) |
| `compute_router_full.py` | Mainnet-style router reading live data from `ProviderOracle` via cross-contract calls, with genuine validator re-reasoning | Non-Comparative (full LLM re-reasoning per validator) |
| `provider_oracle.py` | Provider registry + live marketplace pricing oracle + onchain reliability scoring | Comparative (5% tolerance on live price feeds) |

### Security

- Owner-gated admin functions (`register_provider`, oracle address, pricing updates)
- Hard VRAM constraint enforced in Python, never left to LLM judgment
- Input length limits on job specs, priorities, and evidence payloads
- Reliability score derived only from actual escrow outcomes, immune to self-reported claims
- Provider id charset restricted (alphanumeric + underscore) to prevent injection into prompts

## Frontend: Wallet + Networks

The frontend connects with a real EVM wallet instead of a throwaway key —
click **Connect Wallet** and pick any installed EIP-1193 wallet (MetaMask,
Rabby, Coinbase Wallet, Brave, OKX, Rainbow, Trust Wallet, etc.); discovery
uses [EIP-6963](https://eips.ethereum.org/EIPS/eip-6963) so it isn't tied to
any single wallet. The connected wallet signs `route_job`, `fund_escrow`, and
`resolve_completion`; reads (providers, history) work without a wallet
connected.

The network dropdown switches between:
- **Bradbury Testnet** (`testnetBradbury`)
- **GenLayer Studio** (`studionet` — the hosted sandbox at studio.genlayer.com)

Switching networks or connecting a wallet calls `client.connect(...)` to
prompt the wallet to add/switch to the right chain automatically.

## Run Locally

```bash
npm install -g genlayer
pip install genlayer-test

# Deploy the testnet-friendly router to Bradbury (needs funded account)
DEPLOYER_KEY=0x... node deploy/deploy-compute-bradbury.mjs

# Or deploy the full cross-contract version to GenLayer Studio (studionet)
node deploy/deploy-compute-studionet.mjs

# Run tests
pytest tests/test_compute_router.py tests/test_provider_oracle.py -v

# Open frontend locally
cp frontend/compute-config.example.js frontend/compute-config.js
# edit frontend/compute-config.js with your deployed router addresses
open frontend/compute-index.html
```

## Deploying to Vercel

On Vercel, router addresses are managed as Environment Variables
(`BRADBURY_ROUTER_ADDR`, `STUDIONET_ROUTER_ADDR`) rather than hand-edited in
a file — a small build step (`frontend/generate-config.mjs`) writes them into
`compute-config.js` at deploy time, so switching networks in the UI always
reads the right contract address. See [VERCEL_DEPLOY.md](./VERCEL_DEPLOY.md)
for the full walkthrough.

## What We Learned (carried over from WindfallRouter, still true here)

1. **Structural validation is not consensus.** Checking `"provider" in data` means the validator is rubber-stamping the leader. Real subjective consensus requires the validator to check the reasoning actually engages with the stated tradeoffs.

2. **"Defensible" is the right standard, not "optimal."** Different LLMs — and different humans — can reasonably disagree on the close calls (is $0.31/hr extra worth 8% more reliability?). Requiring identical answers defeats the point of having independent validators.

3. **Validator LLM re-reasoning times out under testnet load.** `compute_router.py` uses semantic checks (does the reasoning reference cost/speed/reliability/queue) for testnet reliability; `compute_router_full.py` shows the fuller re-reasoning path for mainnet-style environments where a second LLM call per validator is affordable.

4. **Separating hard constraints from soft judgment tightens the equivalence principle.** Filtering on VRAM *before* the LLM sees the candidates means the LLM's job — and every validator's job — is strictly about the tradeoff, not about constraint-checking it might get wrong.

5. **The same "defensible, not identical" primitive generalizes.** Applying it twice — once to routing, once to escrow release — is what turns a routing demo into something resembling actual settlement infrastructure for a decentralized compute market.

## Track

**Bradbury Special — Subjective Consensus**

GPU-job matching and completion verification are both multi-variable judgment calls (cost vs speed vs reliability; "did this defensibly complete") with no formula. Multiple validators reaching independent, defensible verdicts — rather than identical ones — is the problem Optimistic Democracy was built to solve, applied to a real decentralized-compute settlement problem instead of a single routing decision.

## License

MIT
