import sqlite3

def _get_db_path():
    from scripts.server.config import get_db_path
    return get_db_path()


conn = sqlite3.connect(_get_db_path())
cursor = conn.cursor()

# Check the specific errors during the order submission period
cursor.execute("""
    SELECT occurred_at, event_type, operation_status, status_before, status_after, 
           response_payload_json, error_message, ib_order_id, ib_perm_id
    FROM ib_order_transactions 
    WHERE occurred_at BETWEEN '2026-05-11T19:46:00.000000+00:00' AND '2026-05-11T20:05:00.000000+00:00'
    AND (operation_status = 'ERROR' OR error_message IS NOT NULL)
    ORDER BY occurred_at ASC
""")

errors = cursor.fetchall()

print("IB ERRORS DURING ORDER SUBMISSION PERIOD:")
print("=" * 120)

for error in errors:
    print(f"Time: {error[0]}")
    print(f"Event: {error[1]}, Status: {error[2]}")
    print(f"IB Order ID: {error[7]}, Perm ID: {error[8]}")
    print(f"Status Change: {error[3]} -> {error[4]}")
    if error[6]:
        print(f"Error: {error[6]}")
    if error[5]:
        response = error[5][:200] + "..." if len(error[5]) > 200 else error[5]
        print(f"Response: {response}")
    print("-" * 60)

# Check what happened to the specific orders that became STALE
print(f"\n\nDETAILED ANALYSIS OF STALE ORDERS:")
print("=" * 120)

cursor.execute("""
    SELECT o.ib_order_id, o.ib_perm_id, o.ticker, o.status, o.submitted_at,
           COUNT(t.id) as transaction_count,
           COUNT(CASE WHEN t.operation_status = 'ERROR' THEN 1 END) as error_count
    FROM orders o
    LEFT JOIN ib_order_transactions t ON (t.ib_order_id = o.ib_order_id OR t.ib_perm_id = o.ib_perm_id)
    WHERE o.status = 'STALE' AND UPPER(o.order_role) = 'ENTRY'
    GROUP BY o.id
    ORDER BY o.submitted_at DESC
""")

stale_analysis = cursor.fetchall()

for analysis in stale_analysis:
    print(f"Ticker: {analysis[2]}, IB Order ID: {analysis[0]}, Perm ID: {analysis[1]}")
    print(f"Status: {analysis[3]}, Submitted: {analysis[4]}")
    print(f"Transactions: {analysis[5]}, Errors: {analysis[6]}")
    print("-" * 60)

# Check if there were any ORDER_CANCEL events
print(f"\n\nORDER CANCEL EVENTS:")
print("=" * 120)

cursor.execute("""
    SELECT occurred_at, ib_order_id, ib_perm_id, event_type, operation_status, error_message
    FROM ib_order_transactions 
    WHERE event_type LIKE '%CANCEL%' 
    AND occurred_at BETWEEN '2026-05-11T19:46:00.000000+00:00' AND '2026-05-11T20:05:00.000000+00:00'
    ORDER BY occurred_at ASC
""")

cancels = cursor.fetchall()

if cancels:
    for cancel in cancels:
        print(f"Time: {cancel[0]}")
        print(f"IB Order ID: {cancel[1]}, Perm ID: {cancel[2]}")
        print(f"Event: {cancel[3]}, Status: {cancel[4]}")
        if cancel[5]:
            print(f"Error: {cancel[5]}")
        print("-" * 60)
else:
    print("No cancel events found during this period")

conn.close()
