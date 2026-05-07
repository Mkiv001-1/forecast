"""
Загрузка исторических данных через Finnhub API
"""

import requests
import json
import time
import logging
from datetime import datetime, timedelta
import pandas as pd

class FinnhubLoader:
    """Класс для работы с Finnhub API"""
    
    def __init__(self, providers_manager=None):
        if providers_manager:
            config = providers_manager.get_finnhub_config()
            if config:
                self.api_key = config['api_key']
                self.rate_limit = config.get('rate_limit', 60)
            else:
                raise ValueError("Конфигурация Finnhub не найдена")
        else:
            raise ValueError("ProvidersManager не передан")
        
        self.last_request_time = 0
        self.base_url = "https://finnhub.io/api/v1"
    
    def _wait_for_rate_limit(self):
        """Ожидание для соблюдения лимитов API"""
        current_time = time.time()
        time_since_last_request = current_time - self.last_request_time
        min_interval = 60 / self.rate_limit  # 1 секунда для 60 запросов/минуту
        
        if time_since_last_request < min_interval:
            sleep_time = min_interval - time_since_last_request
            logging.info(f"⏱️ Ожидание {sleep_time:.1f}с для соблюдения лимитов Finnhub API")
            time.sleep(sleep_time)
        
        self.last_request_time = time.time()
    
    def fetch_daily_data(self, symbol, days=250):
        """Загружает дневные данные"""
        try:
            self._wait_for_rate_limit()
            
            # Рассчитываем даты
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days + 30)  # Берем с запасом
            
            # Формируем URL
            url = f"{self.base_url}/stock/candle"
            params = {
                'symbol': symbol,
                'resolution': 'D',
                'from': int(start_date.timestamp()),
                'to': int(end_date.timestamp()),
                'token': self.api_key
            }
            
            logging.info(f"📈 Запрос к Finnhub API для {symbol}")
            
            response = requests.get(url, params=params, timeout=30, verify=False)
            response.raise_for_status()
            
            data = response.json()
            
            # Проверяем наличие данных
            if data.get('s') != 'ok':
                error_msg = data.get('error', 'Unknown error')
                logging.error(f"❌ Ошибка Finnhub API: {error_msg}")
                return None
            
            # Получаем временные ряды
            timestamps = data.get('t', [])
            opens = data.get('o', [])
            highs = data.get('h', [])
            lows = data.get('l', [])
            closes = data.get('c', [])
            volumes = data.get('v', [])
            
            if not timestamps:
                logging.error("❌ Нет данных в ответе Finnhub")
                return None
            
            # Конвертируем в список словарей
            price_data = []
            for i in range(len(timestamps)):
                try:
                    date_str = datetime.fromtimestamp(timestamps[i]).strftime('%Y-%m-%d')
                    price_data.append({
                        'date': date_str,
                        'open': float(opens[i]),
                        'high': float(highs[i]),
                        'low': float(lows[i]),
                        'close': float(closes[i]),
                        'volume': int(volumes[i])
                    })
                except (ValueError, KeyError, IndexError) as e:
                    logging.warning(f"⚠️ Пропуск некорректных данных: {e}")
                    continue
            
            # Сортируем по дате
            price_data.sort(key=lambda x: x['date'])
            
            logging.info(f"✅ Загружено {len(price_data)} дней через Finnhub")
            return price_data
            
        except Exception as e:
            logging.error(f"❌ Ошибка загрузки данных Finnhub: {e}")
            return None
    
    def parse_to_standard_format(self, raw_data, ticker):
        """Конвертирует данные в стандартный формат"""
        if not raw_data:
            return []
        
        # Добавляем тикер к каждой записи
        for item in raw_data:
            item['ticker'] = ticker
        
        return raw_data

def fetch_price_data_finnhub(ticker, days=250, db_manager=None):
    """
    Основная функция для загрузки данных через Finnhub
    """
    # Создаем providers_manager из db_manager
    if db_manager:
        from providers_manager import ProvidersManager
        providers_manager = ProvidersManager(db_manager)
    else:
        raise ValueError("db_manager не передан")
    
    loader = FinnhubLoader(providers_manager)
    
    # Конвертируем формат тикера NASDAQ:NVDA -> NVDA
    symbol = ticker.split(':')[-1]
    
    # Загружаем данные
    raw_data = loader.fetch_daily_data(symbol, days)
    if not raw_data:
        return []
    
    # Конвертируем в стандартный формат
    price_data = loader.parse_to_standard_format(raw_data, ticker)
    
    return price_data
