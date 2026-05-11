"""
Integration tests for API config endpoints.

Run with: python -m pytest scripts/tests/test_integration_api.py -v
"""

import os
import sqlite3
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


def test_api_ib_position_status_endpoint(monkeypatch):
    from scripts.server.config import ServerConfig
    import scripts.server.api as api_mod

    async def _dummy_start_scheduler(_db):
        return None

    async def _dummy_stop_scheduler():
        return None

    def _fake_position_status(con_id: int, host: str, port: int, client_id: int):
        assert con_id == 76792991
        assert host == "127.0.0.1"
        assert port == 7497
        assert client_id == 1
        return {
            "found": True,
            "con_id": con_id,
            "status": "OPEN",
            "position": {
                "con_id": con_id,
                "symbol": "TSLA",
                "account": "DU7093209",
                "quantity": 20.0,
            },
        }

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_file = os.path.join(tmpdir, "api_test.db")
        ini_file = os.path.join(tmpdir, "server_config.ini")
        api_key = "test-api-key"
        _write_server_ini(ini_file, db_file, api_key)

        monkeypatch.setattr(ServerConfig, "CONFIG_FILE", ini_file)
        monkeypatch.setattr("scripts.core.scheduler.start_scheduler", _dummy_start_scheduler)
        monkeypatch.setattr("scripts.core.scheduler.stop_scheduler", _dummy_stop_scheduler)
        monkeypatch.setattr(
            "scripts.core.ib_gateway_client.fetch_ib_position_status_by_con_id",
            _fake_position_status,
        )

        headers = {"X-API-Key": api_key}
        with TestClient(api_mod.app) as client:
            r = client.get("/ib/positions/76792991/status", headers=headers)
            assert r.status_code == 200
            payload = r.json()
            assert payload["found"] is True
            assert payload["con_id"] == 76792991
            assert payload["status"] == "OPEN"
            assert payload["position"]["symbol"] == "TSLA"


def test_api_ib_order_status_endpoint(monkeypatch):
    from scripts.server.config import ServerConfig
    import scripts.server.api as api_mod

    async def _dummy_start_scheduler(_db):
        return None

    async def _dummy_stop_scheduler():
        return None

    def _fake_order_status(order_id: int, host: str, port: int, client_id: int):
        assert order_id == 670162540
        assert host == "127.0.0.1"
        assert port == 7497
        assert client_id == 14
        return {
            "found": True,
            "ib_order_id": order_id,
            "status": "Filled",
            "source": "executions",
            "order": {
                "ib_order_id": order_id,
                "symbol": "TSLA",
                "filled_qty": 20.0,
            },
        }

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_file = os.path.join(tmpdir, "api_test.db")
        ini_file = os.path.join(tmpdir, "server_config.ini")
        api_key = "test-api-key"
        _write_server_ini(ini_file, db_file, api_key)

        monkeypatch.setattr(ServerConfig, "CONFIG_FILE", ini_file)
        monkeypatch.setattr("scripts.core.scheduler.start_scheduler", _dummy_start_scheduler)
        monkeypatch.setattr("scripts.core.scheduler.stop_scheduler", _dummy_stop_scheduler)
        monkeypatch.setattr(
            "scripts.core.ib_gateway_client.fetch_ib_order_status_by_order_id",
            _fake_order_status,
        )

        headers = {"X-API-Key": api_key}
        with TestClient(api_mod.app) as client:
            r = client.get("/ib/orders/670162540/status", headers=headers)
            assert r.status_code == 200
            payload = r.json()
            assert payload["found"] is True
            assert payload["ib_order_id"] == 670162540
            assert payload["status"] == "Filled"
            assert payload["order"]["symbol"] == "TSLA"


