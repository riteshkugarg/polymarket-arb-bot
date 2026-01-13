#!/usr/bin/env python3
"""
Regenerate L2 API Credentials for Polymarket

Per Polymarket Support (FINAL - CORRECTED):
- Python method: create_or_derive_api_creds() (with "creds" not "key")
- TypeScript equivalent: createOrDeriveApiKey()
- Each wallet can only have ONE active API key
- create_or_derive_api_creds() handles everything automatically (no manual deletion needed)
- Returns existing valid credentials OR creates new ones if corrupted/invalid
- Must reinitialize client with new credentials after generation
- Method is SYNCHRONOUS - call without await

Method Differences:
- create_or_derive_api_creds() - Gets existing or creates new (RECOMMENDED)
- create_api_key() - Always creates new, invalidates old (causes 400 if key exists)
- derive_api_key() - Retrieves existing with same nonce

Usage:
    python scripts/regenerate_l2_credentials.py

What this script does:
1. Loads wallet private key from AWS Secrets Manager
2. Initializes client WITHOUT L2 credentials
3. Calls create_or_derive_api_creds() to get/create credentials
4. Reinitializes client WITH new credentials (verification)
5. Updates AWS Secrets Manager with new L2 credentials

IMPORTANT: Run this from your eu-west-1 EC2 instance with IAM role attached
"""

import asyncio
import sys
import os
import json

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from config.constants import (
    CLOB_API_URL,
    POLYGON_CHAIN_ID,
    PROXY_WALLET_ADDRESS,
    AWS_REGION,
    AWS_SECRET_ID
)
from config.aws_config import get_aws_config
import boto3


