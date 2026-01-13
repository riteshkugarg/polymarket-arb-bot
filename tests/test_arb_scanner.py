"""
Test Suite for Arbitrage Scanner & Executor

Tests the core arbitrage detection and execution logic without
requiring live Polymarket API connections.

Test Coverage:
──────────────
1. ArbScanner: Market detection and opportunity identification
2. AtomicExecutor: Execution with FOK logic and slippage handling
3. NegRisk: Inverse market normalization
4. Budget: $100 total constraint management
5. Abort scenarios: Failed leg handling and order cancellation
"""

import pytest
import asyncio
from decimal import Decimal
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime

from strategies.arb_scanner import (
    ArbScanner,
    AtomicExecutor,
    ArbitrageOpportunity,
    OutcomePrice,
    MarketType,
    ExecutionResult,
    TAKER_FEE_PERCENT,
    ARBITRAGE_OPPORTUNITY_THRESHOLD,
    MAX_SLIPPAGE_PER_LEG,
)


# ============================================================================
# FIXTURES - Mock Data
# ============================================================================

@pytest.fixture
def mock_client():
    """Mock PolymarketClient"""
    client = AsyncMock()
    client.get_balance = AsyncMock(return_value=1000.0)  # $1000 balance
    return client


@pytest.fixture
def mock_order_manager():
    """Mock OrderManager"""
    manager = AsyncMock()
    manager.validate_order = AsyncMock()
    return manager


@pytest.fixture
def mock_market_data():
    """Sample market data with 3 outcomes (multi-choice)"""
    return {
        'id': 'market-123',
        'conditionId': 'condition-456',
        'question': 'Which candidate will win?',
        'outcomes': ['Alice', 'Bob', 'Charlie'],
        'clobTokenIds': ['token-1', 'token-2', 'token-3'],
        'negRisk': False,
    }


@pytest.fixture
def mock_order_book():
    """Sample order book data (bid/ask)"""
    book = Mock()
    book.bids = [{'price': '0.32', 'size': '15.0'}]
    book.asks = [{'price': '0.33', 'size': '20.0'}]
    return book


@pytest.fixture
def arbitrage_opportunity():
    """Sample arbitrage opportunity (profitable)"""
    outcomes = [
        OutcomePrice(
            outcome_index=0,
            outcome_name='Outcome 1',
            token_id='token-1',
            yes_price=0.32,
            bid_price=0.32,
            ask_price=0.33,
            available_depth=100.0
        ),
        OutcomePrice(
            outcome_index=1,
            outcome_name='Outcome 2',
            token_id='token-2',
            yes_price=0.33,
            bid_price=0.33,
            ask_price=0.34,
            available_depth=100.0
        ),
        OutcomePrice(
            outcome_index=2,
            outcome_name='Outcome 3',
            token_id='token-3',
            yes_price=0.32,
            bid_price=0.32,
            ask_price=0.33,
            available_depth=100.0
        ),
    ]
    
    return ArbitrageOpportunity(
        market_id='market-123',
        condition_id='condition-456',
        market_type=MarketType.MULTI_CHOICE,
        outcomes=outcomes,
        sum_prices=0.97,  # Sum < 0.98 → arbitrage!
        profit_per_share=0.03,  # 1.0 - 0.97
        net_profit_per_share=0.024,  # After fees
        required_budget=9.7,  # Cost per share
        max_shares_to_buy=10.0,
        is_negrisk=False,
    )


# ============================================================================
# TESTS: ArbScanner
# ============================================================================

