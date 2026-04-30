"""
Генерация прогнозов с использованием Perplexity API
"""

import requests
import json
import re
import logging
from datetime import datetime, timedelta

def format_number(num):
    """Форматирует большие числа для удобного отображения"""
    if num >= 1000000:
        return f"{num/1000000:.1f}M"
    elif num >= 1000:
        return f"{num/1000:.1f}K"
    else:
        return str(int(num))

def get_method_instructions(method, ind, atr_percent):
    """Возвращает инструкции для конкретного метода анализа."""
    bb = ind.get('bb', {})
    bb_upper = ind.get('bb_upper') or bb.get('upper', 0)
    bb_lower = ind.get('bb_lower') or bb.get('lower', 0)
    ma20     = ind.get('ma20', 0) or 1  # avoid division by zero
    vol_ratio = (ind.get('volume_current', 0) / ind.get('volume_avg_20', 1)
                 if ind.get('volume_avg_20') else 0)

    instructions = {
        'momentum_trend': f"""
Метод: MOMENTUM TREND
- Тренд: MA20={ind.get('ma20',0):.2f} MA50={ind.get('ma50',0):.2f} MA200={ind.get('ma200',0):.2f}
- EMA9={ind.get('ema9',0):.2f} vs EMA21={ind.get('ema21',0):.2f}
- ADX={ind.get('adx14',0):.1f} (тренд {'сильный' if ind.get('adx14',0)>25 else 'слабый'})
- MACD hist={ind.get('macd_hist',0):+.2f}
- RSI={ind.get('rsi14',0):.1f} | OBV={'растет' if ind.get('obv',0)>0 else 'падает'}
Определи направление тренда и силу импульса. Используй выравнивание MA и ADX.
        """,

        'price_action': f"""
Метод: PRICE ACTION
- Цена: {ind.get('price',0):.2f} | BB верхняя: {bb_upper:.2f} нижняя: {bb_lower:.2f}
- Stoch RSI: {ind.get('stoch_rsi',0):.2f} | RSI: {ind.get('rsi14',0):.1f}
- ATR: {atr_percent:.1f}%
- Динамика: 5д={ind.get('change_5d',0):+.1f}% 20д={ind.get('change_20d',0):+.1f}%
Оцени уровни поддержки/сопротивления, перекупленность и свечные паттерны.
        """,

        'relative_strength': f"""
Метод: RELATIVE STRENGTH
- RSI={ind.get('rsi14',0):.1f} | ADX={ind.get('adx14',0):.1f}
- Динамика: 5д={ind.get('change_5d',0):+.1f}% 10д={ind.get('change_10d',0):+.1f}% 20д={ind.get('change_20d',0):+.1f}% 50д={ind.get('change_50d',0):+.1f}%
- Объемы: {format_number(ind.get('volume_current',0))} ({vol_ratio:.1f}x среднего)
Оцени относительную силу актива vs рынок, динамику тренда.
        """,

        'volatility': f"""
Метод: VOLATILITY BREAKOUT
- ATR: ${ind.get('atr14',0):.2f} ({atr_percent:.1f}%)
- BB: [{bb_lower:.2f} — {bb_upper:.2f}] ширина {bb_upper-bb_lower:.2f}
- RSI={ind.get('rsi14',0):.1f} | ADX={ind.get('adx14',0):.1f}
Оцени режим волатильности, риск пробоя Bollinger Bands.
        """,

        'mean_reversion': f"""
Метод: MEAN REVERSION
- Цена: {ind.get('price',0):.2f} | MA20: {ma20:.2f} (откл. {((ind.get('price',0)/ma20)-1)*100:.1f}%)
- MA50: {ind.get('ma50',0):.2f} | MA200: {ind.get('ma200',0):.2f}
- RSI={ind.get('rsi14',0):.1f} | Stoch RSI={ind.get('stoch_rsi',0):.2f}
Оцени вероятность возврата цены к MA20/MA50. Ищи дивергенции.
        """,

        'volume_breakout': f"""
Метод: VOLUME BREAKOUT
- Цена: {ind.get('price',0):.2f} | Объем: {format_number(ind.get('volume_current',0))} ({vol_ratio:.1f}x)
- OBV тренд: {'↑ бычий накопление' if ind.get('obv',0)>0 else '↓ медвежье распределение'}
- ATR={atr_percent:.1f}% | ADX={ind.get('adx14',0):.1f}
Оцени силу объемного импульса и вероятность пробоя ключевого уровня.
        """
    }

    return instructions.get(method, instructions['momentum_trend'])