def test_api_ib_position_status_invalid_id(monkeypatch):
    from scripts.server.config import ServerConfig
    import scripts.server.api as api_mod

    async def _dummy_start_scheduler(_db):
        return None

    async def _dummy_stop_scheduler():
        return None

    def _raise_invalid(*args, **kwargs):
        raise ValueError("con_id must be > 0")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_file = os.path.join(tmpdir, "api_test.db")
        ini_file = os.path.join(tmpdir, "server_config.ini")
        api_key = "test-api-key"
        _write_server_ini(ini_file, db_file, api_key)

        monkeypatch.setattr(ServerConfig, "CONFIG_FILE", ini_file)
        monkeypatch.setattr("scripts.core.scheduler.start_scheduler", _dummy_start_scheduler)
        monkeypatch.setattr("scripts.core.scheduler.stop_scheduler", _dummy_stop_scheduler)
        monkeypatch.setattr(
            "scripts.core.ib_gateway_client.fetch_ib_position_status_by_con_id",
            _raise_invalid,
        )

        headers = {"X-API-Key": api_key}
        with TestClient(api_mod.app) as client:
            r = client.get("/ib/positions/-1/status", headers=headers)
            assert r.status_code == 400
            assert "con_id must be > 0" in str(r.json().get("detail", ""))


def test_api_ib_order_status_invalid_id(monkeypatch):
    from scripts.server.config import ServerConfig
    import scripts.server.api as api_mod

    async def _dummy_start_scheduler(_db):
        return None

    async def _dummy_stop_scheduler():
        return None

    def _raise_invalid(*args, **kwargs):
        raise ValueError("order_id must be > 0")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_file = os.path.join(tmpdir, "api_test.db")
        ini_file = os.path.join(tmpdir, "server_config.ini")
        api_key = "test-api-key"
        _write_server_ini(ini_file, db_file, api_key)

        monkeypatch.setattr(ServerConfig, "CONFIG_FILE", ini_file)
        monkeypatch.setattr("scripts.core.scheduler.start_scheduler", _dummy_start_scheduler)
        monkeypatch.setattr("scripts.core.scheduler.stop_scheduler", _dummy_stop_scheduler)
        monkeypatch.setattr(
            "scripts.core.ib_gateway_client.fetch_ib_order_status_by_order_id",
            _raise_invalid,
        )

        headers = {"X-API-Key": api_key}
        with TestClient(api_mod.app) as client:
            r = client.get("/ib/orders/0/status", headers=headers)
            assert r.status_code == 400
            assert "order_id must be > 0" in str(r.json().get("detail", ""))


def test_api_portfolio_sync_persists_last_sync(monkeypatch):
    from scripts.server.config import ServerConfig
    import scripts.server.api as api_mod

    async def _dummy_start_scheduler(_db):
        return None

    async def _dummy_stop_scheduler():
        return None

    async def _fake_sync_portfolio_with_ib_async(db_manager, host, port, client_id, type):
        return True

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_file = os.path.join(tmpdir, "api_test.db")
        ini_file = os.path.join(tmpdir, "server_config.ini")
        api_key = "test-api-key"
        _write_server_ini(ini_file, db_file, api_key)

        monkeypatch.setattr(ServerConfig, "CONFIG_FILE", ini_file)
        monkeypatch.setattr("scripts.core.scheduler.start_scheduler", _dummy_start_scheduler)
        monkeypatch.setattr("scripts.core.scheduler.stop_scheduler", _dummy_stop_scheduler)
        monkeypatch.setattr(
            "scripts.core.ib_gateway_client.sync_portfolio_with_ib_async",
            _fake_sync_portfolio_with_ib_async,
        )

        headers = {"X-API-Key": api_key}
        with TestClient(api_mod.app) as client:
            r = client.post("/portfolio/sync", headers=headers)
            assert r.status_code == 200
            payload = r.json()
            assert payload.get("synced") is True
            assert bool(payload.get("synced_at"))

            cfg = client.get("/config", headers=headers)
            assert cfg.status_code == 200
            cfg_map = {item.get("key"): item.get("value") for item in cfg.json().get("items", [])}
            assert bool(cfg_map.get("LAST_PORTFOLIO_SYNC_AT"))


