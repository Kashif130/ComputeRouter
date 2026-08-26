# Deploying ComputeRouter's Frontend to Vercel

The frontend (`frontend/compute-index.html`) is a static page — no framework —
but it now uses a tiny build step so each network's router address can be
managed as a **Vercel Environment Variable** instead of being hand-edited in
a file. This way:

- `BRADBURY_ROUTER_ADDR` → used whenever the network dropdown is on **Bradbury Testnet**
- `STUDIONET_ROUTER_ADDR` → used whenever it's on **GenLayer Studio**

Switching the dropdown just switches which of these two the page reads —
nothing to change in code.

## 1. Deploy your contracts

```bash
npm install -g genlayer
pip install genlayer-test

# Bradbury Testnet
DEPLOYER_KEY=0x... node deploy/deploy-compute-bradbury.mjs

# GenLayer Studio (studionet — no key needed, hits studio.genlayer.com)
node deploy/deploy-compute-studionet.mjs
```

Each script prints a router address at the end — copy both. If you already
deployed to Bradbury earlier, you only need to run the Studio one now and
grab its address.

## 2. Set the two Environment Variables in Vercel

In your Vercel project: **Settings → Environment Variables**, add:

| Name | Value | Environments |
|---|---|---|
| `BRADBURY_ROUTER_ADDR` | your Bradbury router address | Production, Preview, Development |
| `STUDIONET_ROUTER_ADDR` | your GenLayer Studio router address | Production, Preview, Development |

(If `BRADBURY_ROUTER_ADDR` is already there from before, just add
`STUDIONET_ROUTER_ADDR` alongside it.)

## 3. Redeploy

Vercel does **not** pick up new/changed env vars on an existing deployment —
you need a new build. Either:
- push any commit, or
- go to **Deployments** → the three-dot menu on the latest one → **Redeploy**.

At build time, `frontend/generate-config.mjs` (wired up via
`frontend/package.json`'s `build` script) reads both variables and writes
`frontend/compute-config.js`, which the page loads. You can see the values it
picked up in the Vercel build logs.

## First-time project setup (skip if already deployed once)

1. [vercel.com/new](https://vercel.com/new) → import the repo.
2. **Root Directory** → set to `frontend`.
3. **Framework Preset** → "Other".
4. Leave Build Command / Output Directory as default — `frontend/vercel.json`
   already sets `buildCommand: npm run build` and `outputDirectory: .`, and
   its rewrite serves `compute-index.html` at `/`.
5. Add the two environment variables from step 2 above *before* clicking
   Deploy (or add them after and redeploy once).

## Verify

- Open the deployed URL.
- Switch the network dropdown to **Bradbury Testnet** — Providers/History
  tabs should load data from your Bradbury contract.
- Switch to **GenLayer Studio** — same tabs should now load from your Studio
  contract instead.
- Click **Connect Wallet**, pick an installed EVM wallet, and try **Route a
  Job** on each network to confirm both write paths work.

## Local (non-Vercel) use

There's no build step when just opening the HTML file locally. Either run
`node frontend/generate-config.mjs` yourself with the env vars set in your
shell, or hand-edit `frontend/compute-config.js` directly (copy it from
`frontend/compute-config.example.js` as a starting point):

```js
window.COMPUTE_CONFIG = {
  BRADBURY_ROUTER_ADDR: '0xYourBradburyRouterAddress',
  STUDIONET_ROUTER_ADDR: '0xYourStudioRouterAddress',
};
```

## Notes

- These are public contract addresses, not secrets — using env vars here is
  for convenience (change an address without touching code), not privacy.
- No private key lives on Vercel or in the frontend at all; the connected
  wallet signs every transaction client-side.
- CLI alternative to the dashboard:
  ```bash
  npm install -g vercel
  cd frontend
  vercel env add BRADBURY_ROUTER_ADDR
  vercel env add STUDIONET_ROUTER_ADDR
  vercel --prod
  ```
