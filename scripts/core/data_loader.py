"""
Загрузка исторических данных о ценах
"""

import yfinance as yf
import requests
import pandas as pd
import logging
import time
from datetime import datetime, timedelta

def fetch_price_data_yfinance(ticker, days=250, max_retries=3):
    """
    Загружает исторические данные через yfinance
    Альтернатива GOOGLEFINANCE
    """
    for attempt in range(max_retries):
        try:
            import yfinance as yf
            
            # Конвертируем формат тикера NASDAQ:NVDA -> NVDA
            symbol = ticker.split(':')[-1]
            
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)
            
            logging.info(f"📈 Загрузка данных для {ticker} ({symbol}) за {days} дней (попытка {attempt + 1})")
            
            # Создаем объект Ticker и загружаем данные
            ticker_obj = yf.Ticker(symbol)
            data = ticker_obj.history(start=start_date, end=end_date)
            
            if data.empty:
                if attempt < max_retries - 1:
                    logging.warning(f"⚠️ Нет данных для тикера {ticker}, повторная попытка...")
                    time.sleep(2)
                    continue
                else:
                    logging.error(f"❌ Нет данных для тикера {ticker} после {max_retries} попыток")
                    return []
            
            # Проверяем качество данных
            if len(data) < days * 0.8:  # Если данных меньше 80% от запрошенного
                logging.warning(f"⚠️ Загружено только {len(data)} дней из {days} запрошенных")
            
            # Преобразуем DataFrame в список словарей
            price_data = []
            for date, row in data.iterrows():
                # Проверяем на пропущенные значения
                if any(pd.isna(row[col]) for col in ['Open', 'High', 'Low', 'Close', 'Volume']):
                    continue
                    
                price_data.append({
                    'date': date,
                    'open': float(row['Open']),
                    'high': float(row['High']),
                    'low': float(row['Low']),
                    'close': float(row['Close']),
                    'volume': int(row['Volume'])
                })
            
            if not price_data:
                if attempt < max_retries - 1:
                    logging.warning(f"⚠️ Все данные содержат пропуски, повторная попытка...")
                    time.sleep(2)
                    continue
                else:
                    logging.error(f"❌ Нет валидных данных для тикера {ticker}")
                    return []
            
            logging.info(f"✅ Загружено {len(price_data)} дней данных для {ticker}")
            return price_data
            
        except ImportError as e:
            logging.error(f"❌ Библиотека yfinance не установлена: {e}")
            logging.error("❌ Установите: pip install yfinance")
            return []
        except Exception as e:
            if attempt < max_retries - 1:
                logging.warning(f"⚠️ Ошибка загрузки данных для {ticker}: {e}, повторная попытка...")
                time.sleep(2)
                continue
            else:
                logging.error(f"❌ Ошибка загрузки данных для {ticker} после {max_retries} попыток: {e}")
                return []
    
    return []

def fetch_price_data(ticker, days=250, excel_manager=None):
    """
    Универсальная функция загрузки данных с кэшированием в Excel
    """
    from config import DATA_SOURCE
    
    # Сначала пробуем кэш Excel, но не блокируем загрузку из API
    if excel_manager:
        try:
            cached_data = load_cached_data_from_excel(ticker, days, excel_manager)
            if cached_data:
                logging.info(f"✅ Загружено {len(cached_data)} дней из кэша Excel для {ticker}")
                return cached_data
            else:
                logging.info(f"ℹ️ В кэше Excel нет данных для {ticker}, пробуем API")
        except Exception as e:
            logging.warning(f"⚠️ Ошибка загрузки из кэша Excel: {e}, пробуем API")
    
    # Загружаем новые данные
    new_data = None
    
    # Используем умную загрузку с активными провайдерами
    try:
        from smart_data_loader import fetch_price_data_smart
        new_data = fetch_price_data_smart(ticker, days, excel_manager)
        if new_data:
            logging.info(f"✅ Загружено {len(new_data)} дней через умную систему")
        else:
            logging.warning("⚠️ Умная система не смогла загрузить данные")
    except ImportError as e:
        logging.warning(f"⚠️ Умная загрузка недоступна: {e}")
        
        # Fallback на старую логику
        logging.info("🔄 Используем резервную логику загрузки")
        
        # Пробуем Alpha Vantage
        try:
            from alpha_vantage_loader import fetch_price_data_alphavantage
            logging.info(f"🔄 Используем Alpha Vantage для {ticker}")
            new_data = fetch_price_data_alphavantage(ticker, days, excel_manager)
            if new_data:
                logging.info(f"✅ Загружено {len(new_data)} дней через Alpha Vantage")
        except ImportError as e:
            logging.warning(f"⚠️ Alpha Vantage недоступен: {e}")
        
        # Fallback на yfinance
        if not new_data:
            logging.info(f"🔄 Используем yfinance для {ticker}")
            new_data = fetch_price_data_yfinance(ticker, days)
    
    if not new_data:
        logging.error(f"❌ Не удалось загрузить данные для {ticker}")
        return []
    
    return new_data

def load_cached_data_from_excel(ticker, days, excel_manager):
    """Загружает кэшированные данные из Excel"""
    try:
        price_df = excel_manager.read_sheet('PriceData')
        if price_df is None or price_df.empty:
            return []
        
        # Фильтруем по тикеру
        if 'ticker' in price_df.columns:
            ticker_data = price_df[price_df['ticker'] == ticker]
        else:
            # Если нет колонки ticker, используем все данные
            ticker_data = price_df
        
        if ticker_data.empty:
            return []
        
        # Конвертируем в нужный формат
        price_data = []
        for _, row in ticker_data.iterrows():
            try:
                # Определяем колонку с датой
                date_col = 'date' if 'date' in row else 'Date'
                price_data.append({
                    'date': row[date_col],
                    'open': float(row['open']),
                    'high': float(row['high']),
                    'low': float(row['low']),
                    'close': float(row['close']),
                    'volume': int(row['volume'])
                })
            except (ValueError, KeyError) as e:
                continue
        
        # Сортируем и ограничиваем
        price_data.sort(key=lambda x: str(x['date']), reverse=True)
        return price_data[:days]
        
    except Exception as e:
        logging.error(f"❌ Ошибка загрузки кэша: {e}")
        return []

def save_price_data_to_sheet(sheets_client, price_data):
    """Сохраняет исторические данные о ценах в Google Sheets"""
    try:
        ws = sheets_client.get_worksheet('PriceData')
        if not ws:
            logging.error("❌ Лист PriceData не найден")
            return
        
        # Очищаем все данные кроме заголовков
        if len(ws.get_all_values()) > 1:
            ws.delete_rows(2, len(ws.get_all_values()))
        
        # Добавляем заголовки
        headers = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
        ws.append_row(headers)
        
        # Добавляем данные
        for record in price_data:
            row = [
                record['date'].strftime('%Y-%m-%d'),
                record['open'],
                record['high'],
                record['low'],
                record['close'],
                record['volume']
            ]
            ws.append_row(row)
        
        logging.info(f"✅ Сохранено {len(price_data)} записей в PriceData")
        
    except Exception as e:
        logging.error(f"❌ Ошибка сохранения данных в PriceData: {e}")
