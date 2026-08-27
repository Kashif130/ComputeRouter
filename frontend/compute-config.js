// Frontend config for ComputeRouter — copy to config.js and fill in your values.
// Never commit a real deployer private key to a public repo.
window.COMPUTE_CONFIG = {
  BRADBURY_ROUTER_ADDR: '0x09E7230c108918e7d6587Ff20F8C81885302Bdec',        // from deploy/deploy-compute-bradbury.mjs output
  STUDIONET_ROUTER_ADDR: '0xCb850619fead89c9B3F2eBdF047e7DC50220aDC6',       // from deploy/deploy-compute-studionet.mjs output
  STUDIONET_ORACLE_ADDR: '0xf54c6e75c7d42054E6c2940906934B25f8e5B839',       // ProviderOracle address — also printed by deploy-compute-studionet.mjs
  BRADBURY_DEPLOYER_KEY: '',       // hackathon testnet demo key only — leave blank to auto-generate a fresh throwaway account
};
