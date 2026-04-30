"""
Основной файл торгового робота с Excel
"""

import time
import logging
from datetime import datetime, timedelta

# Импортируем модули
from data_loader import fetch_price_data
from indicators import calculate_indicators, save_indicators
from forecast_engine import generate_forecast, save_forecast, log_error
from actuals_evaluator import fetch_actual_data, evaluate_forecast
from unified_logs_manager import get_forecasts_to_evaluate, update_forecast_with_actuals

def process_ticker(excel_manager, ticker):
    """Полностью обрабатывает один тикер."""
    try:
        logging.info(f"🚀 Начало обработки {ticker}")

        # Загружаем исторические данные
        price_data = fetch_price_data(ticker, days=250, excel_manager=excel_manager)
        if not price_data:
            raise ValueError("Не удалось загрузить данные о ценах")

        excel_manager.save_price_data(price_data, ticker=ticker)

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

        save_indicators(excel_manager, indicators)

        # Генерируем прогнозы для всех активных моделей × методов
        from multi_model_forecaster import generate_multi_model_forecasts
        raw_forecasts = generate_multi_model_forecasts(excel_manager, ticker, indicators, methods)

        logging.info(f"✅ Сгенерировано {len(raw_forecasts)} прогнозов для {ticker}")

        # Считаем консенсус
        if raw_forecasts:
            from consensus import calculate_consensus, save_consensus
            from unified_logs_manager import get_forecast_statistics
            stats = get_forecast_statistics(excel_manager, days_back=30)
            accuracy = stats.get("accuracy", {})
            method_stats = {
                m: {"win_rate": accuracy.get(m, 50.0) / 100.0}
                for m in stats.get("methods", {})
            }
            cons = calculate_consensus(raw_forecasts, method_stats)
            save_consensus(excel_manager, ticker, cons)
            logging.info(f"📊 Консенсус: {cons['signal']} {cons['confidence']:.1f}%")

        logging.info(f"✅ Завершена обработка {ticker}")
        
    except Exception as e:
        logging.error(f"❌ Критическая ошибка обработки {ticker}: {e}")
        log_error(excel_manager, ticker, 'GENERAL', str(e))

def evaluate_past_forecasts(excel_manager):
    """Оценивает предыдущие прогнозы и добавляет фактические данные"""
    try:
        logging.info("📊 Начало оценки предыдущих прогнозов")
        
        # Получаем прогнозы для оценки
        forecasts_to_evaluate = get_forecasts_to_evaluate(excel_manager)
        
        if not forecasts_to_evaluate:
            logging.info("ℹ️ Нет прогнозов для оценки")
            return
        
        evaluated_count = 0
        for forecast in forecasts_to_evaluate:
            try:
                ticker = forecast['ticker']
                forecast_date = forecast['forecast_date']
                log_id = forecast['id']
                
                logging.info(f"📊 Оценка прогноза {log_id} для {ticker} на {forecast_date}")
                
                # Загружаем фактические данные
                actual_data = fetch_actual_data(ticker, forecast_date, excel_manager)
                if not actual_data:
                    logging.warning(f"⚠️ Не удалось загрузить фактические данные для {ticker} на {forecast_date}")
                    continue
                
                # Оцениваем прогноз
                evaluation = evaluate_forecast(forecast, actual_data)
                if not evaluation:
                    logging.warning(f"⚠️ Не удалось оценить прогноз {log_id}")
                    continue
                
                # Обновляем запись с фактическими данными
                success = update_forecast_with_actuals(excel_manager, log_id, {**actual_data, **evaluation})
                if success:
                    evaluated_count += 1
                    logging.info(f"✅ Обновлен прогноз {log_id}")
                else:
                    logging.error(f"❌ Не удалось обновить прогноз {log_id}")
                
                # Задержка между оценками
                time.sleep(1)
                
            except Exception as e:
                logging.error(f"❌ Ошибка оценки прогноза: {e}")
                continue
        
        logging.info(f"✅ Оценка завершена. Обработано {evaluated_count}/{len(forecasts_to_evaluate)} прогнозов")
        
    except Exception as e:
        logging.error(f"❌ Критическая ошибка оценки прогнозов: {e}")
        raise

def run_trading_bot(db_file: str = None):
    """Основная функция запуска торгового робота"""
    try:
        logging.info("🚀 Запуск торгового робота")

        from sqlite_manager import SQLiteManager
        import os
        if not db_file:
            db_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                'trading_robot.db'
            )
        excel_manager = SQLiteManager(db_file)

        # Читаем настройки
        active_tickers = excel_manager.get_settings()
        
        if not active_tickers:
            logging.warning("⚠️ Нет активных тикеров для обработки")
            return
        
        logging.info(f"📊 Активные тикеры: {', '.join(active_tickers)}")
        
        # Обрабатываем каждый тикер
        for ticker in active_tickers:
            process_ticker(excel_manager, ticker)
        
        logging.info("✅ Работа торгового робота завершена")
        
    except Exception as e:
        logging.error(f"❌ Критическая ошибка: {e}")
        raise

def test_single_ticker(ticker='NASDAQ:NVDA', db_file: str = None):
    """Тестирование робота на одном тикере"""
    try:
        logging.info(f"🧪 Тестирование на тикере {ticker}")

        from sqlite_manager import SQLiteManager
        import os
        if not db_file:
            db_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                'trading_robot.db'
            )
        excel_manager = SQLiteManager(db_file)
        process_ticker(excel_manager, ticker)
        
        logging.info(f"✅ Тест для {ticker} завершен")
        
    except Exception as e:
        logging.error(f"❌ Ошибка теста: {e}")

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
        excel_manager = SQLiteManager(db_file)

        sheets_to_clear = ['PriceData', 'Indicators', 'Logs']
        
        for sheet_name in sheets_to_clear:
            success = excel_manager.clear_sheet(sheet_name, keep_headers=True)
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
            excel_manager = SQLiteManager()
            evaluate_past_forecasts(excel_manager)
            
        elif command == '--forecast':
            logging.info("🤖 Генерация новых прогнозов")
            run_trading_bot()
            
        elif command == '--full':
            logging.info("🔄 Полный цикл: оценка + генерация")
            from sqlite_manager import SQLiteManager
            excel_manager = SQLiteManager()
            evaluate_past_forecasts(excel_manager)
            run_trading_bot()
            
        elif command == '--clear':
            clear_all_data()
            
        else:
            print("Неизвестная команда")
            print("Доступные команды:")
            print("  --init - инициализация Excel файла")
            print("  --test [ticker] - тестовый запуск")
            print("  --evaluate - оценка предыдущих прогнозов")
            print("  --forecast - генерация новых прогнозов")
            print("  --full - полный цикл (оценка + генерация)")
            print("  --clear - очистка данных")
    else:
        # Обычный запуск
        run_trading_bot()
