"""Weather: today's numbers and the proactive alerts already raised."""
from ._util import latest

PROVIDER = {"name": "weather", "title": "Weather"}
ALERTS = {"rain_cushions": "Rain coming: bring in the deck cushions", "snow_shoveling": "Snow coming: plan to shovel",
          "severe_weather": "Severe weather expected"}


def collect(conn, today):
    def v(m):
        r = latest(conn, m, today, 1)
        return r[1] if r else None

    alerts = [dict(r) for r in conn.execute(
        "SELECT alert_type, target_date FROM weather_alerts WHERE target_date>=? ORDER BY target_date",
        (today.isoformat(),)).fetchall()]
    attention = [{"key": f"weather:{a['alert_type']}:{a['target_date']}", "href": "/home/weather",
                  "level": "urgent" if a["alert_type"] == "severe_weather" else "warn",
                  "text": f"{ALERTS.get(a['alert_type'], a['alert_type'])} ({a['target_date']})"} for a in alerts]
    return {"card": {"high_f": v("weather:high_f"), "low_f": v("weather:low_f"), "precip_in": v("weather:precip_in"),
                     "wind_mph": v("weather:wind_mph"), "alerts": alerts}, "attention": attention}
