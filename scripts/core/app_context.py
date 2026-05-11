"""
Application context — centralized dependency container.

Provides singleton access to:
- Database manager (SQLiteManager)
- Configuration
- External service clients

Usage:
    from scripts.core.app_context import get_context, init_context
    
    # Initialize once at startup
    init_context(db_file="trading_robot.db")
    
    # Get context anywhere
    ctx = get_context()
    db = ctx.db_manager
"""

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.core.sqlite_manager import SQLiteManager

logger = logging.getLogger(__name__)


class AppContext:
    """Container for application dependencies."""
    
    def __init__(self, db_file: Optional[str] = None):
        from scripts.core.sqlite_manager import SQLiteManager
        
        self._db_manager: SQLiteManager = SQLiteManager(db_file)
        self._initialized = True
        logger.info(f"AppContext initialized with DB: {self._db_manager.db_file}")
    
    @property
    def db_manager(self) -> "SQLiteManager":
        """Get the database manager."""
        return self._db_manager
    
    @property
    def db_file(self) -> str:
        """Get the database file path."""
        return self._db_manager.db_file


# Module-level singleton
_context: Optional[AppContext] = None


def init_context(db_file: Optional[str] = None) -> AppContext:
    """Initialize the application context singleton.
    
    Must be called once at application startup.
    
    Args:
        db_file: Path to SQLite database (optional, uses default if not provided)
        
    Returns:
        AppContext instance
    """
    global _context
    if _context is None:
        _context = AppContext(db_file)
    return _context


def get_context() -> AppContext:
    """Get the application context singleton.
    
    Raises:
        RuntimeError: if context not initialized
        
    Returns:
        AppContext instance
    """
    if _context is None:
        raise RuntimeError(
            "AppContext not initialized. Call init_context() first."
        )
    return _context


def reset_context() -> None:
    """Reset the context singleton (useful for testing)."""
    global _context
    _context = None
    logger.debug("AppContext reset")


def get_db_manager() -> "SQLiteManager":
    """Convenience function to get DB manager directly."""
    return get_context().db_manager
