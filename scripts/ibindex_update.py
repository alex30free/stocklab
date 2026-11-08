#!/usr/bin/env python3
"""
Weekly update of the ibindex discount/premium tracker for StockLab.

Downloads the price history CSV that ibindex.se offers for each investment
company (price, reported NAV and ibindex's calculated NAV per trading day),
computes the discount/premium series, current statistics and a simple
mean-reversion backtest, and writes everything to ibindex/data.json.

Only the Python standard library is used.
Sign convention: positive = discount (price below NAV), negative = premium.
"""
import csv
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

BASE = "https://ibindex.se/ibi/"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "ibindex", "data.json"))
HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "StockLab weekly updater (github.com/alex30free/stocklab)",
}

# Fallback list if ibindex's company list can't be read.
FALLBACK = {
    "BURE": "Bure", "CRED A": "Creades", "INDU C": "Industrivärden", "INVE B": "Investor",
    "KINV B": "Kinnevik", "LATO B": "Latour", "LINC": "Linc", "LUND B": "Lundbergs",
    "SVOL B": "Svolder", "TRAC B": "Traction", "VEFAB": "VEF", "VNV": "VNV Global", "ORES": "Öresund",
}

THRESHOLD = 1.0      # z-score that defines buy / sell zone
WIN_1Y, WIN_3Y, WIN_5Y = 250, 750, 1250   # trading days
FWD = 250            # forward horizon for the backtest (trading days)
MIN_HISTORY = 500    # first backtest day needs this many prior days
BAND_MONTHS = 36     # rolling window for the monthly band
MIN_BAND_MONTHS = 24


def post(path, payload, timeout=60):
    url = BASE + path  # mirrors the site: ".//company/..." -> "/ibi//company/..."
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def decode(raw):
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def get_companies():
    raw = post("/companies/getCompanies.req", {})
    items = json.loads(decode(raw))
    out = []
    for it in items:
        tk = it.get("product")
        if tk:
            out.append((tk, it.get("productName") or FALLBACK.get(tk, tk)))
    return out


def get_price_csv(ticker):
    raw = post("/company/downloadPriceData.req", {"product": ticker, "currency": "SEK"})
    return decode(raw)


