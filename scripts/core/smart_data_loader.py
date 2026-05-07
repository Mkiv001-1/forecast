"""
Умный загрузчик данных с поддержкой нескольких активных провайдеров
"""

import logging

def fetch_price_data_smart(ticker, days=250, db_manager=None):
    """
    Умная загрузка данных с поддержкой нескольких активных провайдеров
    """
    # Сначала пробуем кэш
    if db_manager:
        try:
            from data_loader import load_cached_data_from_excel
            cached_data = load_cached_data_from_excel(ticker, days, db_manager)
            if cached_data:
                logging.info(f"✅ Загружено {len(cached_data)} дней из кэша Excel для {ticker}")
                return cached_data
        except Exception as e:
            logging.warning(f"⚠️ Ошибка загрузки из кэша: {e}")
    
    # Получаем активных провайдеров
    active_providers = get_active_providers(db_manager)
    
    if not active_providers:
        logging.warning("⚠️ Нет активных провайдеров")
        return []
    
    # Пробуем каждого активного провайдера в порядке приоритета
    for provider in active_providers:
        try:
            logging.info(f"🔄 Пробуем провайдера {provider} для {ticker}")
            
            if provider == 'finnhub':
                from finnhub_loader import fetch_price_data_finnhub
                data = fetch_price_data_finnhub(ticker, days, db_manager)
                
            elif provider == 'alpha_vantage':
                from alpha_vantage_loader import fetch_price_data_alphavantage
                data = fetch_price_data_alphavantage(ticker, days, db_manager)
                
            elif provider == 'yfinance':
                from data_loader import fetch_price_data_yfinance
                data = fetch_price_data_yfinance(ticker, days)
                
            elif provider == 'massive.com':
                # massive.com - это провайдер ИИ, не рыночных данных
                logging.warning(f"⚠️ Провайдер {provider} не поддерживает рыночные данные")
                continue
                
            else:
                # Проверяем, не является ли провайдер ИИ-сервисом
                ai_providers = ['perplexity', 'deepseek', 'gemini', 'open_router']
                if provider in ai_providers:
                    logging.warning(f"⚠️ Провайдер {provider} - это ИИ сервис, не рыночные данные")
                    continue
                else:
                    logging.warning(f"⚠️ Неизвестный провайдер: {provider}")
                    continue
            
            if data:
                logging.info(f"✅ Успешно загружено {len(data)} дней через {provider}")
                return data
            else:
                logging.warning(f"⚠️ Провайдер {provider} не вернул данные")
                
        except ImportError as e:
            logging.warning(f"⚠️ Провайдер {provider} недоступен: {e}")
        except Exception as e:
            logging.error(f"❌ Ошибка провайдера {provider}: {e}")
    
    logging.error(f"❌ Ни один провайдер не смог загрузить данные для {ticker}")
    return []

def get_active_providers(db_manager):
    """Получает список активных провайдеров"""
    try:
        # Читаем всех провайдеров
        providers_df = db_manager.read_sheet('Providers')
        if providers_df is None or providers_df.empty:
            return []
        
        # Фильтруем активных провайдеров рыночных данных (type == 'data')
        active_providers = providers_df[
            (providers_df['active'] == 1) & (providers_df['type'] == 'data')
        ]['name'].tolist()
        
        logging.info(f"📊 Активные провайдеры: {active_providers}")
        return active_providers
        
    except Exception as e:
        logging.error(f"❌ Ошибка получения активных провайдеров: {e}")
        return []