_PROMPT_FOOTER = """
ОТВЕТЬ СТРОГО В ФОРМАТЕ JSON:
{
    "confidence": число от 0 до 100,
    "side": "LONG" или "SHORT" или "NEUTRAL",
    "entry_price": "$xxx.xx",
    "entry_conditions": ["условие1", "условие2"],
    "exit_target": "$xxx.xx (+x.x%)",
    "exit_stop": "$xxx.xx (-x.x%)",
    "rationale": "подробное обоснование прогноза"
}

Требования: confidence реалистичный (не более 80 без явных сигналов),
entry_price — цена вашего входа в сделку, rationale — детальный анализ."""


def build_prompt(db_manager, ticker, ind, method):
    """Build prompt from DB template if available, else fallback."""
    template = None
    if db_manager:
        try:
            template = db_manager.get_prompt_template(method)
        except Exception:
            pass

    if not template:
        return build_prompt_fallback(ticker, ind, method, db_manager=db_manager)

    from multi_model_forecaster import _METHOD_HORIZON
    horizon = _METHOD_HORIZON.get(method, 1)
    forecast_date = (datetime.now() + timedelta(days=horizon)).strftime('%Y-%m-%d')

    atr_percent = (ind['atr14'] / ind['price'] * 100) if ind.get('price') else 0
    bb = ind.get('bb', {})
    bb_upper = bb.get('upper') or ind.get('bb_upper', 0)
    bb_lower = bb.get('lower') or ind.get('bb_lower', 0)
    bb_pos = 0
    if bb_upper > bb_lower:
        bb_pos = (ind['price'] - bb_lower) / (bb_upper - bb_lower) * 100
    vol_ratio = (ind['volume_current'] / ind['volume_avg_20']
                 if ind.get('volume_avg_20') and ind['volume_avg_20'] > 0 else 0)
    ma20 = ind.get('ma20', 1) or 1
    ma20_dev = ((ind.get('price', 0) / ma20) - 1) * 100

    mkt_ctx = ""
    try:
        from market_context import fetch_market_context, format_market_context
        mkt_ctx = format_market_context(fetch_market_context(db_manager))
    except Exception:
        pass

    history = ""
    if db_manager:
        try:
            from unified_logs_manager import get_forecast_statistics
            stats = get_forecast_statistics(db_manager, days_back=30)
            m_stats = stats.get("methods", {}).get(method, {})
            if m_stats:
                wr = stats.get("accuracy", {}).get(method, 0)
                history = (
                    f"\nИСТОРИЯ МЕТОДА {method.upper()} (30 дней):\n"
                    f"- Win rate: {wr:.0f}% ({m_stats.get('total', 0)} прогнозов)\n"
                    f"- Средний PnL: {stats.get('avg_pnl', 0) or 0:+.2f}%\n"
                )
        except Exception:
            pass

    def _fmt(v, default=0):
        try:
            return float(v)
        except Exception:
            return default

    ctx = {
        "ticker":         ticker,
        "forecast_date":  forecast_date,
        "horizon":        horizon,
        "market_regime":  ind.get('market_regime', 'N/A'),
        "market_context": mkt_ctx,
        "history":        history,
        "footer":         _PROMPT_FOOTER,
        "price":          _fmt(ind.get('price')),
        "ma20":           _fmt(ind.get('ma20')),
        "ma50":           _fmt(ind.get('ma50')),
        "ma200":          _fmt(ind.get('ma200')),
        "ema9":           _fmt(ind.get('ema9')),
        "ema21":          _fmt(ind.get('ema21')),
        "rsi":            _fmt(ind.get('rsi14')),
        "adx":            _fmt(ind.get('adx14')),
        "macd":           _fmt(ind.get('macd')),
        "macd_hist":      _fmt(ind.get('macd_hist')),
        "stoch_rsi":      _fmt(ind.get('stoch_rsi')),
        "atr":            _fmt(ind.get('atr14')),
        "atr_pct":        atr_percent,
        "bb_upper":       bb_upper,
        "bb_lower":       bb_lower,
        "bb_pos":         bb_pos,
        "bb_width":       bb_upper - bb_lower,
        "obv_trend":      "↑ бычий накопление" if _fmt(ind.get('obv')) > 0 else "↓ медвежье распределение",
        "change_5d":      _fmt(ind.get('change_5d')),
        "change_10d":     _fmt(ind.get('change_10d')),
        "change_20d":     _fmt(ind.get('change_20d')),
        "change_50d":     _fmt(ind.get('change_50d')),
        "volume_current": format_number(_fmt(ind.get('volume_current'))),
        "vol_ratio":      vol_ratio,
        "ma20_dev":       ma20_dev,
    }
    try:
        return template.format_map(ctx)
    except Exception as e:
        logging.warning(f"Template format error for {method}: {e}, using fallback")
        return build_prompt_fallback(ticker, ind, method, db_manager=db_manager)