def test_api_orders_sync_persists_last_sync(monkeypatch):
    from scripts.server.config import ServerConfig
    import scripts.server.api as api_mod

    async def _dummy_start_scheduler(_db):
        return None

    async def _dummy_stop_scheduler():
        return None

    def _fake_sync_orders_with_ib(db_manager, host, port, client_id, source="manual"):
        assert source == "manual"
        return {
            "ok": True,
            "scanned": 1,
            "updated_orders": 1,
            "updated_trades": 0,
            "errors": [],
            "synced_at": "2026-05-11T17:11:24.230000+00:00",
        }

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_file = os.path.join(tmpdir, "api_test.db")
        ini_file = os.path.join(tmpdir, "server_config.ini")
        api_key = "test-api-key"
        _write_server_ini(ini_file, db_file, api_key)

        monkeypatch.setattr(ServerConfig, "CONFIG_FILE", ini_file)
        monkeypatch.setattr("scripts.core.scheduler.start_scheduler", _dummy_start_scheduler)
        monkeypatch.setattr("scripts.core.scheduler.stop_scheduler", _dummy_stop_scheduler)
        monkeypatch.setattr(
            "scripts.core.order_status_sync.sync_orders_with_ib",
            _fake_sync_orders_with_ib,
        )

        headers = {"X-API-Key": api_key}
        with TestClient(api_mod.app) as client:
            r = client.post("/orders/sync", headers=headers)
            assert r.status_code == 200
            payload = r.json()
            assert payload.get("ok") is True
            assert payload.get("synced_at") == "2026-05-11T17:11:24.230000+00:00"

            cfg = client.get("/config", headers=headers)
            assert cfg.status_code == 200
            cfg_map = {item.get("key"): item.get("value") for item in cfg.json().get("items", [])}
            assert cfg_map.get("LAST_ORDERS_SYNC_AT") == "2026-05-11T17:11:24.230000+00:00"


def test_api_ib_transactions_endpoint(monkeypatch):
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
            with sqlite3.connect(db_file) as con:
                con.execute(
                    """
                    INSERT INTO ib_order_transactions(
                        occurred_at, event_source, event_type, operation_status,
                        ticker, ib_order_id, ib_parent_id, order_id, trade_id,
                        request_payload_json, response_payload_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "2026-05-11T18:00:00+00:00",
                        "submit_manual",
                        "ORDER_SUBMIT_RESPONSE",
                        "SUCCESS",
                        "NASDAQ:NVDA",
                        12345,
                        12345,
                        None,
                        None,
                        "{}",
                        "{}",
                    ),
                )

            r = client.get(
                "/ib-transactions",
                headers=headers,
                params={"ticker": "NASDAQ:NVDA", "event_source": "submit_manual", "limit": 20},
            )
            assert r.status_code == 200
            payload = r.json()
            assert payload.get("total") == 1
            items = payload.get("items", [])
            assert len(items) == 1
            assert items[0].get("event_type") == "ORDER_SUBMIT_RESPONSE"
            assert items[0].get("event_source") == "submit_manual"


def test_api_cancel_order_does_not_mark_cancelled_before_sync(monkeypatch):
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
        monkeypatch.setattr("scripts.core.ib_gateway_client.cancel_order", lambda order_id, port=7497: True)

        headers = {"X-API-Key": api_key}
        with TestClient(api_mod.app) as client:
            with sqlite3.connect(db_file) as con:
                con.execute(
                    """
                    INSERT INTO orders(
                        ticker, ib_order_id, ib_parent_id, order_role, order_type,
                        action, quantity, status, account_type, created_at, submitted_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "NASDAQ:NVDA",
                        670162540,
                        670162540,
                        "ENTRY",
                        "LMT",
                        "BUY",
                        9,
                        "SUBMITTED",
                        "paper",
                        "2026-05-11T18:00:00+00:00",
                        "2026-05-11T18:00:01+00:00",
                    ),
                )
                order_id = con.execute("SELECT id FROM orders WHERE ib_order_id=670162540").fetchone()[0]

            r = client.post(f"/orders/{order_id}/cancel", headers=headers)
            assert r.status_code == 200
            payload = r.json()
            assert payload.get("cancelled") is True
            assert payload.get("ib_order_id") == 670162540

            with sqlite3.connect(db_file) as con:
                status = con.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()[0]

            assert status == "SUBMITTED"


