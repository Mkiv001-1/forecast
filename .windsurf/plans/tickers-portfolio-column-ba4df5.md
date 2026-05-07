# Tickers Tab — Portfolio Column

Add a "Portfolio" column to the Tickers tab showing whether a position exists for each ticker.

## Changes Required

### UI (`scripts/client/gui_main.py`)
- **TickersTab._build_ui()**: Increase column count from 3 to 4, add "Portfolio" header
- **TickersTab.load()**: Fetch portfolio positions alongside tickers, store in `self._positions`
- **TickersTab._populate_table()**: Display "Yes"/"No" or checkmark in the Portfolio column based on whether ticker exists in positions

## Implementation Notes
- Use existing `PositionRecord` model (already imported)
- Fetch via `api.get_portfolio()` which returns `PortfolioResponse`
- Check if ticker exists in any position (sum quantity > 0 means "Yes")
- Make column non-editable, center-aligned
