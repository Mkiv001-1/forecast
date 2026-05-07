"""
Управление API ключами и настройками провайдеров из Excel
"""

import pandas as pd
import logging
from typing import Dict, Optional

class ProvidersManager:
    def __init__(self, db_manager):
        self.db_manager = db_manager
        self._providers_cache = None
    
    def get_provider_config(self, provider_name: str) -> Optional[Dict]:
        """Получает конфигурацию провайдера из Excel"""
        try:
            if self._providers_cache is None:
                self._load_providers()
            
            # Ищем по имени (колонка 'name')
            provider_data = self._providers_cache[self._providers_cache['name'] == provider_name]
            
            if provider_data.empty:
                logging.error(f"❌ Провайдер {provider_name} не найден в таблице Providers")
                return None
            
            if provider_data.iloc[0]['active'] != 1:
                logging.warning(f"⚠️ Провайдер {provider_name} неактивен")
                return None
            
            config = provider_data.iloc[0].to_dict()
            
            # Конвертируем в нужный формат
            result_config = {
                'provider': config['name'],
                'api_key': config.get('api_key') or config.get('api', ''),
                'active': config['active'],
                'model': 'sonar-pro',  # значения по умолчанию
                'temperature': 0.2,
                'max_tokens': 1500,
                'rate_limit': 60
            }
            
            return result_config
            
        except Exception as e:
            logging.error(f"❌ Ошибка получения конфигурации провайдера {provider_name}: {e}")
            return None
    
    def _load_providers(self):
        """Загружает данные провайдеров из Excel"""
        try:
            self._providers_cache = self.db_manager.read_sheet('Providers')
            if self._providers_cache is None or self._providers_cache.empty:
                logging.error("❌ Таблица Providers пуста или не найдена")
                self._providers_cache = pd.DataFrame()
        except Exception as e:
            logging.error(f"❌ Ошибка загрузки провайдеров: {e}")
            self._providers_cache = pd.DataFrame()
    
    def get_perplexity_config(self) -> Optional[Dict]:
        """Получает конфигурацию Perplexity API"""
        return self.get_provider_config('perplexity')
    
    def get_alpha_vantage_config(self) -> Optional[Dict]:
        """Получает конфигурацию Alpha Vantage API"""
        return self.get_provider_config('alpha_vantage')
    
    def get_finnhub_config(self) -> Optional[Dict]:
        """Получает конфигурацию Finnhub API"""
        return self.get_provider_config('finnhub')
    
    def update_provider_config(self, provider_name: str, config: Dict) -> bool:
        """Обновляет конфигурацию провайдера в Excel"""
        try:
            if self._providers_cache is None:
                self._load_providers()
            
            # Находим индекс провайдера
            provider_idx = self._providers_cache[self._providers_cache['provider'] == provider_name].index
            
            if provider_idx.empty:
                logging.error(f"❌ Провайдер {provider_name} не найден")
                return False
            
            idx = provider_idx[0]
            
            # Обновляем значения
            for key, value in config.items():
                if key in self._providers_cache.columns:
                    self._providers_cache.at[idx, key] = value
            
            # Сохраняем в Excel
            success = self.db_manager.update_sheet('Providers', self._providers_cache)
            
            if success:
                logging.info(f"✅ Конфигурация провайдера {provider_name} обновлена")
            else:
                logging.error(f"❌ Не удалось сохранить конфигурацию провайдера {provider_name}")
            
            return success
            
        except Exception as e:
            logging.error(f"❌ Ошибка обновления конфигурации провайдера {provider_name}: {e}")
            return False
    
    def refresh_cache(self):
        """Обновляет кэш провайдеров"""
        self._providers_cache = None
        self._load_providers()
