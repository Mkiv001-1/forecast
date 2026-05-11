"""
Основной файл торгового робота
"""

import time
import logging
from datetime import datetime, timedelta

from scripts.core.config import CONFIDENCE_THRESHOLD

# Импортируем модули
from data_loader import fetch_price_data
from indicators import calculate_indicators, save_indicators
from forecast_engine import generate_forecast, save_forecast, log_error
from actuals_evaluator import fetch_actual_data, evaluate_forecast
from unified_logs_manager import get_forecasts_to_evaluate, update_forecast_with_actuals

def process_ticker(db_manager, ticker, run_id=None):
    """Полностью обрабатывает один тикер. Возвращает (log_ids, has_non_neutral_consensus)."""
    log_ids = []
    has_consensus = False
    try:
        logging.info(f"🚀 Начало обработки {ticker}")

        # Загружаем исторические данные
        price_data = fetch_price_data(ticker, days=250, db_manager=db_manager)
        if not price_data:
            raise ValueError("Не удалось загрузить данные о ценах")

        db_manager.save_price_data(price_data, ticker=ticker)

        # Проверяем сталость данных перед расчётом индикаторов
        is_stale, last_date, hours_diff = db_manager.check_price_data_staleness(ticker)
        if is_stale and last_date:
            logging.warning(f"⚠️ Price data for {ticker} is stale (last update: {last_date}, {hours_diff}h ago), skipping forecast")
            raise ValueError(f"Stale price data for {ticker}: last update {last_date}")
        elif is_stale:
            logging.warning(f"⚠️ No price data found for {ticker}, skipping forecast")
            raise ValueError(f"No price data for {ticker}")

        # Рассчитываем индикаторы (с новыми EMA, MACD, ADX, OBV)
        indicators = calculate_indicators(ticker, price_data)
        if not indicators:
            raise ValueError("Не удалось рассчитать индикаторы")

        # Определяем рыночный режим
        from market_regime import detect_regime, get_methods_for_regime
        regime = detect_regime(indicators)
        indicators['market_regime'] = regime
        methods = get_methods_for_regime(regime)
        logging.info(f"📈 Режим: {regime} → методы: {methods}")

        save_indicators(db_manager, indicators)

        # Генерируем прогнозы для всех активных моделей × методов
        from multi_model_forecaster import generate_multi_model_forecasts
        raw_forecasts, forecast_log_ids = generate_multi_model_forecasts(db_manager, ticker, indicators, methods, run_id=run_id)
        log_ids = forecast_log_ids

        logging.info(f"✅ Сгенерировано {len(raw_forecasts)} прогнозов для {ticker}")

        # Считаем консенсус
        if raw_forecasts:
            from consensus import calculate_consensus, save_consensus
            from unified_logs_manager import get_forecast_statistics
            stats = get_forecast_statistics(db_manager, days_back=30)
            accuracy = stats.get("accuracy", {})
            method_stats = {
                m: {"win_rate": accuracy.get(m, 50.0) / 100.0}
                for m in stats.get("methods", {})
            }
            # Enrich method_stats with timeframe_hours from method_config
            method_timeframes = db_manager.get_method_config_timeframes()
            for m, hours in method_timeframes.items():
                if m not in method_stats:
                    method_stats[m] = {}
                method_stats[m]["timeframe_hours"] = hours
            
            # Build model_stats keyed by AI model name (providers.name) for ema_accuracy lookup
            model_stats = db_manager.get_providers_ema_accuracy()
            current_price = price_data[-1]['close'] if price_data else 0.0
            cons = calculate_consensus(raw_forecasts, method_stats, current_price=current_price, run_id=run_id, log_ids=log_ids, model_stats=model_stats)
            save_consensus(db_manager, ticker, cons, method_stats=method_stats, run_id=run_id)
            has_consensus = cons['signal'] in ('LONG', 'SHORT')
            logging.info(f"📊 Консенсус: {cons['signal']} {cons['confidence']:.1f}%")

            # Immediate activation path (optional, uses shared entrypoint)
            # Normal flow: scheduler job process_pending_consensus_orders handles activation.
            auto_order = db_manager.get_config_value("AUTO_ORDER_SUBMISSION", "false").lower() == "true"
            if auto_order and has_consensus and cons['confidence'] >= CONFIDENCE_THRESHOLD:
                try:
                    from order_manager import activate_consensus_order
                    # Retrieve the id of the consensus record we just saved
                    consensus_id = db_manager.get_last_consensus_id(ticker)
                    if consensus_id:
                        result = activate_consensus_order(consensus_id, db_manager)
                        logging.info(f"📤 Активация ордера: {result['status']} - {result.get('message', '')}")
                except Exception as _e:
                    logging.warning(f"⚠️ Ошибка немедленной активации ордера: {_e}")


        logging.info(f"✅ Завершена обработка {ticker}")
        return log_ids, has_consensus
        
    except Exception as e:
        logging.error(f"❌ Критическая ошибка обработки {ticker}: {e}")
        log_error(db_manager, ticker, 'GENERAL', str(e))
        return log_ids, False

def evaluate_past_forecasts(db_manager):
    """Оценивает предыдущие прогнозы через consensus_evaluator (основной путь)."""
    try:
        logging.info("📊 Начало оценки предыдущих прогнозов")
        from consensus_evaluator import evaluate_consensus_records
        count = evaluate_consensus_records(db_manager)
        logging.info(f"✅ Оценка завершена. Обработано {count} consensus записей")
    except Exception as e:
        logging.error(f"❌ Критическая ошибка оценки прогнозов: {e}")
        raise

