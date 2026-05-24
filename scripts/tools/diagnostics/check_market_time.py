from datetime import datetime, timezone

# Current time in UTC
now_utc = datetime.now(timezone.utc)
print(f'Current UTC time: {now_utc.strftime("%Y-%m-%d %H:%M:%S")}')
print(f'Current UTC hour: {now_utc.hour}')

# Market hours (NYSE in UTC): 14:30-21:00 UTC
market_open = now_utc.replace(hour=14, minute=30, second=0, microsecond=0)
market_close = now_utc.replace(hour=21, minute=0, second=0, microsecond=0)
print(f'Market open UTC: {market_open.strftime("%H:%M")}')
print(f'Market close UTC: {market_close.strftime("%H:%M")}')

# Check if in market hours
in_market = market_open <= now_utc <= market_close
print(f'In market hours: {in_market}')

# Check weekday
print(f'Weekday: {now_utc.strftime("%A")} ({now_utc.weekday()})')
print(f'Is weekday: {now_utc.weekday() < 5}')

# Final check
def _is_market_hours():
    if now_utc.weekday() >= 5:
        return False
    return market_open <= now_utc <= market_close

print(f'Final _is_market_hours(): {_is_market_hours()}')

# Check ALLOW_EXTENDED_HOURS setting
print(f'ALLOW_EXTENDED_HOURS is True, so orders will be SUBMITTED regardless of market hours')
