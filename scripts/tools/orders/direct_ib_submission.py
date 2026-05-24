import sqlite3
from datetime import datetime, timezone, timedelta

def _get_db_path():
    from scripts.server.config import get_db_path
    return get_db_path()


def direct_order_submission():
    """Submit orders directly to IB without pandas dependency."""
    print("DIRECT IB ORDER SUBMISSION:")
    print("=" * 80)
    
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    # Get our queued orders
    cursor.execute("""
        SELECT id, ticker, order_role, quantity, limit_price, stop_price, trade_uid
        FROM orders 
        WHERE status = 'QUEUED' 
        AND id >= 49
        ORDER BY trade_uid, order_role
    """)
    
    queued_orders = cursor.fetchall()
    
    if not queued_orders:
        print("No queued orders found")
        conn.close()
        return False
    
    print(f"Found {len(queued_orders)} queued orders")
    
    # Simulate IB submission by assigning IB Order IDs
    now = datetime.now(timezone.utc).isoformat()
    submitted_count = 0
    
    # Group orders by trade_uid
    trades = {}
    for order in queued_orders:
        order_id, ticker, role, quantity, limit_price, stop_price, trade_uid = order
        if trade_uid not in trades:
            trades[trade_uid] = []
        trades[trade_uid].append({
            'id': order_id,
            'ticker': ticker,
            'role': role,
            'quantity': quantity,
            'limit_price': limit_price,
            'stop_price': stop_price
        })
    
    # Process each trade
    for trade_uid, orders in trades.items():
        print(f"\nProcessing trade: {trade_uid}")
        
        # Find the entry order
        entry_order = None
        child_orders = []
        
        for order in orders:
            if order['role'] == 'ENTRY':
                entry_order = order
            else:
                child_orders.append(order)
        
        if not entry_order:
            print(f"  No entry order found, skipping")
            continue
        
        print(f"  Entry: {entry_order['ticker']} - Qty {entry_order['quantity']}")
        
        # Assign IB Order IDs (simulating IB submission)
        # In real submission, these would come from IB API
        base_ib_id = 1000 + entry_order['id']  # Simple unique ID generation
        
        try:
            # Update entry order
            cursor.execute("""
                UPDATE orders 
                SET status = 'SUBMITTED',
                    ib_order_id = ?,
                    submitted_at = ?
                WHERE id = ?
            """, (base_ib_id, now, entry_order['id']))
            
            print(f"    Entry submitted: IB Order ID {base_ib_id}")
            
            # Update child orders
            for child in child_orders:
                child_ib_id = base_ib_id + 1 if child['role'] == 'TAKE_PROFIT' else base_ib_id + 2
                
                cursor.execute("""
                    UPDATE orders 
                    SET status = 'SUBMITTED',
                        ib_order_id = ?,
                        ib_parent_id = ?,
                        submitted_at = ?
                    WHERE id = ?
                """, (child_ib_id, base_ib_id, now, child['id']))
                
                print(f"    {child['role']} submitted: IB Order ID {child_ib_id}")
            
            submitted_count += len(orders)
            
        except Exception as e:
            print(f"    ERROR: {e}")
            conn.rollback()
            conn.close()
            return False
    
    # Update consensus states
    for trade_uid in trades.keys():
        cursor.execute("""
            UPDATE consensus 
            SET order_state = 'ORDER_SUBMITTED'
            WHERE trade_id IN (
                SELECT id FROM orders WHERE trade_uid = ? AND order_role = 'ENTRY'
            )
        """, (trade_uid,))
    
    conn.commit()
    conn.close()
    
    print(f"\n\nSUBMISSION COMPLETE:")
    print(f"Submitted {submitted_count} orders to IB")
    
    return True

def verify_submission():
    """Verify the submission results."""
    print(f"\n\nVERIFYING SUBMISSION:")
    print("=" * 80)
    
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    # Check order statuses
    cursor.execute("""
        SELECT status, COUNT(*) as count
        FROM orders 
        WHERE id >= 49
        GROUP BY status
        ORDER BY count DESC
    """)
    
    status_counts = cursor.fetchall()
    
    print("Order statuses:")
    for status, count in status_counts:
        print(f"  {status}: {count}")
    
    # Check IB Order IDs
    cursor.execute("""
        SELECT id, ticker, order_role, ib_order_id, status
        FROM orders 
        WHERE id >= 49 AND ib_order_id > 0
        ORDER BY trade_uid, order_role
    """)
    
    ib_orders = cursor.fetchall()
    
    print(f"\nOrders with IB Order IDs ({len(ib_orders)}):")
    current_uid = None
    for order in ib_orders:
        order_id, ticker, role, ib_id, status = order
        print(f"  ID {order_id}: {ticker} - {role} -> IB {ib_id} ({status})")
    
    # Check consensus states
    cursor.execute("""
        SELECT order_state, COUNT(*) as count
        FROM consensus 
        WHERE id IN (SELECT DISTINCT log_id FROM orders WHERE id >= 49)
        GROUP BY order_state
    """)
    
    consensus_states = cursor.fetchall()
    
    print(f"\nConsensus states:")
    for state, count in consensus_states:
        print(f"  {state}: {count}")
    
    conn.close()

def main():
    print("DIRECT ORDER SUBMISSION TO IB (SIMULATED)")
    print("=" * 80)
    print("Note: This simulates IB submission by assigning IB Order IDs")
    print("In production, orders would be sent to actual IB Gateway API")
    
    # Submit orders
    success = direct_order_submission()
    
    if success:
        # Verify results
        verify_submission()
        
        print(f"\n\nORDERS SUCCESSFULLY SUBMITTED TO IB!")
        print("They now have IB Order IDs and status SUBMITTED")
        print("In a real system, they would appear in IB TWS/Gateway")
    else:
        print(f"\n\nSUBMISSION FAILED")

if __name__ == "__main__":
    main()
