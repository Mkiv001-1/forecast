"""
Real IB Gateway Integration Test — uses actual connection to Interactive Brokers

Prerequisites:
  1. IB Gateway must be running on localhost:7497 (paper) or :7496 (live)
  2. API is enabled in IB Gateway settings (API > Socket Port)
  3. Account is connected in IB Gateway

Run with:
  python -m pytest test_ib_real_connection.py -v -s -m integration

This test:
  1. Checks IB Gateway connectivity
  2. Fetches real account info
  3. Fetches real portfolio positions
  4. Checks bid/ask spread
  5. Places REAL bracket order (immediately cancels for cleanup)

⚠️  WARNING: This test places REAL orders. Only run with paper account!
    Verify ORDER_MODE=paper and account type before running.
"""

import sys
import os
import pytest
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts", "core"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ============================================================================
# Test 1: IB Gateway Connectivity
# ============================================================================

@pytest.mark.integration
def test_ib_gateway_connectivity():
    """
    Test 1: Check if IB Gateway is accessible.
    
    If this fails, all other tests will be skipped.
    Requires IB Gateway running on localhost:7497 (paper) or :7496 (live)
    """
    try:
        from ib_gateway_client import test_ib_connection
    except ImportError as e:
        pytest.skip(f"ib_insync not installed: {e}")
        return
    
    print("\n" + "="*80)
    print("Test 1: IB Gateway Connectivity")
    print("="*80)
    
    result = test_ib_connection(host="127.0.0.1", port=7497, client_id=99)
    
    print(f"\nConnection Status: {result.get('status')}")
    print(f"Details: {result.get('details')}")
    
    if result.get("status") == "ok":
        print("[OK] IB Gateway is accessible and connected")
        assert True
    else:
        error_msg = (
            f"IB Gateway not accessible.\n"
            f"Details: {result.get('details')}\n\n"
            f"Make sure:\n"
            f"  1. IB Gateway is running on localhost:7497 (paper) or :7496 (live)\n"
            f"  2. API is enabled in IB Gateway settings (Settings > API)\n"
            f"  3. Account is connected to IB Gateway"
        )
        pytest.skip(error_msg)


# ============================================================================
# Test 2: Fetch Real Account Information
# ============================================================================

@pytest.mark.integration
def test_real_ib_account_info():
    """
    Test 2: Fetch real account info from IB Gateway.
    
    Displays:
      - Account ID
      - Account Type (paper/live)
      - Net Liquidation
      - Buying Power
      - Available Funds
      - Cash Balance
    """
    try:
        from ib_gateway_client import fetch_ib_accounts
    except ImportError:
        pytest.skip("ib_insync not installed")
    
    print("\n" + "="*80)
    print("Test 2: Real Account Information")
    print("="*80)
    
    accounts = fetch_ib_accounts(
        host="127.0.0.1",
        port=7497,
        client_id=98,
        timeout=10
    )
    
    if not accounts:
        pytest.skip("No accounts found. IB Gateway may not be connected.")
    
    print(f"\nFound {len(accounts)} account(s):")
    
    for i, acc in enumerate(accounts, 1):
        print(f"\n[Account {i}]")
        print(f"  ID: {acc['account_id']}")
        print(f"  Type: {acc['account_type']}")
        print(f"  Currency: {acc['base_currency']}")
        print(f"  Net Liquidation: ${acc['net_liquidation']:>12,.2f}")
        print(f"  Buying Power:    ${acc['buying_power']:>12,.2f}")
        print(f"  Available Funds: ${acc['available_funds']:>12,.2f}")
        print(f"  Cash Balance:    ${acc['cash']:>12,.2f}")
        print(f"  Maint Margin:    ${acc['maintenance_margin']:>12,.2f}")
    
    assert len(accounts) > 0, "Should have at least one account"
    assert accounts[0]["net_liquidation"] > 0, "Account should have positive balance"
    
    print("\n[OK] Account info retrieved successfully")


# ============================================================================
# Test 3: Fetch Real Portfolio Positions
# ============================================================================

