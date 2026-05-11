"""Entry point for Forecast Trading Robot GUI Client."""

import os
import sys
import logging

# Suppress PyQt6 diagnostic messages (fonts, layout warnings)
os.environ["QT_DEBUG_PLUGINS"] = "0"

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

_CLIENT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_CLIENT_DIR)
_PROJECT_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _p in [_PROJECT_ROOT, _SCRIPTS_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main():
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("PyQt6 not installed. Run: pip install PyQt6")
        return 1

    from scripts.core.single_instance import SingleInstance
    guard = SingleInstance("client")

    from scripts.client.config import ClientConfig
    from scripts.client.gui_main import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Forecast Trading Robot")
    app.setOrganizationName("forecast")

    # Check for existing instance — show GUI dialog instead of stderr exit
    existing_pid = guard._read_pid()
    if existing_pid and guard._pid_alive(existing_pid):
        QMessageBox.warning(
            None,
            "Already Running",
            f"Forecast Trading Robot GUI is already running (PID {existing_pid}).\n\n"
            "Close the existing window before opening a new one.",
        )
        return 1

    guard.acquire()

    config = ClientConfig()
    logger.info(f"Connecting to server: {config.server_url}")

    window = MainWindow(config)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
