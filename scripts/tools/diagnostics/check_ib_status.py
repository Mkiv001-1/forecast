import sys
sys.path.append('scripts')

from core.ib_gateway_client import fetch_open_order_statuses

try:
    # Fetch current IB order statuses
    statuses = fetch_open_order_statuses(host="127.0.0.1", port=7497, client_id=14)
    
    print(f"Found {len(statuses)} IB order statuses")
    print("-" * 80)
    
    # Look for order ID 136 specifically
    order_136_found = False
    for status in statuses:
        ib_order_id = status.get('ib_order_id', 0)
        if ib_order_id == 136:
            order_136_found = True
            print(f"Order 136 found in IB:")
            print(f"  Status: {status.get('status', 'N/A')}")
            print(f"  Avg Fill Price: {status.get('avg_fill_price', 'N/A')}")
            print(f"  Filled Qty: {status.get('filled_qty', 'N/A')}")
            print(f"  Last Update: {status.get('last_update', 'N/A')}")
            print(f"  Order Ref: {status.get('order_ref', 'N/A')}")
            break
    
    if not order_136_found:
        print("Order 136 NOT found in current IB statuses")
        print("\nAll current IB orders:")
        for status in statuses:
            print(f"  IB Order ID: {status.get('ib_order_id', 'N/A')}, Status: {status.get('status', 'N/A')}, Ref: {status.get('order_ref', 'N/A')[:50]}")
            
except Exception as e:
    print(f"Error fetching IB statuses: {e}")
    print("Make sure IB Gateway is running on port 7497")
