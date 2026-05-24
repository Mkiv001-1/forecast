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

def check_ib_account_connection():
    """Check IB account data retrieval."""
    print("CHECKING IB ACCOUNT DATA RETRIEVAL:")
    print("=" * 80)
    
    try:
        from scripts.core.sqlite_manager import SQLiteManager
        from scripts.core.ib_gateway_client import IBGatewayClient
    except ImportError as e:
        print(f"Import error: {e}")
        return False
    
    # Get database manager
    db_manager = SQLiteManager(_get_db_path())
    
    # Check configuration
    print("Configuration:")
    cursor = db_manager._connect().cursor()
    cursor.execute("SELECT key, value FROM config WHERE key IN ('ORDER_MODE', 'IB_HOST', 'IB_PORT', 'IB_CLIENT_ID')")
    config = cursor.fetchall()
    for key, value in config:
        print(f"  {key}: {value}")
    cursor.close()
    
    # Try to connect to IB and get account data
    try:
        print(f"\nAttempting to connect to IB Gateway...")
        
        # Get IB connection settings
        ib_host = db_manager.get_config('IB_HOST', '127.0.0.1')
        ib_port = int(db_manager.get_config('IB_PORT', '7497'))
        ib_client_id = int(db_manager.get_config('IB_CLIENT_ID', '1'))
        
        print(f"  Host: {ib_host}")
        print(f"  Port: {ib_port}")
        print(f"  Client ID: {ib_client_id}")
        
        # Create IB client
        ib_client = IBGatewayClient(
            host=ib_host,
            port=ib_port,
            client_id=ib_client_id
        )
        
        print(f"  Connecting...")
        
        # Try to connect (this might fail if IB Gateway is not running)
        connected = ib_client.connect()
        
        if connected:
            print(f"  ✓ Connected to IB Gateway")
            
            # Try to get account data
            print(f"  Getting account data...")
            
            try:
                account_data = ib_client.get_account_data()
                print(f"  ✓ Account data retrieved:")
                print(f"    Net Liquidation: {account_data.get('net_liquidation', 'N/A')}")
                print(f"    Available Funds: {account_data.get('available_funds', 'N/A')}")
                print(f"    Buying Power: {account_data.get('buying_power', 'N/A')}")
                print(f"    Total Cash: {account_data.get('total_cash', 'N/A')}")
                
                return True
                
            except Exception as e:
                print(f"  ✗ Failed to get account data: {e}")
                return False
            finally:
                ib_client.disconnect()
                print(f"  Disconnected from IB Gateway")
                
        else:
            print(f"  ✗ Failed to connect to IB Gateway")
            print(f"    Make sure IB Gateway is running on port {ib_port}")
            return False
            
    except Exception as e:
        print(f"  ✗ IB connection error: {e}")
        return False

def check_position_sizing_config():
    """Check position sizing configuration."""
    print(f"\n\nCHECKING POSITION SIZING CONFIGURATION:")
    print("=" * 80)
    
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    # Check position sizing related settings
    cursor.execute("""
        SELECT key, value, description 
        FROM config 
        WHERE key LIKE '%POSITION%' OR key LIKE '%CAPITAL%' OR key LIKE '%RISK%' OR key LIKE '%SIZE%'
        ORDER BY key
    """)
    
    position_settings = cursor.fetchall()
    
    if position_settings:
        print("Position sizing settings:")
        for key, value, desc in position_settings:
            print(f"  {key}: {value}")
            if desc:
                print(f"    {desc}")
    else:
        print("No position sizing settings found")
    
    # Check for default capital settings
    cursor.execute("""
        SELECT key, value, description 
        FROM config 
        WHERE key IN ('DEFAULT_CAPITAL', 'MAX_POSITION_SIZE_PCT', 'RISK_PER_TRADE_PCT')
    """)
    
    capital_settings = cursor.fetchall()
    
    if capital_settings:
        print(f"\nCapital/risk settings:")
        for key, value, desc in capital_settings:
            print(f"  {key}: {value}")
            if desc:
                print(f"    {desc}")
    else:
        print(f"\nNo capital/risk settings found")
    
    conn.close()

def check_portfolio_data():
    """Check if there's any portfolio data in the database."""
    print(f"\n\nCHECKING PORTFOLIO DATA:")
    print("=" * 80)
    
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    # Check if portfolio table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='portfolio'")
    portfolio_table = cursor.fetchone()
    
    if portfolio_table:
        print("Portfolio table exists")
        
        # Get portfolio data
        cursor.execute("SELECT COUNT(*) FROM portfolio")
        portfolio_count = cursor.fetchone()[0]
        print(f"Portfolio records: {portfolio_count}")
        
        if portfolio_count > 0:
            cursor.execute("SELECT * FROM portfolio ORDER BY updated_at DESC LIMIT 5")
            recent_portfolio = cursor.fetchall()
            
            print(f"Recent portfolio data:")
            for record in recent_portfolio:
                print(f"  {record}")
    else:
        print("No portfolio table found")
    
    # Check account summary table
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='account_summary'")
    account_table = cursor.fetchone()
    
    if account_table:
        print(f"\nAccount summary table exists")
        
        cursor.execute("SELECT COUNT(*) FROM account_summary")
        account_count = cursor.fetchone()[0]
        print(f"Account summary records: {account_count}")
        
        if account_count > 0:
            cursor.execute("SELECT * FROM account_summary ORDER BY updated_at DESC LIMIT 3")
            recent_account = cursor.fetchall()
            
            print(f"Recent account data:")
            for record in recent_account:
                print(f"  {record}")
    else:
        print(f"No account summary table found")
    
    conn.close()

def main():
    print("CHECK IB ACCOUNT DATA FOR POSITION SIZING")
    print("=" * 80)
    
    # Check IB connection
    ib_connected = check_ib_account_connection()
    
    # Check position sizing config
    check_position_sizing_config()
    
    # Check portfolio data
    check_portfolio_data()
    
    print(f"\n\nSUMMARY:")
    print("=" * 80)
    
    if ib_connected:
        print("✓ IB Gateway connection working")
        print("✓ Account data can be retrieved")
        print("→ Position sizing should work with IB data")
    else:
        print("✗ IB Gateway connection failed")
        print("✗ Cannot retrieve account data")
        print("→ Position sizing will fail without capital data")
        print("\nTO FIX:")
        print("1. Start IB Gateway/TWS")
        print("2. Configure correct port (7497 for paper, 7496 for live)")
        print("3. Ensure API connections are enabled in IB Gateway")

if __name__ == "__main__":
    main()
