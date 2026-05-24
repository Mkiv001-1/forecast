import sqlite3
from datetime import datetime, timezone, timedelta

def _get_db_path():
    from scripts.server.config import get_db_path
    return get_db_path()


def check_order_submission_conditions():
    """Check when orders will be submitted to IB."""
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    print("ORDER SUBMISSION TIMING ANALYSIS:")
    print("=" * 80)
    
    # Check current time and market hours
    now = datetime.now(timezone.utc)
    print(f"Current UTC time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Current weekday: {now.strftime('%A')} ({now.weekday()})")
    
    # Market hours (NYSE): 14:30-21:00 UTC, Mon-Fri
    market_open = now.replace(hour=14, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=21, minute=0, second=0, microsecond=0)
    is_weekday = now.weekday() < 5
    is_market_hours = is_weekday and market_open <= now <= market_close
    
    print(f"Market open: {market_open.strftime('%H:%M')} UTC")
    print(f"Market close: {market_close.strftime('%H:%M')} UTC")
    print(f"Is weekday: {is_weekday}")
    print(f"Is market hours: {is_market_hours}")
    
    # Check configuration settings
    cursor.execute("SELECT value FROM config WHERE key = 'ALLOW_EXTENDED_HOURS'")
    allow_extended = cursor.fetchone()
    allow_extended = allow_extended[0] if allow_extended else "false"
    print(f"ALLOW_EXTENDED_HOURS: {allow_extended}")
    
    cursor.execute("SELECT value FROM config WHERE key = 'QUEUE_DAY_ORDERS'")
    queue_day_orders = cursor.fetchone()
    queue_day_orders = queue_day_orders[0] if queue_day_orders else "true"
    print(f"QUEUE_DAY_ORDERS: {queue_day_orders}")
    
    # Check queued orders
    cursor.execute("""
        SELECT COUNT(*) as count
        FROM orders 
        WHERE status = 'QUEUED' 
        AND UPPER(order_role) = 'ENTRY'
    """)
    
    queued_count = cursor.fetchone()[0]
    print(f"Queued ENTRY orders: {queued_count}")
    
    # Determine when orders will be submitted
    print(f"\n\nSUBMISSION TIMING:")
    print("=" * 80)
    
    if allow_extended == "true":
        print("ALLOWED: Orders can be submitted anytime (extended hours enabled)")
        print("STATUS: Orders should be submitted immediately")
        submission_time = "IMMEDIATELY"
    elif is_market_hours:
        print("ALLOWED: Currently within market hours")
        print("STATUS: Orders should be submitted immediately")
        submission_time = "IMMEDIATELY"
    else:
        print("NOT ALLOWED: Outside market hours and extended hours disabled")
        
        if is_weekday:
            # Calculate next market open
            if now < market_open:
                next_open = market_open
                hours_until = (next_open - now).total_seconds() / 3600
                print(f"Next market open: Today at {next_open.strftime('%H:%M')} UTC")
                print(f"Hours until open: {hours_until:.1f}")
            else:
                # Market already closed today, next open is tomorrow
                tomorrow = now + timedelta(days=1)
                next_open = tomorrow.replace(hour=14, minute=30, second=0, microsecond=0)
                days_until = (next_open - now).days
                hours_until = (next_open - now).total_seconds() / 3600
                print(f"Next market open: Tomorrow at {next_open.strftime('%H:%M')} UTC")
                print(f"Days until open: {days_until}")
                print(f"Hours until open: {hours_until:.1f}")
        else:
            # Weekend, find next Monday
            days_until_monday = (7 - now.weekday()) % 7 or 7
            next_monday = now + timedelta(days=days_until_monday)
            next_open = next_monday.replace(hour=14, minute=30, second=0, microsecond=0)
            print(f"Next market open: Next Monday at {next_open.strftime('%H:%M')} UTC")
            print(f"Days until open: {days_until_monday}")
        
        submission_time = "NEXT MARKET OPEN"
    
    # Check scheduler status
    print(f"\n\nSCHEDULER STATUS:")
    print("=" * 80)
    
    cursor.execute("""
        SELECT key, value 
        FROM config 
        WHERE key IN ('PENDING_ORDERS_INTERVAL_MINUTES', 'ORDER_STATUS_SYNC_INTERVAL_SECONDS')
    """)
    
    scheduler_settings = cursor.fetchall()
    for setting in scheduler_settings:
        print(f"{setting[0]}: {setting[1]}")
    
    print("\nThe scheduler processes PENDING_ORDER consensus every minute")
    print("This should trigger order submission when conditions are met")
    
    conn.close()
    
    return submission_time, is_market_hours, allow_extended == "true"

def check_order_queue_status():
    """Check detailed status of queued orders."""
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    print(f"\n\nDETAILED QUEUE STATUS:")
    print("=" * 80)
    
    cursor.execute("""
        SELECT id, ticker, order_role, status, created_at, trade_uid
        FROM orders 
        WHERE status = 'QUEUED' 
        AND id >= 49
        ORDER BY trade_uid, order_role
    """)
    
    queued_orders = cursor.fetchall()
    
    current_uid = None
    for order in queued_orders:
        order_id, ticker, role, status, created_at, trade_uid = order
        if trade_uid != current_uid:
            print(f"\nTrade: {trade_uid}")
            current_uid = trade_uid
        print(f"  ID {order_id}: {ticker} - {role} ({status})")
        print(f"    Created: {created_at}")
    
    conn.close()

def main():
    submission_time, is_market_hours, allow_extended = check_order_submission_conditions()
    check_order_queue_status()
    
    print(f"\n\nFINAL ANSWER:")
    print("=" * 80)
    
    if submission_time == "IMMEDIATELY":
        print("Orders will be submitted IMMEDIATELY to IB")
        if allow_extended and not is_market_hours:
            print("(Extended hours are enabled)")
        else:
            print("(Currently within market hours)")
    else:
        print(f"Orders will be submitted at {submission_time}")
        print("(When market hours begin and extended hours are disabled)")

if __name__ == "__main__":
    main()
