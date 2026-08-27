/**
 * Deploy ComputeRouter to GenLayer Bradbury Testnet, seed with realistic
 * GPU marketplace provider data, and run a test job routing + escrow flow.
 *
 * Usage: DEPLOYER_KEY=0x... node deploy/deploy-compute-bradbury.mjs
 */

import { createClient, createAccount } from 'genlayer-js';
import { testnetBradbury } from 'genlayer-js/chains';
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..');

const DEPLOYER_KEY = process.env.DEPLOYER_KEY;
if (!DEPLOYER_KEY) {
  console.error('Set DEPLOYER_KEY env var. Example:');
  console.error('  DEPLOYER_KEY=0xabc... node deploy/deploy-compute-bradbury.mjs');
  process.exit(1);
}

const account = createAccount(DEPLOYER_KEY);
const client = createClient({ chain: testnetBradbury, account });

console.log('Network:  Bradbury Testnet');
console.log('Deployer:', account.address);
console.log('RPC:     ', testnetBradbury.rpcUrls.default.http[0]);
console.log('Explorer: https://explorer-bradbury.genlayer.com');
console.log('');

async function deployContract(name, filePath) {
  console.log(`Deploying ${name}...`);
  const code = readFileSync(filePath, 'utf-8');

  const txHash = await client.deployContract({
    code: new TextEncoder().encode(code),
    args: [],
  });
  console.log(`  tx: ${txHash}`);
  console.log(`  explorer: https://explorer-bradbury.genlayer.com/txs/${txHash}`);

  const receipt = await client.waitForTransactionReceipt({
    hash: txHash,
    retries: 120,
    interval: 5000,
  });

  const addr = receipt.data?.contract_address
    || receipt.txDataDecoded?.contractAddress
    || receipt.contractAddress;

  console.log(`  ${name} deployed at: ${addr}`);
  return addr;
}

async function callWrite(addr, fn, args, label) {
  console.log(`  ${label}...`);
  const tx = await client.writeContract({
    address: addr, functionName: fn, args, value: BigInt(0),
  });
  await client.waitForTransactionReceipt({ hash: tx, retries: 120, interval: 5000 });
  console.log(`    done (${tx.slice(0, 10)}...)`);
  return tx;
}

async function main() {
  // Step 1: Deploy ComputeRouter (standalone, no oracle dependency —
  // this is the testnet-friendly contract with the built-in provider
  // registry, matching compute_router.py)
  const routerAddr = await deployContract('ComputeRouter', resolve(root, 'contracts/compute_router.py'));

  // Step 2: Seed realistic GPU marketplace providers
  console.log('\nSeeding providers...');
  const providers = [
    ['vastA100', { gpu_type: 'A100', vram_gb: 80, cost_per_hr: 1.10, reliability_pct: 97, queue_wait_min: 2 }],
    ['ioT4', { gpu_type: 'T4', vram_gb: 16, cost_per_hr: 0.19, reliability_pct: 91, queue_wait_min: 12 }],
    ['lambdaH100', { gpu_type: 'H100', vram_gb: 80, cost_per_hr: 2.49, reliability_pct: 99, queue_wait_min: 0 }],
    ['runpodA6000', { gpu_type: 'A6000', vram_gb: 48, cost_per_hr: 0.79, reliability_pct: 94, queue_wait_min: 5 }],
  ];
  for (const [id, data] of providers) {
    await callWrite(routerAddr, 'register_provider', [id, JSON.stringify(data)], `Register ${id}`);
  }

  // Step 3: Verify
  const providersData = await client.readContract({ address: routerAddr, functionName: 'get_providers', args: [] });
  console.log('  Providers:', providersData);

  // Step 4: Test routing — a mid-size fine-tuning job prioritizing cost
  console.log('\nTest routing (cost-sensitive 24GB job)...');
  const routeTx = await client.writeContract({
    address: routerAddr,
    functionName: 'route_job',
    args: [
      JSON.stringify({ vram_needed_gb: 24, est_hours: 3 }),
      JSON.stringify({ cost: 8, speed: 3, reliability: 5 }),
    ],
    value: BigInt(0),
  });
  console.log(`  tx: ${routeTx}`);
  console.log(`  explorer: https://explorer-bradbury.genlayer.com/txs/${routeTx}`);

  const routeReceipt = await client.waitForTransactionReceipt({ hash: routeTx, retries: 120, interval: 5000 });
  const routeResult = routeReceipt.data || routeReceipt.result;
  console.log('  Result:', JSON.stringify(routeResult, null, 2));
  const routeParsed = typeof routeResult === 'string' ? JSON.parse(routeResult) : routeResult;
  const routedJobId = routeParsed?.job_id;
  const routedProvider = routeParsed?.provider;

  // Step 5: Test escrow lifecycle — fund_escrow is payable now: the
  // escrowed amount is whatever real GEN value is attached to the tx, and
  // job_id/provider_id must match what route_job actually assigned above.
  if (routedJobId && routedProvider) {
    console.log('\nTest escrow lifecycle...');
    const fundTx = await client.writeContract({
      address: routerAddr,
      functionName: 'fund_escrow',
      args: [routedJobId, routedProvider],
      value: BigInt(1000), // real GEN attached — this is what gets locked
    });
    await client.waitForTransactionReceipt({ hash: fundTx, retries: 120, interval: 5000 });
    console.log(`  funded: ${fundTx}`);

    const resolveTx = await client.writeContract({
      address: routerAddr,
      functionName: 'resolve_completion',
      args: [routedJobId, JSON.stringify({ log_summary: 'completed in 2h51m', output_hash: '0xdeadbeef' })],
      value: BigInt(0),
    });
    console.log(`  resolve tx: ${resolveTx}`);
    const resolveReceipt = await client.waitForTransactionReceipt({ hash: resolveTx, retries: 120, interval: 5000 });
    console.log('  Escrow result:', JSON.stringify(resolveReceipt.data || resolveReceipt.result, null, 2));
  } else {
    console.log('\nSkipping escrow test — routing did not return a job_id.');
  }

  // Summary
  console.log('\n═══════════════════════════════════════════════════');
  console.log('BRADBURY DEPLOYMENT COMPLETE');
  console.log('═══════════════════════════════════════════════════');
  console.log(`ComputeRouter: ${routerAddr}`);
  console.log('');
  console.log('Explorer:');
  console.log(`  ${routerAddr}: https://explorer-bradbury.genlayer.com/address/${routerAddr}`);
  console.log('');
  console.log('Update frontend config:');
  console.log(`  COMPUTE_ROUTER_ADDR = '${routerAddr}'`);
  console.log('═══════════════════════════════════════════════════');
}

main().catch(e => { console.error('Deploy failed:', e); process.exit(1); });
