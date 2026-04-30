"""Entry point for Forecast Trading Robot Server."""

import os
import sys
import logging
import argparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_SERVER_DIR)
_PROJECT_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _p in [_PROJECT_ROOT, _SCRIPTS_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main():
    from scripts.core.single_instance import SingleInstance
    SingleInstance("server").acquire()

    parser = argparse.ArgumentParser(description="Forecast Trading Robot Server")
    parser.add_argument("--host", help="Host to bind (overrides config)")
    parser.add_argument("--port", type=int, help="Port to bind (overrides config)")
    parser.add_argument("--config", help="Path to server_config.ini")
    args = parser.parse_args()

    from scripts.server.config import ServerConfig
    cfg = ServerConfig(args.config) if args.config else ServerConfig()

    host = args.host or cfg.host
    port = args.port or cfg.port

    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed. Run: pip install uvicorn[standard]")
        return 1

    logger.info(f"Starting server on http://{host}:{port}")
    logger.info(f"Excel: {cfg.excel_file}")
    logger.info(f"API Key: {cfg.api_key}")

    uvicorn.run(
        "scripts.server.api:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