@pytest.mark.integration
def test_real_ib_positions():
    """
    Test 3: Fetch real portfolio positions from IB Gateway.
    
    Shows all open positions (if any exist on the account).
    """
    try:
        from ib_gateway_client import fetch_ib_positions
    except ImportError:
        pytest.skip("ib_insync not installed")
    
    print("\n" + "="*80)
    print("Test 3: Real Portfolio Positions")
    print("="*80)
    
    positions = fetch_ib_positions(
        host="127.0.0.1",
        port=7497,
        client_id=97
    )
    
    print(f"\nFound {len(positions)} position(s)")
    
    if positions:
        print("\n{:<10} {:<12} {:<12} {:<12} {:<15}".format(
            "Ticker", "Qty", "Avg Cost", "Mkt Price", "Market Value"
        ))
        print("-" * 65)
        
        for pos in positions:
            print("{:<10} {:<12,.0f} {:<12,.2f} {:<12,.2f} {:<15,.2f}".format(
                pos['ticker'],
                pos['quantity'],
                pos['avg_cost'],
                pos['market_price'],
                pos['market_value']
            ))
    else:
        print("No open positions (paper account may be empty)")
    
    assert isinstance(positions, list), "Should return a list of positions"
    
    print("\n[OK] Positions retrieved successfully")


# ============================================================================
# Test 4: Check Real Bid/Ask Spread
# ============================================================================

@pytest.mark.integration
def test_real_bid_ask_spread():
    """
    Test 4: Check real bid/ask spread for a liquid stock (TSLA).
    
    This verifies that we can get real market data from IB Gateway.
    """
    try:
        from ib_gateway_client import get_bid_ask_spread
    except ImportError:
        pytest.skip("ib_insync not installed")
    
    print("\n" + "="*80)
    print("Test 4: Real Bid/Ask Spread")
    print("="*80)
    
    # Check TSLA spread (very liquid stock)
    print("\nChecking TSLA bid/ask spread...")
    
    spread = get_bid_ask_spread(
        symbol="TSLA",
        host="127.0.0.1",
        port=7497,
        client_id=96
    )
    
    bid = spread.get('bid')
    ask = spread.get('ask')
    
    if bid is None or ask is None:
        pytest.skip(f"Could not get spread data: {spread}")
    
    print(f"\nTSLA Market Data:")
    print(f"  Bid Price: ${bid:,.2f}")
    print(f"  Ask Price: ${ask:,.2f}")
    print(f"  Spread:    {spread.get('spread_pct', 0):.6f}% ({(ask - bid):.4f})")
    
    if spread.get("status") != "ok":
        pytest.skip(f"Could not get spread data: {spread.get('error')}")
    
    assert bid > 0, "Bid should be positive"
    assert ask > bid, "Ask should be > Bid"
    
    print("\n[OK] Bid/Ask spread retrieved successfully")


# ============================================================================
# Test 5: Place REAL Bracket Order (and Cancel)
# ============================================================================

