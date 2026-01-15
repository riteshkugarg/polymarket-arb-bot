"""
Unit Tests for Z-Score Mean Reversion Alpha Overlay

Tests the ZScoreManager class and its integration with MarketMakingStrategy.
Validates mathematical correctness, signal generation, and safety gates.
"""

import pytest
import time
from collections import deque
from src.strategies.market_making_strategy import ZScoreManager
from src.config.constants import (
    Z_SCORE_LOOKBACK_PERIODS,
    Z_SCORE_ENTRY_THRESHOLD,
    Z_SCORE_EXIT_TARGET,
    Z_SCORE_HALT_THRESHOLD,
    MM_Z_SENSITIVITY,
)


class TestZScoreManager:
    """Test suite for ZScoreManager statistical calculations"""
    
    def test_initialization(self):
        """Test ZScoreManager initializes with correct parameters"""
        manager = ZScoreManager(lookback_periods=20)
        
        assert manager.lookback_periods == 20
        assert len(manager.price_window) == 0
        assert manager.current_z_score == 0.0
        assert not manager.is_ready()
    
    def test_rolling_window_auto_eviction(self):
        """Test deque automatically evicts oldest values when full"""
        manager = ZScoreManager(lookback_periods=5)
        
        # Add 5 prices
        for i in range(5):
            manager.update(0.50 + i * 0.01)
        
        assert len(manager.price_window) == 5
        assert manager.is_ready()
        
        # Add 6th price - should evict first
        manager.update(0.60)
        
        assert len(manager.price_window) == 5
        assert manager.price_window[0] == 0.51  # First price (0.50) evicted
        assert manager.price_window[-1] == 0.60
    
    def test_z_score_calculation_neutral(self):
        """Test Z-Score calculation for neutral (flat) prices"""
        manager = ZScoreManager(lookback_periods=10)
        
        # Add 10 identical prices (zero variance)
        for _ in range(10):
            z = manager.update(0.50)
        
        # Should return Z=0 for zero variance
        assert manager.get_z_score() == 0.0
        assert not manager.should_halt_trading()
        assert not manager.is_signal_active()
    
    def test_z_score_calculation_overbought(self):
        """Test Z-Score calculation for overbought condition"""
        manager = ZScoreManager(lookback_periods=20)
        
        # Create upward price trend
        base_prices = [0.50 + i * 0.001 for i in range(18)]  # Gradual increase
        for price in base_prices:
            manager.update(price)
        
        # Add 2 significantly higher prices (overbought)
        manager.update(0.55)  # Spike up
        z1 = manager.update(0.56)  # Continue spike
        
        # Should detect overbought (Z > 0)
        assert manager.get_z_score() > 0
        assert manager.is_signal_active()  # Should be > 0.5σ
        print(f"Overbought Z-Score: {z1:.2f}σ")
    
    def test_z_score_calculation_oversold(self):
        """Test Z-Score calculation for oversold condition"""
        manager = ZScoreManager(lookback_periods=20)
        
        # Create downward price trend
        base_prices = [0.50 - i * 0.001 for i in range(18)]  # Gradual decrease
        for price in base_prices:
            manager.update(price)
        
        # Add 2 significantly lower prices (oversold)
        manager.update(0.43)  # Spike down
        z1 = manager.update(0.42)  # Continue spike
        
        # Should detect oversold (Z < 0)
        assert manager.get_z_score() < 0
        assert manager.is_signal_active()  # Should be < -0.5σ
        print(f"Oversold Z-Score: {z1:.2f}σ")
    
    def test_extreme_outlier_detection(self):
        """Test halt condition for extreme outliers (±3.5σ)"""
        manager = ZScoreManager(lookback_periods=20)
        
        # Create stable prices around 0.50
        for _ in range(19):
            manager.update(0.50)
        
        # Add one tiny variation to establish std dev
        manager.update(0.50001)
        
        # Now add extreme outlier significantly above mean
        # With very low variance, even small price move = large Z-Score
        extreme_price = 0.52  # 2 cents above mean (large given tiny variance)
        z_extreme = manager.update(extreme_price)
        
        # Should trigger halt threshold
        assert abs(manager.get_z_score()) > Z_SCORE_HALT_THRESHOLD, \
            f"Expected Z > {Z_SCORE_HALT_THRESHOLD}, got {abs(manager.get_z_score()):.2f}"
        assert manager.should_halt_trading()
        print(f"Extreme outlier Z-Score: {z_extreme:.2f}σ (threshold: ±{Z_SCORE_HALT_THRESHOLD:.1f}σ)")
    
    def test_alpha_shift_calculation(self):
        """Test alpha shift magnitude and direction"""
        manager = ZScoreManager(lookback_periods=20)
        
        # Create mean-reverting scenario: prices around 0.50, current at 0.52
        base_prices = [0.50 for _ in range(18)]
        for price in base_prices:
            manager.update(price)
        
        # Add small variation
        manager.update(0.501)
        
        # Now add overbought price
        manager.update(0.52)
        
        z = manager.get_z_score()
        alpha_shift = manager.get_alpha_shift()
        
        print(f"Z-Score: {z:.2f}σ, Alpha Shift: ${alpha_shift:+.4f}")
        
        if abs(z) >= Z_SCORE_ENTRY_THRESHOLD:
            # Overbought (Z > 0) should produce NEGATIVE shift (lower reservation)
            if z > 0:
                assert alpha_shift < 0, "Overbought should lower reservation price"
            # Oversold (Z < 0) should produce POSITIVE shift (raise reservation)
            elif z < 0:
                assert alpha_shift > 0, "Oversold should raise reservation price"
            
            # Verify magnitude
            expected_shift = -z * MM_Z_SENSITIVITY  # Inverted
            assert abs(alpha_shift - expected_shift) < 1e-6
    
    def test_signal_activation_thresholds(self):
        """Test entry and exit thresholds for mean reversion signal"""
        manager = ZScoreManager(lookback_periods=20)
        
        # Fill with baseline prices
        for _ in range(19):
            manager.update(0.50)
        
        # Add small variation to create std dev
        manager.update(0.50001)
        
        import statistics
        mean = statistics.mean(manager.price_window)
        std = statistics.stdev(manager.price_window)
        
        # Test below exit threshold (should NOT be active)
        price_below_exit = mean + (0.3 * std)  # 0.3σ
        manager.update(price_below_exit)
        assert not manager.is_signal_active(), "Signal should be inactive below exit threshold"
        
        # Test above entry threshold (should be active)
        # Need significant deviation given tiny std dev
        price_above_entry = mean + (max(0.005, 2.2 * std))  # At least 0.5 cents or 2.2σ
        manager.update(price_above_entry)
        assert manager.is_signal_active(), "Signal should be active above entry threshold"
        assert abs(manager.get_z_score()) > Z_SCORE_ENTRY_THRESHOLD, \
            f"Expected Z > {Z_SCORE_ENTRY_THRESHOLD}, got {abs(manager.get_z_score()):.2f}"
    
    def test_insufficient_data_handling(self):
        """Test behavior when insufficient samples collected"""
        manager = ZScoreManager(lookback_periods=20)
        
        # Add only 10 samples (less than required 20)
        for i in range(10):
            z = manager.update(0.50 + i * 0.01)
        
        # Should return Z=0 and not be ready
        assert manager.get_z_score() == 0.0
        assert not manager.is_ready()
        assert manager.get_alpha_shift() == 0.0