async def regenerate_credentials():
    """Regenerate L2 API credentials using Polymarket's method"""
    
    print("=" * 80)
    print("Polymarket L2 Credentials Regeneration")
    print("=" * 80)
    print()
    
    # Step 1: Load wallet private key
    print("📥 Step 1: Loading wallet private key from AWS Secrets Manager...")
    try:
        aws_config = get_aws_config()
        private_key = aws_config.get_wallet_private_key()
        print(f"✅ Private key loaded successfully")
        print(f"   Region: {AWS_REGION}")
        print(f"   Secret ID: {AWS_SECRET_ID}")
    except Exception as e:
        print(f"❌ Failed to load private key: {e}")
        print("\nTroubleshooting:")
        print("1. Verify IAM role is attached to EC2 instance")
        print("2. Check Secrets Manager permissions")
        print("3. Verify secret exists in eu-west-1 region")
        return False
    
    print()
    
    # Step 2: Initialize CLOB client WITHOUT L2 credentials
    print("🔧 Step 2: Initializing CLOB client (without L2 credentials)...")
    try:
        # Per Polymarket: Initialize with ONLY private key, no creds
        client = ClobClient(
            host=CLOB_API_URL,
            chain_id=POLYGON_CHAIN_ID,
            key=private_key
        )
        print(f"✅ CLOB client initialized")
        print(f"   Host: {CLOB_API_URL}")
        print(f"   Chain ID: {POLYGON_CHAIN_ID}")
    except Exception as e:
        print(f"❌ Failed to initialize client: {e}")
        return False
    
    print()
    
    # Step 3: Get existing or create new L2 credentials
    print("🔑 Step 3: Getting or creating L2 API credentials...")
    print("   Method: create_or_derive_api_creds()")
    print("   This will:")
    print("   - Return existing valid credentials if they work")
    print("   - Create fresh credentials if old ones are invalid")
    print("   - Automatically invalidate corrupted credentials")
    print("   - No manual deletion needed")
    print()
    
    try:
        # Per Polymarket: Use create_or_derive_api_creds() (with "creds" not "key")
        # This is NOT async - call synchronously without await
        api_creds = client.create_or_derive_api_creds()
        
        print("✅ L2 credentials generated successfully!")
        print()
        print("Credential Details:")
        print(f"   API Key: {api_creds.api_key[:8]}...{api_creds.api_key[-4:]}")
        print(f"   API Secret: {api_creds.api_secret[:8]}...{api_creds.api_secret[-4:]}")
        print(f"   API Passphrase: {api_creds.api_passphrase[:4]}...{api_creds.api_passphrase[-2:]}")
        print()
        
    except Exception as e:
        print(f"❌ Failed to generate credentials: {e}")
        print("\nCommon causes:")
        print("1. Network connectivity issues")
        print("2. Invalid wallet private key")
        print("3. Polymarket API temporarily unavailable")
        return False
    
    # Step 4: Verify credentials by reinitializing client
    print("🔧 Step 4: Verifying credentials...")
    print("   Reinitializing client with new credentials...")
    print()
    
    try:
        # Per Polymarket: Reinitialize client with new credentials
        client = ClobClient(
            host=CLOB_API_URL,
            chain_id=POLYGON_CHAIN_ID,
            key=private_key,
            creds=api_creds,
            signature_type=2,
            funder=PROXY_WALLET_ADDRESS
        )
        print("✅ Client reinitialized with new credentials!")
        print("   Credentials are now active and ready to use")
        print()
        
    except Exception as e:
        print(f"❌ Failed to reinitialize client: {e}")
        return False
    
    # Step 5: Update AWS Secrets Manager
    print("💾 Step 5: Updating AWS Secrets Manager...")
    print(f"   Region: {AWS_REGION}")
    print(f"   Secret ID: {AWS_SECRET_ID}")
    print()
    
    try:
        secrets_client = boto3.client('secretsmanager', region_name=AWS_REGION)
        
        # Get current secrets
        response = secrets_client.get_secret_value(SecretId=AWS_SECRET_ID)
        current_secrets = json.loads(response['SecretString'])
        
        # Update L2 credentials (keep wallet private key)
        current_secrets['POLY_API_KEY'] = api_creds.api_key
        current_secrets['POLY_API_SECRET'] = api_creds.api_secret
        current_secrets['POLY_API_PASS'] = api_creds.api_passphrase
        
        # Write back to Secrets Manager
        secrets_client.update_secret(
            SecretId=AWS_SECRET_ID,
            SecretString=json.dumps(current_secrets)
        )
        
        print("✅ AWS Secrets Manager updated successfully!")
        print()
        
    except Exception as e:
        print(f"❌ Failed to update Secrets Manager: {e}")
        print()
        print("⚠️  MANUAL UPDATE REQUIRED:")
        print()
        print("Run these commands to update manually:")
        print()
        print(f'aws secretsmanager update-secret \\')
        print(f'  --secret-id {AWS_SECRET_ID} \\')
        print(f'  --region {AWS_REGION} \\')
        print(f"  --secret-string '{{")
        print(f'    "WALLET_PRIVATE_KEY": "<your_private_key>",')
        print(f'    "POLY_API_KEY": "{api_creds.api_key}",')
        print(f'    "POLY_API_SECRET": "{api_creds.api_secret}",')
        print(f'    "POLY_API_PASS": "{api_creds.api_passphrase}"')
        print(f"  }}' ")
        print()
        return False
    
    # Success summary
    print("=" * 80)
    print("✅ SUCCESS - L2 Credentials Regenerated")
    print("=" * 80)
    print()
    print("Next Steps:")
    print("1. Restart your bot: sudo systemctl restart polymarket-bot")
    print("2. Check logs: sudo journalctl -u polymarket-bot -f")
    print("3. Verify no more 401 errors appear")
    print()
    print("Expected log output:")
    print("   ✅ L2 API credentials loaded successfully")
    print("   ✅ CLOB client initialized with L2 authentication")
    print("   ✓ BUY/SELL order executed: [order_id]")
    print()
    
    return True


if __name__ == "__main__":
    success = asyncio.run(regenerate_credentials())
    sys.exit(0 if success else 1)
