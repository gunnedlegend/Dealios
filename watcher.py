"""
Phone watcher: checks eBay.ca searches and WooCommerce products (like vippilot.com)
and sends a push notification to your phone through the free ntfy app.

Runs on GitHub Actions every few minutes (see .github/workflows/watch.yml).
Settings live in config.json; secrets come from GitHub repository secrets:
  NTFY_TOPIC          your private ntfy topic name (required)
  EBAY_CLIENT_ID      eBay App ID (only needed for eBay searches)
  EBAY_CLIENT_SECRET  eBay Cert ID (only needed for eBay searches)
Uses only the Python standard library, so there's nothing to install.
"""
import base64
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
STATE_PATH = BASE / "state.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

NTFY_SERVER = CONFIG.get("ntfy_server", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()


# ---------------------------------------------------------------- helpers
def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})},
                                 method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def notify(title, message, click=None, image=None, tags=None, priority=4):
    payload = {"topic": NTFY_TOPIC, "title": title[:250], "message": message[:3500],
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
        payload["actions"] = [{"action": "view", "label": "Open", "url": click}]
    if image:
        payload["attach"] = image
    http(NTFY_SERVER, data=json.dumps(payload).encode("utf-8"),
         headers={"Content-Type": "application/json"}, method="POST")
    print(f"  -> notified: {title}")


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- eBay
def ebay_token():
    cid, secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if not cid or not secret:
        raise RuntimeError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET secrets are not set")
    creds = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
    data = json.loads(http("https://api.ebay.com/identity/v1/oauth2/token", data=body, headers={
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/x-www-form-urlencoded"}))
    return data["access_token"]


def ebay_params(search):
    """Build Browse API params from an ebay.ca search URL (or a plain 'query')."""
    params = {"sort": "newlyListed", "limit": "50"}
    filters = []
    if search.get("url"):
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(search["url"]).query).items()}
        if qs.get("_nkw"):
            params["q"] = qs["_nkw"]
        if qs.get("_sacat") and qs["_sacat"] != "0":
            params["category_ids"] = qs["_sacat"]
        lo, hi = qs.get("_udlo", ""), qs.get("_udhi", "")
        if lo or hi:
            filters += [f"price:[{lo}..{hi}]", "priceCurrency:CAD"]
        bin_, auc = qs.get("LH_BIN") == "1", qs.get("LH_Auction") == "1"
        if bin_ and not auc:
            filters.append("buyingOptions:{FIXED_PRICE}")
        elif auc and not bin_:
            filters.append("buyingOptions:{AUCTION}")
        if qs.get("LH_ItemCondition"):
            filters.append("conditionIds:{" + qs["LH_ItemCondition"].replace(",", "|") + "}")
        if qs.get("LH_PrefLoc") == "1":
            filters.append("itemLocationCountry:CA")
    if search.get("query"):
        params["q"] = search["query"]
    if filters:
        params["filter"] = ",".join(filters)
    if "q" not in params and "category_ids" not in params:
        raise ValueError(f"eBay search '{search['name']}' needs keywords or a category")
    return params


def check_ebay(state):
    searches = CONFIG.get("ebay_searches", [])
    if not searches:
        return
    token = ebay_token()
    seen_all = state.setdefault("ebay", {})
    for s in searches:
        name = s["name"]
        try:
            url = ("https://api.ebay.com/buy/browse/v1/item_summary/search?"
                   + urllib.parse.urlencode(ebay_params(s)))
            data = json.loads(http(url, headers={"Authorization": f"Bearer {token}",
                                                 "X-EBAY-C-MARKETPLACE-ID": "EBAY_CA"}))
        except Exception as e:
            print(f"eBay '{name}' failed: {e}")
            continue
        items = data.get("itemSummaries", [])
        old = seen_all.get(name)
        seen = set(old or [])
        new = [i for i in items if i.get("itemId") and i["itemId"] not in seen]
        if old is None:
            print(f"eBay '{name}': first run, remembering {len(items)} listings")
        else:
            print(f"eBay '{name}': {len(new)} new")
            for item in reversed(new):
                price = item.get("price") or item.get("currentBidPrice") or {}
                bits = [f"{price.get('value')} {price.get('currency', '')}".strip()] if price else []
                if item.get("condition"):
                    bits.append(item["condition"])
                if item.get("buyingOptions"):
                    bits.append(", ".join(item["buyingOptions"]).replace("FIXED_PRICE", "Buy It Now").title())
                notify(f"New eBay listing: {name}",
                       f"{item.get('title', '')}\n" + " · ".join(bits),
                       click=item.get("itemWebUrl"),
                       image=(item.get("image") or {}).get("imageUrl"),
                       tags=["shopping_cart"])
        seen_all[name] = ([i["itemId"] for i in new] + (old or []))[:1000]


# ---------------------------------------------------------------- WooCommerce
def woo_product(product_url):
    parts = urllib.parse.urlparse(product_url)
    slug = [p for p in parts.path.split("/") if p][-1]
    api = (f"{parts.scheme}://{parts.netloc}/wp-json/wc/store/v1/products?"
           + urllib.parse.urlencode({"slug": slug}))
    data = json.loads(http(api))
    if not data:
        raise RuntimeError(f"no product found for '{slug}'")
    p = data[0]
    prices = p.get("prices") or {}
    unit = 10 ** int(prices.get("currency_minor_unit", 2))
    rng = prices.get("price_range") or {}
    return {
        "name": html.unescape(p.get("name", "")),
        "on_sale": bool(p.get("on_sale")),
        "min": int(rng.get("min_amount") or prices.get("price") or 0) / unit,
        "max": int(rng.get("max_amount") or prices.get("price") or 0) / unit,
        "regular": int(prices.get("regular_price") or 0) / unit,
        "currency": prices.get("currency_code", ""),
        "in_stock": bool(p.get("is_in_stock")),
        "image": ((p.get("images") or [{}])[0]).get("src"),
    }


def fmt_range(p):
    if abs(p["min"] - p["max"]) < 0.005:
        return f"${p['min']:.2f} {p['currency']}"
    return f"${p['min']:.2f}–${p['max']:.2f} {p['currency']}"


def check_woo(state):
    store = state.setdefault("products", {})
    for w in CONFIG.get("product_watches", []):
        name = w["name"]
        try:
            cur = woo_product(w["url"])
        except Exception as e:
            print(f"Product '{name}' failed: {e}")
            continue
        prev = store.get(name)
        store[name] = cur
        if not prev:
            print(f"Product '{name}': first run, {fmt_range(cur)}, on_sale={cur['on_sale']}")
            continue
        reasons = []
        if cur["on_sale"] and not prev.get("on_sale"):
            reasons.append("Now ON SALE")
        if cur["min"] < prev["min"] - 0.005 or cur["max"] < prev["max"] - 0.005:
            reasons.append(f"Price dropped: {fmt_range(prev)} → {fmt_range(cur)}")
        if w.get("alert_back_in_stock", True) and cur["in_stock"] and not prev.get("in_stock"):
            reasons.append("Back in stock")
        print(f"Product '{name}': {fmt_range(cur)}, changes: {reasons or 'none'}")
        if reasons:
            notify(f"Sale alert: {cur['name'] or name}",
                   "\n".join(reasons) + f"\nNow: {fmt_range(cur)}",
                   click=w["url"], image=cur.get("image"), tags=["moneybag"], priority=5)


# ---------------------------------------------------------------- main
def main():
    if not NTFY_TOPIC:
        sys.exit("NTFY_TOPIC secret is not set")
    if os.environ.get("TEST_NOTIFY", "").lower() == "true":
        notify("Watcher test", "It works! You'll get alerts like this one.", tags=["tada"])
    state = load_state()
    for check in (check_ebay, check_woo):
        try:
            check(state)
        except Exception as e:
            print(f"{check.__name__} failed: {e}")
    save_state(state)


if __name__ == "__main__":
    main()