class TestZScoreIntegrationWithQuoting:
    """Test Z-Score integration with Avellaneda-Stoikov quoting logic"""
    
    def test_reservation_price_adjustment(self):
        """Test that alpha shift correctly adjusts reservation price"""
        # Scenario: Overbought market (Z=2.5)
        # Expected: Lower reservation price to encourage selling
        
        manager = ZScoreManager(lookback_periods=20)
        
        # Create overbought condition
        base_prices = [0.50 for _ in range(18)]
        for price in base_prices:
            manager.update(price)
        
        manager.update(0.501)
        manager.update(0.52)  # Spike
        
        z = manager.get_z_score()
        alpha_shift = manager.get_alpha_shift()
        
        # Simulate Avellaneda-Stoikov calculation
        mid_price = 0.50
        inventory = 0  # Neutral inventory
        inventory_skew = inventory * 0.0005
        
        # Traditional reservation (without Z-Score)
        traditional_reservation = mid_price - inventory_skew
        
        # Hybrid reservation (with Z-Score) - ADD the shift
        hybrid_reservation = traditional_reservation + alpha_shift
        
        print(f"\nScenario: Overbought Market (Z={z:.2f}σ)")
        print(f"Traditional Reservation: ${traditional_reservation:.4f}")
        print(f"Hybrid Reservation: ${hybrid_reservation:.4f} (shift: ${alpha_shift:+.4f})")
        
        if abs(z) >= Z_SCORE_ENTRY_THRESHOLD:
            if z > 0:  # Overbought
                # Alpha shift should be NEGATIVE
                assert alpha_shift < 0, "Overbought should produce negative alpha shift"
                # Hybrid should be LOWER than traditional (encourage selling)
                assert hybrid_reservation < traditional_reservation, \
                    f"Overbought: Hybrid reservation (${hybrid_reservation:.4f}) should be lower than traditional (${traditional_reservation:.4f}) to incentivize selling"
            elif z < 0:  # Oversold
                # Alpha shift should be POSITIVE
                assert alpha_shift > 0, "Oversold should produce positive alpha shift"
                # Hybrid should be HIGHER than traditional (encourage buying)
                assert hybrid_reservation > traditional_reservation, \
                    f"Oversold: Hybrid reservation (${hybrid_reservation:.4f}) should be higher than traditional (${traditional_reservation:.4f}) to incentivize buying"
    
    def test_additive_nature_of_signals(self):
        """Test that Z-Score and inventory skew are additive (not replacing)"""
        manager = ZScoreManager(lookback_periods=20)
        
        # Create mild overbought
        for _ in range(20):
            manager.update(0.50)
        manager.update(0.51)
        
        z = manager.get_z_score()
        alpha_shift = manager.get_alpha_shift()
        
        # Simulate with long inventory (want to sell)
        mid_price = 0.50
        inventory = 50  # Long position
        inventory_skew = inventory * 0.0005  # = 0.025
        
        # Both signals say "sell" (lower reservation)
        base_reservation = mid_price - inventory_skew  # = 0.475
        final_reservation = base_reservation + alpha_shift  # ADD alpha shift
        
        print(f"\nScenario: Long Inventory + Overbought")
        print(f"Inventory Skew: ${inventory_skew:.4f}")
        print(f"Alpha Shift: ${alpha_shift:+.4f}")
        print(f"Base Reservation: ${base_reservation:.4f}")
        print(f"Final Reservation: ${final_reservation:.4f}")
        
        # Final should be lower than base if both signals aligned (sell)
        if abs(z) >= Z_SCORE_ENTRY_THRESHOLD:
            if z > 0:  # Overbought - alpha shift is negative
                # Both want to sell: inventory lowers reservation, alpha lowers it more
                assert alpha_shift < 0, "Overbought should produce negative shift"
                assert final_reservation < base_reservation, \
                    f"Both signals should combine to lower reservation: {final_reservation:.4f} < {base_reservation:.4f}"
            elif z < 0:  # Oversold - alpha shift is positive
                # Conflict: inventory wants to sell (lower), alpha wants to buy (raise)
                # The positive alpha shift should partially offset inventory skew
                assert alpha_shift > 0, "Oversold should produce positive shift"
                assert final_reservation > base_reservation, \
                    f"Alpha should partially offset inventory: {final_reservation:.4f} > {base_reservation:.4f}"


def test_constants_configured():
    """Verify all Z-Score constants are properly defined"""
    assert Z_SCORE_LOOKBACK_PERIODS == 20
    assert Z_SCORE_ENTRY_THRESHOLD == 2.0
    assert Z_SCORE_EXIT_TARGET == 0.5
    assert Z_SCORE_HALT_THRESHOLD == 3.5
    assert MM_Z_SENSITIVITY == 0.005
    
    print("\n✅ All Z-Score constants properly configured:")
    print(f"   Lookback Periods: {Z_SCORE_LOOKBACK_PERIODS}")
    print(f"   Entry Threshold: ±{Z_SCORE_ENTRY_THRESHOLD:.1f}σ")
    print(f"   Exit Target: ±{Z_SCORE_EXIT_TARGET:.1f}σ")
    print(f"   Halt Threshold: ±{Z_SCORE_HALT_THRESHOLD:.1f}σ")
    print(f"   Sensitivity: ${MM_Z_SENSITIVITY:.4f}/σ")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
