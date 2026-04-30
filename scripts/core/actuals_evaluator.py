"""
Модуль оценки прогнозов и добавления фактических данных
"""

import logging
from datetime import datetime, timedelta
import pandas as pd
from data_loader import fetch_price_data

def fetch_actual_data(ticker, forecast_date, excel_manager=None):
    try:
        # Преобразуем forecast_date в datetime если это строка
        if isinstance(forecast_date, str):
            target_date = datetime.strptime(forecast_date, '%Y-%m-%d').date()
        elif hasattr(forecast_date, 'date'):
            target_date = forecast_date.date()
        else:
            target_date = forecast_date

        # Загружаем данные за период вокруг даты прогноза
        # Берем данные на 7 дней вперед от даты прогноза
        end_date = target_date + timedelta(days=7)
        days_needed = (end_date - target_date).days + 30  # +30 дней истории для индикаторов
        
        logging.info(f"📊 Загрузка фактических данных для {ticker} на {forecast_date}")
        
        # Используем кэширование если доступно
        price_data = fetch_price_data(ticker, days=days_needed, excel_manager=excel_manager)
        
        if not price_data:
            logging.error(f"❌ Не удалось загрузить данные для {ticker}")
            return None
        
        # Ищем данные за конкретную дату
        actual_record = None
        for record in price_data:
            # Унифицируем форматы дат для сравнения
            record_date = record['date'].strftime('%Y-%m-%d') if hasattr(record['date'], 'strftime') else str(record['date'])
            forecast_date_str = target_date.strftime('%Y-%m-%d') if hasattr(target_date, 'strftime') else str(target_date)
            
            if record_date == forecast_date_str:
                actual_record = record
                break
        
        if not actual_record:
            logging.warning(f"⚠️ Данные за {forecast_date} не найдены для {ticker}")
            # Берем ближайшую доступную дату
            if price_data:
                # Унифицируем даты для сравнения
                def date_distance(record):
                    record_date = record['date']
                    if hasattr(record_date, 'date'):
                        record_date = record_date.date()
                    elif isinstance(record_date, str):
                        record_date = datetime.strptime(record_date, '%Y-%m-%d').date()
                    return abs((record_date - target_date).days)
                
                actual_record = min(price_data, key=date_distance)
                logging.info(f"📅 Используем ближайшую дату: {actual_record['date']}")
            else:
                return None
        
        # Формируем результат
        actual_date = actual_record['date']
        if hasattr(actual_date, 'strftime'):
            actual_date_str = actual_date.strftime('%Y-%m-%d')
        else:
            actual_date_str = str(actual_date)
        
        actual_data = {
            'actual_date': actual_date_str,
            'actual_open': float(actual_record['open']),
            'actual_close': float(actual_record['close']),
            'actual_high': float(actual_record['high']),
            'actual_low': float(actual_record['low']),
            'actual_volume': int(actual_record['volume'])
        }
        
        logging.info(f"✅ Загружены фактические данные для {ticker}: close=${actual_data['actual_close']:.2f}")
        return actual_data
        
    except Exception as e:
        logging.error(f"❌ Ошибка загрузки фактических данных: {e}")
        return None

def evaluate_forecast(forecast_record, actual_data):
    """
    Оценивает прогноз против фактических данных
    
    Args:
        forecast_record: запись прогноза
        actual_data: фактические данные
    
    Returns:
        dict: результаты оценки
    """
    try:
        evaluation = {}
        
        # entry_price: use value saved in the forecast record; fallback to actual_close
        forecast_price = (
            float(forecast_record.get('entry_price') or 0)
            or actual_data.get('forecast_price')
            or actual_data['actual_close']
        )
        actual_close = actual_data['actual_close']
        actual_high  = actual_data['actual_high']
        actual_low   = actual_data['actual_low']
        
        # Парсим цели
        exit_target = forecast_record.get('exit_target', '')
        exit_stop = forecast_record.get('exit_stop', '')
        forecast_side = forecast_record.get('side', 'NEUTRAL')
        
        # Проверяем достижение целей
        target_hit, stop_hit, entry_triggered = check_targets(
            forecast_side, exit_target, exit_stop, actual_high, actual_low, forecast_price
        )
        
        # Determine if exit was successful (target hit before stop)
        exit_successful = None
        if forecast_side != 'NEUTRAL' and entry_triggered:
            if target_hit and not stop_hit:
                exit_successful = 1  # Success: target hit, no stop
            elif stop_hit and not target_hit:
                exit_successful = 0  # Failure: stop hit, no target
            elif target_hit and stop_hit:
                # Both hit - need to determine which came first (approximation: lower price movement = hit first)
                exit_successful = 1  # Conservative: count as success if both hit
            # If neither hit, exit_successful remains None (position still open)

        evaluation.update({
            'entry_triggered': entry_triggered,
            'target_hit': target_hit,
            'stop_hit': stop_hit,
            'exit_successful': exit_successful
        })
        
        # Рассчитываем PnL
        pnl_pct = calculate_pnl(forecast_side, forecast_price, actual_close, target_hit, stop_hit)
        evaluation['pnl_pct'] = pnl_pct
        
        # Оцениваем правильность направления
        direction_correct = evaluate_direction(forecast_side, forecast_price, actual_close)
        evaluation['direction_correct'] = direction_correct
        
        # confidence_calibration is not persisted in Logs
        
        logging.info(f"📊 Оценка прогноза {forecast_record.get('method')}: "
                    f"direction_correct={direction_correct}, pnl_pct={pnl_pct:.2f}%")
        
        return evaluation
        
    except Exception as e:
        logging.error(f"❌ Ошибка оценки прогноза: {e}")
        return {}

