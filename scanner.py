import os, time, logging, requests
from datetime import datetime, timezone

# === Telegram secrets ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === Filters ===
THRESHOLD_HIGH = float(os.getenv("THRESHOLD_HIGH", 0.80))
THRESHOLD_LOW  = float(os.getenv("THRESHOLD_LOW",  0.20))
MIN_LIQ_USD    = float(os.getenv("MIN_LIQ_USD",    1000))
MIN_VOL_USD    = float(os.getenv("MIN_VOL_USD",    2000))
MAX_ALERTS     = int(os.getenv("MAX_ALERTS",       10))
TIMEOUT = 15

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

session = requests.Session()
session.headers.update({"User-Agent": "polymarket-scanner/1.2"})
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

def tg_send(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM:", text)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        session.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=TIMEOUT,
        ).raise_for_status()
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def fetch_simplified_markets():
    out, next_cursor, pages = [], "", 0
    while True:
        params = {"next_cursor": next_cursor} if next_cursor else {}
        r = session.get(f"{CLOB}/simplified-markets", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("data", []) if isinstance(data, dict) else (data or [])
        for m in items:
            if isinstance(m, dict) and m.get("active") and not m.get("closed"):
                out.append(m)
        next_cursor = (data.get("next_cursor") if isinstance(data, dict) else None) or "LTE="
        pages += 1
        if next_cursor == "LTE=" or pages > 200:
            break
    return out

def _midpoints_post(token_ids):
    """Try POST schema; return dict or raise."""
    # schema 1: {"params":[{"token_id":"..."}]}
    payload = {"params": [{"token_id": tid} for tid in token_ids]}
    r = session.post(f"{CLOB}/midpoints", json=payload, timeout=TIMEOUT)
    if not r.ok:
        raise requests.HTTPError(f"POST /midpoints {r.status_code}: {r.text[:200]}")
    data = r.json()
    if isinstance(data, dict):
        return {str(k): float(v) for k, v in data.items()}
    raise ValueError("Unexpected POST /midpoints response type")

def _midpoints_get(token_ids):
    """Fallback GET schema; return dict."""
    ids = ",".join(token_ids)
    r = session.get(f"{CLOB}/midpoints", params={"ids": ids}, timeout=TIMEOUT)
    if not r.ok:
        raise requests.HTTPError(f"GET /midpoints {r.status_code}: {r.text[:200]}")
    data = r.json()
    if isinstance(data, dict):
        return {str(k): float(v) for k, v in data.items()}
    # sommige varianten geven lijst terug -> maak leeg (we hebben dan geen data)
    return {}

def batch_midpoints(token_ids):
    """Robuust: probeer POST, val terug op GET; chunk om 400/414 te voorkomen."""
    result = {}
    if not token_ids:
        return result
    # kleine chunks om 400/414 en rate limits te vermijden
    CHUNK = 80
    for i in range(0, len(token_ids), CHUNK):
        chunk = token_ids[i:i+CHUNK]
        try:
            part = _midpoints_post(chunk)
        except Exception as e_post:
            logging.warning(f"midpoints POST failed (chunk {i//CHUNK}): {e_post}")
            try:
                part = _midpoints_get(chunk)
            except Exception as e_get:
                logging.error(f"midpoints GET failed (chunk {i//CHUNK}): {e_get}")
                part = {}
        result.update(part)
        time.sleep(0.15)
    return result

def gamma_lookup(condition_ids):
    res, B = {}, 40
    for i in range(0, len(condition_ids), B):
        chunk = condition_ids[i:i+B]
        params = [("condition_ids", cid) for cid in chunk]
        params += [("active", "true"), ("closed", "false")]
        try:
            r = session.get(f"{GAMMA}/markets", params=params, timeout=TIMEOUT)
            r.raise_for_status()
            arr = r.json()
            if isinstance(arr, list):
                for m in arr:
                    if not isinstance(m, dict): 
                        continue
                    cid = str(m.get("id") or m.get("condition_id") or m.get("conditionId") or "")
                    if cid:
                        res[cid] = m
        except Exception as e:
            logging.warning(f"gamma lookup failed chunk {i//B}: {e}")
        time.sleep(0.15)
    return res

def pick_yes_no_tokens(tokens):
    yes = no = None
    for t in tokens or []:
        if not isinstance(t, dict): 
            continue
        vals = [str(t.get(k,"")).strip().lower() for k in ("outcome","label","ticker","symbol","name")]
        if any(v in ("yes","y") or v.endswith(":yes") or v.endswith("-yes") for v in vals):
            yes = t
        if any(v in ("no","n") or v.endswith(":no") or v.endswith("-no") for v in vals):
            no = t
    return yes, no

def market_url(slug):
    return f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com/"

def main():
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    try:
        markets = fetch_simplified_markets()
    except Exception as e:
        tg_send(f"❌ Scan error ({ts}): simplified-markets failed: {e}")
        return

    if not markets:
        tg_send(f"ℹ️ Polymarket scan ({ts}): geen actieve markten gevonden.")
        return

    token_ids, by_cid = [], {}
    for m in markets:
        if not isinstance(m, dict): 
            continue
        cid = str(m.get("condition_id") or m.get("conditionId") or "")
        toks = m.get("tokens") or []
        if not cid or len(toks) < 2:
            continue
        by_cid[cid] = m
        for t in toks:
            if not isinstance(t, dict): 
                continue
            tid = str(t.get("token_id") or t.get("id") or "")
            if tid:
                token_ids.append(tid)

    try:
        mids = batch_midpoints(token_ids)
    except Exception as e:
        tg_send(f"❌ Scan error ({ts}): midpoints failed: {e}")
        mids = {}

    gamma = gamma_lookup(list(by_cid.keys()))

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

        yes_mid = float(mids[yid])
        no_mid  = float(mids[nid])
        g = gamma.get(cid, {}) if isinstance(gamma, dict) else {}
        liq = g.get("liquidity_num") or g.get("liquidity") or g.get("liquidityUsd") or 0.0
        vol = g.get("volume_num")   or g.get("volume")   or g.get("volumeUsd")   or 0.0
        try:
            liq = float(liq); vol = float(vol)
        except Exception:
            liq, vol = 0.0, 0.0
        slug = g.get("slug") or m.get("slug") or ""

        if liq < MIN_LIQ_USD or vol < MIN_VOL_USD:
            continue

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
        edge_pp = (0.5 - play_price) * 100.0
        skew_pp = abs(yes_mid - 0.5) * 100.0

        candidates.append({
            "slug": slug, "yes_mid": yes_mid, "no_mid": no_mid,
            "liq": liq, "vol": vol,
            "side": play_side, "price": play_price, "mult": multiple,
            "edge_pp": edge_pp, "skew_pp": skew_pp
        })

    candidates.sort(key=lambda x: (x["mult"], x["vol"]), reverse=True)
    top = candidates[:MAX_ALERTS]

    if not top:
        tg_send(
            f"ℹ️ Polymarket scan • {ts}\n"
            f"Geen extreme odds gevonden na filters (liq ≥ ${int(MIN_LIQ_USD)}, vol ≥ ${int(MIN_VOL_USD)})."
        )
        return

    lines = [
        f"<b>Polymarket auto-scan</b> • {ts}",
        f"Filters: liq ≥ ${int(MIN_LIQ_USD)}, vol ≥ ${int(MIN_VOL_USD)}, extremes Yes ≥ {int(THRESHOLD_HIGH*100)}% / ≤ {int(THRESHOLD_LOW*100)}%",
        "Top kansen:"
    ]
    for a in top:
        lines.append(
            f"• <a href='{market_url(a['slug'])}'>{a['slug'] or 'market'}</a>\n"
            f"  Yes {a['yes_mid']*100:.1f}% | No {a['no_mid']*100:.1f}% | "
            f"Liq ${a['liq']:.0f} | Vol ${a['vol']:.0f}\n"
            f"  ▶️ Contrarian: <b>{a['side']}</b> @ {a['price']*100:.1f}%  (≈ {a['mult']:.2f}×)\n"
            f"  📈 Skew: {a['skew_pp']:.1f} pp | Edge (naïef): {a['edge_pp']:.1f}%\n"
        )
    tg_send("\n".join(lines))

if __name__ == "__main__":
    main()
