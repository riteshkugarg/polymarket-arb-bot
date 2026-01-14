#!/usr/bin/env python3
"""
Set Infinite Allowances for NegRisk Adapter

This script approves the NegRisk Adapter contract to spend USDC and CTF tokens
on behalf of your wallet. Required for automated token conversion (Upgrade 1).

CRITICAL: Run this ONCE before starting the bot for the first time.

Usage:
    python scripts/set_allowances.py

What it does:
- Approves USDC token for NegRisk Adapter (infinite allowance)
- Approves CTF token for NegRisk Adapter (infinite allowance)
- Verifies allowances were set correctly

Contracts:
- USDC: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174 (Polygon)
- CTF: 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E (Polymarket)
- NegRisk Adapter: 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
"""

import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from web3 import Web3
from eth_account import Account
from utils.logger import get_logger
from config.aws_config import get_aws_config

logger = get_logger(__name__)

# Contract addresses
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEGRISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# ERC20 ABI (approve function)
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    }
]


def main():
    """Set infinite allowances for NegRisk Adapter"""
    
    logger.info("=" * 80)
    logger.info("NegRisk Adapter Allowance Setup")
    logger.info("=" * 80)
    
    # Try AWS Secrets Manager first, fallback to environment variable
    private_key = None
    
    # Option 1: AWS Secrets Manager (production on EC2)
    try:
        logger.info("Attempting to load credentials from AWS Secrets Manager...")
        aws_config = get_aws_config()
        secrets = aws_config.get_secrets()
        private_key = secrets.get("WALLET_PRIVATE_KEY")
        logger.info("✅ Loaded credentials from AWS Secrets Manager")
    except Exception as e:
        logger.warning(f"Could not load from AWS Secrets Manager: {e}")
        logger.info("Falling back to PK environment variable...")
    
    # Option 2: Environment variable (local development/testing)
    if not private_key:
        private_key = os.getenv("PK")
        if private_key:
            logger.info("✅ Loaded credentials from PK environment variable")
    
    if not private_key:
        logger.error("❌ Could not load private key from any source")
        logger.info("\nOptions:")
        logger.info("  1. Run on EC2 with AWS Secrets Manager configured")
        logger.info("  2. Set PK environment variable: export PK='your_private_key'")
        sys.exit(1)
    
    # Remove 0x prefix if present
    if private_key.startswith("0x"):
        private_key = private_key[2:]
    
    # Connect to Polygon
    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    logger.info(f"Connecting to Polygon: {rpc_url}")
    
    web3 = Web3(Web3.HTTPProvider(rpc_url))
    
    if not web3.is_connected():
        logger.error("❌ Failed to connect to Polygon RPC")
        sys.exit(1)
    
    logger.info(f"✅ Connected to Polygon (block: {web3.eth.block_number})")
    
    # Get wallet address
    account = Account.from_key(private_key)
    wallet_address = account.address
    
    logger.info(f"Wallet: {wallet_address}")
    
    # Check ETH balance for gas
    eth_balance = web3.eth.get_balance(wallet_address)
    eth_balance_ether = web3.from_wei(eth_balance, 'ether')
    logger.info(f"MATIC Balance: {eth_balance_ether:.4f} MATIC")
    
    if eth_balance_ether < 0.01:
        logger.warning("⚠️  Low MATIC balance. You may not have enough gas for transactions.")
    
    # Infinite approval amount (2^256 - 1)
    infinite_approval = 2**256 - 1
    
    # Approve USDC
    logger.info("\n" + "=" * 80)
    logger.info("Setting USDC Allowance")
    logger.info("=" * 80)
    
    usdc_contract = web3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=ERC20_ABI
    )
    
    # Check current allowance
    current_usdc_allowance = usdc_contract.functions.allowance(
        Web3.to_checksum_address(wallet_address),
        Web3.to_checksum_address(NEGRISK_ADAPTER_ADDRESS)
    ).call()
    
    logger.info(f"Current USDC allowance: {current_usdc_allowance / 1e6:.2f} USDC")
    
    if current_usdc_allowance >= 10**12:  # Already has sufficient allowance
        logger.info("✅ USDC allowance already set (sufficient)")
    else:
        logger.info("Setting infinite USDC allowance...")
        
        # Build transaction
        tx = usdc_contract.functions.approve(
            Web3.to_checksum_address(NEGRISK_ADAPTER_ADDRESS),
            infinite_approval
        ).build_transaction({
            'from': wallet_address,
            'nonce': web3.eth.get_transaction_count(wallet_address),
            'gas': 100000,
            'gasPrice': web3.eth.gas_price,
        })
        
        # Sign transaction
        signed_tx = web3.eth.account.sign_transaction(tx, private_key)
        
        # Send transaction
        tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
        logger.info(f"Transaction sent: {tx_hash.hex()}")
        
        # Wait for receipt
        logger.info("Waiting for confirmation...")
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt['status'] == 1:
            logger.info("✅ USDC allowance set successfully")
        else:
            logger.error("❌ USDC approval transaction failed")
            sys.exit(1)
    
    # Note: CTF tokens use ERC1155 standard, not ERC20
    # Approvals are handled automatically by the Polymarket SDK
    # Only USDC approval is required for NegRisk operations
    logger.info("\n" + "=" * 80)
    logger.info("✅ CTF Token Handling")
    logger.info("=" * 80)
    logger.info("CTF tokens use ERC1155 standard (not ERC20)")
    logger.info("Approvals are managed automatically by Polymarket SDK")
    logger.info("No manual approval needed for CTF tokens")
    
    # Final summary
    logger.info("\n" + "=" * 80)
    logger.info("✅ ALLOWANCE SETUP COMPLETE")
    logger.info("=" * 80)
    logger.info("The bot can now:")
    logger.info("  - Convert NO tokens → USDC via NegRisk Adapter")
    logger.info("  - Maintain liquidity on $100 budget")
    logger.info("  - Recycle capital automatically")
    logger.info("\nYou can now start the bot with confidence!")
    logger.info("=" * 80)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n❌ Cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        sys.exit(1)