def post_text(path, text, timeout=60):
    """The site posts some requests with the bare ticker string as body."""
    req = urllib.request.Request(BASE + path, data=text.encode("utf-8"), headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(decode(resp.read()))


DEBT_CATEGORIES = {"DBT", "C D"}   # excluded from the share-of-NAV denominator, as on ibindex
CASH_CATEGORIES = {"CSH"}
TOP_N = 5


def _labels(obj):
    """All string values in a category record, used to recognise e.g. 'Onoterade'."""
    return " ".join(str(v) for v in obj.values() if isinstance(v, str)).lower() if isinstance(obj, dict) else ""


def get_holdings(ticker):
    """Top listed holdings, total unlisted and cash as % of NAV (ibindex's own grouping)."""
    holdings = post_text("/company/getHoldings.req", ticker)
    try:
        categories = post_text("/company/getHoldingsCategories.req", ticker)
    except Exception:  # noqa: BLE001
        categories = []
    try:
        date = post_text("/company/getHoldingsDate.req", ticker)
    except Exception:  # noqa: BLE001
        date = None
    if isinstance(date, dict):
        date = next((v for v in date.values() if isinstance(v, str)), None)
    if isinstance(date, (int, float)):
        date = datetime.fromtimestamp(date / 1000, timezone.utc).strftime("%Y-%m-%d")
    date = str(date)[:10] if date else None

    kind = {}
    for cat in categories or []:
        code = cat.get("category") if isinstance(cat, dict) else None
        text = _labels(cat)
        if not code:
            continue
        if code == "LST":
            kind[code] = "listed"
        elif code in DEBT_CATEGORIES:
            kind[code] = "debt"
        elif code in CASH_CATEGORIES or "kassa" in text:
            kind[code] = "cash"
        elif "onoter" in text:
            kind[code] = "unlisted"
        elif "juster" in text:
            kind[code] = "adjust"

    items = []
    for h in holdings or []:
        code = h.get("category")
        value = h.get("holdingValue")
        if value is None:
            continue
        k = kind.get(code) or ("listed" if code == "LST" else "debt" if code in DEBT_CATEGORIES
                               else "cash" if code in CASH_CATEGORIES else "unlisted")
        name = str(h.get("holdingName") or "").strip()
        low = name.lower()
        # ibindex sometimes files listed remainders or interest-bearing assets under unlisted
        if k == "unlisted" and "noterade" in low and "onoterade" not in low:
            k = "listed"
        elif k == "unlisted" and any(w in low for w in ("räntebärande", "likvida", "kassa")):
            k = "cash"
        items.append((k, name, float(value)))

    total = sum(v for k, _, v in items if k != "debt")
    if total <= 0:
        raise ValueError("no holdings")
    listed = sorted(((n, v) for k, n, v in items if k == "listed"), key=lambda x: -x[1])
    return {
        "date": date,
        "top_listed": [{"name": n, "pct": round(v / total * 100, 2)} for n, v in listed[:TOP_N]],
        "unlisted_pct": round(sum(v for k, _, v in items if k == "unlisted") / total * 100, 2),
        "cash_pct": round(sum(v for k, _, v in items if k == "cash") / total * 100, 2),
    }


def parse_csv(text):
    """Return list of (date, price, nav) with nav = calculated NAV if present, else reported."""
    rows = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 4:
        raise ValueError("unexpected CSV header: %r" % (header,))
    for rec in reader:
        if len(rec) < 4:
            continue
        try:
            d = rec[0].strip()
            p, rep, calc = float(rec[1] or 0), float(rec[2] or 0), float(rec[3] or 0)
        except ValueError:
            continue
        nav = calc if calc > 0 else rep
        if p > 0 and nav > 0 and len(d) == 10:
            rows.append((d, p, nav))
    rows.sort(key=lambda r: r[0])
    if len(rows) < 50:
        raise ValueError("too few rows (%d)" % len(rows))
    return rows


def mean_sd(values):
    n = len(values)
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / n
    return m, var ** 0.5


def r1(x):
    return None if x is None else round(x, 1)


def analyse(rows):
    dates = [r[0] for r in rows]
    price = [r[1] for r in rows]
    disc = [(r[2] - r[1]) / r[2] * 100 for r in rows]
    n = len(disc)
    now = disc[-1]

    # Monthly series (last trading day of each month) with a rolling band
    # built from the previous BAND_MONTHS month-end values.
    mvals, mdates = [], []
    for i in range(n):
        if i == n - 1 or dates[i + 1][:7] != dates[i][:7]:
            mvals.append(disc[i])
            mdates.append(dates[i][:7])
    mu, hi, lo = [], [], []
    for k in range(len(mvals)):
        past = mvals[max(0, k - BAND_MONTHS):k]
        if len(past) >= MIN_BAND_MONTHS:
            m, s = mean_sd(past)
            mu.append(r1(m)); hi.append(r1(m + THRESHOLD * s)); lo.append(r1(m - THRESHOLD * s))
        else:
            mu.append(None); hi.append(None); lo.append(None)

    def window(length):
        return disc[max(0, n - length):]

    w1, w3, w5 = window(WIN_1Y), window(WIN_3Y), window(WIN_5Y)
    m1, s1 = mean_sd(w1)
    m3, s3 = mean_sd(w3)
    m5, s5 = mean_sd(w5)
    z3 = (now - m3) / s3 if s3 else 0.0
    z5 = (now - m5) / s5 if s5 else 0.0
    zone = "buy" if z3 >= THRESHOLD else ("sell" if z3 <= -THRESHOLD else "neutral")

    # Backtest: each day, z against the previous WIN_3Y days only (no look-ahead),
    # then measure price change and discount change FWD days later.
    S = [0.0] * (n + 1)
    Q = [0.0] * (n + 1)
    for i, v in enumerate(disc):
        S[i + 1] = S[i] + v
        Q[i + 1] = Q[i] + v * v
    buckets = {"buy": [], "sell": [], "all": []}
    for i in range(MIN_HISTORY, n - FWD):
        a = max(0, i - WIN_3Y)
        L = i - a
        m = (S[i] - S[a]) / L
        var = max(0.0, (Q[i] - Q[a]) / L - m * m)
        sd = var ** 0.5
        if sd == 0:
            continue
        z = (disc[i] - m) / sd
        obs = ((price[i + FWD] / price[i] - 1) * 100, disc[i + FWD] - disc[i])
        buckets["all"].append(obs)
        if z >= THRESHOLD:
            buckets["buy"].append(obs)
        elif z <= -THRESHOLD:
            buckets["sell"].append(obs)

    def summarise(obs):
        if not obs:
            return {"days": 0, "ret": None, "up": None, "ddisc": None}
        k = len(obs)
        return {
            "days": k,
            "ret": r1(sum(o[0] for o in obs) / k),
            "up": r1(sum(1 for o in obs if o[0] > 0) / k * 100),
            "ddisc": r1(sum(o[1] for o in obs) / k),
        }

    return {
        "first_date": dates[0],
        "last_date": dates[-1],
        "price": price[-1],
        "monthly": {"start": mdates[0], "v": [r1(v) for v in mvals], "mu": mu, "hi": hi, "lo": lo},
        "now": {
            "disc": r1(now), "mean_1y": r1(m1), "mean_3y": r1(m3), "sd_3y": r1(s3), "z_3y": round(z3, 2),
            "mean_5y": r1(m5), "sd_5y": r1(s5), "z_5y": round(z5, 2),
            "pct_5y": r1(sum(1 for v in w5 if v <= now) / len(w5) * 100),
            "pct_all": r1(sum(1 for v in disc if v <= now) / n * 100),
            "min_5y": r1(min(w5)), "max_5y": r1(max(w5)), "zone": zone,
        },
        "backtest": {k: summarise(v) for k, v in buckets.items()},
    }


def load_previous():
    try:
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
        return {c["ticker"]: c for c in prev.get("companies", [])}
    except (OSError, ValueError, KeyError):
        return {}


def main():
    previous = load_previous()
    try:
        companies = get_companies()
        print("Company list: %d companies" % len(companies))
    except Exception as exc:  # noqa: BLE001
        print("WARN could not read company list (%s), using fallback" % exc)
        companies = [(c["ticker"], c["name"]) for c in previous.values()] or list(FALLBACK.items())

    result, fresh = [], 0
    for ticker, name in companies:
        try:
            rows = parse_csv(get_price_csv(ticker))
            entry = {"ticker": ticker, "name": name}
            entry.update(analyse(rows))
            try:
                time.sleep(1)
                entry["holdings"] = get_holdings(ticker)
            except Exception as exc:  # noqa: BLE001
                old = previous.get(ticker, {}).get("holdings")
                if old:
                    entry["holdings"] = old
                print("WARN %-7s holdings failed (%s)%s" % (ticker, exc, ", kept previous" if old else ""))
            result.append(entry)
            fresh += 1
            print("OK   %-7s %5d days, last %s, discount %+.1f%%, z3 %+.2f" % (
                ticker, len(rows), entry["last_date"], entry["now"]["disc"], entry["now"]["z_3y"]))
        except Exception as exc:  # noqa: BLE001
            if ticker in previous:
                result.append(previous[ticker])
                print("WARN %-7s failed (%s), kept previous data" % (ticker, exc))
            else:
                print("WARN %-7s failed (%s), skipped" % (ticker, exc))
        time.sleep(2)  # be gentle with ibindex.se

    if fresh == 0:
        print("ERROR no company could be updated; data.json left unchanged")
        return 1

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_through": max(c["last_date"] for c in result),
        "source": "ibindex.se price history CSV (price, reported and calculated NAV)",
        "threshold": THRESHOLD,
        "windows": {"z": "3y = 750 trading days", "band": "%d months" % BAND_MONTHS, "forward": "%d trading days" % FWD},
        "companies": result,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("Wrote %s (%d companies, %d fresh)" % (OUT, len(result), fresh))
    return 0


if __name__ == "__main__":
    sys.exit(main())
