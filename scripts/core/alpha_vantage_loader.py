"""
Загрузка исторических данных через Alpha Vantage API
"""

import requests
import json
import time
import logging
from datetime import datetime, timedelta
import pandas as pd

# Импортируем конфигурацию
# from config import ALPHA_VANTAGE_API_KEY, ALPHA_VANTAGE_RATE_LIMIT

class AlphaVantageLoader:
    """Класс для работы с Alpha Vantage API"""
    
    def __init__(self, providers_manager=None):
        if providers_manager:
            config = providers_manager.get_provider_config('alpha_vantage')
            if config:
                self.api_key = config['api_key']
                self.rate_limit = config.get('rate_limit', 5)
            else:
                raise ValueError("Конфигурация Alpha Vantage не найдена")
        else:
            raise ValueError("ProvidersManager не передан")
        
        self.last_request_time = 0
        self.base_url = "https://www.alphavantage.co/query"
    
    def _wait_for_rate_limit(self):
        """Ожидание для соблюдения лимитов API"""
        current_time = time.time()
        time_since_last_request = current_time - self.last_request_time
        min_interval = 60 / self.rate_limit  # 12 секунд для 5 запросов/минуту
        
        if time_since_last_request < min_interval:
            wait_time = min_interval - time_since_last_request
            logging.info(f"⏳ Ожидание {wait_time:.1f} секунд для соблюдения лимитов API")
            time.sleep(wait_time)
        
        self.last_request_time = time.time()
    
    def fetch_daily_data(self, symbol, days=250):
        """Загружает дневные данные для тикера"""
        try:
            self._wait_for_rate_limit()
            
            # Alpha Vantage возвращает максимум 100+ дней за раз
            # Для больших периодов нужно несколько запросов
            all_data = {}
            current_symbol = symbol
            
            # Используем compact (бесплатный) размер - последние 100 дней
            params = {
                'function': 'TIME_SERIES_DAILY',
                'symbol': current_symbol,
                'outputsize': 'compact',
                'apikey': self.api_key
            }
            
            logging.info(f"📈 Загрузка данных для {symbol} через Alpha Vantage")
            
            response = requests.get(self.base_url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            
            # Проверяем на ошибки
            if 'Error Message' in data:
                logging.error(f"❌ Ошибка Alpha Vantage: {data['Error Message']}")
                return None
            
            if 'Note' in data:
                logging.error(f"❌ Лимит API превышен: {data['Note']}")
                return None
            
            if 'Time Series (Daily)' not in data:
                logging.error(f"❌ Нет данных для тикера {symbol}")
                logging.error(f"❌ Ответ API: {data}")
                return None
            
            # Парсим данные
            time_series = data['Time Series (Daily)']
            
            # Фильтруем по нужному периоду
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)
            
            filtered_data = {}
            for date_str, values in time_series.items():
                date = datetime.strptime(date_str, '%Y-%m-%d')
                if start_date <= date <= end_date:
                    filtered_data[date_str] = values
            
            if not filtered_data:
                logging.warning(f"⚠️ Нет данных в указанном периоде для {symbol}")
                return None
            
            logging.info(f"✅ Загружено {len(filtered_data)} дней данных для {symbol}")
            return filtered_data
            
        except requests.exceptions.RequestException as e:
            logging.error(f"❌ Ошибка запроса к Alpha Vantage: {e}")
            return None
        except Exception as e:
            logging.error(f"❌ Ошибка загрузки данных: {e}")
            return None
    
    def parse_to_standard_format(self, data, ticker):
        """Конвертирует данные Alpha Vantage в стандартный формат"""
        if not data:
            return []
        
        price_data = []
        
        for date_str in sorted(data.keys()):
            values = data[date_str]
            
            try:
                record = {
                    'date': datetime.strptime(date_str, '%Y-%m-%d'),
                    'open': float(values['1. open']),
                    'high': float(values['2. high']),
                    'low': float(values['3. low']),
                    'close': float(values['4. close']),
                    'volume': int(values['5. volume'])
                }
                price_data.append(record)
                
            except (ValueError, KeyError) as e:
                logging.warning(f"⚠️ Пропуск некорректных данных за {date_str}: {e}")
                continue
        
        # Сортируем по дате
        price_data.sort(key=lambda x: x['date'])
        
        return price_data

def fetch_price_data_alphavantage(ticker, days=250, excel_manager=None):
    """
    Основная функция для загрузки данных через Alpha Vantage
    """
    # Создаем providers_manager из excel_manager
    if excel_manager:
        from providers_manager import ProvidersManager
        providers_manager = ProvidersManager(excel_manager)
    else:
        raise ValueError("excel_manager не передан")
    
    loader = AlphaVantageLoader(providers_manager)
    
    # Конвертируем формат тикера NASDAQ:NVDA -> NVDA
    symbol = ticker.split(':')[-1]
    
    # Загружаем данные
    raw_data = loader.fetch_daily_data(symbol, days)
    if not raw_data:
        return []
    
    # Конвертируем в стандартный формат
    price_data = loader.parse_to_standard_format(raw_data, ticker)
    
    return price_data