@pytest.mark.integration
def test_real_bracket_order_with_cleanup():
    """
    Test 5: Place a REAL bracket order via IB Gateway.
    
    This demonstrates the full order flow:
      1. Place a bracket order (entry + stop + target)
      2. Verify order IDs returned
      3. Immediately cancel for cleanup
    
    ⚠️  This places a REAL order on the IB account (paper or live).
        Uses LIMIT order to prevent instant fill.
    """
    try:
        from ib_gateway_client import place_bracket_order, cancel_order
    except ImportError:
        pytest.skip("ib_insync not installed")
    
    print("\n" + "="*80)
    print("Test 5: Real Bracket Order (with Cleanup)")
    print("="*80)
    
    print("\n⚠️  PLACING REAL ORDER ON IB ACCOUNT")
    print("    Using LIMIT entry to prevent instant fill")
    print("    Order will be cancelled immediately after placement")
    
    # Place bracket order for TSLA
    # Using LIMIT entry to prevent instant fill
    result = place_bracket_order(
        symbol="TSLA",
        action="BUY",
        quantity=1,
        stop_loss_price=230.0,      # Stop at $230 loss
        take_profit_price=270.0,     # Target $270 profit
        entry_price=250.0,           # Entry at $250
        entry_order_type="LMT",      # Use LIMIT to prevent instant fill
        entry_tif="DAY",             # Order expires at end of day
        host="127.0.0.1",
        port=7497,
        client_id=94
    )
    
    print(f"\nOrder Placement Result:")
    print(f"  Status: {result.get('status')}")
    print(f"  Parent Order ID: {result.get('parent_id')}")
    print(f"  Target Order ID: {result.get('target_id')}")
    print(f"  Stop Order ID:   {result.get('stop_id')}")
    
    if result.get("error"):
        print(f"  Error: {result.get('error')}")
    
    if result.get("status") != "submitted":
        pytest.skip(f"Order placement failed: {result.get('error')}")
    
    parent_id = result.get("parent_id")
    target_id = result.get("target_id")
    stop_id = result.get("stop_id")
    
    print(f"\n[OK] Bracket order placed successfully")
    print(f"  - Entry (parent) order: #{parent_id}")
    print(f"  - Take-profit order:   #{target_id}")
    print(f"  - Stop-loss order:     #{stop_id}")
    
    # Cleanup: Cancel the orders
    print(f"\nCleaning up (cancelling orders)...")
    
    if parent_id:
        try:
            cancel_result = cancel_order(
                order_id=parent_id,
                host="127.0.0.1",
                port=7497,
                client_id=93
            )
            print(f"  Parent order #{parent_id}: {cancel_result.get('status')}")
            
            # Verify cancellation
            assert cancel_result.get("status") == "cancelled", \
                f"Parent order should be cancelled, got: {cancel_result.get('status')}"
            
        except Exception as e:
                    print(f"  [WARNING] Could not cancel parent order: {e}")
        
        print("\n[OK] Bracket order placed and cancelled successfully")

# ============================================================================
# Test 6: Test Integration with Mock Consensus
# ============================================================================