def check_targets(side, target_str, stop_str, actual_high, actual_low, entry_price):
    """
    Проверяет достижение целей и стопов
    
    Args:
        side: LONG/SHORT/NEUTRAL
        target_str: цель выхода (например, "цена + 5%")
        stop_str: стоп-лосс (например, "цена - 3%")
        actual_high: максимальная цена дня
        actual_low: минимальная цена дня
        entry_price: цена входа
    
    Returns:
        tuple: (target_hit, stop_hit, entry_triggered)
    """
    try:
        # Парсим цели
        target_price = parse_price_target(target_str, entry_price, side)
        stop_price = parse_price_target(stop_str, entry_price, side, is_stop=True)
        
        if side == 'NEUTRAL':
            return False, False, False
        
        # Проверяем достижение
        target_hit = False
        stop_hit = False
        entry_triggered = True  # Предполагаем, что вход произошел
        
        if side == 'LONG':
            target_hit = actual_high >= target_price if target_price else False
            stop_hit = actual_low <= stop_price if stop_price else False
        elif side == 'SHORT':
            target_hit = actual_low <= target_price if target_price else False
            stop_hit = actual_high >= stop_price if stop_price else False
        
        return target_hit, stop_hit, entry_triggered
        
    except Exception as e:
        logging.error(f"❌ Ошибка проверки целей: {e}")
        return False, False, False

def parse_price_target(target_str, entry_price, side, is_stop=False):
    """
    Парсит строку цели в числовое значение
    
    Args:
        target_str: строка цели ("цена + 5%", "$185.50", и т.д.)
        entry_price: цена входа
        side: LONG/SHORT
        is_stop: это стоп-лосс
    
    Returns:
        float: целевая цена или None
    """
    try:
        if not target_str or pd.isna(target_str):
            return None
        
        target_str = str(target_str).strip()
        
        # Если указана абсолютная цена
        if '$' in target_str or target_str.replace('.', '').isdigit():
            # Извлекаем число
            import re
            numbers = re.findall(r'[\d.]+', target_str)
            if numbers:
                return float(numbers[0])
        
        # Если указан процент
        if '%' in target_str:
            import re
            percent_match = re.search(r'([+-]?\d+\.?\d*)%', target_str)
            if percent_match:
                percent = float(percent_match.group(1))
                if side == 'SHORT':
                    percent = -percent  # Для шорта инвертируем знак
                return entry_price * (1 + percent / 100)
        
        return None
        
    except Exception as e:
        logging.error(f"❌ Ошибка парсинга цели '{target_str}': {e}")
        return None

def calculate_pnl(side, entry_price, exit_price, target_hit, stop_hit):
    """
    Рассчитывает PnL в процентах
    
    Args:
        side: LONG/SHORT/NEUTRAL
        entry_price: цена входа
        exit_price: цена выхода
        target_hit: была ли достигнута цель
        stop_hit: был ли сработан стоп
    
    Returns:
        float: PnL в процентах
    """
    try:
        if side == 'NEUTRAL':
            return 0.0
        
        # Проверяем валидность цен
        if entry_price is None or entry_price <= 0 or exit_price is None or exit_price <= 0:
            logging.warning(f"⚠️ Некорректные цены: entry={entry_price}, exit={exit_price}")
            return 0.0
        
        # Базовый PnL
        if side == 'LONG':
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:  # SHORT
            pnl_pct = (entry_price - exit_price) / entry_price * 100
        
        # No artificial multipliers — PnL reflects actual price movement only
        
        return round(pnl_pct, 2)
        
    except Exception as e:
        logging.error(f"❌ Ошибка расчета PnL: {e}")
        return 0.0

def evaluate_direction(side, entry_price, exit_price):
    """
    Оценивает правильность направления прогноза
    
    Args:
        side: LONG/SHORT/NEUTRAL
        entry_price: цена входа
        exit_price: цена выхода
    
    Returns:
        bool: правильность направления
    """
    try:
        if side == 'NEUTRAL':
            return abs(exit_price - entry_price) / entry_price < 0.01  # <1% изменения
        
        if side == 'LONG':
            return exit_price > entry_price
        else:  # SHORT
            return exit_price < entry_price
            
    except Exception as e:
        logging.error(f"❌ Ошибка оценки направления: {e}")
        return False

def calibrate_confidence(predicted_confidence, was_correct):
    """
    Калибрует уверенность прогноза
    
    Args:
        predicted_confidence: предсказанная уверенность (0-100)
        was_correct: был ли прогноз правильным
    
    Returns:
        float: калиброванная уверенность
    """
    try:
        # Простая калибровка: если прогноз правильный, повышаем уверенность
        # если неправильный, понижаем
        if was_correct:
            # Увеличиваем уверенность, но не выше 95
            calibrated = min(95, predicted_confidence + 5)
        else:
            # Уменьшаем уверенность, но не ниже 5
            calibrated = max(5, predicted_confidence - 10)
        
        return round(calibrated, 1)
        
    except Exception as e:
        logging.error(f"❌ Ошибка калибровки уверенности: {e}")
        return predicted_confidence