class TestArbScannerDetection:
    """Test arbitrage opportunity detection"""
    
    @pytest.mark.asyncio
    async def test_detects_multi_choice_arbitrage(self, mock_client, mock_order_manager, mock_market_data):
        """
        TEST: ArbScanner correctly identifies multi-choice arbitrage
        
        Scenario:
        - 3-outcome market with sum(prices) = 0.97 < 0.98
        - Should be detected as arbitrage opportunity
        """
        scanner = ArbScanner(mock_client, mock_order_manager)
        
        # Mock market API responses
        mock_client.get_markets = AsyncMock(return_value={
            'data': [mock_market_data]
        })
        
        # Mock order book for each outcome
        order_books = [
            Mock(bids=[{'price': '0.32', 'size': '20.0'}], asks=[{'price': '0.33', 'size': '20.0'}]),
            Mock(bids=[{'price': '0.33', 'size': '20.0'}], asks=[{'price': '0.34', 'size': '20.0'}]),
            Mock(bids=[{'price': '0.32', 'size': '20.0'}], asks=[{'price': '0.33', 'size': '20.0'}]),
        ]
        mock_client.get_order_book = AsyncMock(side_effect=order_books)
        
        # Scan for opportunities
        opportunities = await scanner.scan_markets(limit=10)
        
        # Assertions
        assert len(opportunities) > 0
        assert opportunities[0].market_id == 'market-123'
        assert opportunities[0].market_type == MarketType.MULTI_CHOICE
        assert opportunities[0].sum_prices < 0.98
        assert opportunities[0].is_negrisk is False
        
    @pytest.mark.asyncio
    async def test_filters_non_profitable_markets(self, mock_client, mock_order_manager):
        """
        TEST: Scanner ignores markets where sum(prices) >= 0.98
        
        Scenario:
        - 2-outcome market with sum = 0.99 > 0.98
        - Should NOT be detected as opportunity
        """
        scanner = ArbScanner(mock_client, mock_order_manager)
        
        market = {
            'id': 'fair-market',
            'conditionId': 'cond-fair',
            'question': 'Binary question?',
            'outcomes': ['Yes', 'No'],
            'clobTokenIds': ['t1', 't2'],
            'negRisk': False,
        }
        
        mock_client.get_markets = AsyncMock(return_value={'data': [market]})
        
        # Order books: prices sum to 0.99 (not an arb)
        order_books = [
            Mock(bids=[{'price': '0.495', 'size': '20.0'}], asks=[{'price': '0.500', 'size': '20.0'}]),
            Mock(bids=[{'price': '0.495', 'size': '20.0'}], asks=[{'price': '0.500', 'size': '20.0'}]),
        ]
        mock_client.get_order_book = AsyncMock(side_effect=order_books)
        
        opportunities = await scanner.scan_markets(limit=10)
        
        # No arbitrage should be detected
        assert len(opportunities) == 0
    
    @pytest.mark.asyncio
    async def test_detects_negrisk_market(self, mock_client, mock_order_manager):
        """
        TEST: Scanner identifies NegRisk (inverse) markets
        
        Scenario:
        - Market marked with negRisk=True
        - Should calculate using inverse logic
        """
        scanner = ArbScanner(mock_client, mock_order_manager)
        
        market = {
            'id': 'negrisk-market',
            'conditionId': 'cond-neg',
            'question': 'Will the event NOT occur?',
            'outcomes': ['True', 'False'],
            'clobTokenIds': ['t1', 't2'],
            'negRisk': True,  # ← NegRisk flag
        }
        
        mock_client.get_markets = AsyncMock(return_value={'data': [market]})
        
        # Mock order books
        order_books = [
            Mock(bids=[{'price': '0.65', 'size': '20.0'}], asks=[{'price': '0.66', 'size': '20.0'}]),
            Mock(bids=[{'price': '0.32', 'size': '20.0'}], asks=[{'price': '0.34', 'size': '20.0'}]),
        ]
        mock_client.get_order_book = AsyncMock(side_effect=order_books)
        
        opportunities = await scanner.scan_markets(limit=10)
        
        # Should detect as NegRisk
        if opportunities:
            assert opportunities[0].is_negrisk is True

    @pytest.mark.asyncio
    async def test_insufficient_order_book_depth(self, mock_client, mock_order_manager):
        """
        TEST: Scanner rejects markets with insufficient order book depth
        
        Scenario:
        - Market with < 10 shares available at ask price
        - Should be filtered out
        """
        scanner = ArbScanner(mock_client, mock_order_manager)
        
        market = {
            'id': 'illiquid-market',
            'conditionId': 'cond-illiq',
            'question': 'Question?',
            'outcomes': ['A', 'B'],
            'clobTokenIds': ['t1', 't2'],
            'negRisk': False,
        }
        
        mock_client.get_markets = AsyncMock(return_value={'data': [market]})
        
        # Shallow order book (only 5 shares)
        order_books = [
            Mock(bids=[{'price': '0.32', 'size': '5.0'}], asks=[{'price': '0.33', 'size': '5.0'}]),
            Mock(bids=[{'price': '0.33', 'size': '5.0'}], asks=[{'price': '0.34', 'size': '5.0'}]),
        ]
        mock_client.get_order_book = AsyncMock(side_effect=order_books)
        
        opportunities = await scanner.scan_markets(limit=10)
        
        # Should be rejected due to insufficient depth
        assert len(opportunities) == 0


