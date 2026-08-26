# ComputeRouter Demo Video — 2 minutes

## Setup Before Recording

- Deploy via `DEPLOYER_KEY=0x... node deploy/deploy-compute-bradbury.mjs`, note the router address
- Fill `frontend/compute-config.js` with the deployed address
- Open `frontend/compute-index.html` (defaults to Bradbury)
- Open https://explorer-bradbury.genlayer.com in another tab
- Confirm at least one successful `route_job` and one `resolve_completion` before recording

## Beat 1: The Problem (20s)

[Screen: ComputeRouter frontend, pause on the provider cards]

VOICEOVER:
"Decentralized GPU marketplaces already exist — but matching a job to a provider means trusting a centralized broker, or shopping around yourself. And once a job is routed, there's no trustless way to confirm it actually finished before you pay."

## Beat 2: The Providers (15s)

[Point to the 4 provider cards]

VOICEOVER:
"Four GPU providers, real tradeoffs. An A100 at a dollar ten an hour with a two minute queue. A T4 at nineteen cents but a twelve minute wait. An H100 with zero queue and 99% reliability, at two forty-nine an hour. There's no formula that picks the right one — it depends what you're optimizing for."

## Beat 3: Live Routing (45s)

[Set VRAM=24, hours=3, sliders: cost=8 speed=3 reliability=5. Click "Route Job".]

VOICEOVER:
"I need 24 gigs of VRAM for about three hours, and I care mostly about cost."

[Loading state: "Routing job through validator consensus"]

"The contract filters providers by VRAM first — that's a hard constraint, decided in code, not left to the model. Then the leader LLM picks the best tradeoff from what's left, and validators check whether that pick is defensible."

[Result appears: e.g. runpodA6000, with reasoning text]

"Routed to the A6000 — cheaper than the A100 or H100, still comfortably fits the VRAM requirement."

[Change sliders: cost=1 speed=2 reliability=9. Click "Route Job" again.]

"Same job, but now reliability is what matters most."

[Result appears: lambdaH100]

"This time it picks the H100 — zero queue, 99% reliability — even though it's the most expensive option. Same router, same job, different priorities, different — but still defensible — decision."

## Beat 4: Escrow & Completion (25s)

[Switch to Escrow tab. Fund escrow for a job. Switch to resolve, paste evidence, click Resolve.]

VOICEOVER:
"Once a job runs, payment sits in escrow until completion is verified — not by a centralized platform, but by the same subjective-consensus mechanism. The leader LLM looks at the submitted evidence — logs, output hash, duration — and judges whether the job was defensibly completed. Validators independently check that verdict before funds release."

[Result: status "released", reasoning shown]

"Released — the evidence supported completion. If it didn't, this would move to disputed instead of silently paying out."

## Beat 5: Close (15s)

[Back to frontend, hold on the ComputeRouter header]

VOICEOVER:
"ComputeRouter. Hard constraints decided in code, judgment calls decided by validators who have to defend their reasoning — applied twice, once to route the job, once to release the payment. Subjective consensus for a real decentralized compute market."

---

## Recording Notes

- QuickTime screen recording or OBS, 1920x1080
- Keep mouse movements slow and deliberate
- If routing takes >30s, cut during the "Routing job..." spinner and resume when the result appears
- Total target: 1:50–2:10
- The two live routing demos (cost-priority vs reliability-priority) are the core beat — confirm both succeed before recording
