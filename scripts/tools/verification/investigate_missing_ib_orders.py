import sqlite3
from datetime import datetime, timezone, timedelta

def _get_db_path():
    from scripts.server.config import get_db_path
    return get_db_path()


conn = sqlite3.connect(_get_db_path())
cursor = conn.cursor()

# Check the STALE orders and their IB transaction history
cursor.execute("""
    SELECT o.id, o.ticker, o.ib_order_id, o.ib_perm_id, o.status, o.submitted_at, o.created_at
    FROM orders o
    WHERE o.status = 'STALE' AND UPPER(o.order_role)='ENTRY'
    ORDER BY o.submitted_at DESC
""")

stale_entry_orders = cursor.fetchall()

print("STALE ENTRY ORDERS - INVESTIGATION:")
print("=" * 100)

for order in stale_entry_orders:
    order_id = order[0]
    ticker = order[1]
    ib_order_id = order[2]
    ib_perm_id = order[3]
    status = order[4]
    submitted_at = order[5]
    created_at = order[6]
    
    print(f"\nOrder ID: {order_id}, Ticker: {ticker}")
    print(f"IB Order ID: {ib_order_id}, IB Perm ID: {ib_perm_id}")
    print(f"Status: {status}")
    print(f"Created: {created_at}")
    print(f"Submitted: {submitted_at}")
    
    # Check IB transaction history for this order
    cursor.execute("""
        SELECT occurred_at, event_type, operation_status, status_before, status_after, 
               response_payload_json, error_message
        FROM ib_order_transactions 
        WHERE ib_order_id = ? OR ib_perm_id = ?
        ORDER BY occurred_at ASC
    """, (ib_order_id, ib_perm_id))
    
    transactions = cursor.fetchall()
    
    print(f"Transaction history ({len(transactions)} events):")
    for tx in transactions:
        print(f"  {tx[0]}: {tx[1]} - {tx[2]}")
        print(f"    Status: {tx[3]} -> {tx[4]}")
        if tx[6]:  # error_message
            print(f"    ERROR: {tx[6]}")
        if tx[5]:  # response_payload
            response = tx[5][:100] + "..." if len(tx[5]) > 100 else tx[5]
            print(f"    Response: {response}")
    
    print("-" * 60)

# Check if there were any IB connection issues during that time
print(f"\n\nIB CONNECTION ISSUES DURING THAT PERIOD:")
print("=" * 100)

# Get the time range of the stale orders
if stale_entry_orders:
    earliest_time = min(order[6] for order in stale_entry_orders)
    latest_time = max(order[6] for order in stale_entry_orders)
    
    print(f"Time range: {earliest_time} to {latest_time}")
    
    # Check for any error logs in transactions during this period
    cursor.execute("""
        SELECT COUNT(*) as error_count
        FROM ib_order_transactions 
        WHERE occurred_at BETWEEN ? AND ? 
        AND (operation_status = 'ERROR' OR error_message IS NOT NULL)
    """, (earliest_time, latest_time))
    
    error_count = cursor.fetchone()[0]
    print(f"IB transaction errors during this period: {error_count}")

# Check the order submission logs
print(f"\n\nORDER SUBMISSION PATTERN ANALYSIS:")
print("=" * 100)

cursor.execute("""
    SELECT 
        DATE(created_at) as date,
        COUNT(*) as total_orders,
        COUNT(CASE WHEN status = 'SUBMITTED' THEN 1 END) as submitted,
        COUNT(CASE WHEN status = 'STALE' THEN 1 END) as stale,
        COUNT(CASE WHEN status = 'FILLED_ENTRY' THEN 1 END) as filled
    FROM orders 
    WHERE created_at >= datetime('now', '-7 days')
    GROUP BY DATE(created_at)
    ORDER BY date DESC
""")

daily_stats = cursor.fetchall()
for stat in daily_stats:
    print(f"{stat[0]}: Total={stat[1]}, Submitted={stat[2]}, Stale={stat[3]}, Filled={stat[4]}")

conn.close()
