"""
Integration tests for API config endpoints.

Run with: python -m pytest test_integration_api.py -v
"""

import os
import tempfile

from fastapi.testclient import TestClient


def _write_server_ini(ini_path: str, db_file: str, api_key: str) -> None:
    with open(ini_path, "w", encoding="utf-8") as f:
        f.write("[server]\n")
        f.write("host = 127.0.0.1\n")
        f.write("port = 8000\n")
        f.write("[data]\n")
        f.write(f"db_file = {db_file}\n")
        f.write("excel_file = trading_robot.xlsx\n")
        f.write("[security]\n")
        f.write(f"api_key = {api_key}\n")


def test_api_config_scheduler_max_workers_roundtrip(monkeypatch):
    from scripts.server.config import ServerConfig
    import scripts.server.api as api_mod

    async def _dummy_start_scheduler(_db):
        return None

    async def _dummy_stop_scheduler():
        return None

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_file = os.path.join(tmpdir, "api_test.db")
        ini_file = os.path.join(tmpdir, "server_config.ini")
        api_key = "test-api-key"
        _write_server_ini(ini_file, db_file, api_key)

        monkeypatch.setattr(ServerConfig, "CONFIG_FILE", ini_file)
        monkeypatch.setattr("scripts.core.scheduler.start_scheduler", _dummy_start_scheduler)
        monkeypatch.setattr("scripts.core.scheduler.stop_scheduler", _dummy_stop_scheduler)

        headers = {"X-API-Key": api_key}
        with TestClient(api_mod.app) as client:
            # 1) default key should be present in /config (seeded by SQLiteManager)
            r = client.get("/config", headers=headers)
            assert r.status_code == 200
            items = r.json().get("items", [])
            cfg = {item.get("key"): item.get("value") for item in items}
            assert cfg.get("SCHEDULER_MAX_WORKERS") == "4"

            # 2) update config through API
            r2 = client.put(
                "/config/SCHEDULER_MAX_WORKERS",
                headers=headers,
                json={"key": "SCHEDULER_MAX_WORKERS", "value": "7"},
            )
            assert r2.status_code == 200
            assert r2.json().get("value") == "7"

            # 3) read back and verify persistence
            r3 = client.get("/config", headers=headers)
            assert r3.status_code == 200
            items2 = r3.json().get("items", [])
            cfg2 = {item.get("key"): item.get("value") for item in items2}
            assert cfg2.get("SCHEDULER_MAX_WORKERS") == "7"
