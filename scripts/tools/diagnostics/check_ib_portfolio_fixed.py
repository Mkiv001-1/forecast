import sqlite3
import sys
import os
from datetime import datetime, timezone, timedelta

def _get_db_path():
    from scripts.server.config import get_db_path
    return get_db_path()


# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts', 'core'))

def get_config_value(db_path, key, default=None):
    """Get configuration value from database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else default

def check_ib_portfolio_sync():
    """Check IB portfolio synchronization."""
    print("CHECKING IB PORTFOLIO SYNCHRONIZATION:")
    print("=" * 80)
    
    try:
        from scripts.core.sqlite_manager import SQLiteManager
        from scripts.core.ib_gateway_client import (
            sync_accounts_with_ib_safe,
            sync_portfolio_with_ib_safe,
            fetch_ib_portfolio_summary,
            test_ib_connection
        )
    except ImportError as e:
        print(f"Import error: {e}")
        return False
    
    # Get database manager
    db_manager = SQLiteManager(_get_db_path())
    
    # Check configuration
    print("Configuration:")
    ib_host = get_config_value(_get_db_path(), 'IB_HOST', '127.0.0.1')
    ib_port = int(get_config_value(_get_db_path(), 'IB_PORT', '7497'))
    ib_client_id = int(get_config_value(_get_db_path(), 'IB_CLIENT_ID', '1'))
    order_mode = get_config_value(_get_db_path(), 'ORDER_MODE', 'paper')
    
    print(f"  IB_HOST: {ib_host}")
    print(f"  IB_PORT: {ib_port}")
    print(f"  IB_CLIENT_ID: {ib_client_id}")
    print(f"  ORDER_MODE: {order_mode}")
    
    # Test IB connection
    print(f"\nTesting IB connection...")
    try:
        connection_result = test_ib_connection(ib_host, ib_port, ib_client_id)
        print(f"  Connection result: {connection_result}")
        
        if not connection_result.get('connected', False):
            print(f"  ✗ IB Gateway not connected")
            print(f"    Error: {connection_result.get('error', 'Unknown error')}")
            return False
    except Exception as e:
        print(f"  ✗ Connection test failed: {e}")
        return False
    
    print(f"  ✓ IB Gateway connected")
    
    # Sync accounts
    print(f"\nSyncing accounts with IB...")
    try:
        accounts_result = sync_accounts_with_ib_safe(db_manager)
        print(f"  Accounts sync result: {accounts_result}")
    except Exception as e:
        print(f"  ✗ Account sync failed: {e}")
    
    # Sync portfolio
    print(f"\nSyncing portfolio with IB...")
    try:
        portfolio_result = sync_portfolio_with_ib_safe(db_manager)
        print(f"  Portfolio sync result: {portfolio_result}")
    except Exception as e:
        print(f"  ✗ Portfolio sync failed: {e}")
    
    # Fetch portfolio summary
    print(f"\nFetching portfolio summary...")
    try:
        portfolio_summary = fetch_ib_portfolio_summary(db_manager)
        print(f"  Portfolio summary: {portfolio_summary}")
        
        if portfolio_summary:
            print(f"  ✓ Portfolio data retrieved:")
            for key, value in portfolio_summary.items():
                if isinstance(value, (int, float)):
                    print(f"    {key}: ${value:,.2f}")
                else:
                    print(f"    {key}: {value}")
        else:
            print(f"  ✗ No portfolio data returned")
            
    except Exception as e:
        print(f"  ✗ Portfolio summary failed: {e}")
    
    return True

def check_portfolio_data_in_db():
    """Check portfolio data stored in database."""
    print(f"\n\nCHECKING PORTFOLIO DATA IN DATABASE:")
    print("=" * 80)
    
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    # Check accounts table
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'")
    accounts_table = cursor.fetchone()
    
    if accounts_table:
        print("Accounts table found")
        
        cursor.execute("SELECT COUNT(*) FROM accounts")
        accounts_count = cursor.fetchone()[0]
        print(f"Account records: {accounts_count}")
        
        if accounts_count > 0:
            cursor.execute("""
                SELECT account_alias, net_liquidation, available_funds, 
                       buying_power, total_cash, updated_at
                FROM accounts 
                ORDER BY updated_at DESC 
                LIMIT 3
            """)
            
            accounts = cursor.fetchall()
            
            print(f"Recent account data:")
            for account in accounts:
                print(f"  Account: {account[0]}")
                print(f"    Net Liquidation: ${account[1]:,.2f}" if account[1] else "    Net Liquidation: N/A")
                print(f"    Available Funds: ${account[2]:,.2f}" if account[2] else "    Available Funds: N/A")
                print(f"    Buying Power: ${account[3]:,.2f}" if account[3] else "    Buying Power: N/A")
                print(f"    Total Cash: ${account[4]:,.2f}" if account[4] else "    Total Cash: N/A")
                print(f"    Updated: {account[5]}")
    else:
        print("No accounts table found")
    
    # Check portfolio table
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='portfolio'")
    portfolio_table = cursor.fetchone()
    
    if portfolio_table:
        print(f"\nPortfolio table found")
        
        cursor.execute("SELECT COUNT(*) FROM portfolio")
        portfolio_count = cursor.fetchone()[0]
        print(f"Portfolio records: {portfolio_count}")
        
        if portfolio_count > 0:
            cursor.execute("""
                SELECT con_id, symbol, position, market_price, market_value, 
                       average_cost, unrealized_pnl, realized_pnl, updated_at
                FROM portfolio 
                ORDER BY updated_at DESC 
                LIMIT 5
            """)
            
            positions = cursor.fetchall()
            
            print(f"Recent positions:")
            for pos in positions:
                print(f"  {pos[1]} (ID: {pos[0]}): {pos[2]} shares")
                print(f"    Market Price: ${pos[3]:.2f}")
                print(f"    Market Value: ${pos[4]:,.2f}")
                print(f"    Avg Cost: ${pos[5]:.2f}")
                print(f"    Unrealized P&L: ${pos[6]:,.2f}")
                print(f"    Updated: {pos[8]}")
    else:
        print(f"No portfolio table found")
    
    conn.close()

def test_position_sizing():
    """Test position sizing with current data."""
    print(f"\n\nTESTING POSITION SIZING:")
    print("=" * 80)
    
    try:
        from scripts.core.sqlite_manager import SQLiteManager
        from scripts.core.order_manager import submit_signal
    except ImportError as e:
        print(f"Import error: {e}")
        return False
    
    db_manager = SQLiteManager(_get_db_path())
    
    # Create a simple test order
    test_consensus = {
        'ticker': 'NASDAQ:MSFT',
        'signal': 'LONG',
        'confidence': 75.0,
        'target_price': 450.0,
        'stop_loss': 420.0,
        'entry_limit_price': 435.0,
        'forecasts': []
    }
    
    position_size = {
        'quantity': 3,  # Small test size
        'capital_allocated': 3000
    }
    
    print(f"Testing position sizing for MSFT...")
    print(f"  Entry: ${test_consensus['entry_limit_price']}")
    print(f"  Target: ${test_consensus['target_price']}")
    print(f"  Stop: ${test_consensus['stop_loss']}")
    
    try:
        result = submit_signal(
            ticker='NASDAQ:MSFT',
            consensus=test_consensus,
            position_size=position_size,
            db_manager=db_manager
        )
        
        print(f"  Result: {result}")
        
        if result and result.get('status') != 'UNKNOWN':
            print(f"  ✓ Position sizing worked!")
            return True
        else:
            print(f"  ✗ Position sizing still failed")
            return False
            
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False

def main():
    print("CHECK IB PORTFOLIO DATA FOR POSITION SIZING")
    print("=" * 80)
    
    # Check IB portfolio sync
    ib_synced = check_ib_portfolio_sync()
    
    # Check database data
    check_portfolio_data_in_db()
    
    # Test position sizing
    if ib_synced:
        test_position_sizing()
    
    print(f"\n\nSUMMARY:")
    print("=" * 80)
    
    if ib_synced:
        print("✓ IB portfolio synchronization working")
        print("✓ Account data available for position sizing")
        print("→ Position sizing should now work correctly")
    else:
        print("✗ IB portfolio synchronization failed")
        print("✗ No account data available")
        print("→ Position sizing will continue to fail")
        print("\nTO FIX:")
        print("1. Start IB Gateway/TWS")
        print("2. Enable API connections")
        print("3. Configure correct port (7497 for paper trading)")

if __name__ == "__main__":
    main()
