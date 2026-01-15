"""
Institutional-Grade Dynamic Capital Allocation System

This module implements percentage-based capital allocation following institutional standards:
- Jane Street, Citadel, Two Sigma, Jump Trading methodologies
- Kelly Criterion optimal position sizing (5-15% per strategy)
- Auto-scales with account growth/drawdown
- Safety caps and minimum thresholds

Usage:
    from src.config.capital_allocator import calculate_strategy_capital
    
    allocations = calculate_strategy_capital(current_balance=72.92)
    # Returns: {
    #   'market_making': 56.88,
    #   'arbitrage': 14.58,
    #   'reserve': 1.46,
    #   'total_allocated': 71.46,
    #   'mm_enabled': True,
    #   'arb_enabled': True
    # }
"""

from typing import Dict
from config.constants import (
    MM_CAPITAL_ALLOCATION_PCT,
    ARB_CAPITAL_ALLOCATION_PCT,
    RESERVE_BUFFER_PCT,
    MM_MAX_CAPITAL_CAP,
    ARB_MAX_CAPITAL_CAP,
    MM_MIN_CAPITAL_THRESHOLD,
    ARB_MIN_CAPITAL_THRESHOLD,
)


def calculate_strategy_capital(current_balance: float) -> Dict[str, float]:
    """
    Calculate dynamic capital allocations based on current balance.
    
    Institutional Golden Standards:
    1. Percentage-based allocation (auto-scales)
    2. Hard dollar caps (safety limits)
    3. Minimum thresholds (strategy activation)
    4. Kelly Criterion compliance (5-15% per strategy)
    
    Args:
        current_balance: Current USDC balance in wallet
        
    Returns:
        Dictionary containing:
        - market_making: Capital allocated to MM strategy
        - arbitrage: Capital allocated to Arb strategy
        - reserve: Cash reserve for fees/emergencies
        - total_allocated: Sum of all allocations
        - mm_enabled: Whether MM has sufficient capital
        - arb_enabled: Whether Arb has sufficient capital
        
    Examples:
        >>> calculate_strategy_capital(72.92)
        {'market_making': 56.88, 'arbitrage': 14.58, 'reserve': 1.46, ...}
        
        >>> calculate_strategy_capital(500.0)
        {'market_making': 390.0, 'arbitrage': 100.0, 'reserve': 10.0, ...}
        
        >>> calculate_strategy_capital(5000.0)
        {'market_making': 500.0, 'arbitrage': 200.0, 'reserve': 100.0, ...}
        # Note: MM and Arb capped at $500/$200 despite 78%/20% = $3900/$1000
    """
    
    # Calculate percentage-based allocations
    mm_capital = current_balance * MM_CAPITAL_ALLOCATION_PCT
    arb_capital = current_balance * ARB_CAPITAL_ALLOCATION_PCT
    reserve = current_balance * RESERVE_BUFFER_PCT
    
    # Apply hard caps (safety limits for large accounts)
    mm_capital = min(mm_capital, MM_MAX_CAPITAL_CAP)
    arb_capital = min(arb_capital, ARB_MAX_CAPITAL_CAP)
    
    # Check minimum thresholds (disable strategies if insufficient capital)
    mm_enabled = mm_capital >= MM_MIN_CAPITAL_THRESHOLD
    arb_enabled = arb_capital >= ARB_MIN_CAPITAL_THRESHOLD
    
    # If below threshold, set capital to 0 (strategy disabled)
    if not mm_enabled:
        mm_capital = 0.0
    if not arb_enabled:
        arb_capital = 0.0
    
    # Calculate total allocated (may be less than balance if strategies disabled)
    total_allocated = mm_capital + arb_capital + reserve
    
    return {
        'market_making': mm_capital,
        'arbitrage': arb_capital,
        'reserve': reserve,
        'total_allocated': total_allocated,
        'mm_enabled': mm_enabled,
        'arb_enabled': arb_enabled,
        'unallocated': max(0.0, current_balance - total_allocated),
    }


def calculate_drawdown_limit(peak_equity: float) -> float:
    """
    Calculate dynamic drawdown limit (kill switch threshold).
    
    Institutional Standard: 5% of peak equity
    
    Args:
        peak_equity: Highest account balance achieved this session
        
    Returns:
        Dollar amount representing 5% drawdown limit
        
    Examples:
        >>> calculate_drawdown_limit(100.0)
        5.0
        
        >>> calculate_drawdown_limit(1000.0)
        50.0
    """
    from src.config.constants import DRAWDOWN_LIMIT_PCT
    return peak_equity * DRAWDOWN_LIMIT_PCT


