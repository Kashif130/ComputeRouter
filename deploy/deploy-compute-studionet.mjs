/**
 * Deploy ProviderOracle + ComputeRouterFull to GenLayer Studionet.
 * No Docker needed — uses hosted studio.genlayer.com/api.
 *
 * This is the cross-contract path (mainnet-style): ComputeRouterFull reads
 * live provider data from ProviderOracle instead of holding its own
 * registry. Use this to demo genuine validator re-reasoning + reliability
 * scores computed from real escrow history.
 *
 * Usage: node deploy/deploy-compute-studionet.mjs
 */

import { createClient, createAccount, generatePrivateKey } from 'genlayer-js';
import { studionet } from 'genlayer-js/chains';
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..');

const account = createAccount(generatePrivateKey());
const client = createClient({ chain: studionet, account });

console.log('Deployer address:', account.address);
console.log('Connected to:', studionet.rpcUrls.default.http[0]);
console.log('');

async function deployContract(name, filePath, args = []) {
  console.log(`Deploying ${name}...`);
  const code = readFileSync(filePath, 'utf-8');
  try {
    const txHash = await client.deployContract({ code: new TextEncoder().encode(code), args });
    console.log(`  tx: ${txHash}`);
    const receipt = await client.waitForTransactionReceipt({ hash: txHash, retries: 60, interval: 5000 });
    const addr = receipt.data?.contract_address || receipt.txDataDecoded?.contractAddress || receipt.contractAddress;
    console.log(`  ${name} deployed at: ${addr}`);
    return addr;
  } catch (e) {
    console.error(`  FAILED: ${e.message}`);
    throw e;
  }
}

async function callWrite(addr, fn, args, label) {
  console.log(`  ${label}...`);
  try {
    const tx = await client.writeContract({ address: addr, functionName: fn, args, value: BigInt(0) });
    await client.waitForTransactionReceipt({ hash: tx, retries: 30, interval: 3000 });
    console.log(`    done`);
  } catch (e) {
    console.error(`    FAILED: ${e.message}`);
  }
}

async function main() {
  // Step 1: Deploy ProviderOracle
  const oracleAddr = await deployContract('ProviderOracle', resolve(root, 'contracts/provider_oracle.py'));
  await callWrite(oracleAddr, 'set_owner', [], 'Set oracle owner');

  // Step 2: Seed providers — each with a payout address the escrow will
  // actually pay out to (using the deployer's own address for this demo;
  // in production each provider would supply its own wallet)
  console.log('\nSeeding providers...');
  const providers = [
    ['vastA100', { gpu_type: 'A100', vram_gb: 80, cost_per_hr: 1.10 }],
    ['ioT4', { gpu_type: 'T4', vram_gb: 16, cost_per_hr: 0.19 }],
    ['lambdaH100', { gpu_type: 'H100', vram_gb: 80, cost_per_hr: 2.49 }],
    ['runpodA6000', { gpu_type: 'A6000', vram_gb: 48, cost_per_hr: 0.79 }],
  ];
  for (const [id, data] of providers) {
    await callWrite(oracleAddr, 'register_provider', [id, JSON.stringify(data), account.address], `Register ${id}`);
  }

  const providersData = await client.readContract({ address: oracleAddr, functionName: 'get_all_providers', args: [] });
  console.log('  Providers:', providersData);

  // Step 3: Deploy ComputeRouterFull with NO constructor args — it has no
  // __init__ accepting parameters, so args here would just be silently
  // wrong. Owner is set explicitly afterwards via set_owner(), same as
  // the oracle.
  const routerAddr = await deployContract('ComputeRouterFull', resolve(root, 'contracts/compute_router_full.py'));
  await callWrite(routerAddr, 'set_owner', [account.address], 'Set router owner');
  await callWrite(routerAddr, 'set_oracle', [oracleAddr], 'Point router at oracle');

  // Step 4: Test routing
  console.log('\nTest routing...');
  let routedJobId = null;
  try {
    const tx = await client.writeContract({
      address: routerAddr,
      functionName: 'route_job',
      args: [
        JSON.stringify({ vram_needed_gb: 24, est_hours: 3 }),
        JSON.stringify({ cost: 8, speed: 3, reliability: 5 }),
      ],
      value: BigInt(0),
    });
    console.log(`  tx: ${tx}`);
    const receipt = await client.waitForTransactionReceipt({ hash: tx, retries: 60, interval: 5000 });
    const result = receipt.data || receipt.result;
    console.log('  Result:', JSON.stringify(result, null, 2));
    const parsed = typeof result === 'string' ? JSON.parse(result) : result;
    routedJobId = parsed?.job_id || null;
  } catch (e) {
    console.error(`  Test routing failed: ${e.message}`);
  }

  // Step 5: Test the full escrow lifecycle — fund with REAL value, then
  // resolve. This exercises the payable path end to end.
  if (routedJobId) {
    console.log('\nTest escrow lifecycle...');
    try {
      const providerOfJob = JSON.parse(providersData);
      const firstProviderId = Object.keys(providerOfJob)[0];
      const fundTx = await client.writeContract({
        address: routerAddr,
        functionName: 'fund_escrow',
        args: [routedJobId, firstProviderId],
        value: BigInt(1000), // 1000 wei-equivalent GEN for the demo
      });
      await client.waitForTransactionReceipt({ hash: fundTx, retries: 60, interval: 5000 });
      console.log(`  funded: ${fundTx}`);

      const resolveTx = await client.writeContract({
        address: routerAddr,
        functionName: 'resolve_completion',
        args: [routedJobId, JSON.stringify({ log_summary: 'completed in 2h51m', output_hash: '0xdeadbeef' })],
        value: BigInt(0),
      });
      const resolveReceipt = await client.waitForTransactionReceipt({ hash: resolveTx, retries: 60, interval: 5000 });
      console.log('  Escrow result:', JSON.stringify(resolveReceipt.data || resolveReceipt.result, null, 2));
    } catch (e) {
      console.error(`  Escrow test failed (may be a provider/job_id mismatch on this run): ${e.message}`);
    }
  }

  // Summary
  console.log('\n═══════════════════════════════════════');
  console.log('DEPLOYMENT COMPLETE');
  console.log('═══════════════════════════════════════');
  console.log(`ProviderOracle:    ${oracleAddr}`);
  console.log(`ComputeRouterFull: ${routerAddr}`);
  console.log('');
  console.log('Update frontend config:');
  console.log(`  PROVIDER_ORACLE_ADDR = '${oracleAddr}';`);
  console.log(`  COMPUTE_ROUTER_ADDR  = '${routerAddr}';`);
  console.log('═══════════════════════════════════════');
}

main().catch(e => { console.error('Deploy failed:', e); process.exit(1); });
