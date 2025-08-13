import os
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

THRESHOLD_HIGH = float(os.getenv("THRESHOLD_HIGH", 0.85))
THRESHOLD_LOW = float(os.getenv("THRESHOLD_LOW", 0.15))
MIN_LIQ_USD = float(os.getenv("MIN_LIQ_USD", 2500))
MIN_VOL_USD = float(os.getenv("MIN_VOL_USD", 5000))
MAX_ALERTS = int(os.getenv("MAX_ALERTS", 7))

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Telegram send error:", e)

def fetch_markets():
    r = requests.get("https://clob.polymarket.com/simplified-markets", timeout=10)
    markets = r.json()
    return [m for m in markets if m.get("active")]

def fetch_midpoints(token_ids):
    ids_str = ",".join(token_ids)
    r = requests.get(f"https://clob.polymarket.com/midpoints?ids={ids_str}", timeout=10)
    return r.json()

def fetch_gamma():
    r = requests.get("https://gamma-api.polymarket.com/markets", timeout=10)
    return {m["conditionId"]: m for m in r.json()}

def main():
    markets = fetch_markets()
    token_ids = []
    market_lookup = {}
    for m in markets:
        for t in m.get("tokens", []):
            token_ids.append(t["id"])
            market_lookup[t["id"]] = m

    midpoints = fetch_midpoints(token_ids)
    gamma_data = fetch_gamma()

    alerts = []
    for token_id, price in midpoints.items():
        market = market_lookup.get(token_id)
        if not market or not price:
            continue

        cond_id = market.get("conditionId")
        gdata = gamma_data.get(cond_id, {})
        liq = gdata.get("liquidity", 0)
        vol = gdata.get("volume", 0)
        slug = gdata.get("slug", "")

        if liq < MIN_LIQ_USD or vol < MIN_VOL_USD:
            continue

        outcome = next((t for t in market["tokens"] if t["id"] == token_id), None)
        if not outcome:
            continue

        yes_price = price if outcome["outcome"] == "Yes" else 1 - price
        if yes_price >= THRESHOLD_HIGH:
            contrarian = "No"
            payoff = round(1 / (1 - yes_price), 2)
        elif yes_price <= THRESHOLD_LOW:
            contrarian = "Yes"
            payoff = round(1 / yes_price, 2)
        else:
            continue

        alerts.append(
            f"*{slug}*\n"
            f"Yes price: {yes_price:.2%}\n"
            f"Liquidity: ${liq:,.0f} | Volume: ${vol:,.0f}\n"
            f"Contrarian side: *{contrarian}* | Payoff ~{payoff}x\n"
            f"https://polymarket.com/event/{slug}"
        )

    if not alerts:
        send_telegram("Geen kansen gevonden.")
    else:
        msg = "🚨 *Polymarket Contrarian Kansen* 🚨\n\n" + "\n\n".join(alerts[:MAX_ALERTS])
        send_telegram(msg)

if __name__ == "__main__":
    main()
