"""
Multi-model forecaster using OpenRouter as the single AI gateway.
All active AI models from the providers table are called via AIClient.
"""

import logging
import re
import time
from datetime import datetime, timedelta

_METHOD_HORIZON_HOURS = {
    'momentum_trend':    24,
    'price_action':       8,
    'relative_strength': 48,
    'volatility':         4,
    'mean_reversion':    72,
    'volume_breakout':    2,
}

_METHOD_HORIZON = _METHOD_HORIZON_HOURS  # backward-compat alias (values now in hours)


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


def generate_multi_model_forecasts(db_manager, ticker, indicators, methods, run_id=None):
    """Generate forecasts for all active AI models × given methods.
    
    Returns:
        tuple: (all_forecasts, log_ids) where log_ids is dict mapping forecast index to log_id
    """
    from ai_client import get_active_ai_models
    from unified_logs_manager import save_forecast_to_logs

    from ai_client import RateLimitError

    active_models = get_active_ai_models(db_manager)
    if not active_models:
        logging.warning("⚠️ No active AI models configured")
        return [], {}

    logging.info(f"🚀 {len(active_models)} models × {len(methods)} methods for {ticker}")
    all_forecasts = []
    log_ids = {}
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
                    timeframe_hours = forecast.get('timeframe_hours') or _METHOD_HORIZON_HOURS.get(method, 24)
                    try:
                        timeframe_hours = int(timeframe_hours)
                    except (TypeError, ValueError):
                        timeframe_hours = _METHOD_HORIZON_HOURS.get(method, 24)
                    horizon_days = max(1, round(timeframe_hours / 24))
                    forecast_date = (datetime.now() + timedelta(hours=timeframe_hours)).strftime('%Y-%m-%d')

                    raw_entry = forecast.get('entry_price', '')
                    ep_match = re.search(r'[\d.]+', str(raw_entry))
                    entry_price = float(ep_match.group()) if ep_match else indicators.get('price', 0)

                    # R/R validation
                    from forecast_engine import validate_signal_rr
                    rr_valid, rr_reason = validate_signal_rr(forecast, entry_price)
                    if not rr_valid:
                        logging.warning(f"⏭️ {method}/{model_name}: skipped ({rr_reason})")
                        continue

                    stop_loss = forecast.get('stop_loss')
                    rr_value = None
                    if stop_loss and entry_price > 0:
                        try:
                            exit_target_str = str(forecast.get('exit_target', ''))
                            t_nums = re.findall(r'[\d.]+', exit_target_str)
                            if t_nums:
                                target_price = float(t_nums[-1])
                                side = str(forecast.get('side', '')).upper()
                                if side == 'LONG' and entry_price > stop_loss:
                                    rr_value = round((target_price - entry_price) / (entry_price - stop_loss), 2)
                                elif side == 'SHORT' and stop_loss > entry_price:
                                    rr_value = round((entry_price - target_price) / (stop_loss - entry_price), 2)
                        except Exception:
                            pass

                    # Extract numeric target_price
                    raw_target = forecast.get('target_price')
                    if raw_target is None:
                        exit_target_str = str(forecast.get('exit_target', ''))
                        t_nums2 = re.findall(r'[\d.]+', exit_target_str)
                        raw_target = float(t_nums2[-1]) if t_nums2 else None
                    else:
                        try:
                            raw_target = float(raw_target)
                        except (TypeError, ValueError):
                            raw_target = None

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
                        'target_price':     raw_target,
                        'stop_loss':        stop_loss,
                        'rr_ratio':         rr_value,
                        'timeframe_hours':  timeframe_hours,
                        'position_size':    '',
                        'rationale':        forecast['rationale'],
                        'horizon_days':     horizon_days,
                        'entry_order_type': forecast.get('entry_order_type', 'LMT'),
                        'entry_limit_price': forecast.get('entry_limit_price'),
                        'entry_tif':        forecast.get('entry_tif', 'DAY'),
                        'take_profit_tif':  forecast.get('take_profit_tif', 'GTC'),
                        'stop_loss_tif':    forecast.get('stop_loss_tif', 'GTC'),
                    }
                    log_id = save_forecast_to_logs(
                        db_manager, forecast_data,
                        prompt_text=prompt, api_response=response,
                        model_name=model_name,
                    )
                    if log_id:
                        # Update logs with run_id
                        if run_id:
                            db_manager.update_log_run_id(log_id, run_id)
                        
                        # Flatten structure for consensus calculation
                        ai_entry_limit = forecast.get('entry_limit_price') or entry_price
                        forecast_idx = len(all_forecasts)
                        all_forecasts.append({
                            'model': model_name,
                            'method': method,
                            'side': forecast.get('side', 'NEUTRAL'),
                            'confidence': forecast.get('confidence', 50),
                            'exit_target': forecast.get('exit_target', ''),
                            'target_price': raw_target,
                            'stop_loss': stop_loss,
                            'entry_limit_price': ai_entry_limit,
                            'entry_tif': forecast.get('entry_tif', 'DAY'),
                            'take_profit_tif': forecast.get('take_profit_tif', 'GTC'),
                            'stop_loss_tif': forecast.get('stop_loss_tif', 'GTC'),
                            'log_id': log_id,  # Keep log_id for linking
                            'run_id': run_id,
                        })
                        log_ids[forecast_idx] = log_id

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

    logging.info(f"✅ Generated {len(all_forecasts)} forecasts (run_id={run_id})")
    return all_forecasts, log_ids
