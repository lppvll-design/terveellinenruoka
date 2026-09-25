#!/usr/bin/env python3
"""
tarjousbotti.py – seuraa omaa tuotelistaa suomalaisissa verkkokaupoissa
ja lähettää Discord-hälytykset hintojen pudotessa (Black Week / Black Friday).

Käyttö:
  python tarjousbotti.py                 # tarkistuskierros (cron ajaa tätä)
  python tarjousbotti.py --force         # ohita ajoväli, tarkista heti
  python tarjousbotti.py --test URL      # testaa yhden tuotesivun hinnanluku
  python tarjousbotti.py --summary       # lähetä numerokoonti Discordiin
  python tarjousbotti.py --katsaus       # Claude Haikun päivittäinen hintakatsaus
  python tarjousbotti.py --list          # tulosta viimeisimmät hinnat
  python tarjousbotti.py --dry-run       # älä lähetä Discordiin, tulosta vain
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import yaml

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "tuotteet.yaml"
DB_PATH = BASE / "hinnat.db"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.7",
}
TIMEOUT = 25


# ---------------------------------------------------------------- hinnanluku

@dataclass
class Hinta:
    price: float | None           # nykyinen hinta € (sis. ALV)
    normal: float | None = None   # kaupan ilmoittama normaalihinta
    in_stock: bool | None = None
    title: str | None = None
    source: str = ""              # millä tavalla hinta luettiin


def _get(url: str, session: requests.Session, as_json=False):
    r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json() if as_json else r.text


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return round(float(str(v).replace(",", ".").replace("\xa0", "").replace(" ", "")), 2)
    except ValueError:
        return None


def fetch_shopify(url: str, s) -> Hinta:
    """Happy Angler ym. Shopify-kaupat: /products/<handle>.js"""
    p = urlparse(url)
    handle = re.search(r"/products/([^/?#]+)", p.path).group(1)
    j = _get(f"{p.scheme}://{p.netloc}/products/{handle}.js", s, as_json=True)
    variants = j.get("variants", [])
    want = parse_qs(p.query).get("variant", [None])[0]
    v = next((x for x in variants if str(x["id"]) == str(want)), None)
    if v is None:  # halvin saatavilla oleva variantti
        pool = [x for x in variants if x.get("available")] or variants
        v = min(pool, key=lambda x: x["price"])
    cmp_ = v.get("compare_at_price")
    title = j["title"] + ("" if v.get("title") in (None, "Default Title") else f" ({v['title']})")
    return Hinta(v["price"] / 100, cmp_ / 100 if cmp_ else None, bool(v.get("available")),
                 title, "shopify")


def fetch_woocommerce(url: str, s) -> Hinta:
    """Ruthless Fishing ym. WooCommerce: Store API ?slug="""
    p = urlparse(url)
    slug = re.search(r"/(?:tuote|product)/([^/?#]+)", p.path).group(1)
    arr = _get(f"{p.scheme}://{p.netloc}/wp-json/wc/store/v1/products?slug={slug}", s, as_json=True)
    if not arr:
        raise ValueError("WooCommerce: tuotetta ei löytynyt")
    j = arr[0]
    pr = j["prices"]
    div = 10 ** int(pr.get("currency_minor_unit", 2))
    price = int(pr["price"]) / div
    if pr.get("price_range"):  # vaihtoehtotuote -> halvin
        price = int(pr["price_range"]["min_amount"]) / div
    regular = int(pr["regular_price"]) / div if pr.get("regular_price") else None
    return Hinta(price, regular if regular and regular > price else None,
                 bool(j.get("is_in_stock")), htmllib.unescape(j.get("name", "")), "woocommerce")


def fetch_nethit(url: str, s) -> Hinta:
    """Eräkellari ym. Nethit-kaupat: /backend/api/v1/products/<koodi>"""
    p = urlparse(url)
    code = re.search(r"/p/([^/?#]+)", p.path).group(1)
    j = _get(f"{p.scheme}://{p.netloc}/backend/api/v1/products/{code}?lang=fi", s, as_json=True)
    pi = j["price_info"]
    price = pi["price"]["with_tax"]
    normal = (pi.get("normal_price") or {}).get("with_tax")
    name = j.get("name", "") + (f" {j['model']}" if j.get("model") else "")
    return Hinta(round(price, 2), round(normal, 2) if pi.get("is_discount") and normal else None,
                 not j.get("sold_out", False) and (j.get("free_quantity") or 0) > 0,
                 name, "nethit")


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def fetch_html(url: str, s) -> Hinta:
    """Yleinen: JSON-LD (Kärkkäinen, Hintaopas, Tokmanni...) + meta-tagit (Magento)."""
    text = _get(url, s)
    prices, stock, title = [], None, None
    variant_hit = None
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip(), strict=False)
        except Exception:
            continue
        for node in _walk(data):
            t = node.get("@type")
            t = t if isinstance(t, list) else [t]
            if ("Product" in t or "ProductGroup" in t) and not title:
                title = node.get("name")
            if "Product" in t and node.get("url") == url and isinstance(node.get("offers"), dict):
                variant_hit = node["offers"]
            if "Offer" in t and node.get("price") is not None:
                prices.append(_num(node["price"]))
                av = str(node.get("availability", ""))
                if av:
                    stock = (stock or False) or ("InStock" in av or "PreOrder" in av)
            if "AggregateOffer" in t and node.get("lowPrice") is not None:
                prices.append(_num(node["lowPrice"]))
                stock = True if stock is None else stock
    price = None
    if variant_hit:
        price = _num(variant_hit.get("lowPrice", variant_hit.get("price")))
    if price is None:
        prices = [p for p in prices if p]
        price = min(prices) if prices else None
    src = "json-ld"
    if price is None:
        m = re.search(r'<meta[^>]+(?:property|itemprop)="(?:product:price:amount|price)"[^>]+content="([\d.,]+)"', text)
        if m:
            price, src = _num(m.group(1)), "meta"
    normal = None
    m = re.search(r'data-price-amount="([\d.]+)"\s+data-price-type="oldPrice"', text)  # Magento
    if m:
        normal = _num(m.group(1))
    if not title:
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', text)
        title = htmllib.unescape(m.group(1)) if m else None
    if price is None:
        raise ValueError("hintaa ei löytynyt sivulta")
    return Hinta(price, normal if normal and normal > price else None, stock,
                 htmllib.unescape(title) if title else None, src)


def fetch_price(url: str, s) -> Hinta:
    path = urlparse(url).path
    tries = []
    if "/products/" in path:
        tries.append(fetch_shopify)
    if re.search(r"/(tuote|product)/[^/]+", path):
        tries.append(fetch_woocommerce)
    if re.search(r"/p/[^/]+/?$", path):
        tries.append(fetch_nethit)
    tries.append(fetch_html)
    err = None
    for fn in tries:
        try:
            return fn(url, s)
        except Exception as e:  # kokeillaan seuraavaa tapaa
            err = e
    raise err


_HO_CACHE: dict[str, list] = {}


def hintaopas_id(url_or_id) -> str | None:
    if url_or_id is None:
        return None
    s = str(url_or_id)
    if s.isdigit():
        return s
    m = re.search(r"hintaopas\.fi/product\.php\?(?:[^#]*&)?p=(\d+)", s)
    return m.group(1) if m else None


def fetch_hintaopas_history(pid: str, s) -> list[dict]:
    """Hintaoppaan hintahistoria (alin hinta kaikista kaupoista, n. 3 kk).
    Data on upotettu tuotesivun Next.js-payloadiin (self.__next_f.push)."""
    if pid in _HO_CACHE:
        return _HO_CACHE[pid]
    text = _get(f"https://hintaopas.fi/product.php?p={pid}", s)
    chunks = re.findall(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)', text)
    payload = "".join(json.loads(c) for c in chunks)
    i = payload.find('"priceHistory":')
    if i < 0:
        raise ValueError("Hintaopas: hintahistoriaa ei löytynyt")
    obj, _ = json.JSONDecoder().raw_decode(payload[i + len('"priceHistory":'):])
    items = []
    for it in obj.get("historyItems", []):
        if it.get("price") is None:
            continue
        items.append({"date": it["date"][:10], "price": it["price"] / 100,
                      "shop": it.get("shopName"), "member": bool(it.get("isMembershipPrice"))})
    _HO_CACHE[pid] = items
    return items


def ho_stats(items: list[dict], days: int) -> dict | None:
    """Alin/ylin hinta viimeisen `days` päivän ajalta (jäsenhinnat pois)."""
    cut = (date.today() - timedelta(days=days)).isoformat()
    xs = [x for x in items if not x["member"]]
    # hinta joka oli voimassa jakson alussa + jakson muutokset
    before = [x for x in xs if x["date"] < cut]
    inside = [x for x in xs if x["date"] >= cut]
    pool = ([before[-1]] if before else []) + inside
    if not pool:
        return None
    lo = min(pool, key=lambda x: x["price"])
    return {"min": lo["price"], "min_shop": lo["shop"], "min_date": lo["date"],
            "max": max(x["price"] for x in pool)}


def store_name(url: str) -> str:
    host = urlparse(url).netloc.removeprefix("www.")
    names = {"happyangler.fi": "Happy Angler", "tokmanni.fi": "Tokmanni",
             "karkkainen.com": "Kärkkäinen", "ruthlessfishing.fi": "Ruthless Fishing",
             "erakellari.fi": "Eräkellari", "hintaopas.fi": "Hintaopas"}
    return names.get(host, host)


# ---------------------------------------------------------------- tietokanta

def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS prices(
        url TEXT, ts TEXT, price REAL, normal REAL, in_stock INTEGER, title TEXT);
    CREATE INDEX IF NOT EXISTS ix_prices ON prices(url, ts);
    CREATE TABLE IF NOT EXISTS alerts(
        key TEXT PRIMARY KEY, price REAL, ts TEXT);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    return c


def history(c, url, days=None):
    q = "SELECT price, ts, in_stock FROM prices WHERE url=? AND price IS NOT NULL"
    args = [url]
    if days:
        q += " AND ts>=?"
        args.append((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"))
    return c.execute(q + " ORDER BY ts", args).fetchall()


def last(c, url):
    return c.execute("SELECT price, in_stock, ts FROM prices WHERE url=? ORDER BY ts DESC LIMIT 1",
                     (url,)).fetchone()


def alerted(c, key, price) -> bool:
    """True jos tästä (tai alemmasta) hinnasta on jo hälytetty."""
    row = c.execute("SELECT price FROM alerts WHERE key=?", (key,)).fetchone()
    return row is not None and price >= row[0] - 0.005


def mark(c, key, price):
    c.execute("INSERT OR REPLACE INTO alerts VALUES(?,?,?)",
              (key, price, datetime.now().isoformat(timespec="seconds")))


def clear(c, key):
    c.execute("DELETE FROM alerts WHERE key=?", (key,))


# ---------------------------------------------------------------- Discord

def send_discord(webhook: str, embeds: list[dict], content: str | None = None, dry=False):
    for i in range(0, max(len(embeds), 1), 10):
        payload = {"username": "Tarjousbotti", "embeds": embeds[i:i + 10]}
        if content and i == 0:
            payload["content"] = content
        if dry or not webhook:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            continue
        r = requests.post(webhook, json=payload, timeout=20)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            r = requests.post(webhook, json=payload, timeout=20)
        r.raise_for_status()
        time.sleep(1)


def eur(x):
    return "–" if x is None else f"{x:,.2f} €".replace(",", " ").replace(".", ",")


# ---------------------------------------------------------------- Claude

def ask_claude(cfg, system: str, prompt: str, max_tokens=800) -> str | None:
    """Kutsuu Claude Haikua. Palauttaa None jos avain puuttuu tai kutsu epäonnistuu."""
    ccfg = cfg.get("claude", {})
    if ccfg.get("kaytossa", True) is False or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=ccfg.get("malli", "claude-haiku-4-5"), max_tokens=max_tokens,
            system=system, messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        print(f"Claude-virhe: {e}", file=sys.stderr)
        return None


CLAUDE_SYSTEM = (
    "Olet suomalainen hintavahti. Tehtäväsi on arvioida, onko verkkokaupan tarjous aito vai "
    "keinotekoinen (esim. hintaa nostettu ennen alennusta tai normaalihinta liioiteltu). "
    "Perusta arvio VAIN annettuun hintadataan, älä keksi lukuja. Kirjoita suomeksi, lyhyesti ja "
    "suoraan. Jos data on liian vähäistä, sano se.")


# ---------------------------------------------------------------- logiikka

def in_bf_window(cfg) -> bool:
    bw = cfg.get("black_week", {})
    try:
        a = date.fromisoformat(str(bw["alku"]))
        b = date.fromisoformat(str(bw["loppu"]))
    except Exception:
        return False
    return a <= date.today() <= b


def due(c, cfg, force) -> bool:
    if force:
        return True
    ajo = cfg.get("ajovali_min", {})
    mins = ajo.get("black_week", 15) if in_bf_window(cfg) else ajo.get("normaali", 180)
    row = c.execute("SELECT v FROM meta WHERE k='last_run'").fetchone()
    if row and datetime.now() - datetime.fromisoformat(row[0]) < timedelta(minutes=mins - 1):
        return False
    return True


def product_ho_id(prod) -> str | None:
    pid = hintaopas_id(prod.get("hintaopas"))
    if pid:
        return pid
    for u in prod.get("urlit", []):
        pid = hintaopas_id(u)
        if pid:
            return pid
    return None


def ho_history_safe(prod, s) -> list[dict] | None:
    pid = product_ho_id(prod)
    if not pid:
        return None
    try:
        return fetch_hintaopas_history(pid, s)
    except Exception as e:
        print(f"Hintaopas-historia {pid}: {e}", file=sys.stderr)
        return None


def price_changes(c, url, days=90) -> list[str]:
    """Hinnan muutoskohdat tiiviisti: ['2026-10-02 159.00', '2026-10-20 199.00', ...]"""
    out, prev = [], None
    for price, ts, _ in history(c, url, days):
        if prev is None or abs(price - prev) > 0.005:
            out.append(f"{ts[:10]} {price:.2f}")
            prev = price
    return out


def check(cfg, dry=False, force=False):
    c = db()
    if not due(c, cfg, force):
        return
    c.execute("INSERT OR REPLACE INTO meta VALUES('last_run', ?)",
              (datetime.now().isoformat(timespec="seconds"),))
    c.commit()

    s = requests.Session()
    h = cfg.get("halytykset", {})
    drop_pct = float(h.get("pudotus_pct", 10))
    min_obs_for_low = int(h.get("alin_hinta_min_havaintoja", 5))
    embeds, errors, claude_items = [], [], []
    now = datetime.now().isoformat(timespec="seconds")

    for prod in (cfg.get("tuotteet") or []):
        name = prod["nimi"]
        target = _num(prod.get("tavoitehinta"))
        seen, prod_embeds = [], []
        for url in prod.get("urlit", []):
            time.sleep(random.uniform(1.5, 4))  # kohtelias tahti
            prev = last(c, url)
            try:
                hp = fetch_price(url, s)
            except Exception as e:
                errors.append(f"{name} @ {store_name(url)}: {e}")
                continue
            c.execute("INSERT INTO prices VALUES(?,?,?,?,?,?)",
                      (url, now, hp.price, hp.normal,
                       None if hp.in_stock is None else int(hp.in_stock), hp.title))
            seen.append((url, hp))
            reasons = []
            hist30 = [r[0] for r in history(c, url, 30)][:-1]  # ilman juuri lisättyä
            all_hist = [r[0] for r in history(c, url)][:-1]

            # 1) tavoitehinta alittui
            if target and hp.price <= target and hp.in_stock is not False:
                key = f"target|{url}"
                if not alerted(c, key, hp.price):
                    reasons.append(f"🎯 Alitti tavoitehinnan {eur(target)}")
                    mark(c, key, hp.price)
            elif target and hp.price > target:
                clear(c, f"target|{url}")

            # 2) iso pudotus edellisestä
            if prev and prev[0] and hp.price < prev[0] * (1 - drop_pct / 100):
                reasons.append(f"📉 Pudotus {eur(prev[0])} → {eur(hp.price)} "
                               f"(−{(1 - hp.price / prev[0]) * 100:.0f} %)")

            # 3) uusi alin botin näkemä hinta
            if len(all_hist) >= min_obs_for_low and hp.price < min(all_hist) - 0.005:
                key = f"low|{url}"
                if not alerted(c, key, hp.price):
                    reasons.append(f"🏆 Alin seurattu hinta (aiempi alin {eur(min(all_hist))})")
                    mark(c, key, hp.price)

            # 4) palasi varastoon
            if prev and prev[1] == 0 and hp.in_stock:
                reasons.append("📦 Palasi varastoon")

            if not reasons:
                continue

            # --- aitoustarkistus: oma historia + Hintaoppaan historia
            notes = []
            if hp.normal and hist30 and min(hist30) <= hp.price + 0.005:
                notes.append(f"⚠️ Kauppa näyttää norm. {eur(hp.normal)}, mutta hinta oli "
                             f"{eur(min(hist30))} jo viimeisen 30 pv aikana → ei aito ale")
            ho = ho_history_safe(prod, s)
            ho30 = ho_stats(ho, 30) if ho else None
            ho90 = ho_stats(ho, 90) if ho else None
            if ho30 and ho30["min"] < hp.price - 0.005:
                notes.append(f"⚠️ Hintaoppaan mukaan halvempi {eur(ho30['min'])} "
                             f"({ho30['min_shop']}, hinta voimassa {ho30['min_date']} alkaen) viim. 30 pv aikana")

            fields = [
                {"name": "Hinta", "value": f"**{eur(hp.price)}**", "inline": True},
                {"name": "Kaupan normaalihinta", "value": eur(hp.normal), "inline": True},
                {"name": "Varasto", "value": {True: "✅ on", False: "❌ loppu", None: "?"}[hp.in_stock],
                 "inline": True},
            ]
            if hist30:
                fields.append({"name": "Botti 30 pv alin / ylin",
                               "value": f"{eur(min(hist30))} / {eur(max(hist30))}", "inline": True})
            if ho90:
                fields.append({"name": "Hintaopas 90 pv alin / ylin",
                               "value": f"{eur(ho90['min'])} ({ho90['min_shop']}) / {eur(ho90['max'])}",
                               "inline": True})
            e = {
                "title": f"{name} – {store_name(url)}"[:250],
                "url": url,
                "description": "\n".join(reasons + notes),
                "color": 0xE67E22 if notes else 0x2ECC71,
                "fields": fields,
                "footer": {"text": (hp.title or "")[:200]},
            }
            prod_embeds.append(e)
            claude_items.append((e, {
                "tuote": name, "kauppa": store_name(url), "hinta_nyt": hp.price,
                "kaupan_normaalihinta": hp.normal, "tavoitehinta": target,
                "syyt": reasons, "botin_hintamuutokset_90pv": price_changes(c, url),
                "hintaopas_alin_30pv": ho30, "hintaopas_alin_90pv": ho90,
            }))

        # halvin kauppa, jos tuotteella on useampi URL
        if len(seen) > 1 and prod_embeds:
            best = min(seen, key=lambda x: x[1].price)
            for e in prod_embeds:
                e["fields"].append({"name": "Halvin nyt",
                                    "value": f"{store_name(best[0])} {eur(best[1].price)}",
                                    "inline": False})
        embeds += prod_embeds
        c.commit()

    # Claude Haiku kommentoi kaikki hälytykset yhdellä kutsulla
    if claude_items:
        data = [d for _, d in claude_items]
        prompt = ("Arvioi jokainen alla oleva hintahälytys: onko tarjous aito, ja kannattaako ostaa "
                  "nyt vai odottaa. Vastaa pelkkänä JSON-listana, jossa on yksi merkkijono "
                  "(max 250 merkkiä) kutakin hälytystä kohden samassa järjestyksessä.\n\n"
                  + json.dumps(data, ensure_ascii=False, default=str))
        ans = ask_claude(cfg, CLAUDE_SYSTEM, prompt, max_tokens=150 * len(data) + 200)
        comments = None
        if ans:
            m = re.search(r"\[.*\]", ans, re.S)
            try:
                comments = json.loads(m.group(0)) if m else None
            except Exception:
                comments = None
        if isinstance(comments, list):
            for (e, _), txt in zip(claude_items, comments):
                if txt:
                    e["fields"].append({"name": "🤖 Claude", "value": str(txt)[:1000], "inline": False})

    if embeds:
        send_discord(cfg["discord_webhook"], embeds, dry=dry)
    # virheistä ilmoitus vain kerran vuorokaudessa, ettei spämmää
    if errors:
        print("VIRHEET:\n  " + "\n  ".join(errors), file=sys.stderr)
        row = c.execute("SELECT v FROM meta WHERE k='last_err'").fetchone()
        if not row or datetime.now() - datetime.fromisoformat(row[0]) > timedelta(hours=24):
            send_discord(cfg["discord_webhook"], [{
                "title": "Tarjousbotti: hintaa ei saatu luettua",
                "description": "\n".join(errors)[:3900], "color": 0xE74C3C}], dry=dry)
            c.execute("INSERT OR REPLACE INTO meta VALUES('last_err', ?)",
                      (datetime.now().isoformat(timespec="seconds"),))
    c.commit()
    print(f"{now}: {sum(len(p.get('urlit', [])) for p in (cfg.get('tuotteet') or []))} URLia, "
          f"{len(embeds)} hälytystä, {len(errors)} virhettä")


def katsaus(cfg, dry=False):
    """Päivittäinen Claude-katsaus kaikista seurattavista tuotteista."""
    c = db()
    s = requests.Session()
    data = []
    for prod in (cfg.get("tuotteet") or []):
        kaupat = []
        for url in prod.get("urlit", []):
            row = last(c, url)
            if not row:
                continue
            h30 = [r[0] for r in history(c, url, 30)]
            normal = c.execute("SELECT normal FROM prices WHERE url=? ORDER BY ts DESC LIMIT 1",
                               (url,)).fetchone()[0]
            kaupat.append({
                "kauppa": store_name(url), "hinta_nyt": row[0], "varastossa": row[1],
                "kaupan_normaalihinta": normal,
                "botti_30pv_alin": min(h30) if h30 else None,
                "botti_30pv_ylin": max(h30) if h30 else None,
                "seurattu_alkaen": c.execute("SELECT MIN(ts) FROM prices WHERE url=?",
                                             (url,)).fetchone()[0],
                "hintamuutokset_90pv": price_changes(c, url),
            })
        if not kaupat:
            continue
        ho = ho_history_safe(prod, s)
        data.append({
            "tuote": prod["nimi"], "tavoitehinta": _num(prod.get("tavoitehinta")), "kaupat": kaupat,
            "hintaopas_30pv": ho_stats(ho, 30) if ho else None,
            "hintaopas_90pv": ho_stats(ho, 90) if ho else None,
        })
    if not data:
        print("Ei vielä dataa.")
        return

    bw = cfg.get("black_week", {})
    prompt = (
        f"Tänään on {date.today().isoformat()}. Black Week -jakso: {bw.get('alku')}–{bw.get('loppu')}.\n"
        "Tee päivittäinen katsaus seurattavista tuotteista Discordiin (max 1800 merkkiä). "
        "Kerro: 1) mitkä hinnat ovat nyt aidosti hyviä ja kannattaa ostaa, 2) epäilyttävät tapaukset "
        "(hintaa nostettu viime viikkoina, normaalihinta ei vastaa historiaa, Hintaoppaalla halvempi), "
        "3) mitkä kannattaa odottaa. Mainitse erityisesti ennen Black Weekiä tehdyt hinnankorotukset. "
        "Jos jossain ei ole mitään kerrottavaa, älä mainitse sitä. Käytä lyhyitä ranskalaisia "
        "viivoja ja euroja muodossa 12,90 €.\n\n" + json.dumps(data, ensure_ascii=False, default=str))
    text = ask_claude(cfg, CLAUDE_SYSTEM, prompt, max_tokens=1200)
    if not text:
        print("Claude ei vastannut (puuttuuko ANTHROPIC_API_KEY?) – lähetetään numerokoonti.")
        summary(cfg, dry=dry)
        return
    send_discord(cfg["discord_webhook"], [{
        "title": f"🤖 Hintakatsaus {date.today().strftime('%-d.%-m.')}",
        "description": text[:4000], "color": 0x9B59B6}], dry=dry)


def summary(cfg, dry=False):
    c = db()
    lines = []
    for prod in (cfg.get("tuotteet") or []):
        target = _num(prod.get("tavoitehinta"))
        parts = []
        for url in prod.get("urlit", []):
            row = last(c, url)
            if not row:
                continue
            h = [r[0] for r in history(c, url)]
            mark_ = " 🎯" if target and row[0] <= target else ""
            stock = "" if row[1] != 0 else " (loppu)"
            parts.append(f"[{store_name(url)}]({url}) {eur(row[0])}{mark_}{stock} · alin {eur(min(h))}")
        if parts:
            tgt = f" (tavoite {eur(target)})" if target else ""
            lines.append(f"**{prod['nimi']}**{tgt}\n" + "\n".join("  " + p for p in parts))
    if not lines:
        print("Ei vielä dataa.")
        return
    chunks, cur = [], ""
    for l in lines:
        if len(cur) + len(l) > 3800:
            chunks.append(cur)
            cur = ""
        cur += l + "\n\n"
    chunks.append(cur)
    embeds = [{"title": "Tarjousbotti – koonti" if i == 0 else "…jatkuu",
               "description": ch, "color": 0x3498DB} for i, ch in enumerate(chunks)]
    send_discord(cfg["discord_webhook"], embeds, dry=dry)


def list_prices(cfg):
    c = db()
    for prod in (cfg.get("tuotteet") or []):
        print(prod["nimi"])
        for url in prod.get("urlit", []):
            row = last(c, url)
            print(f"   {store_name(url):16} {eur(row[0]) if row else '–':>12}  {row[2] if row else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", metavar="URL")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--katsaus", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    a = ap.parse_args()

    if a.test:
        hp = fetch_price(a.test, requests.Session())
        print(f"Kauppa:   {store_name(a.test)}\nTuote:    {hp.title}\nHinta:    {eur(hp.price)}\n"
              f"Normaali: {eur(hp.normal)}\nVarasto:  {hp.in_stock}\nTapa:     {hp.source}")
        pid = hintaopas_id(a.test)
        if pid:
            items = fetch_hintaopas_history(pid, requests.Session())
            for d in (30, 90):
                st = ho_stats(items, d)
                if st:
                    print(f"Hintaopas {d} pv: alin {eur(st['min'])} ({st['min_shop']}, "
                          f"{st['min_date']}), ylin {eur(st['max'])}")
        return

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    cfg["discord_webhook"] = os.environ.get("TARJOUS_WEBHOOK") or cfg.get("discord_webhook", "")
    if a.list:
        list_prices(cfg)
    elif a.katsaus:
        katsaus(cfg, dry=a.dry_run)
    elif a.summary:
        summary(cfg, dry=a.dry_run)
    else:
        check(cfg, dry=a.dry_run, force=a.force)


if __name__ == "__main__":
    main()
