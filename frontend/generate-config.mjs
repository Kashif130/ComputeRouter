// Runs at Vercel build time (see package.json "build" script).
// Reads router addresses from Vercel Environment Variables and writes them
// into compute-config.js, which compute-index.html loads via <script src>.
//
// Set these in Vercel: Project → Settings → Environment Variables
//   BRADBURY_ROUTER_ADDR   = 0x... (from deploy/deploy-compute-bradbury.mjs)
//   STUDIONET_ROUTER_ADDR  = 0x... (from deploy/deploy-compute-studionet.mjs)
//   STUDIONET_ORACLE_ADDR  = 0x... (ProviderOracle address, also printed by
//                                    deploy/deploy-compute-studionet.mjs —
//                                    the Studio tab reads the provider list
//                                    from here, not from the router)
//
// After adding/changing an env var in Vercel you must trigger a new
// deployment (Vercel doesn't rebuild automatically just because a var
// changed) — either push a commit or click "Redeploy" in the dashboard.

import { writeFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));

const bradbury = process.env.BRADBURY_ROUTER_ADDR || '';
const studio = process.env.STUDIONET_ROUTER_ADDR || '';
const studioOracle = process.env.STUDIONET_ORACLE_ADDR || '';

const content = `// AUTO-GENERATED at build time from Vercel Environment Variables.
// Do not hand-edit on Vercel — edit BRADBURY_ROUTER_ADDR / STUDIONET_ROUTER_ADDR /
// STUDIONET_ORACLE_ADDR in Project Settings > Environment Variables instead,
// then redeploy. For local (non-Vercel) use, editing this file directly is fine.
window.COMPUTE_CONFIG = {
  BRADBURY_ROUTER_ADDR: '${bradbury}',
  STUDIONET_ROUTER_ADDR: '${studio}',
  STUDIONET_ORACLE_ADDR: '${studioOracle}',
};
`;

writeFileSync(resolve(__dirname, 'compute-config.js'), content);

console.log('Generated compute-config.js from environment variables:');
console.log('  BRADBURY_ROUTER_ADDR:', bradbury || '(empty — Bradbury tab will show "not configured")');
console.log('  STUDIONET_ROUTER_ADDR:', studio || '(empty — Studio tab will show "not configured")');
console.log('  STUDIONET_ORACLE_ADDR:', studioOracle || '(empty — Studio provider list will show "not configured")');