def build_prompt_fallback(ticker, ind, method, db_manager=None):
    """Build enriched forecast prompt with extended indicators and market context."""
    from multi_model_forecaster import _METHOD_HORIZON
    horizon = _METHOD_HORIZON.get(method, 1)
    forecast_date = (datetime.now() + timedelta(days=horizon)).strftime('%Y-%m-%d')

    atr_percent = (ind['atr14'] / ind['price'] * 100) if ind.get('price') else 0
    bb = ind.get('bb', {})
    bb_upper = bb.get('upper') or ind.get('bb_upper', 0)
    bb_lower = bb.get('lower') or ind.get('bb_lower', 0)
    bb_position = 0
    if bb_upper > bb_lower:
        bb_position = (ind['price'] - bb_lower) / (bb_upper - bb_lower) * 100
    volume_ratio = (ind['volume_current'] / ind['volume_avg_20']
                    if ind.get('volume_avg_20') and ind['volume_avg_20'] > 0 else 0)

    # Market context
    mkt_ctx = ""
    try:
        from market_context import fetch_market_context, format_market_context
        ctx = fetch_market_context(db_manager)
        mkt_ctx = format_market_context(ctx)
    except Exception:
        mkt_ctx = ""

    # Performance history for this method
    hist_section = ""
    if db_manager:
        try:
            from unified_logs_manager import get_forecast_statistics
            stats = get_forecast_statistics(db_manager, days_back=30)
            m_stats = stats.get("methods", {}).get(method, {})
            if m_stats:
                accuracy = stats.get("accuracy", {})
                wr = accuracy.get(method, 0)
                avg_pnl = stats.get("avg_pnl", 0) or 0
                total = m_stats.get("total", 0)
                hist_section = (
                    f"\nИСТОРИЯ МЕТОДА {method.upper()} (30 дней):\n"
                    f"- Win rate: {wr:.0f}% ({total} прогнозов)\n"
                    f"- Средний PnL: {avg_pnl:+.2f}%\n"
                )
        except Exception:
            pass

    base_prompt = f"""Сделай торговый прогноз для {ticker} на {forecast_date}.
Горизонт прогноза: {horizon} торговых дней.
Рыночный режим: {ind.get('market_regime', 'N/A')} (ADX={ind.get('adx14', 0):.1f})

РЫНОЧНЫЙ КОНТЕКСТ:
{mkt_ctx}
{hist_section}
ТЕХНИЧЕСКИЕ ДАННЫЕ:
Цена и тренд:
- Текущая цена: ${ind['price']:.2f}
- EMA9: ${ind.get('ema9', 0):.2f}  EMA21: ${ind.get('ema21', 0):.2f}
- MA20: ${ind['ma20']:.2f} ({'▲' if ind['price'] > ind['ma20'] else '▼'})
- MA50: ${ind['ma50']:.2f} ({'▲' if ind['price'] > ind['ma50'] else '▼'})
- MA200: ${ind['ma200']:.2f} ({'▲' if ind['price'] > ind['ma200'] else '▼'})

Осцилляторы:
- RSI(14): {ind['rsi14']:.1f} ({'перекуплен' if ind['rsi14'] > 70 else 'перепродан' if ind['rsi14'] < 30 else 'нейтрален'})
- Stoch RSI: {ind.get('stoch_rsi', 0):.2f}
- MACD: {ind.get('macd', 0):.2f}  Signal: {ind.get('macd_signal', 0):.2f}  Hist: {ind.get('macd_hist', 0):+.2f}

Волатильность:
- ATR(14): ${ind['atr14']:.2f} ({atr_percent:.1f}%)
- Bollinger Bands: верх ${bb_upper:.2f} / низ ${bb_lower:.2f} — позиция {bb_position:.0f}%

Объемы:
- Текущий: {format_number(ind['volume_current'])}  Средний 20д: {format_number(ind['volume_avg_20'])}  ({volume_ratio:.1f}x)
- OBV тренд: {'↑ бычий' if ind.get('obv', 0) > 0 else '↓ медвежий'}

Динамика цены:
- 5д: {ind['change_5d']:+.1f}%  10д: {ind.get('change_10d', 0):+.1f}%  20д: {ind['change_20d']:+.1f}%  50д: {ind.get('change_50d', 0):+.1f}%
"""

    method_instructions = get_method_instructions(method, ind, atr_percent)

    footer = """
ОТВЕТЬ СТРОГО В ФОРМАТЕ JSON:
{
    "confidence": число от 0 до 100,
    "side": "LONG" или "SHORT" или "NEUTRAL",
    "entry_price": "$xxx.xx",
    "entry_conditions": ["условие1", "условие2"],
    "exit_target": "$xxx.xx (+x.x%)",
    "exit_stop": "$xxx.xx (-x.x%)",
    "rationale": "подробное обоснование прогноза"
}

Требования: confidence реалистичный (не более 80 без явных сигналов),
entry_price — цена вашего входа в сделку, rationale — детальный анализ.
"""

    return base_prompt + method_instructions + footer

