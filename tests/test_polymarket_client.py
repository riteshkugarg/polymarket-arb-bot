"""
Tests for Polymarket Client
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from decimal import Decimal

from core.polymarket_client import PolymarketClient
from utils.exceptions import APIError, AuthenticationError


@pytest.mark.asyncio
class TestPolymarketClient:
    """Test Polymarket client functionality"""
    
    async def test_client_initialization(self, mock_aws_secrets):
        """Test client initialization"""
        client = PolymarketClient()
        
        assert client._is_initialized is False
        assert client._client is None
    
    async def test_lazy_initialization(self, mock_aws_secrets):
        """Test lazy initialization on first use"""
        with patch('core.polymarket_client.ClobClient') as mock_clob:
            with patch('core.polymarket_client.Account') as mock_account:
                mock_account.from_key.return_value = Mock(
                    address='0x5967c88F93f202D595B9A47496b53E28cD61F4C3'
                )
                
                client = PolymarketClient()
                await client.initialize()
                
                assert client._is_initialized is True
                mock_clob.assert_called_once()
    
    async def test_wallet_address_property(self, mock_client):
        """Test wallet address property"""
        address = mock_client.wallet_address
        
        assert address.startswith('0x')
        assert len(address) == 42
    
    async def test_ensure_initialized_raises(self):
        """Test error when using uninitialized client"""
        client = PolymarketClient()
        
        with pytest.raises(AuthenticationError, match="not initialized"):
            client._ensure_initialized()
    
    async def test_get_balance(self, mock_client):
        """Test balance retrieval"""
        mock_client._client.get_balance.return_value = 1000000000  # 1000 USDC
        
        with patch('asyncio.to_thread', new=AsyncMock(return_value=1000000000)):
            balance = await mock_client.get_balance()
            
            assert isinstance(balance, Decimal)
            assert balance == Decimal('1000.0')
    
    async def test_get_markets(self, mock_client, sample_market_data):
        """Test market data retrieval"""
        mock_response = {'data': [sample_market_data]}
        
        with patch('asyncio.to_thread', new=AsyncMock(return_value=mock_response)):
            markets = await mock_client.get_markets()
            
            assert 'data' in markets
            assert len(markets['data']) > 0
    
    async def test_get_order_book(self, mock_client, sample_order_book):
        """Test order book retrieval"""
        with patch('asyncio.to_thread', new=AsyncMock(return_value=sample_order_book)):
            order_book = await mock_client.get_order_book('token_123')
            
            assert 'bids' in order_book
            assert 'asks' in order_book
    
    async def test_get_positions(self, mock_client, sample_positions):
        """Test position retrieval"""
        with patch('asyncio.to_thread', new=AsyncMock(return_value=sample_positions)):
            positions = await mock_client.get_positions()
            
            assert isinstance(positions, list)
            assert len(positions) > 0
    
    async def test_get_best_price_buy(self, mock_client, sample_order_book):
        """Test getting best buy price"""
        with patch.object(mock_client, 'get_order_book', return_value=sample_order_book):
            price = await mock_client.get_best_price('token_123', 'BUY')
            
            # Best ask (lowest sell price)
            assert price == 0.66
    
    async def test_get_best_price_sell(self, mock_client, sample_order_book):
        """Test getting best sell price"""
        with patch.object(mock_client, 'get_order_book', return_value=sample_order_book):
            price = await mock_client.get_best_price('token_123', 'SELL')
            
            # Best bid (highest buy price)
            assert price == 0.64
    
    async def test_api_error_handling(self, mock_client):
        """Test API error handling"""
        with patch('asyncio.to_thread', side_effect=Exception("API Error")):
            with pytest.raises(APIError, match="API Error"):
                await mock_client.get_markets()
    
    async def test_close_client(self, mock_client):
        """Test client cleanup"""
        await mock_client.close()
        
        assert mock_client._is_initialized is False
        assert mock_client._client is None


@pytest.mark.unit
class TestClientRetry:
    """Test retry logic"""
    
    @pytest.mark.asyncio
    async def test_retry_on_network_error(self, mock_client):
        """Test retry behavior on network errors"""
        call_count = 0
        
        async def failing_call():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Network error")
            return {'data': []}
        
        with patch('asyncio.to_thread', side_effect=failing_call):
            # Should succeed after retries
            result = await mock_client.get_markets()
            
            assert 'data' in result
            assert call_count == 3