def run_trading_bot(db_file: str = None, run_id: int = None, db_manager=None):
    """Основная функция запуска торгового робота.
    
    Args:
        db_file: Путь к файлу БД (не используется если передан db_manager)
        run_id: ID запуска для трекинга (если None - создаётся автоматически)
        db_manager: Готовый экземпляр SQLiteManager (оптимально для scheduler)
    
    Returns:
        int: run_id или None при ошибке
    """
    try:
        logging.info("🚀 Запуск торгового робота")

        from sqlite_manager import SQLiteManager
        import os
        
        if db_manager is None:
            if not db_file:
                db_file = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'trading_robot.db'
                )
            db_manager = SQLiteManager(db_file)

        # Читаем настройки (один вызов — используется и для tickers_planned, и для итерации)
        active_tickers = db_manager.get_settings()

        # Создаём запись о запуске если run_id не передан
        if run_id is None:
            run_id = db_manager.create_forecast_run('scheduler', len(active_tickers))
            if run_id:
                logging.info(f"📋 Создан forecast run #{run_id}")
        
        if not active_tickers:
            logging.warning("⚠️ Нет активных тикеров для обработки")
            if run_id:
                db_manager.complete_forecast_run(run_id, status='completed', tickers_processed=0, consensus_count=0)
            return run_id
        
        logging.info(f"📊 Активные тикеры: {', '.join(active_tickers)}")
        
        # Обрабатываем каждый тикер
        processed = 0
        consensus_count = 0
        for ticker in active_tickers:
            try:
                _log_ids, _has_consensus = process_ticker(db_manager, ticker, run_id=run_id)
                processed += 1
                if _has_consensus:
                    consensus_count += 1
            except Exception as e:
                logging.error(f"❌ Ошибка обработки {ticker}: {e}")
        
        # Завершаем запись о запуске
        if run_id:
            db_manager.complete_forecast_run(run_id, status='completed', 
                                            tickers_processed=processed, 
                                            consensus_count=consensus_count)
            logging.info(f"✅ Forecast run #{run_id} завершён: {processed} тикеров, {consensus_count} консенсусов")
        else:
            logging.info("✅ Работа торгового робота завершена")
        
        return run_id
        
    except Exception as e:
        logging.error(f"❌ Критическая ошибка: {e}")
        # При ошибке отмечаем run как failed
        if run_id and db_manager:
            try:
                db_manager.complete_forecast_run(run_id, status='failed', error_message=str(e))
            except:
                pass
        raise

def test_single_ticker(ticker='NASDAQ:NVDA', db_file: str = None):
    """Тестирование робота на одном тикере с созданием run"""
    try:
        logging.info(f"🧪 Тестирование на тикере {ticker}")

        from sqlite_manager import SQLiteManager
        import os
        if not db_file:
            db_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                'trading_robot.db'
            )
        db_manager = SQLiteManager(db_file)
        
        # Создаём run для теста
        run_id = db_manager.create_forecast_run('manual', 1)
        _, has_consensus = process_ticker(db_manager, ticker, run_id=run_id)
        consensus_count = 1 if has_consensus else 0
        db_manager.complete_forecast_run(run_id, status='completed', tickers_processed=1, consensus_count=consensus_count)
        
        logging.info(f"✅ Тест для {ticker} завершен, run #{run_id}")
        return run_id
        
    except Exception as e:
        logging.error(f"❌ Ошибка теста: {e}")
        return None

def clear_all_data(db_file: str = None):
    """Очищает все данные (кроме настроек и конфига)"""
    try:
        logging.info("🧹 Очистка всех данных...")

        from sqlite_manager import SQLiteManager
        import os
        if not db_file:
            db_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                'trading_robot.db'
            )
        db_manager = SQLiteManager(db_file)

        sheets_to_clear = ['PriceData', 'Indicators', 'Logs']
        
        for sheet_name in sheets_to_clear:
            success = db_manager.clear_sheet(sheet_name, keep_headers=True)
            if success:
                logging.info(f"✅ Лист {sheet_name} очищен")
            else:
                logging.warning(f"⚠️ Лист {sheet_name} не найден")
        
        logging.info("✅ Очистка завершена")
        
    except Exception as e:
        logging.error(f"❌ Ошибка очистки: {e}")

if __name__ == "__main__":
    import sys
    
    # Обработка командной строки
    if len(sys.argv) > 1:
        command = sys.argv[1]
        
        if command == '--init':
            logging.info("🔧 Инициализация базы данных...")
            from sqlite_manager import SQLiteManager
            SQLiteManager()

        elif command == '--test':
            ticker = sys.argv[2] if len(sys.argv) > 2 else 'NASDAQ:NVDA'
            test_single_ticker(ticker)
            
        elif command == '--evaluate':
            logging.info("📊 Оценка предыдущих прогнозов")
            from sqlite_manager import SQLiteManager
            db_manager = SQLiteManager()
            evaluate_past_forecasts(db_manager)
            
        elif command == '--forecast':
            logging.info("🤖 Генерация новых прогнозов")
            run_trading_bot()
            
        elif command == '--full':
            logging.info("🔄 Полный цикл: оценка + генерация")
            from sqlite_manager import SQLiteManager
            db_manager = SQLiteManager()
            evaluate_past_forecasts(db_manager)
            run_trading_bot()
            
        elif command == '--clear':
            clear_all_data()
            
        else:
            print("Неизвестная команда")
            print("Доступные команды:")
            print("  --init - инициализация базы данных")
            print("  --test [ticker] - тестовый запуск")
            print("  --evaluate - оценка предыдущих прогнозов")
            print("  --forecast - генерация новых прогнозов")
            print("  --full - полный цикл (оценка + генерация)")
            print("  --clear - очистка данных")
    else:
        # Обычный запуск
        run_trading_bot()
