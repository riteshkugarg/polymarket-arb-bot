"""
Tests for Mirror Strategy
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime

from strategies.mirror_strategy import MirrorStrategy
from utils.exceptions import StrategyError


@pytest.mark.asyncio
class TestMirrorStrategy:
    """Test mirror trading strategy"""
    
    async def test_strategy_initialization(self, mock_client, mock_order_manager):
        """Test strategy initialization"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        assert strategy.name == 'MirrorStrategy'
        assert strategy.target_address is not None
        assert strategy.is_running is False
    
    async def test_get_target_positions(self, mock_client, mock_order_manager, sample_positions):
        """Test retrieving target wallet positions"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        with patch.object(mock_client, 'get_positions', return_value=sample_positions):
            positions = await strategy._get_target_positions()
            
            assert isinstance(positions, dict)
            assert len(positions) == len(sample_positions)
    
    async def test_get_own_positions(self, mock_client, mock_order_manager, sample_positions):
        """Test retrieving own positions"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        with patch.object(mock_client, 'get_positions', return_value=sample_positions):
            positions = await strategy._get_own_positions()
            
            assert isinstance(positions, dict)
    
    async def test_find_position_differences_new_position(
        self, mock_client, mock_order_manager
    ):
        """Test finding new positions to mirror"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        target_positions = {'token_123': 100.0}
        own_positions = {}
        
        with patch.object(mock_client, 'get_best_price', return_value=0.65):
            opportunities = await strategy._find_position_differences(
                target_positions,
                own_positions
            )
            
            assert len(opportunities) == 1
            assert opportunities[0]['action'] == 'BUY'
            assert opportunities[0]['token_id'] == 'token_123'
            assert opportunities[0]['size'] == 100.0
    
    async def test_find_position_differences_close_position(
        self, mock_client, mock_order_manager
    ):
        """Test closing positions not in target"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        target_positions = {}
        own_positions = {'token_123': 50.0}
        
        with patch.object(mock_client, 'get_best_price', return_value=0.65):
            opportunities = await strategy._find_position_differences(
                target_positions,
                own_positions
            )
            
            assert len(opportunities) == 1
            assert opportunities[0]['action'] == 'SELL'
            assert opportunities[0]['size'] == 50.0
    
    async def test_should_execute_trade_valid(self, mock_client, mock_order_manager):
        """Test trade execution validation - valid case"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        opportunity = {
            'action': 'BUY',
            'token_id': 'token_123',
            'size': 50.0,
            'current_price': 0.65,
            'target_price': 0.65,
        }
        
        with patch.object(mock_client, 'get_balance', return_value=1000.0):
            with patch.object(mock_client, 'get_positions', return_value=[]):
                should_execute = await strategy.should_execute_trade(opportunity)
                
                assert should_execute is True
    
    async def test_should_execute_trade_dust_amount(
        self, mock_client, mock_order_manager
    ):
        """Test rejecting dust-sized trades"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        opportunity = {
            'action': 'BUY',
            'token_id': 'token_123',
            'size': 0.01,  # Below dust threshold
            'current_price': 0.65,
        }
        
        should_execute = await strategy.should_execute_trade(opportunity)
        
        assert should_execute is False
        assert 'dust threshold' in opportunity.get('reason', '').lower()
    
    async def test_should_execute_trade_insufficient_balance(
        self, mock_client, mock_order_manager
    ):
        """Test rejecting trades with insufficient balance"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        opportunity = {
            'action': 'BUY',
            'token_id': 'token_123',
            'size': 1000.0,
            'current_price': 0.65,
        }
        
        with patch.object(mock_client, 'get_balance', return_value=10.0):
            with patch.object(mock_client, 'get_positions', return_value=[]):
                should_execute = await strategy.should_execute_trade(opportunity)
                
                assert should_execute is False
    
    async def test_execute_mirror_trade(self, mock_client, mock_order_manager):
        """Test executing a mirror trade"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        opportunity = {
            'action': 'BUY',
            'token_id': 'token_123',
            'size': 50.0,
        }
        
        mock_result = {
            'order_id': 'order_123',
            'status': 'filled',
        }
        
        with patch.object(
            mock_order_manager,
            'execute_market_order',
            return_value=mock_result
        ):
            await strategy._execute_mirror_trade(opportunity)
            
            mock_order_manager.execute_market_order.assert_called_once_with(
                token_id='token_123',
                side='BUY',
                size=50.0
            )
    
    async def test_strategy_lifecycle(self, mock_client, mock_order_manager):
        """Test strategy start/stop lifecycle"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        # Start strategy
        assert strategy.is_running is False
        
        # Mock execute to avoid actual trading
        async def mock_execute():
            await strategy.stop()
        
        with patch.object(strategy, 'execute', side_effect=mock_execute):
            await strategy.run()
        
        assert strategy.is_running is False
    
    async def test_get_status(self, mock_client, mock_order_manager):
        """Test getting strategy status"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        status = strategy.get_status()
        
        assert 'name' in status
        assert 'is_running' in status
        assert 'target_address' in status
        assert status['name'] == 'MirrorStrategy'


@pytest.mark.unit
class TestMirrorStrategyConfig:
    """Test strategy configuration"""
    
    @pytest.mark.asyncio
    async def test_custom_config(self, mock_client, mock_order_manager):
        """Test using custom configuration"""
        custom_config = {
            'check_interval_sec': 30,
            'max_markets': 5,
        }
        
        strategy = MirrorStrategy(
            mock_client,
            mock_order_manager,
            config=custom_config
        )
        
        assert strategy.config['check_interval_sec'] == 30
        assert strategy.config['max_markets'] == 5
    
    @pytest.mark.asyncio
    async def test_update_config(self, mock_client, mock_order_manager):
        """Test updating configuration"""
        strategy = MirrorStrategy(mock_client, mock_order_manager)
        
        strategy.update_config({'max_markets': 15})
        
        assert strategy.config['max_markets'] == 15
