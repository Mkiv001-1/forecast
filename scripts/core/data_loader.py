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


def fetch_intraday_yfinance(ticker: str, days: int = 60, interval: str = "1h", max_retries: int = 3) -> list:
    """Fetch intraday bars via yfinance.

    Args:
        ticker:   Exchange-prefixed ticker, e.g. 'NASDAQ:TQQQ'
        days:     How many calendar days of history to fetch (max 730 for 1h)
        interval: yfinance interval string, e.g. '1h', '30m', '15m'

    Returns:
        list of dicts: {datetime, interval, open, high, low, close, volume}
        datetime is an ISO string: '2026-05-07 14:00:00'
    """
    symbol = ticker.split(":")[-1]
    period = f"{min(days, 730)}d"

    for attempt in range(max_retries):
        try:
            logging.info(f"📈 Intraday {ticker} ({symbol}) period={period} interval={interval} (attempt {attempt+1})")
            ticker_obj = yf.Ticker(symbol)
            data = ticker_obj.history(period=period, interval=interval)

            if data.empty:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                logging.warning(f"⚠️ No intraday data for {ticker}")
                return []

            bars = []
            for dt_idx, row in data.iterrows():
                if any(pd.isna(row[col]) for col in ["Open", "High", "Low", "Close", "Volume"]):
                    continue
                # Normalize timezone-aware index to naive UTC-local string
                if hasattr(dt_idx, "tz_localize"):
                    dt_str = str(dt_idx)[:19]
                elif hasattr(dt_idx, "strftime"):
                    dt_str = dt_idx.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    dt_str = str(dt_idx)[:19]

                bars.append({
                    "datetime": dt_str,
                    "interval": interval,
                    "open":     float(row["Open"]),
                    "high":     float(row["High"]),
                    "low":      float(row["Low"]),
                    "close":    float(row["Close"]),
                    "volume":   int(row["Volume"]),
                })

            logging.info(f"✅ Fetched {len(bars)} intraday bars for {ticker} ({interval})")
            return bars

        except Exception as e:
            if attempt < max_retries - 1:
                logging.warning(f"⚠️ Intraday fetch error for {ticker}: {e}, retrying...")
                time.sleep(2)
            else:
                logging.error(f"❌ Intraday fetch failed for {ticker} after {max_retries} attempts: {e}")
                return []

    return []


def fetch_price_data(ticker, days=250, db_manager=None):
    """
    Универсальная функция загрузки данных с кэшированием в Excel
    """
    from config import DATA_SOURCE
    
    # Загружаем данные через умную систему (кэш проверяется внутри smart_data_loader)
    new_data = None
    
    # Используем умную загрузку с активными провайдерами
    try:
        from smart_data_loader import fetch_price_data_smart
        new_data = fetch_price_data_smart(ticker, days, db_manager)
        if new_data:
            logging.info(f"✅ Загружено {len(new_data)} дней через умную систему")
        else:
            logging.warning("⚠️ Умная система не смогла загрузить данные")
    except ImportError as e:
        logging.warning(f"⚠️ Умная загрузка недоступна: {e}")

    # Fallback на старую логику если умная система не вернула данные
    if not new_data:
        logging.info("🔄 Используем резервную логику загрузки")
        
        # Пробуем Alpha Vantage
        try:
            from alpha_vantage_loader import fetch_price_data_alphavantage
            logging.info(f"🔄 Используем Alpha Vantage для {ticker}")
            new_data = fetch_price_data_alphavantage(ticker, days, db_manager)
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

def load_cached_data_from_excel(ticker, days, db_manager):
    """Загружает кэшированные данные из Excel"""
    try:
        price_df = db_manager.read_sheet('PriceData')
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
        
        if not price_data:
            return []

        # Сортируем и ограничиваем
        price_data.sort(key=lambda x: str(x['date']), reverse=True)

        # Проверяем свежесть кэша: последняя дата не должна быть старше 2 дней
        from datetime import date as _date
        latest = price_data[0]['date']
        if hasattr(latest, 'date'):
            latest = latest.date()
        elif isinstance(latest, str):
            latest = datetime.strptime(latest[:10], '%Y-%m-%d').date()
        staleness = (_date.today() - latest).days
        if staleness > 2:
            logging.info(f"ℹ️ Кэш устарел ({staleness} дней), принудительная загрузка через API")
            return []

        return price_data[:days]
        
    except Exception as e:
        logging.error(f"❌ Ошибка загрузки кэша: {e}")
        return []

