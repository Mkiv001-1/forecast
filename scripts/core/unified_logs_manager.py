"""
Управление единой таблицей Logs (объединение Forecasts + Actuals)
"""

import logging
from datetime import datetime, timedelta
import pandas as pd

def save_forecast_to_logs(excel_manager, forecast_data, prompt_text=None, api_response=None, model_name=None):
    """Сохраняет прогноз в единую таблицу Logs со статусом NEW"""
    try:
        # Добавляем обязательные поля для новой структуры
        log_data = forecast_data.copy()
        log_data['status'] = 'NEW'
        log_data['id'] = generate_log_id()
        
        # Добавляем использованный промпт
        log_data['forecast_prompt'] = prompt_text if prompt_text else ''
        
        # Добавляем ответ от ИИ
        log_data['prompt_response'] = api_response if api_response else ''
        
        # Добавляем модель ИИ
        log_data['model'] = model_name if model_name else 'perplexity'
        
        # Поля actuals остаются пустыми
        actuals_fields = [
            'actual_date', 'actual_open', 'actual_close', 'actual_high', 'actual_low',
            'entry_triggered', 'target_hit', 'stop_hit',
            'pnl_pct', 'direction_correct', 'exit_successful'
        ]
        
        for field in actuals_fields:
            if field not in log_data:
                log_data[field] = None
        
        return excel_manager.append_to_sheet('Logs', log_data)
        
    except Exception as e:
        logging.error(f"❌ Ошибка сохранения прогноза в Logs: {e}")
        return False

def generate_log_id():
    """Генерирует уникальный ID для записи лога"""
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    import random
    random_suffix = random.randint(1000, 9999)
    return f"LOG_{timestamp}_{random_suffix}"

def get_forecasts_to_evaluate(excel_manager, days_back=30):
    """Возвращает NEW-прогнозы готовые к оценке.

    Условие: created_at старше 1 дня (данные уже должны быть доступны)
    и не старше days_back дней (не трогаем очень старые).
    forecast_date может быть чуть в будущем — берём ближайшую торговую дату.
    """
    try:
        df = excel_manager.read_sheet('Logs')
        if df.empty:
            return []

        now = datetime.now()
        # Оцениваем прогнозы созданные > 3 часов назад (данные уже появились)
        threshold = now - timedelta(hours=3)
        cutoff    = now - timedelta(days=days_back)

        df['created_at'] = pd.to_datetime(df['created_at'], errors='coerce')

        to_evaluate = df[
            (df['status'] == 'NEW') &
            (df['created_at'] <= threshold) &
            (df['created_at'] >= cutoff)
        ]

        records = to_evaluate.to_dict('records')
        logging.info(f"📊 Найдено {len(records)} прогнозов для оценки")
        return records

    except Exception as e:
        logging.error(f"❌ Ошибка получения прогнозов для оценки: {e}")
        return []

def update_forecast_with_actuals(excel_manager, log_id, actuals_data):
    """Обновляет запись прогноза с фактическими данными"""
    try:
        valid_fields = ['actual_date', 'actual_close', 'actual_high', 'actual_low',
                        'entry_triggered', 'target_hit', 'stop_hit', 'pnl_pct', 'direction_correct']
        update_payload = {k: v for k, v in actuals_data.items() if k in valid_fields}
        update_payload['status'] = 'EVALUATED'
        success = excel_manager.update_row_by_id('Logs', log_id, update_payload)
        if success:
            logging.info(f"✅ Обновлена запись {log_id} с фактическими данными")
        return success

    except Exception as e:
        logging.error(f"❌ Ошибка обновления записи {log_id}: {e}")
        return False


def update_forecast_status(excel_manager, log_id, status):
    """Обновляет статус записи прогноза"""
    try:
        success = excel_manager.update_row_by_id('Logs', log_id, {'status': status})
        if success:
            logging.info(f"✅ Обновлен статус записи {log_id} на {status}")
        return success
    except Exception as e:
        logging.error(f"❌ Ошибка обновления статуса записи {log_id}: {e}")
        return False

def get_forecast_statistics(excel_manager, days_back=30):
    """Получает статистику по прогнозам"""
    try:
        df = excel_manager.read_sheet('Logs')
        
        if df.empty:
            return {}
        
        # Фильтруем по дате
        cutoff_date = datetime.now() - timedelta(days=days_back)
        df['forecast_date'] = pd.to_datetime(df['forecast_date'], errors='coerce')
        recent_df = df[df['forecast_date'] >= cutoff_date]
        
        if recent_df.empty:
            return {}
        
        # Статистика
        stats = {
            'total_forecasts': len(recent_df),
            'evaluated': len(recent_df[recent_df['status'] == 'EVALUATED']),
            'pending': len(recent_df[recent_df['status'] == 'NEW']),
            'methods': {},
            'accuracy': {},
            'avg_confidence': 0,
            'avg_pnl': 0
        }
        
        # Статистика по методам
        for method in recent_df['method'].unique():
            method_df = recent_df[recent_df['method'] == method]
            stats['methods'][method] = {
                'total': len(method_df),
                'evaluated': len(method_df[method_df['status'] == 'EVALUATED']),
                'avg_confidence': method_df['confidence'].mean() if 'confidence' in method_df.columns else 0
            }
            
            # Точность для оцененных прогнозов
            evaluated_method_df = method_df[method_df['status'] == 'EVALUATED']
            if not evaluated_method_df.empty and 'direction_correct' in evaluated_method_df.columns:
                correct_count = evaluated_method_df['direction_correct'].sum()
                stats['accuracy'][method] = correct_count / len(evaluated_method_df) * 100
        
        # Общие метрики
        evaluated_df = recent_df[recent_df['status'] == 'EVALUATED']
        if not evaluated_df.empty:
            if 'confidence' in evaluated_df.columns:
                stats['avg_confidence'] = evaluated_df['confidence'].mean()
            if 'pnl_pct' in evaluated_df.columns:
                stats['avg_pnl'] = evaluated_df['pnl_pct'].mean()
        
        return stats
        
    except Exception as e:
        logging.error(f"❌ Ошибка получения статистики: {e}")
        return {}
