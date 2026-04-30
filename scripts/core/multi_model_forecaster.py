"""
Multi-model forecaster using OpenRouter as the single AI gateway.
All active AI models from the providers table are called via AIClient.
"""

import logging
import time
from datetime import datetime, timedelta

_METHOD_HORIZON = {
    'momentum_trend':    5,
    'price_action':      2,
    'relative_strength': 10,
    'volatility':        3,
    'mean_reversion':    7,
    'volume_breakout':   5,
}


def generate_forecast_with_model(db_manager, ticker, indicators, method, model_cfg):
    """Generate a forecast for one method + one model via OpenRouter."""
    from forecast_engine import build_prompt, call_ai_model, parse_json_response
    model_name = model_cfg['name']
    logging.info(f"🤖 {ticker} | {method} | {model_name} ({model_cfg['model']})")

    prompt = build_prompt(db_manager, ticker, indicators, method)

    response = call_ai_model(db_manager, model_cfg, prompt)
    if not response:
        logging.error(f"❌ No response from {model_name}")
        return None, None, None

    forecast = parse_json_response(response)
    if not forecast:
        logging.error(f"❌ Could not parse JSON from {model_name}")
        return None, None, None

    logging.info(f"✅ {method}/{model_name}: {forecast['side']} conf={forecast['confidence']}%")
    return forecast, prompt, response


def generate_multi_model_forecasts(db_manager, ticker, indicators, methods):
    """Generate forecasts for all active AI models × given methods."""
    from ai_client import get_active_ai_models
    from unified_logs_manager import save_forecast_to_logs

    from ai_client import RateLimitError

    active_models = get_active_ai_models(db_manager)
    if not active_models:
        logging.warning("⚠️ No active AI models configured")
        return []

    logging.info(f"🚀 {len(active_models)} models × {len(methods)} methods for {ticker}")
    all_forecasts = []
    import re

    for model_cfg in active_models:
        rate_limit = model_cfg.get('rate_limit', 60)
        min_interval = 60.0 / max(rate_limit, 1)
        model_name = model_cfg['name']

        for method in methods:
            try:
                t0 = time.time()
                forecast, prompt, response = generate_forecast_with_model(
                    db_manager, ticker, indicators, method, model_cfg
                )

                if forecast:
                    horizon = _METHOD_HORIZON.get(method, 1)
                    forecast_date = (datetime.now() + timedelta(days=horizon)).strftime('%Y-%m-%d')

                    raw_entry = forecast.get('entry_price', '')
                    ep_match = re.search(r'[\d.]+', str(raw_entry))
                    entry_price = float(ep_match.group()) if ep_match else indicators.get('price', 0)

                    forecast_data = {
                        'forecast_date':    forecast_date,
                        'created_at':       datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'ticker':           ticker,
                        'method':           method,
                        'confidence':       forecast['confidence'],
                        'side':             forecast['side'],
                        'entry_price':      entry_price,
                        'entry_conditions': '; '.join(forecast.get('entry_conditions', [])),
                        'exit_target':      forecast.get('exit_target', ''),
                        'exit_stop':        forecast.get('exit_stop', ''),
                        'position_size':    '',
                        'rationale':        forecast['rationale'],
                        'horizon_days':     horizon,
                    }
                    success = save_forecast_to_logs(
                        db_manager, forecast_data,
                        prompt_text=prompt, api_response=response,
                        model_name=model_name,
                    )
                    if success:
                        all_forecasts.append({'model': model_name, 'method': method, 'forecast': forecast})

                # Rate limiting between requests
                elapsed = time.time() - t0
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)

            except RateLimitError as e:
                logging.warning(f"⏭️ Skipping model '{model_name}' — rate limited (retry_after={e.retry_after}s)")
                break  # skip remaining methods for this model, try next model

            except Exception as e:
                logging.error(f"❌ Error {method}/{model_name}: {e}")
                continue

    logging.info(f"✅ Generated {len(all_forecasts)} forecasts")
    return all_forecasts
