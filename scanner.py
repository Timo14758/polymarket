import os, time, logging, requests
from datetime import datetime, timezone

# === Telegram secrets (uit GitHub Secrets) ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === Filters / drempels (kun je in scan.yml via env override'n) ===
THRESHOLD_HIGH = float(os.getenv("THRESHOLD_HIGH", 0.85))  # Yes >= 85% -> contrarian = No
THRESHOLD_LOW  = float(os.getenv("THRESHOLD_LOW",  0.15))  # Yes <= 15% -> contrarian = Yes
MIN_LIQ_USD    = float(os.getenv("MIN_LIQ_USD",    2500))
MIN_VOL_USD    = float(os.getenv("MIN_VOL_USD",    5000))
MAX_ALERTS     = int(os.getenv("MAX_ALERTS", 7))
TIMEOUT = 15

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

session = requests.Session()
session.headers.update({"User-Agent": "polymarket-scanner/1.0"})

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

def tg_send(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured, printing message:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = session.post(url, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def fetch_simplified_markets():
    """Returns list of active, not closed markets from CLOB simplified-markets (handles pagination)."""
    out = []
    next_cursor = ""
    pages = 0
    while True:
        params = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        r = session.get(f"{CLOB}/simplified-markets", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("data", [])
        for m in items:
            if m.get("active") and not m.get("closed"):
                out.append(m)
        next_cursor = data.get("next_cursor") or "LTE="
        pages += 1
        if next_cursor == "LTE=" or pages > 200:
            break
    return out

def batch_midpoints(token_ids):
    """POST /midpoints with token_ids and return {token_id: midprice} floats."""
    if not token_ids:
        return {}
    params = [{"token_id": tid} for tid in token_ids]
    r = session.post(f"{CLOB}/midpoints", json={"params": params}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    # values zijn strings -> cast naar float
    return {str(k): float(v) for k, v in data.items()}

def gamma_lookup(condition_ids):
    """Map condition_id -> gamma market (slug, liquidity, volume)."""
    res = {}
    B = 40
    for i in range(0, len(condition_ids), B):
        chunk = condition_ids[i:i+B]
        params = [("condition_ids", cid) for cid in chunk]
        params += [("active", "true"), ("closed", "false")]
        r = session.get(f"{GAMMA}/markets", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        arr = r.json()
        for m in arr:
            cid = str(m.get("id") or m.get("condition_id") or m.get("conditionId") or "")
            if cid:
                res[cid] = m
        time.sleep(0.2)
    return res

def pick_yes_no_tokens(tokens):
    """Return (yes_token, no_token) from simplified-market tokens."""
    yes = no = None
    for t in tokens or []:
        vals = [str(t.get(k,"")).strip().lower() for k in ("outcome","label","ticker","symbol","name")]
        if any(v in ("yes","y") or v.endswith(":yes") or v.endswith("-yes") for v in vals):
            yes = t
        if any(v in ("no","n") or v.endswith(":no") or v.endswith("-no") for v in vals):
            no = t
    return yes, no

def market_url(slug): return f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com/"

def main():
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    markets = fetch_simplified_markets()
    if not markets:
        tg_send(f"ℹ️ Polymarket scan ({ts}): geen actieve markten gevonden.")
        return

    # verzamel token_ids en condition_ids
    token_ids, by_cid = [], {}
    for m in markets:
        cid = str(m.get("condition_id") or m.get("conditionId") or "")
        toks = m.get("tokens") or []
        if not cid or len(toks) < 2:
            continue
        by_cid[cid] = m
        for t in toks:
            tid = str(t.get("token_id") or t.get("id") or "")
            if tid:
                token_ids.append(tid)

    # midpoints ophalen in batches
    mids, B = {}, 80
    for i in range(0, len(token_ids), B):
        mids.update(batch_midpoints(token_ids[i:i+B]))
        time.sleep(0.2)

    # gamma info voor liq/vol/slug
    gamma = gamma_lookup(list(by_cid.keys()))

    # kansen bouwen
    candidates = []
    for cid, m in by_cid.items():
        yes_tok, no_tok = pick_yes_no_tokens(m.get("tokens"))
        if not yes_tok or not no_tok: 
            continue
        yid = str(yes_tok.get("token_id") or yes_tok.get("id") or "")
        nid = str(no_tok.get("token_id") or no_tok.get("id") or "")
        if not yid or not nid: 
            continue
        if yid not in mids or nid not in mids:
            continue

        yes_mid = mids[yid]
        no_mid  = mids[nid]
        g = gamma.get(cid, {})
        liq = float(g.get("liquidity_num") or g.get("liquidity") or 0.0)
        vol = float(g.get("volume_num") or g.get("volume") or 0.0)
        slug = g.get("slug") or ""

        if liq < MIN_LIQ_USD or vol < MIN_VOL_USD:
            continue

        # contrarian keuze op basis van extreme odds
        play_side = None
        play_price = None
        if yes_mid >= THRESHOLD_HIGH:
            play_side = "No"
            play_price = max(no_mid, 1.0 - yes_mid)
        elif yes_mid <= THRESHOLD_LOW:
            play_side = "Yes"
            play_price = yes_mid
        else:
            continue

        multiple = 1.0 / max(play_price, 1e-6)
        candidates.append({
            "slug": slug, "yes_mid": yes_mid, "no_mid": no_mid,
            "liq": liq, "vol": vol,
            "side": play_side, "price": play_price, "mult": multiple
        })

    # sorteer aantrekkelijkst eerst (payout multiple, dan volume)
    candidates.sort(key=lambda x: (x["mult"], x["vol"]), reverse=True)
    top = candidates[:MAX_ALERTS]

    if not top:
        tg_send(f"ℹ️ Polymarket scan • {ts}\nGeen extreme odds gevonden na filters "
                f"(liq ≥ ${int(MIN_LIQ_USD)}, vol ≥ ${int(MIN_VOL_USD)}).")
        return

    lines = [f"<b>Polymarket auto-scan</b> • {ts}",
             f"Filters: liq ≥ ${int(MIN_LIQ_USD)}, vol ≥ ${int(MIN_VOL_USD)}, extremes Yes ≥ {int(THRESHOLD_HIGH*100)}% / ≤ {int(THRESHOLD_LOW*100)}%",
             "Top kansen:"]
    for a in top:
        lines.append(
            f"• <a href='{market_url(a['slug'])}'>{a['slug'] or 'market'}</a>\n"
            f"  Yes {a['yes_mid']*100:.1f}% | No {a['no_mid']*100:.1f}% | Liq ${a['liq']:.0f} | Vol ${a['vol']:.0f}\n"
            f"  ▶️ Contrarian: <b>{a['side']}</b> @ {a['price']*100:.1f}%  (≈ {a['mult']:.2f}×)\n"
        )
    tg_send("\n".join(lines))

if __name__ == "__main__":
    main()
