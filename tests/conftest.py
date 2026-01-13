"""
Test Configuration Module
Provides fixtures and shared test utilities
"""

import pytest
import asyncio
import sys
import os
from unittest.mock import Mock, MagicMock
from decimal import Decimal

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))

from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager


@pytest.fixture
def mock_aws_secrets(monkeypatch):
    """Mock AWS Secrets Manager for testing"""
    mock_get_secrets = MagicMock(return_value={
        'WALLET_PRIVATE_KEY': '0x' + '1' * 64  # Mock private key
    })
    
    monkeypatch.setattr(
        'config.aws_config.AWSConfig.get_secrets',
        mock_get_secrets
    )
    
    return mock_get_secrets


@pytest.fixture
async def mock_client(mock_aws_secrets):
    """Create a mock Polymarket client for testing"""
    client = PolymarketClient()
    
    # Mock the initialization
    client._is_initialized = True
    client._account = Mock()
    client._account.address = '0x5967c88F93f202D595B9A47496b53E28cD61F4C3'
    client._client = Mock()
    
    return client


@pytest.fixture
async def mock_order_manager(mock_client):
    """Create a mock order manager for testing"""
    return OrderManager(mock_client)


@pytest.fixture
def sample_market_data():
    """Sample market data for testing"""
    return {
        'condition_id': 'test_condition_123',
        'question': 'Will BTC reach $100k by EOY?',
        'tokens': [
            {
                'token_id': 'token_yes_123',
                'outcome': 'Yes',
                'price': 0.65
            },
            {
                'token_id': 'token_no_123',
                'outcome': 'No',
                'price': 0.35
            }
        ]
    }


@pytest.fixture
def sample_order_book():
    """Sample order book for testing"""
    return {
        'bids': [
            {'price': 0.64, 'size': 100},
            {'price': 0.63, 'size': 200},
        ],
        'asks': [
            {'price': 0.66, 'size': 150},
            {'price': 0.67, 'size': 250},
        ]
    }


@pytest.fixture
def sample_positions():
    """Sample positions for testing"""
    return [
        {
            'token_id': 'token_yes_123',
            'size': 50.0,
            'avg_price': 0.60,
        },
        {
            'token_id': 'token_yes_456',
            'size': 30.0,
            'avg_price': 0.45,
        }
    ]


@pytest.fixture
def event_loop():
    """Create an event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()