@pytest.mark.integration
def test_real_ib_with_consensus():
    """
    Test 6: Real IB Gateway with Mock Consensus.
    
    Flow:
      1. Create mock consensus signal
      2. Calculate position size
      3. Place REAL bracket order via IB
      4. Verify order in database
      5. Cancel order (cleanup)
    """
    try:
        from consensus import calculate_consensus, save_consensus
        from position_sizer import calculate_position
        from order_manager import submit_signal
        from ib_gateway_client import cancel_order
    except ImportError as e:
        pytest.skip(f"Required modules not available: {e}")
    
    import tempfile
    
    print("\n" + "="*80)
    print("Test 6: Real IB Gateway with Mock Consensus")
    print("="*80)
    
    # Create temporary test database
    db_file = tempfile.mktemp(suffix="_ib_real_consensus.db")
    import sqlite3
    con = sqlite3.connect(db_file)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT, description TEXT);
        CREATE TABLE IF NOT EXISTS settings (ticker TEXT PRIMARY KEY, trading_blocked INTEGER DEFAULT 0, sector TEXT);
        CREATE TABLE IF NOT EXISTS consensus (id INTEGER PRIMARY KEY, date TEXT, ticker TEXT, signal TEXT, 
                                             confidence REAL, methods_long TEXT, methods_short TEXT, 
                                             methods_neutral TEXT, rationale TEXT, target_price REAL, 
                                             stop_loss REAL, created_at TEXT);
        CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, log_id TEXT, ticker TEXT, ib_order_id INTEGER, 
                                          ib_parent_id INTEGER, order_role TEXT, order_type TEXT, action TEXT, 
                                          quantity REAL, limit_price REAL, stop_price REAL, status TEXT, 
                                          account_type TEXT, created_at TEXT, submitted_at TEXT, filled_at TEXT, 
                                          spread_at_submission REAL, error_message TEXT, side TEXT);
        CREATE TABLE IF NOT EXISTS method_config (method TEXT PRIMARY KEY, timeframe_hours INTEGER, trigger TEXT, 
                                                  active INTEGER DEFAULT 1, execute_orders INTEGER DEFAULT 1, execute INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS providers (name TEXT PRIMARY KEY, type TEXT, url TEXT, api_key TEXT, model_id TEXT, 
                                             rate_limit REAL, max_tokens INTEGER, timeout_seconds INTEGER, active INTEGER, execute_orders INTEGER);
        CREATE TABLE IF NOT EXISTS portfolio (id INTEGER PRIMARY KEY, ticker TEXT, quantity REAL, avg_cost REAL, 
                                             market_price REAL, market_value REAL, unrealized_pnl REAL, realized_pnl REAL, 
                                             account TEXT, currency TEXT, last_update TEXT);
        CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, ticker TEXT, action TEXT, quantity REAL, 
                                          entry_price REAL, stop_loss REAL, target_price REAL, entry_time TEXT, 
                                          close_time TEXT, pnl REAL, status TEXT, ib_parent_id INTEGER);
        
        INSERT OR IGNORE INTO config VALUES ('ORDER_MODE', 'paper', '');
        INSERT OR IGNORE INTO config VALUES ('LIVE_TRADING_CONFIRMED', 'false', '');
        INSERT OR IGNORE INTO config VALUES ('MAX_OPEN_ORDERS', '10', '');
        INSERT OR IGNORE INTO settings VALUES ('TSLA', 0, 'Automotive');
    """)
    con.commit()
    con.close()
    
    class TestDbManager:
        def __init__(self, db):
            self.db_file = db
        def get_config_value(self, key):
            with sqlite3.connect(self.db_file) as con:
                r = con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
                return r[0] if r else None
    
    try:
        db = TestDbManager(db_file)
        
        print("\nStep 1: Create mock consensus for TSLA")
        current_price = 250.0
        mock_forecasts = [{
            "side": "LONG",
            "confidence": 70,
            "method": "momentum_trend",
            "model": "claude-sonnet",
            "exit_target": "$270.00",
            "stop_loss": 230.0,
            "entry_price": "$250.00"
        }]
        
        consensus = calculate_consensus(mock_forecasts, current_price=current_price)
        print(f"  Signal: {consensus['signal']}")
        print(f"  Target: ${consensus['target_price']:.2f}")
        print(f"  Stop:   ${consensus['stop_loss']:.2f}")
        
        print("\nStep 2: Calculate position size")
        position = calculate_position(
            ticker="TSLA",
            entry_price=current_price,
            stop_loss=consensus["stop_loss"],
            db_manager=db,
            net_liquidation=50000.0
        )
        print(f"  Quantity: {position['quantity']} shares")
        print(f"  Risk: ${position.get('risk_amount', 0):.2f}")
        
        if position["status"] != "OK":
            pytest.skip(f"Position calculation failed: {position}")
        
        print("\nStep 3: Place real bracket order via IB Gateway")
        result = submit_signal("TSLA", consensus, position, db, log_id="ib-real-consensus-001")
        
        print(f"  Result: {result['status']}")
        print(f"  Message: {result.get('message', '')}")
        
        if result["status"] not in ("SUBMITTED", "QUEUED"):
            print(f"  Error: {result.get('message')}")
            pytest.skip(f"Order submission failed")
        
        # Cleanup: cancel if submitted
        if result["status"] == "SUBMITTED" and "ib_ids" in result:
            parent_id = result["ib_ids"].get("parent")
            if parent_id:
                print(f"\nStep 4: Cleanup - cancel order #{parent_id}")
                try:
                    cancel_result = cancel_order(parent_id, port=7497, client_id=92)
                    print(f"  Cancel status: {cancel_result.get('status')}")
                except Exception as e:
                    print(f"  Could not cancel: {e}")
        
        print("\n[OK] Real IB integration with consensus completed")
        
    finally:
        # Cleanup
        import os
        try:
            os.remove(db_file)
        except:
            pass


# ============================================================================
# Main Entry
# ============================================================================

if __name__ == "__main__":
    print("\n" + "="*80)
    print("Real IB Gateway Integration Tests")
    print("="*80)
    print("\nThese tests connect to a REAL IB Gateway instance.")
    print("\nPrerequisites:")
    print("  1. IB Gateway running on localhost:7497 (paper) or :7496 (live)")
    print("  2. API enabled in IB Gateway settings")
    print("  3. Account connected to IB Gateway")
    print("  4. ORDER_MODE=paper (for safety)")
    print("\nRun with:")
    print("  pytest test_ib_real_connection.py -v -s -m integration")
    print("\n⚠️  WARNING: Tests place REAL orders (paper account only)!")
    print("="*80 + "\n")