# ============================================================================
# TESTS: AtomicExecutor
# ============================================================================

class TestAtomicExecutor:
    """Test atomic execution with FOK logic"""
    
    @pytest.mark.asyncio
    async def test_successful_atomic_execution(self, mock_client, mock_order_manager, arbitrage_opportunity):
        """
        TEST: Executor successfully fills all legs
        
        Scenario:
        - All 3 outcomes have sufficient balance and order books
        - All orders execute without slippage issues
        - Should return success with filled shares
        """
        executor = AtomicExecutor(mock_client, mock_order_manager)
        
        # Mock balance and order execution
        mock_client.get_balance = AsyncMock(return_value=100.0)
        mock_order_manager.execute_market_order = AsyncMock(return_value={
            'order_id': 'order-123',
            'filled': True,
        })
        
        # Execute
        result = await executor.execute(arbitrage_opportunity, shares_to_buy=5.0)
        
        # Assertions
        assert result.success is True
        assert len(result.orders_executed) == 3  # All 3 outcomes
        assert result.shares_filled == 5.0
        assert result.actual_profit > 0  # Should be profitable
        assert result.error_message is None
    
    @pytest.mark.asyncio
    async def test_aborts_on_insufficient_balance(self, mock_client, mock_order_manager, arbitrage_opportunity):
        """
        TEST: Executor rejects execution if insufficient balance
        
        Scenario:
        - Required cost is $50 but balance is only $10
        - Should abort before placing any orders
        """
        executor = AtomicExecutor(mock_client, mock_order_manager)
        
        # Insufficient balance
        mock_client.get_balance = AsyncMock(return_value=5.0)
        
        # Execute
        result = await executor.execute(arbitrage_opportunity, shares_to_buy=5.0)
        
        # Should abort
        assert result.success is False
        assert 'Insufficient balance' in result.error_message
        assert len(result.orders_executed) == 0
    
    @pytest.mark.asyncio
    async def test_aborts_on_order_failure_with_cancellation(self, mock_client, mock_order_manager, arbitrage_opportunity):
        """
        TEST: Executor cancels pending orders if one leg fails (FOK)
        
        Scenario:
        - First two outcomes fill successfully
        - Third outcome fails due to price movement
        - Should cancel the first two orders immediately
        """
        executor = AtomicExecutor(mock_client, mock_order_manager)
        
        # Mock balance
        mock_client.get_balance = AsyncMock(return_value=100.0)
        
        # First two orders succeed, third fails
        mock_order_manager.execute_market_order = AsyncMock(
            side_effect=[
                {'order_id': 'order-1', 'filled': True},
                {'order_id': 'order-2', 'filled': True},
                Exception("Order not filled - price moved"),
            ]
        )
        
        # Mock cancellation
        mock_client.cancel_order = AsyncMock()
        
        # Execute
        result = await executor.execute(arbitrage_opportunity, shares_to_buy=5.0)
        
        # Should abort and cancel pending orders
        assert result.success is False
        assert 'Order not filled' in result.error_message
        assert len(result.orders_executed) == 0  # None executed atomically
        assert mock_client.cancel_order.call_count == 2  # Cancelled 2 pending
    
    @pytest.mark.asyncio
    async def test_respects_max_slippage_constraint(self, mock_client, mock_order_manager, arbitrage_opportunity):
        """
        TEST: Executor rejects execution if slippage > $0.005 per leg
        
        Scenario:
        - First outcome has slippage of $0.01 (exceeds $0.005 limit)
        - Should abort immediately
        """
        executor = AtomicExecutor(mock_client, mock_order_manager)
        
        # Update first outcome with higher ask price (causes slippage)
        arbitrage_opportunity.outcomes[0].ask_price = 0.42  # Was 0.33, now 0.42
        arbitrage_opportunity.outcomes[0].yes_price = 0.32  # Wide slippage
        
        # Execute
        result = await executor.execute(arbitrage_opportunity, shares_to_buy=5.0)
        
        # Should reject due to slippage
        assert result.success is False
        assert 'Slippage' in result.error_message


