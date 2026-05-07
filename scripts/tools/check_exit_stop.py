import sqlite3
conn = sqlite3.connect('trading_robot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("Sample exit_stop values:")
cur.execute("""
    SELECT ticker, method, model, side, exit_stop, stop_loss
    FROM logs
    WHERE side IN ('LONG', 'SHORT')
    LIMIT 20
""")

for row in cur.fetchall():
    print(f"{row['ticker']} | {row['method'][:15]} | {row['model'][:15]} | {row['side']}")
    print(f"  exit_stop: '{row['exit_stop']}'")
    print(f"  stop_loss: {row['stop_loss']}")
    
    # Try to parse
    if row['exit_stop']:
        import re
        nums = re.findall(r'[\d.]+', str(row['exit_stop']))
        print(f"  parsed nums: {nums}")
        if nums:
            try:
                val = float(nums[-1])
                print(f"  -> {val}")
            except:
                print(f"  -> parse failed")
    print()

conn.close()
