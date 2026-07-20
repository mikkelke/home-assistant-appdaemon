def on_window(az, window_az=70.0, tol=55.0):
    return abs(((az - window_az + 180) % 360) - 180) <= tol


def beam_heat(az, elev, rad, window_az=70.0, tol=55.0, min_elev=3.0, rad_thr=250.0):
    return on_window(az, window_az, tol) and elev > min_elev and rad >= rad_thr


def daily_high_from_forecast(resp, today_iso):
    """Extract today's daily-high (°C) from a weather.get_forecasts response.
    Tolerates the get_forecasts envelope: result -> response -> <entity> -> forecast (list),
    and also a bare {entity: {forecast: [...]}} or {forecast:[...]} / list shape.
    Accepts daily (one entry/day) OR hourly (many/day -> take max). Reads 'temperature'
    or 'native_temperature'. Returns float or None."""
    # 1) find the forecast list of dicts, recursively
    def find_list(o):
        if isinstance(o, list):
            if o and isinstance(o[0], dict) and ("temperature" in o[0] or "native_temperature" in o[0] or "datetime" in o[0]):
                return o
            for it in o:
                r = find_list(it)
                if r: return r
            return None
        if isinstance(o, dict):
            if "forecast" in o and isinstance(o["forecast"], list):
                return o["forecast"]
            for v in o.values():
                r = find_list(v)
                if r: return r
        return None
    lst = find_list(resp)
    if not lst:
        return None
    highs = []
    for item in lst:
        if not isinstance(item, dict):
            continue
        dt = str(item.get("datetime", ""))[:10]
        if today_iso and dt and dt != today_iso:
            continue
        t = item.get("temperature", item.get("native_temperature"))
        if isinstance(t, (int, float)):
            highs.append(float(t))
    if not highs:
        # if nothing matched today but the list is daily with today missing, return None
        return None
    return max(highs)