def call_ai_model(db_manager, model_cfg: dict, prompt: str) -> str:
    """
    Call an AI model via OpenRouter.
    model_cfg: dict with keys model, temperature, max_tokens.
    Returns raw response string.
    """
    from ai_client import get_ai_client
    client = get_ai_client(db_manager)
    if not client:
        raise ValueError("OpenRouter API key not configured. Set OPENROUTER_API_KEY in Config tab.")
    return client.call(
        model=model_cfg["model"],
        user_prompt=prompt,
        temperature=float(model_cfg.get("temperature", 0.2)),
        max_tokens=int(model_cfg.get("max_tokens", 2000)),
    )


def call_perplexity_api(prompt, providers_manager):
    """Legacy wrapper — routes through OpenRouter via AIClient."""
    try:
        db_manager = providers_manager.excel_manager
        models = []
        try:
            from ai_client import get_active_ai_models
            models = get_active_ai_models(db_manager)
        except Exception:
            pass
        model_cfg = next(
            (m for m in models if "sonar" in m.get("model", "") or "perplexity" in m.get("model", "")),
            {"model": "perplexity/sonar-pro", "temperature": 0.2, "max_tokens": 2000},
        )
        return call_ai_model(db_manager, model_cfg, prompt)
    except Exception as e:
        logging.error(f"❌ Ошибка API: {e}")
        raise

def _extract_json_str(text: str) -> str:
    """Extract the outermost JSON object from text using bracket balancing."""
    # Try markdown code block first
    md = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if md:
        return md.group(1).strip()
    # Balance braces
    start = text.find('{')
    if start == -1:
        return text.strip()
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:].strip()