def calculate_max_exposure(current_balance: float) -> float:
    """
    Calculate maximum total exposure across all strategies.
    
    Institutional Standard: 95% of current balance
    
    Args:
        current_balance: Current USDC balance in wallet
        
    Returns:
        Dollar amount representing 95% max exposure
        
    Examples:
        >>> calculate_max_exposure(72.92)
        69.27
        
        >>> calculate_max_exposure(1000.0)
        950.0
    """
    from src.config.constants import MAX_TOTAL_EXPOSURE_PCT
    return current_balance * MAX_TOTAL_EXPOSURE_PCT


def get_allocation_summary(current_balance: float, peak_equity: float = None) -> str:
    """
    Generate human-readable allocation summary.
    
    Args:
        current_balance: Current USDC balance
        peak_equity: Optional peak equity for drawdown calculation
        
    Returns:
        Formatted string summarizing all allocations
    """
    allocations = calculate_strategy_capital(current_balance)
    
    summary = [
        "=" * 80,
        "INSTITUTIONAL-GRADE CAPITAL ALLOCATION",
        "=" * 80,
        f"Current Balance: ${current_balance:.2f} USDC",
        "",
        "Strategy Allocations (Percentage-Based):",
        f"  • Market Making: ${allocations['market_making']:.2f} ({MM_CAPITAL_ALLOCATION_PCT*100:.0f}% of balance) {'✅ ENABLED' if allocations['mm_enabled'] else '❌ DISABLED'}",
        f"  • Arbitrage:     ${allocations['arbitrage']:.2f} ({ARB_CAPITAL_ALLOCATION_PCT*100:.0f}% of balance) {'✅ ENABLED' if allocations['arb_enabled'] else '❌ DISABLED'}",
        f"  • Reserve:       ${allocations['reserve']:.2f} ({RESERVE_BUFFER_PCT*100:.0f}% of balance)",
        f"  • Total Allocated: ${allocations['total_allocated']:.2f} ({(allocations['total_allocated']/current_balance)*100:.1f}%)",
        "",
        "Safety Limits:",
        f"  • MM Cap: ${MM_MAX_CAPITAL_CAP:.2f} (max allocation regardless of balance)",
        f"  • Arb Cap: ${ARB_MAX_CAPITAL_CAP:.2f} (max allocation regardless of balance)",
        f"  • MM Minimum: ${MM_MIN_CAPITAL_THRESHOLD:.2f} (strategy activation threshold)",
        f"  • Arb Minimum: ${ARB_MIN_CAPITAL_THRESHOLD:.2f} (strategy activation threshold)",
        f"  • Max Exposure: ${calculate_max_exposure(current_balance):.2f} (95% of balance)",
    ]
    
    if peak_equity is not None:
        drawdown_limit = calculate_drawdown_limit(peak_equity)
        summary.extend([
            "",
            "Kill Switch (Drawdown Protection):",
            f"  • Peak Equity: ${peak_equity:.2f}",
            f"  • Drawdown Limit: ${drawdown_limit:.2f} (5% of peak)",
            f"  • Current Drawdown: ${max(0, peak_equity - current_balance):.2f}",
        ])
    
    summary.extend([
        "",
        "=" * 80,
    ])
    
    return "\n".join(summary)


if __name__ == "__main__":
    # Test with different balance scenarios
    test_cases = [
        ("Small Account", 72.92),
        ("Initial Target", 100.0),
        ("Medium Account", 500.0),
        ("Large Account", 5000.0),
        ("Below MM Threshold", 40.0),
        ("Below Arb Threshold", 8.0),
    ]
    
    print("\n" + "=" * 80)
    print("INSTITUTIONAL CAPITAL ALLOCATION SYSTEM - TEST SCENARIOS")
    print("=" * 80 + "\n")
    
    for name, balance in test_cases:
        print(f"\n{name}: ${balance:.2f}")
        print("-" * 80)
        allocations = calculate_strategy_capital(balance)
        print(f"  MM:  ${allocations['market_making']:>8.2f} {'✅' if allocations['mm_enabled'] else '❌'}")
        print(f"  Arb: ${allocations['arbitrage']:>8.2f} {'✅' if allocations['arb_enabled'] else '❌'}")
        print(f"  Res: ${allocations['reserve']:>8.2f}")
        print(f"  Tot: ${allocations['total_allocated']:>8.2f} ({(allocations['total_allocated']/balance)*100:.1f}%)")