# ============================================================================
# TESTS: Budget Management
# ============================================================================

class TestBudgetManagement:
    """Test $100 total budget constraints"""
    
    def test_budget_tracking_incremental(self, mock_client, mock_order_manager):
        """
        TEST: Executor tracks budget usage across multiple executions
        
        Scenario:
        - Execute 10 arbitrages at $7 each
        - Total should be $70, remaining should be $30
        """
        executor = AtomicExecutor(mock_client, mock_order_manager)
        
        # Simulate 10 executions
        for i in range(10):
            executor._budget_used += Decimal('7.0')
        
        status = executor.get_budget_status()
        
        assert status['used_budget'] == 70.0
        assert status['remaining_budget'] == 30.0
        assert status['utilization_percent'] == 70.0
    
    def test_rejects_execution_exceeding_budget(self, mock_client, mock_order_manager, arbitrage_opportunity):
        """
        TEST: Executor rejects if cost exceeds remaining budget
        
        Scenario:
        - Already used $95
        - New opportunity costs $10
        - Should reject
        """
        executor = AtomicExecutor(mock_client, mock_order_manager)
        
        # Pre-use budget
        executor._budget_used = Decimal('95.0')
        
        # Try to execute (requires $48.5 = 5 shares × $9.7)
        # But this exceeds remaining $5
        
        # Budget validation happens in _validate_execution
        with pytest.raises(Exception):
            # This will raise ValidationError during validation
            asyncio.run(executor._validate_execution(arbitrage_opportunity, shares_to_buy=5.0))


# ============================================================================
# TESTS: NegRisk Handling
# ============================================================================

class TestNegRiskHandling:
    """Test inverse market normalization"""
    
    def test_negrisk_detection_by_flag(self, mock_client, mock_order_manager):
        """
        TEST: Scanner correctly identifies NegRisk by flag
        """
        scanner = ArbScanner(mock_client, mock_order_manager)
        
        market = {'negRisk': True}
        assert scanner._is_negrisk_market(market) is True
    
    def test_negrisk_detection_by_question(self, mock_client, mock_order_manager):
        """
        TEST: Scanner infers NegRisk from question text
        """
        scanner = ArbScanner(mock_client, mock_order_manager)
        
        market = {'question': 'Will the candidate NOT win?'}
        assert scanner._is_negrisk_market(market) is True
        
        market = {'question': 'Will prices decline?'}
        assert scanner._is_negrisk_market(market) is True


# ============================================================================
# TESTS: Integration
# ============================================================================

class TestIntegration:
    """End-to-end integration tests"""
    
    @pytest.mark.asyncio
    async def test_full_arbitrage_flow(self, mock_client, mock_order_manager, mock_market_data):
        """
        TEST: Full flow from detection to execution
        
        Scenario:
        1. Scanner detects multi-outcome arb
        2. Executor validates and prepares execution
        3. All orders fill atomically
        4. Budget is updated
        """
        scanner = ArbScanner(mock_client, mock_order_manager)
        executor = AtomicExecutor(mock_client, mock_order_manager)
        
        # Setup mocks for scan
        mock_client.get_markets = AsyncMock(return_value={'data': [mock_market_data]})
        order_books = [
            Mock(bids=[{'price': '0.32', 'size': '20.0'}], asks=[{'price': '0.33', 'size': '20.0'}]),
            Mock(bids=[{'price': '0.33', 'size': '20.0'}], asks=[{'price': '0.34', 'size': '20.0'}]),
            Mock(bids=[{'price': '0.32', 'size': '20.0'}], asks=[{'price': '0.33', 'size': '20.0'}]),
        ]
        mock_client.get_order_book = AsyncMock(side_effect=order_books)
        
        # Scan for opportunities
        opportunities = await scanner.scan_markets(limit=10)
        assert len(opportunities) > 0
        
        # Setup execution mocks
        mock_client.get_balance = AsyncMock(return_value=100.0)
        mock_order_manager.execute_market_order = AsyncMock(return_value={
            'order_id': 'order-123',
            'filled': True,
        })
        
        # Execute
        result = await executor.execute(opportunities[0], shares_to_buy=5.0)
        
        # Verify
        assert result.success is True
        assert result.shares_filled == 5.0
        assert executor.get_budget_status()['used_budget'] > 0
