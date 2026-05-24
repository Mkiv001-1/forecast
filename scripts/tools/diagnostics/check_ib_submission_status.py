import sqlite3
from datetime import datetime, timezone, timedelta
import time

def _get_db_path():
    from scripts.server.config import get_db_path
    return get_db_path()


def check_order_submission_progress():
    """Check if orders have been submitted to IB."""
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    print("CHECKING IB ORDER SUBMISSION PROGRESS:")
    print("=" * 80)
    
    # Check the status of our orders
    cursor.execute("""
        SELECT id, ticker, order_role, status, ib_order_id, 
               submitted_at, created_at, trade_uid
        FROM orders 
        WHERE id >= 49
        ORDER BY trade_uid, order_role
    """)
    
    orders = cursor.fetchall()
    
    current_uid = None
    submitted_orders = 0
    queued_orders = 0
    
    for order in orders:
        order_id, ticker, role, status, ib_order_id, submitted_at, created_at, trade_uid = order
        
        if trade_uid != current_uid:
            print(f"\nTrade: {trade_uid}")
            current_uid = trade_uid
        
        print(f"  ID {order_id}: {ticker} - {role}")
        print(f"    Status: {status}")
        print(f"    IB Order ID: {ib_order_id}")
        print(f"    Submitted: {submitted_at}")
        print(f"    Created: {created_at}")
        
        if status == "SUBMITTED":
            submitted_orders += 1
        elif status == "QUEUED":
            queued_orders += 1
    
    print(f"\n\nSUMMARY:")
    print(f"Submitted orders: {submitted_orders}")
    print(f"Queued orders: {queued_orders}")
    
    # Check consensus status
    cursor.execute("""
        SELECT order_state, COUNT(*) as count
        FROM consensus 
        WHERE id IN (246, 259, 264, 272, 273)
        GROUP BY order_state
    """)
    
    consensus_states = cursor.fetchall()
    
    print(f"\n\nCONSENSUS STATES:")
    for state in consensus_states:
        print(f"  {state[0]}: {state[1]}")
    
    # Check for any recent IB transactions
    cursor.execute("""
        SELECT COUNT(*) as count
        FROM ib_order_transactions 
        WHERE occurred_at > datetime('now', '-5 minutes')
    """)
    
    recent_transactions = cursor.fetchone()[0]
    print(f"\n\nRecent IB transactions (last 5 min): {recent_transactions}")
    
    conn.close()
    
    return submitted_orders, queued_orders

def check_ib_gateway_status():
    """Check IB gateway connection status."""
    print(f"\n\nIB GATEWAY STATUS:")
    print("=" * 80)
    
    # This would require importing the IB gateway client
    # For now, let's check if there are any recent connection errors
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT COUNT(*) as count
        FROM ib_order_transactions 
        WHERE operation_status = 'ERROR'
        AND occurred_at > datetime('now', '-10 minutes')
    """)
    
    recent_errors = cursor.fetchone()[0]
    
    if recent_errors > 0:
        print(f"Recent IB errors: {recent_errors}")
        
        cursor.execute("""
            SELECT occurred_at, event_type, error_message
            FROM ib_order_transactions 
            WHERE operation_status = 'ERROR'
            AND occurred_at > datetime('now', '-10 minutes')
            ORDER BY occurred_at DESC
            LIMIT 5
        """)
        
        errors = cursor.fetchall()
        for error in errors:
            print(f"  {error[0]}: {error[1]} - {error[2]}")
    else:
        print("No recent IB errors found")
    
    conn.close()

def wait_and_check():
    """Wait a bit and check again."""
    print(f"\n\nWAITING 30 SECONDS FOR SCHEDULER TO PROCESS...")
    print("=" * 80)
    
    for i in range(30, 0, -1):
        print(f"  {i} seconds remaining...", end='\r')
        time.sleep(1)
    
    print("\n\nCHECKING AGAIN...")
    return check_order_submission_progress()

def main():
    print("MONITORING IB ORDER SUBMISSION")
    print("=" * 80)
    
    # Initial check
    submitted, queued = check_order_submission_progress()
    check_ib_gateway_status()
    
    if queued > 0:
        print(f"\n\nORDERS STILL QUEUED - WAITING FOR SCHEDULER...")
        submitted_after, queued_after = wait_and_check()
        
        if submitted_after > submitted:
            print(f"\n\nPROGRESS! {submitted_after - submitted} orders submitted to IB")
        else:
            print(f"\n\nNO PROGRESS - Orders still queued")
            print("Possible issues:")
            print("1. Scheduler not running")
            print("2. IB gateway not connected")
            print("3. ORDER_MODE not set to 'paper' or 'live'")
            print("4. activate_consensus_order function not working")
    else:
        print(f"\n\nALL ORDERS SUBMITTED TO IB!")

if __name__ == "__main__":
    main()