def test_api_orders_trades_expose_trade_uid_and_perm_id(monkeypatch):
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
            with sqlite3.connect(db_file) as con:
                con.execute(
                    """
                    INSERT INTO orders(
                        ticker, trade_uid, ib_order_id, ib_perm_id, ib_parent_id, order_role, order_type,
                        action, quantity, status, account_type, created_at, submitted_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "NASDAQ:NVDA",
                        "tid-api-001",
                        670162540,
                        990001,
                        670162540,
                        "ENTRY",
                        "LMT",
                        "BUY",
                        9,
                        "SUBMITTED",
                        "paper",
                        "2026-05-11T18:00:00+00:00",
                        "2026-05-11T18:00:01+00:00",
                    ),
                )
                con.execute(
                    """
                    INSERT INTO trades(
                        ticker, trade_uid, ib_parent_id, signal, quantity, entry_price, stop_loss, target_price,
                        status, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "NASDAQ:NVDA",
                        "tid-api-001",
                        670162540,
                        "LONG",
                        9,
                        100.0,
                        95.0,
                        110.0,
                        "OPEN",
                        "2026-05-11T18:00:00+00:00",
                        "2026-05-11T18:00:01+00:00",
                    ),
                )

            r_orders = client.get("/orders", headers=headers, params={"ticker": "NASDAQ:NVDA", "limit": 20})
            assert r_orders.status_code == 200
            orders = r_orders.json().get("items", [])
            assert len(orders) == 1
            assert orders[0].get("trade_uid") == "tid-api-001"
            assert orders[0].get("ib_perm_id") == 990001

            r_trades = client.get("/trades", headers=headers, params={"ticker": "NASDAQ:NVDA", "limit": 20})
            assert r_trades.status_code == 200
            payload = r_trades.json()
            trades = payload.get("items", [])
            assert len(trades) == 1
            assert payload.get("trades", []) == trades
            assert trades[0].get("trade_uid") == "tid-api-001"


def test_api_ib_transactions_exposes_trade_uid_and_perm_id(monkeypatch):
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
            with sqlite3.connect(db_file) as con:
                con.execute(
                    """
                    INSERT INTO ib_order_transactions(
                        occurred_at, event_source, event_type, operation_status,
                        ticker, trade_uid, ib_order_id, ib_perm_id, ib_parent_id,
                        order_id, trade_id, request_payload_json, response_payload_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "2026-05-11T18:00:00+00:00",
                        "submit_manual",
                        "ORDER_SUBMIT_RESPONSE",
                        "SUCCESS",
                        "NASDAQ:NVDA",
                        "tid-api-001",
                        12345,
                        67890,
                        12345,
                        None,
                        None,
                        "{}",
                        "{}",
                    ),
                )

            r = client.get("/ib-transactions", headers=headers, params={"ticker": "NASDAQ:NVDA", "limit": 20})
            assert r.status_code == 200
            items = r.json().get("items", [])
            assert len(items) == 1
            assert items[0].get("trade_uid") == "tid-api-001"
            assert items[0].get("ib_perm_id") == 67890


def test_api_trades_supports_trade_id_filter(monkeypatch):
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
            with sqlite3.connect(db_file) as con:
                con.execute(
                    """
                    INSERT INTO trades(
                        ticker, trade_uid, ib_parent_id, signal, quantity, entry_price,
                        stop_loss, target_price, status, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "NASDAQ:NVDA",
                        "tid-filter-001",
                        700001,
                        "LONG",
                        3,
                        100.0,
                        95.0,
                        110.0,
                        "OPEN",
                        "2026-05-11T18:00:00+00:00",
                        "2026-05-11T18:00:01+00:00",
                    ),
                )
                con.execute(
                    """
                    INSERT INTO trades(
                        ticker, trade_uid, ib_parent_id, signal, quantity, entry_price,
                        stop_loss, target_price, status, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "NASDAQ:AAPL",
                        "tid-filter-002",
                        700002,
                        "SHORT",
                        2,
                        200.0,
                        210.0,
                        185.0,
                        "OPEN",
                        "2026-05-11T18:10:00+00:00",
                        "2026-05-11T18:10:01+00:00",
                    ),
                )
                target_trade_id = con.execute(
                    "SELECT id FROM trades WHERE trade_uid='tid-filter-002'"
                ).fetchone()[0]

            r = client.get(
                "/trades",
                headers=headers,
                params={"trade_id": target_trade_id, "limit": 20},
            )
            assert r.status_code == 200
            payload = r.json()
            items = payload.get("items", [])
            assert payload.get("total") == 1
            assert len(items) == 1
            assert int(items[0].get("id")) == int(target_trade_id)
            assert items[0].get("trade_uid") == "tid-filter-002"