def parse_json_response(text):
    """Парсит JSON из ответа модели с балансировкой скобок."""
    required_fields = ['confidence', 'side', 'rationale']
    try:
        json_text = _extract_json_str(text)
        json_text = re.sub(r'```', '', json_text).strip()
        data = json.loads(json_text)
        for field in required_fields:
            if field not in data:
                logging.error(f"❌ Отсутствует поле: {field}")
                return None
        return data
    except json.JSONDecodeError as e:
        logging.error(f"❌ Ошибка парсинга JSON: {e}")
        logging.error(f"❌ Сырой ответ: {text[:500]}")
        return None
    except Exception as e:
        logging.error(f"❌ Ошибка парсинга JSON: {e}")
        logging.error(f"❌ Сырой ответ: {text[:500]}")
        return None

def generate_forecast(db_manager, ticker, indicators, method, method_num=1, model_cfg=None):
    """Генерирует прогноз для указанного метода через OpenRouter."""
    try:
        logging.info(f"🤖 Метод {method_num}: {method} для {ticker}")
        prompt = build_prompt(db_manager, ticker, indicators, method)
        if model_cfg is None:
            from ai_client import get_active_ai_models
            models = get_active_ai_models(db_manager)
            model_cfg = models[0] if models else {"model": "perplexity/sonar-pro", "temperature": 0.2, "max_tokens": 2000}
        response = call_ai_model(db_manager, model_cfg, prompt)
        logging.info(f"📝 Получен ответ от {model_cfg.get('model')} для {method}")
        forecast = parse_json_response(response)
        if not forecast:
            raise ValueError("Не удалось распарсить JSON ответ")
        logging.info(f"✅ Прогноз {method}: {forecast['side']} (уверенность: {forecast['confidence']}%)")
        return forecast, prompt, response
    except Exception as e:
        logging.error(f"❌ Ошибка генерации прогноза {method}: {e}")
        raise

def log_error(db_manager, ticker, method, error):
    """Логирует ошибку в таблицу Logs как запись с side=ERROR."""
    try:
        from unified_logs_manager import save_forecast_to_logs
        save_forecast_to_logs(
            db_manager,
            {
                'forecast_date': datetime.now().strftime('%Y-%m-%d'),
                'created_at':    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'ticker':        ticker,
                'method':        method,
                'confidence':    0,
                'side':          'ERROR',
                'entry_conditions': '',
                'exit_target':   '',
                'exit_stop':     '',
                'rationale':     str(error),
            },
            prompt_text=None,
            api_response=None,
            model_name='system',
        )
    except Exception as e:
        logging.error(f"❌ Ошибка логирования ошибки: {e}")

def save_forecast(db_manager, ticker, method, forecast, prompt_text=None, api_response=None, model_name=None):
    """Сохраняет прогноз в единую таблицу Logs"""
    try:
        from unified_logs_manager import save_forecast_to_logs
        from multi_model_forecaster import _METHOD_HORIZON
        horizon = _METHOD_HORIZON.get(method, 1)
        forecast_date = (datetime.now() + timedelta(days=horizon)).strftime('%Y-%m-%d')
        entry_conditions = '; '.join(forecast.get('entry_conditions', []))
        forecast_data = {
            'forecast_date': forecast_date,
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'ticker': ticker,
            'method': method,
            'confidence': forecast['confidence'],
            'side': forecast['side'],
            'entry_conditions': entry_conditions,
            'exit_target': forecast.get('exit_target', ''),
            'exit_stop': forecast.get('exit_stop', ''),
            'position_size': '',
            'rationale': forecast['rationale'],
            'horizon_days': horizon,
        }
        success = save_forecast_to_logs(db_manager, forecast_data, prompt_text, api_response, model_name)
        if success:
            logging.info(f"✅ Сохранен прогноз {method} для {ticker} в Logs")
        else:
            logging.error(f"❌ Не удалось сохранить прогноз {method} для {ticker} в Logs")
        
        return success
        
    except Exception as e:
        logging.error(f"❌ Ошибка сохранения прогноза: {e}")
        return False
