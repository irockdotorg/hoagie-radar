#!/usr/bin/env python3
"""hoagie.radar — find sandwich shops near you, in your local slang."""
import argparse, json, math, os, re, time, urllib.parse, urllib.request
import html as _html
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, Response

UA = "hoagie-radar/1.0 (local app)"
CACHE_TTL = 300          # seconds — keeps public traffic off the rate-limited APIs
_cache = {}
_cache_lock = None

def cache_get(key):
    import threading
    global _cache_lock
    if _cache_lock is None: _cache_lock = threading.Lock()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < CACHE_TTL:
            return hit[1]
        _cache.pop(key, None)
    return None

def cache_put(key, val):
    import threading
    global _cache_lock
    if _cache_lock is None: _cache_lock = threading.Lock()
    with _cache_lock:
        if len(_cache) > 400:            # bounded — drop oldest
            for k in list(_cache.keys())[:200]:
                _cache.pop(k, None)
        _cache[key] = (time.time(), val)
OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_MIRROR = "https://overpass.kumi.systems/api/interpreter"
OVERPASS_MIRROR2 = "https://overpass.private.coffee/api/interpreter"
NOMINATIM = "https://nominatim.openstreetmap.org"
PHOTON = "https://photon.komoot.io/api/"

# --- Regional slang ---------------------------------------------------------
REGION_SLANG = {
    "philadelphia": ["hoagie"], "pittsburgh": ["hoagie"],
    "new york": ["hero"], "brooklyn": ["hero"], "queens": ["hero"],
    "bronx": ["hero"], "staten island": ["hero"], "manhattan": ["hero"],
    "boston": ["grinder"], "cambridge": ["grinder"], "somerville": ["grinder"],
    "new england": ["grinder"], "hartford": ["grinder"], "providence": ["grinder"],
    "worcester": ["grinder"], "springfield": ["grinder"], "main": ["grinder"],
    "new orleans": ["po'boy", "po boy"], "louisiana": ["po'boy"],
    "baton rouge": ["po'boy"], "maine": ["italian sandwich"], "portland": ["italian sandwich"],
    "connecticut": ["grinder"], "rhode island": ["grinder"],
    "vermont": ["grinder"], "new hampshire": ["grinder"],
}
ZIP_PREFIX_SLANG = {
    "19": "hoagie",   # PA southeast (Philly, Bucks, Montco, DE Co)
    "15": "hoagie",   # Pittsburgh
    "10": "hero", "11": "hero",  # NYC boroughs + Long Island
    "02": "grinder", "06": "grinder",  # MA + CT
    "70": "po'boy", "71": "po'boy",    # Louisiana
    "04": "italian sandwich",          # Maine
}

def slang_for(lat, lon, city="", state="", zipc=""):
    """Return (word, alt_words) for the local sandwich slang."""
    z = str(zipc or "")[:2]
    if z in ZIP_PREFIX_SLANG:
        word = ZIP_PREFIX_SLANG[z]
    else:
        loc = f"{city} {state}".lower()
        word = None
        for k, v in REGION_SLANG.items():
            if k in loc:
                word = v[0]; break
        if not word:
            word = "sub"
    alts = ["sub", "sandwich"]
    for a in ("hoagie", "hero", "grinder", "po'boy", "italian sandwich"):
        if a != word and a not in alts:
            alts.append(a)
    return word, alts[:5]

# --- Sandwich type classification --------------------------------------------
# Substring patterns (matched against "name + cuisine", lowercased) that tag a
# result with one or more sandwich types for the front-end filter chips.
TYPE_PATTERNS = {
    "cheesesteak": ("cheesesteak", "cheese steak", "whiz wit", "steak sandwich",
                    "steak hoagie", "pat's", "geno's", "dalessandro", "ishkabibble",
                    "campo", "cosmi", "joe's steaks", "chubby's", "prince of steaks"),
    "italian": ("italian",),
    "roast_pork": ("roast pork", "roast-pork", "roasted pork", "tony luke",
                   "john's roast pork"),
    "roast_beef": ("roast beef", "roast-beef", "roastbeef", "french dip", "au jus"),
    "pastrami": ("pastrami", "kosher", "jewish deli", "corned beef", "reuben",
                 "katz's", "2nd ave deli", "carnegie"),
    "kosher_deli": ("kosher deli", "kosher-style deli", "kosher style deli",
                    "4th st deli", "fourth st deli", "schleisinger", "schleinger",
                    "4th street deli", "kosher"),
    "turkey": ("turkey",),
    "meatball": ("meatball",),
    "breakfast": ("breakfast", "egg sandwich", "egg hoagie", "pork roll",
                  "taylor ham", "scrapple", "bacon, egg", "sausage sandwich"),
    "veggie": ("vegan", "vegetarian", "veggie", "meatless", "garden sandwich"),
    "cold_cut": ("hoagie", "cold cut", "cold-cut", "deli", "sandwich", "sub shop",
                 "wawa", "sheetz", "submarine"),
}

def classify_sandwich(name, cuisine):
    """Tag a shop with the sandwich types it likely sells."""
    s = f"{name or ''} {cuisine or ''}".lower()
    out = [k for k, pats in TYPE_PATTERNS.items() if any(p in s for p in pats)]
    return out or ["cold_cut"]

# Per-type Nominatim search terms (fallback data source).
TYPE_TERMS = {
    "cheesesteak": ["cheesesteak", "cheesesteak hoagie", "chicken cheesesteak"],
    "italian": ["italian hoagie", "italian deli", "italian sandwich"],
    "roast_pork": ["roast pork sandwich", "roast pork italian"],
    "pastrami": ["pastrami", "kosher deli", "corned beef", "reuben"],
    "kosher_deli": ["kosher deli", "kosher style deli", "jewish deli",
                    "4th street deli", "schleisinger"],
    "roast_beef": ["roast beef sandwich", "roast beef deli"],
    "turkey": ["turkey sandwich", "turkey hoagie", "deli turkey"],
    "meatball": ["meatball sandwich", "meatball hoagie"],
    "breakfast": ["breakfast sandwich", "breakfast hoagie", "egg sandwich"],
    "veggie": ["vegan sandwich", "vegan deli", "vegetarian sandwich"],
    "cold_cut": ["hoagie shop", "deli sandwich", "sub sandwich"],
}
DEFAULT_TERMS = ("hoagie shop", "hoagie deli", "deli restaurant", "sandwich restaurant",
                 "sandwich shop", "cheesesteak", "hoagies", "italian deli", "sub shop",
                 "pastrami", "roast beef sandwich", "kosher deli", "corned beef sandwich")

# --- HTTP helper ------------------------------------------------------------
def fetch(url, data=None, headers=None, timeout=25):
    h = {"User-Agent": UA}
    if headers: h.update(headers)
    if data is not None:
        data = data.encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

# --- Geocoding --------------------------------------------------------------
def reverse_geocode(lat, lon):
    try:
        raw = fetch(f"{NOMINATIM}/reverse?format=jsonv2&lat={lat}&lon={lon}&zoom=16")
        d = json.loads(raw); a = d.get("address", {})
        return {
            "display": d.get("display_name", ""),
            "city": a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("suburb") or "",
            "county": a.get("county", ""),
            "state": a.get("state", ""),
            "postcode": a.get("postcode", ""),
        }
    except Exception:
        return {"display": "", "city": "", "county": "", "state": "", "postcode": ""}

def ip_geolocate():
    """Best-effort city-level fallback when the browser denies geolocation."""
    for url in ("https://ipapi.co/json/", "https://ipinfo.io/json"):
        try:
            d = json.loads(fetch(url, timeout=10))
            if "latitude" in d and "longitude" in d:
                return float(d["latitude"]), float(d["longitude"]), {
                    "display": f"{d.get('city','')}, {d.get('region','')} (from IP)",
                    "city": d.get("city", ""), "county": d.get("region", ""),
                    "state": d.get("region", ""), "postcode": d.get("postal", ""),
                    "ip_based": True}
            if "loc" in d:
                lat, lon = d["loc"].split(",")
                return float(lat), float(lon), {
                    "display": f"{d.get('city','')}, {d.get('region','')} (from IP)",
                    "city": d.get("city", ""), "county": d.get("region", ""),
                    "state": d.get("region", ""), "postcode": d.get("postal", ""),
                    "ip_based": True}
        except Exception:
            continue
    return None

# --- Distance ---------------------------------------------------------------
def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

# --- Overpass: the real POI source ------------------------------------------
def overpass_query(query, timeout=40):
    data = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for ep in (OVERPASS, OVERPASS_MIRROR):
        try:
            raw = fetch(ep, data=data, timeout=timeout)
            d = json.loads(raw)
            if isinstance(d, dict) and "elements" in d:
                return d["elements"]
        except Exception as e:
            last = e
    raise last or RuntimeError("overpass failed")

def _sandwich_filter():
    # things that very likely sell sandwiches, matched broadly then narrowed
    return """
      nwr["shop"="deli"](around:{r},{lat},{lon});
      nwr["cuisine"~"sandwich|hoagie|sub|cheesesteak|italian|submarine|deli",i](around:{r},{lat},{lon});
      nwr["name"~"hoagie|deli|sandwich|cheesesteak|cheese steak|steak|hero|grinder|po.?boy|pastrami|kosher|roast beef|roast pork|reuben|corned beef|wawa|sheetz|jimmy john|jersey mike|potbelly|quiznos|blimpie|firehouse|subway|panera|primo hoagies|cosmi|fresk|carangi|ishkabibble|pat's|geno's|john's roast pork|dalessandro|joe's steaks|angelo's|tony luke|chubby's|campo|capriotti|gordon biersch|katz|carnegie",i]["amenity"~"restaurant|fast_food|cafe|bar|pub|ice_cream"](around:{r},{lat},{lon});
      nwr["name"~"wawa|sheetz|quick chek|royal farms|getgo|turkey hill|rutters",i](around:{r},{lat},{lon});
    """

def search_overpass(lat, lon, radius):
    q = "[out:json][timeout:30];(" + _sandwich_filter().format(r=radius, lat=lat, lon=lon) + ");out center tags;"
    els = overpass_query(q)
    return els

def search_nominatim(lat, lon, radius, stype=None):
    """Fallback POI source. Nominatim enforces a strict 1 req/sec policy and
    returns 429 when we exceed it — keep this to a few high-value terms and
    space them out so we never trip the limiter."""
    dlat = radius / 111000.0
    dlon = radius / (111000.0 * max(0.1, math.cos(math.radians(lat))))
    viewbox = f"{lon-dlon},{lat+dlat},{lon+dlon},{lat-dlat}"
    out, seen = [], set()
    terms = TYPE_TERMS.get(stype) if stype else None
    # default: a compact set of the highest-yield terms (each covers many shops)
    terms = terms or ["cheesesteak", "deli", "hoagie", "sandwich shop"]
    for term in terms[:5]:
        for attempt in range(3):
            try:
                raw = fetch(f"{NOMINATIM}/search?format=jsonv2&limit=30&addressdetails=1&extratags=1"
                            f"&q={urllib.parse.quote(term)}&viewbox={viewbox}&bounded=1", timeout=25)
                for r in json.loads(raw):
                    key = (r.get("osm_type"), r.get("osm_id"))
                    if key in seen: continue
                    seen.add(key)
                    r["_center"] = (float(r["lat"]), float(r["lon"]))
                    out.append(r)
                break  # success → next term
            except Exception:
                # likely a 429; back off hard before retrying
                time.sleep(2.0 * (attempt + 1))
                continue
        time.sleep(1.2)  # respect Nominatim's 1 req/sec between terms
    return out

def search_photon(lat, lon, radius, stype=None):
    """Photon (Komoot) OSM geocoder — free, keyless, far more forgiving of
    request volume than Nominatim. Used as the resilient fallback source."""
    terms = TYPE_TERMS.get(stype) if stype else None
    terms = terms or ["cheesesteak", "deli", "hoagie", "sandwich", "pastrami"]
    out, seen = [], set()
    for term in terms[:6]:
        try:
            url = (f"{PHOTON}?q={urllib.parse.quote(term)}&lat={lat}&lon={lon}"
                   f"&limit=25&radius={int(radius/1000)}")
            d = json.loads(fetch(url, timeout=20))
            for f in d.get("features", []):
                p = f.get("properties", {}) or {}
                # only food POIs with a name
                if not p.get("name"): continue
                if p.get("osm_value") not in ("restaurant", "fast_food", "cafe",
                                              "bar", "pub", "ice_cream"): continue
                g = f.get("geometry", {}).get("coordinates", [])
                if len(g) < 2: continue
                key = (p.get("osm_type"), p.get("osm_id"))
                if key in seen: continue
                seen.add(key)
                # shape it like a Nominatim result so normalize_osm handles it
                out.append({
                    "name": p.get("name"),
                    "lat": g[1], "lon": g[0],
                    "type": p.get("osm_value"),
                    "extratags": {k: p.get(k) for k in ("cuisine", "phone",
                                 "website", "opening_hours") if p.get(k)},
                    "address": p,
                })
        except Exception:
            continue
    return out

def _el_center(el):
    if "center" in el:
        return el["center"]["lat"], el["center"]["lon"]
    return el.get("lat"), el.get("lon")

def _clean_tags(t):
    return {k: v for k, v in t.items() if k in (
        "name", "cuisine", "shop", "amenity", "opening_hours", "phone", "website",
        "addr:housenumber", "addr:street", "addr:city", "addr:state", "addr:postcode",
        "brand", "diet:vegetarian", "outdoor_seating", "takeaway", "delivery")}

def normalize_osm(elements, lat, lon, city, state, zipc, source):
    slang, alts = slang_for(lat, lon, city, state, zipc)
    results, seen = [], set()
    for el in elements:
        t = el.get("tags", {}) if "tags" in el else {}
        et = el.get("extratags") or {}
        # Overpass puts name in tags; Nominatim fallback puts it at top level.
        name = t.get("name") or t.get("addr:housenumber") or el.get("name")
        cuisine = t.get("cuisine") or et.get("cuisine") or ""
        hours = t.get("opening_hours") or et.get("opening_hours") or ""
        # For the fallback sources, keep only real food POIs with a name —
        # skip unnamed convenience/bodega clutter that doesn't make sandwiches.
        if source in ("nominatim", "photon"):
            nm = (name or "").lower()
            if not name or not nm.strip():
                continue
            junk = ("grocery", "market", "bodega", "mini mart", "minimart", "convenience",
                    "liquor", "beer", "wine", "grocery store", "food store", "corner store",
                    "supermarket", "candy", "smoke", "tobacco", "gas", "laundromat", "dollar",
                    "family dollar", "pharmacy", "hardware", "auto", "nails", "hair", "barber")
            if any(j in nm for j in junk):
                continue
            typ = (t.get("type") or el.get("type") or "").lower()
            if typ in ("house", "residential", "administrative", "postcode", "road",
                       "neighbourhood", "suburb", "city", "town"):
                continue
        c = el.get("_center") or _el_center(el)
        if not c or c[0] is None: continue
        clat, clon = c
        d = haversine_m(lat, lon, clat, clon)
        key = (name or "", round(clat, 5), round(clon, 5))
        if key in seen: continue
        seen.add(key)
        addr = ", ".join(x for x in (t.get("addr:housenumber","") + " " + t.get("addr:street","") if t.get("addr:street") else "", t.get("addr:city",""), t.get("addr:state","")) if x).strip()
        if not addr and isinstance(el.get("address"), dict):
            a = el["address"]
            addr = ", ".join(x for x in (a.get("house_number",""), a.get("road",""),
                                         a.get("city",""), a.get("state","")) if x)
        results.append({
            "name": name or "(unnamed shop)",
            "distance_m": round(d),
            "lat": clat, "lon": clon,
            "type": t.get("shop") or t.get("amenity") or el.get("type") or "shop",
            "cuisine": cuisine,
            "address": addr,
            "phone": t.get("phone") or et.get("phone") or "",
            "website": t.get("website") or et.get("website") or "",
            "hours": hours,
            "takeaway": t.get("takeaway") or et.get("takeaway") or "",
            "delivery": t.get("delivery") or et.get("delivery") or "",
            "types": classify_sandwich(name, cuisine),
            "tags": _clean_tags(t),
            "source": source,
            "slang": slang,
        })
    results.sort(key=lambda r: r["distance_m"])
    return results, slang, alts

# --- Google / Yelp ----------------------------------------------------------
def google_places(lat, lon, slang, api_key):
    """Live Google Places results when a key is provided."""
    if not api_key: return None
    try:
        url = ("https://maps.googleapis.com/maps/api/place/nearbysearch/json"
               f"?location={lat},{lon}&rankby=distance&keyword={urllib.parse.quote(slang)}&key={api_key}")
        d = json.loads(fetch(url, timeout=20))
        out = []
        for p in d.get("results", []):
            loc = p.get("geometry", {}).get("location", {})
            out.append({
                "name": p.get("name"),
                "address": p.get("vicinity", ""),
                "lat": loc.get("lat"), "lon": loc.get("lng"),
                "rating": p.get("rating"), "reviews": p.get("user_ratings_total"),
                "open_now": p.get("opening_hours", {}).get("open_now"),
                "price": p.get("price_level"),
                "source": "google", "slang": slang,
            })
        return out
    except Exception:
        return None

def yelp_results(lat, lon, slang, api_key):
    """Live Yelp results when a key is provided."""
    if not api_key: return None
    try:
        url = ("https://api.yelp.com/v3/businesses/search"
               f"?latitude={lat}&longitude={lon}&term={urllib.parse.quote(slang)}&sort_by=distance&limit=20")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        out = []
        for b in d.get("businesses", []):
            out.append({
                "name": b.get("name"),
                "address": ", ".join(b.get("location", {}).get("display_address", [])),
                "lat": b.get("coordinates", {}).get("latitude"),
                "lon": b.get("coordinates", {}).get("longitude"),
                "rating": b.get("rating"), "reviews": b.get("review_count"),
                "price": b.get("price"),
                "source": "yelp", "slang": slang,
            })
        return out
    except Exception:
        return None

def external_links(lat, lon, slang):
    """Always-available Google + Yelp search links (no key required)."""
    q = urllib.parse.quote(f"{slang} near me")
    ll = f"{lat},{lon}"
    return {
        "google": f"https://www.google.com/maps/search/{q}/@{lat},{lon},14z",
        "yelp": f"https://www.yelp.com/search?find_desc={q}&l=g%3A{ll}&request_id=x",
        "google_search": f"https://www.google.com/search?q={q}%20near%20{ll}",
        "yelp_search": f"https://www.yelp.com/search?find_desc={q}&find_loc=Current%20Location",
    }

# --- Menu fetching & parsing (inline, no API keys) ---------------------------
# Priority: shop's own website → Yelp → Google → Grubhub. Returns whatever
# structured items it can scrape; each source reports honestly in `via`.
MENU_TAGS = ("sandwich", "hoagie", "sub", "italian", "cheesesteak", "cheese steak",
             "roast pork", "roast beef", "pastrami", "corned beef", "reuben", "turkey",
             "meatball", "club", "wrap", "muffuletta",
             "breakfast", "egg", "blt", "cheeseburger", "burger", "cheese", "wheat", "rye",
             "grilled", "cold", "hot", "vegan", "veggie", "buffalo", "chicken",
             "bacon", "ham", "salami", "prosciutto", "capicola")

def _fetch_text(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type", "")
            if "text" not in ct and "json" not in ct and "html" not in ct:
                return None
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None

def _strip_tags(html):
    txt = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = _html.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()

def _menu_from_jsonld(html):
    """Extract a menu from schema.org JSON-LD (Restaurant/Menu/MenuItem)."""
    items, blobs = [], re.findall(r'<script[^>]+application/ld\+json[^>]*>([\s\S]*?)</script>',
                                  html, flags=re.I)
    def walk(o, seen_ids):
        if isinstance(o, list):
            for x in o: walk(x, seen_ids)
        elif isinstance(o, dict):
            if id(o) in seen_ids: return          # avoid double-counting shared nodes
            seen_ids.add(id(o))
            t = (o.get("@type") or "")
            types = t if isinstance(t, list) else [t]
            if any(str(x).lower() in ("menuitem",) for x in types):
                nm = o.get("name")
                pr = o.get("offers", {})
                price = None
                if isinstance(pr, dict): price = pr.get("price")
                if nm:
                    entry = {"name": str(nm).strip(), "price": str(price).strip() if price else ""}
                    if entry not in items:
                        items.append(entry)
            hm = o.get("hasMenuItem") or o.get("hasMenuSection") or o.get("hasMenu")
            if hm: walk(hm, seen_ids)
            for v in o.values():
                if isinstance(v, (dict, list)): walk(v, seen_ids)
    for b in blobs:
        try: walk(json.loads(b), set())
        except Exception: pass
    return items

def _menu_from_pricelines(text):
    """Extract 'name $price' pairs. Menus flatten to one line after tag-stripping,
    so anchor on the price token: each '$X.XX' closes the current item, and the
    text after it up to the next price is the next item's name."""
    items, seen = [], set()
    # find all price matches; the text between the previous price and this one
    # is the item's name
    matches = list(re.finditer(r"\$\s?(\d{1,3}(?:\.\d{1,2})?)", text))
    prev_end = 0
    for m in matches:
        price = m.group(0).replace(" ", "")
        name = text[prev_end:m.start()].strip()
        prev_end = m.end()
        # cut the name at the last sentence/pipe boundary so we don't swallow
        # the previous item's trailing description
        for sep in (". ", " | ", " · ", "! ", "? "):
            idx = name.rfind(sep)
            if idx != -1:
                name = name[idx+len(sep):]
        name = name.strip(" .·–-|,\t")[:70]
        low = name.lower()
        if len(name) < 4 or low in seen: continue
        if not any(k in low for k in MENU_TAGS): continue
        seen.add(low)
        items.append({"name": name, "price": price})
        if len(items) >= 25: break
    return items

def fetch_menu_from_site(website):
    if not website: return []
    urls = [website if website.startswith("http") else "https://" + website]
    for u in ("menu", "our-menu", "food", "eat"):
        urls.append(urls[0].rstrip("/") + "/" + u)
    for url in urls[:4]:
        h = _fetch_text(url)
        if not h: continue
        items = _menu_from_jsonld(h)
        if items: return items
        items = _menu_from_pricelines(_strip_tags(h))
        if items: return items
    return []

def fetch_menu_yelp(name, city=""):
    """Yelp is JS/bot-walled for direct scraping; return a usable link payload."""
    return {"items": [], "via": "yelp",
            "url": f"https://www.yelp.com/search?find_desc={urllib.parse.quote((name+' menu').strip())}"}

def fetch_menu_google(name, city=""):
    q = urllib.parse.quote(f"{name} menu {city}".strip())
    return {"items": [], "via": "google",
            "url": f"https://www.google.com/search?q={q}"}

def fetch_menu_grubhub(name, city=""):
    q = urllib.parse.quote(name)
    return {"items": [], "via": "grubhub",
            "url": f"https://www.grubhub.com/search?query={q}"}

def gather_menu(shop):
    """Fill a shop's menu from the best source available. Never blocks long."""
    if shop.get("menu"): return shop
    def work():
        site = shop.get("website") or ""
        if site:
            items = fetch_menu_from_site(site)
            if items:
                shop["menu"] = items[:25]; shop["menu_via"] = "site"; return
        shop["menu_links"] = {
            "yelp": fetch_menu_yelp(shop.get("name",""), shop.get("_city","")),
            "google": fetch_menu_google(shop.get("name",""), shop.get("_city","")),
            "grubhub": fetch_menu_grubhub(shop.get("name",""), shop.get("_city","")),
        }
    try:
        work()
    except Exception:
        shop.setdefault("menu", [])
        shop.setdefault("menu_links", {})
    return shop

def prefetch_menus(results, limit=8, timeout=6):
    """Warm menus for the nearest few shops in parallel (best-effort)."""
    def one(r):
        try: gather_menu(r)
        except Exception: pass
    with ThreadPoolExecutor(max_workers=min(4, max(1, limit))) as ex:
        list(ex.map(one, results[:limit], timeout=timeout))
    return results

# --- Known iconic shops OSM often misses ------------------------------------
# OpenStreetMap doesn't map several famous cheesesteak joints as named POIs,
# so they never surface from any OSM query. Keep a small curated seed (real
# coords + types) that merges into results, deduped against what OSM returns.
CURATED_SHOPS = {
    # keyed loosely by city; matched on proximity so it works anywhere nearby
    "philadelphia": [
        {"name": "Pat's King of Steaks", "lat": 39.9338, "lon": -75.1594, "types": ["cheesesteak"], "cuisine": "cheesesteak"},
        {"name": "Geno's Steaks", "lat": 39.9336, "lon": -75.1591, "types": ["cheesesteak"], "cuisine": "cheesesteak"},
        {"name": "Jim's South St (Jim's Steaks)", "lat": 39.9416, "lon": -75.1506, "types": ["cheesesteak"], "cuisine": "cheesesteak"},
        {"name": "Dalessandro's Steaks", "lat": 40.0343, "lon": -75.2099, "types": ["cheesesteak"], "cuisine": "cheesesteak"},
        {"name": "John's Roast Pork", "lat": 39.9265, "lon": -75.1469, "types": ["roast_pork", "cheesesteak"], "cuisine": "roast pork"},
        {"name": "4th St Deli", "lat": 39.9469, "lon": -75.1496, "types": ["kosher_deli", "pastrami"], "cuisine": "kosher deli"},
        {"name": "Schleisinger's", "lat": 39.9498, "lon": -75.1650, "types": ["kosher_deli", "pastrami"], "cuisine": "kosher deli"},
    ],
}

def curated_for(lat, lon, radius):
    """Return curated shops within radius of the point (rough city match)."""
    hits = []
    for city, shops in CURATED_SHOPS.items():
        for s in shops:
            d = haversine_m(lat, lon, s["lat"], s["lon"])
            if d <= radius:
                hits.append({**s, "distance_m": round(d), "source": "curated",
                             "type": "restaurant", "address": "", "phone": "",
                             "website": "", "hours": "", "takeaway": "",
                             "delivery": "", "slang": "hoagie"})
    return hits

# --- Flask app ---------------------------------------------------------------
app = Flask(__name__)

GOOGLE_KEY = os.environ.get("GOOGLE_PLACES_KEY", "")
YELP_KEY = os.environ.get("YELP_API_KEY", "")

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")

@app.route("/api/radar", methods=["POST"])
def radar():
    body = request.get_json(silent=True) or {}
    try:
        radius = float(body.get("radius", 4000))
    except (TypeError, ValueError):
        radius = 4000
    radius = max(500, min(radius, 15000))

    stype = (body.get("type") or "").lower().strip() or None
    if stype and stype not in TYPE_TERMS:
        stype = None

    # parse coords first (needed for the cache key)
    have_coords = False
    lat = lon = None
    try:
        lat = float(body.get("lat"))
        lon = float(body.get("lon"))
        have_coords = True
    except (TypeError, ValueError):
        pass

    # Serve from cache BEFORE any network call. When coords are given we can
    # key on them directly; without coords we fall back to IP (rare path).
    if have_coords:
        _cache_key = f"radar:{round(lat,3)},{round(lon,3)}:{int(radius)}:{stype or 'all'}"
        cached = cache_get(_cache_key)
        if cached is not None:
            return jsonify(cached)

    # cache miss — resolve coords/label, then query OSM
    geo = {}
    if have_coords:
        geo = reverse_geocode(lat, lon)
    else:
        g = ip_geolocate()
        if not g:
            return jsonify({"error": "need coordinates and IP fallback failed"}), 400
        lat, lon, geo = g

    slang, alts = slang_for(lat, lon, geo.get("city",""), geo.get("state",""), geo.get("postcode",""))

    osm, source = [], "overpass"
    try:
        osm = search_overpass(lat, lon, radius)
    except Exception:
        # Overpass mirrors down → try Photon (resilient, keyless) first,
        # then Nominatim (which rate-limits aggressively).
        source = "photon"
        osm = search_photon(lat, lon, radius, stype)
        if not osm:
            source = "nominatim"
            try:
                osm = search_nominatim(lat, lon, radius, stype)
            except Exception as e:
                return jsonify({"error": f"map data unavailable: {e}"}), 502
        if not osm:
            return jsonify({"error": "map data unavailable: all OSM sources busy"}), 502

    results, slang, alts = normalize_osm(osm, lat, lon, geo.get("city",""), geo.get("state",""), geo.get("postcode",""), source)

    # merge curated iconic shops that OSM misses, deduped by name
    existing = {(r["name"] or "").lower() for r in results}
    for c in curated_for(lat, lon, radius):
        if c["name"].lower() not in existing:
            results.append(c)
    results.sort(key=lambda r: r["distance_m"])

    # warm menus for the nearest few shops (best-effort, bounded)
    for r in results:
        r["_city"] = geo.get("city", "")
    try:
        prefetch_menus(results, limit=6)
    except Exception:
        pass

    google = google_places(lat, lon, slang, GOOGLE_KEY) or []
    yelp = yelp_results(lat, lon, slang, YELP_KEY) or []
    links = external_links(lat, lon, slang)

    payload = {
        "location": {"lat": lat, "lon": lon, **geo},
        "slang": slang, "alts": alts,
        "source": source,
        "type": stype,
        "count": len(results),
        "results": results[:60],
        "google": google, "yelp": yelp,
        "links": links,
        "api_keys": {"google": bool(GOOGLE_KEY), "yelp": bool(YELP_KEY)},
    }
    cache_put(_cache_key, payload)
    return jsonify(payload)

@app.route("/api/geocode", methods=["POST"])
def geocode():
    """Turn free text (address, ZIP, place) into coordinates.
    Tries Photon (keyless, forgiving) then Nominatim."""
    body = request.get_json(silent=True) or {}
    q = (body.get("q") or "").strip()
    if not q:
        return jsonify({"error": "empty query"}), 400
    # bare US ZIP (5 digits): Photon mis-geocodes these globally, so go
    # straight to Nominatim which keys ZIPs to the right country.
    is_zip = q.isdigit() and len(q) == 5
    if not is_zip:
        # Photon first (keyless, forgiving)
        try:
            d = json.loads(fetch(f"{PHOTON}?q={urllib.parse.quote(q)}&limit=1", timeout=15))
            feats = d.get("features", [])
            if feats:
                p = feats[0].get("properties", {})
                g = feats[0].get("geometry", {}).get("coordinates", [])
                if len(g) >= 2:
                    label = ", ".join(x for x in (p.get("name"), p.get("street"),
                        p.get("housenumber"), p.get("city"), p.get("state"),
                        p.get("postcode")) if x) or q
                    return jsonify({"lat": g[1], "lon": g[0], "label": label})
        except Exception:
            pass
    # Nominatim fallback (and the path for ZIPs) — countrycodes=us keeps US
    # ZIPs from resolving to identical-looking postcodes abroad.
    try:
        d = json.loads(fetch(f"{NOMINATIM}/search?format=jsonv2&limit=1"
                             f"&countrycodes=us&q={urllib.parse.quote(q)}", timeout=15))
        if d:
            return jsonify({"lat": float(d[0]["lat"]), "lon": float(d[0]["lon"]),
                            "label": d[0].get("display_name", q)})
    except Exception:
        pass
    # last resort for ZIPs: let Photon try, biased to the continental US
    if is_zip:
        try:
            d = json.loads(fetch(f"{PHOTON}?q={urllib.parse.quote(q)}&limit=1"
                                 f"&bbox=-125,24,-66,50", timeout=15))
            feats = d.get("features", [])
            if feats:
                g = feats[0].get("geometry", {}).get("coordinates", [])
                if len(g) >= 2:
                    return jsonify({"lat": g[1], "lon": g[0], "label": q})
        except Exception:
            pass
    return jsonify({"error": "couldn't find that location"}), 404

@app.route("/api/menu", methods=["POST"])
def menu():
    """Lazy-load a single shop's menu: site first, then Yelp/Google/Grubhub links."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    website = (body.get("website") or "").strip()
    city = (body.get("city") or "").strip()
    if not name and not website:
        return jsonify({"error": "need name or website"}), 400
    shop = {"name": name, "website": website, "_city": city}
    gather_menu(shop)
    return jsonify({
        "name": name,
        "menu": shop.get("menu", []),
        "menu_via": shop.get("menu_via", ""),
        "menu_links": shop.get("menu_links", {}),
    })

@app.route("/api/health")
def health():
    return jsonify({"ok": True})

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hoagie.radar</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{
  --wood:#241811;--wood2:#31231a;
  --bun-lt:#eebc6c;--bun:#dd9f42;--bun-dk:#c98832;--crust:#a96f1f;
  --bread:#f6ecd9;--bread-edge:#e0cba4;
  --ink:#3a2712;--ink-soft:#86653f;
  --cheese:#f6b73c;--accent:#e8630a;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:repeating-linear-gradient(90deg,var(--wood) 0 46px,var(--wood2) 46px 48px);
  color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;min-height:100vh}
.board{max-width:940px;margin:0 auto;padding:26px 12px 46px}

/* ---- top bun ---- */
.topbun{position:relative;height:118px;
  background:linear-gradient(180deg,#eebc6c 0%,var(--bun-lt) 35%,var(--bun) 75%,var(--bun-dk) 100%);
  border-radius:52% 52% 14px 14px / 74px 74px 14px 14px;
  box-shadow:inset 0 -8px 14px rgba(120,70,10,.18),0 10px 24px rgba(0,0,0,.45);
  display:flex;flex-direction:column;align-items:center;justify-content:flex-end;
  padding:0 20px 12px;text-align:center}
.logo{font-size:34px;font-weight:900;letter-spacing:-.5px;color:#5c3a10;text-shadow:0 1px 0 rgba(255,235,190,.6)}
.logo .dot{color:#b3541e}
.tagline{font-size:12.5px;color:#7a5220;font-weight:600}
.seed{position:absolute;width:13px;height:6px;background:#faf0da;border-radius:50%;
  transform:rotate(var(--r,0deg));box-shadow:0 1px 2px rgba(90,55,10,.35)}

/* ---- filling layers ---- */
.layer{position:relative}
.lettuce{height:13px;background:linear-gradient(180deg,#63c255,#4da841)}
.lettuce::after{content:"";position:absolute;left:0;right:0;top:12px;height:9px;
  background-image:radial-gradient(circle at 7px 1px,#4da841 7px,transparent 7.5px);
  background-size:14px 9px;background-repeat:repeat-x}
.tomato{height:12px;background:linear-gradient(180deg,#e2594a,#d84a3a);margin-top:8px;
  box-shadow:inset 0 0 0 2px rgba(255,255,255,.14);border-radius:3px}
.cheese{height:12px;background:linear-gradient(180deg,#ffc95e,var(--cheese));margin-top:3px;border-radius:3px}
.drip{position:absolute;top:11px;width:0;height:0;border-left:11px solid transparent;
  border-right:11px solid transparent;border-top:16px solid var(--cheese)}
.drip.d1{left:16%}
.drip.d2{left:62%}

/* ---- bread body ---- */
.bread{background:var(--bread);border-left:5px solid var(--crust);border-right:5px solid var(--crust);
  padding:16px 16px 22px;box-shadow:0 10px 26px rgba(0,0,0,.45);
  background-image:radial-gradient(rgba(170,125,60,.08) 1px,transparent 1.6px);background-size:24px 24px}

/* ---- bottom bun ---- */
.botbun{height:64px;background:linear-gradient(180deg,var(--bun-dk),#b87a2a);
  border-radius:14px 14px 48% 48%/14px 14px 46px 46px;
  box-shadow:0 12px 26px rgba(0,0,0,.5)}
.footnote{text-align:center;color:#caa878;margin-top:14px;font-size:11.5px}
.credit{font-size:12.5px;font-weight:700;color:#e2c9a0;letter-spacing:.2px}
.credit a{color:var(--cheese);text-decoration:none;border-bottom:1px dotted rgba(246,183,60,.5)}
.credit a:hover{color:#fff;border-bottom-color:#fff}
.li{display:inline-flex;align-items:center;justify-content:center;
  width:17px;height:17px;border-radius:4px;background:#0a66c2;color:#fff!important;
  font-weight:900;font-size:12px;border:0!important;vertical-align:-3px;margin-left:2px}
.li:hover{background:#084d94}
.attrib{margin-top:5px;font-size:10.5px;opacity:.75}

/* ---- controls ---- */
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.controls label{font-size:13px;color:var(--ink-soft);font-weight:700}
.btn{border:0;border-radius:999px;padding:10px 18px;font-weight:800;font-size:14px;cursor:pointer;font-family:inherit}
.btn-main{background:var(--cheese);color:#4a2f08;box-shadow:0 3px 0 var(--crust)}
.btn-main:active{transform:translateY(2px);box-shadow:0 1px 0 var(--crust)}
.btn-ghost{background:#fffdf6;border:2px solid var(--bread-edge);color:var(--ink)}
.btn-ghost:hover{border-color:var(--accent)}
input[type=text],input[type=number]{background:#fffdf6;border:2px solid var(--bread-edge);
  color:var(--ink);border-radius:999px;padding:9px 14px;font-size:14px;font-family:inherit}
input:focus{outline:2px solid var(--cheese);outline-offset:1px}
.src{font-size:11.5px;color:var(--ink-soft);text-transform:uppercase;letter-spacing:.6px;font-weight:700}
.pill{background:#fffdf6;border:1.5px solid var(--bread-edge);border-radius:999px;padding:3px 11px;
  font-size:12px;color:var(--ink-soft)}
.spinner{display:inline-block;width:13px;height:13px;border:2px solid var(--bread-edge);
  border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px}
@keyframes spin{to{transform:rotate(360deg)}}

/* ---- map plate ---- */
.plate{background:#fff;padding:6px;border-radius:20px;box-shadow:0 8px 22px rgba(0,0,0,.35);
  margin:6px 0 14px}
#map{height:340px;border-radius:14px;z-index:1}
#mapFallback{margin:6px 0 14px;border-radius:14px;overflow:hidden;display:none}

/* ---- slang line ---- */
.slang-line{margin:2px 0 14px;font-size:14px;color:var(--ink-soft)}
.slang-line b{color:var(--accent);font-size:19px}

/* ---- filter bar ---- */
.filterbar{background:#fffdf6;border:2px solid var(--bread-edge);border-radius:16px;
  padding:12px 14px;margin-bottom:10px}
.filterbar input[type=text]{width:100%}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.chip{border:2px solid var(--bread-edge);background:#fff;color:var(--ink);border-radius:999px;
  padding:6px 13px;font-size:13px;font-weight:800;cursor:pointer;font-family:inherit;
  display:flex;align-items:center;gap:7px}
.chip:hover{border-color:var(--accent)}
.chip .swatch{width:11px;height:11px;border-radius:3px;display:inline-block}
.chip.active{background:var(--c,#e8630a);color:#fff;border-color:var(--c,#e8630a)}
.chip .cnt{font-size:11px;opacity:.75;font-weight:700}

#matchinfo{margin:2px 0 10px}

/* ---- result cards ---- */
.card{background:#fffdf6;border:2px solid var(--bread-edge);border-radius:14px;padding:12px 14px;
  margin-bottom:10px;display:flex;gap:13px;align-items:flex-start;cursor:pointer;
  transition:transform .08s,border-color .08s}
.card:hover{border-color:var(--accent);transform:translateX(2px)}
.dist{flex:0 0 62px;text-align:center;background:var(--cheese);color:#4a2f08;border-radius:12px;
  padding:8px 4px;font-weight:900;box-shadow:0 2px 0 var(--crust)}
.dist b{display:block;font-size:17px;line-height:1.1}
.dist span{font-size:9.5px;text-transform:uppercase;letter-spacing:.5px}
.info{flex:1;min-width:0}
.name{font-size:16px;font-weight:800;margin-bottom:3px}
.tchips{display:flex;gap:6px;flex-wrap:wrap;margin:5px 0}
.tchip{color:#fff;font-size:10.5px;font-weight:800;border-radius:999px;padding:2px 9px;
  text-transform:uppercase;letter-spacing:.4px}
.meta{color:var(--ink-soft);font-size:12.5px;word-break:break-word}
.meta .cuisine{color:var(--accent);font-weight:700}
.row-line{font-size:13px;color:var(--ink);font-weight:600;margin-top:3px;line-height:1.35}
.row-line:first-of-type{margin-top:5px}
.row-line a{color:var(--accent);text-decoration:none}
.row-line a:hover{text-decoration:underline}
.links{display:flex;gap:7px;flex-wrap:wrap;margin-top:7px}
.links a{font-size:12px;color:#8a4a12;text-decoration:none;background:#fff;
  border:1.5px solid var(--bread-edge);border-radius:999px;padding:3px 11px;font-weight:700}
.links a:hover{background:var(--cheese);border-color:var(--crust);color:#4a2f08}
.empty{text-align:center;color:var(--ink-soft);padding:34px 10px}

/* ---- inline menu ---- */
.menu{margin:8px 0 4px;background:#fff;border:2px dashed var(--bread-edge);border-radius:12px;
  padding:8px 12px;max-height:230px;overflow:auto}
.menu-hidden{display:none}
.menu-loaded{border-style:solid;border-color:var(--bread-edge)}
.menu-h{font-size:12px;font-weight:800;color:#8a4a12;text-transform:uppercase;letter-spacing:.5px;
  margin-bottom:5px}
.menu-via{font-weight:600;color:var(--ink-soft);text-transform:none;letter-spacing:0;opacity:.8}
.menu-items{display:flex;flex-direction:column;gap:3px}
.menu-item{display:flex;justify-content:space-between;gap:14px;font-size:13px;padding:2px 0;
  border-bottom:1px dotted #eadcc0}
.menu-item:last-child{border-bottom:0}
.mi-name{color:var(--ink);font-weight:600}
.mi-price{color:var(--accent);font-weight:800;white-space:nowrap}
.menu-src{font-size:12.5px;color:var(--ink-soft)}
.menu-src a{display:inline-block;margin:5px 6px 0 0;text-decoration:none;font-weight:800;
  border:2px solid var(--bread-edge);border-radius:999px;padding:3px 11px;font-size:12px}
/* Menu button styled like the pill links so the action row stays uniform */
.menu-btn{font-size:12px;color:#8a4a12;background:#fff;border:1.5px solid var(--bread-edge);
  border-radius:999px;padding:3px 11px;font-weight:700;cursor:pointer;font-family:inherit}
.menu-btn:hover{background:var(--cheese);border-color:var(--crust);color:#4a2f08}
.menu-btn:disabled{opacity:.6;cursor:default}

/* ---- "did you mean a sandwich?" modal ---- */
.modal-veil{position:fixed;inset:0;background:rgba(20,12,6,.6);backdrop-filter:blur(2px);
  display:flex;align-items:center;justify-content:center;z-index:1000;padding:20px}
.modal-veil[hidden]{display:none}
.modal{background:var(--bread);border:5px solid var(--crust);border-radius:22px;
  box-shadow:0 20px 50px rgba(0,0,0,.5);max-width:340px;width:100%;padding:28px 24px 22px;
  text-align:center;animation:pop .18s ease-out}
@keyframes pop{from{transform:scale(.85);opacity:0}to{transform:scale(1);opacity:1}}
.modal-emoji{font-size:52px;line-height:1;margin-bottom:8px}
.modal-title{font-size:22px;font-weight:900;color:#5c3a10;margin-bottom:6px}
.modal-sub{font-size:14px;color:var(--ink-soft);line-height:1.45;margin-bottom:18px}
.modal-btns{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}

/* ---- external buttons + note ---- */
.ext{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 16px}
.ext a{flex:1;min-width:190px;text-align:center;text-decoration:none;font-weight:800;
  padding:12px;border-radius:14px;font-size:14px}
.ext .g{background:#fff;color:#1a73e8;border:2px solid #d2e3fc}
.ext .y{background:#d32323;color:#fff}
.keybox{background:#fffdf6;border:2px dashed var(--bread-edge);border-radius:14px;
  padding:10px 14px;font-size:12.5px;color:var(--ink-soft)}
.keybox code{color:#8a4a12}

@media(max-width:600px){#map{height:260px}.logo{font-size:26px}.topbun{height:100px}}
</style>
</head>
<body>
<div class="board">

  <header class="topbun">
    <i class="seed" style="left:16%;top:34px;--r:-22deg"></i>
    <i class="seed" style="left:27%;top:16px;--r:10deg"></i>
    <i class="seed" style="left:39%;top:9px;--r:-6deg"></i>
    <i class="seed" style="left:50%;top:8px;--r:4deg"></i>
    <i class="seed" style="left:61%;top:10px;--r:12deg"></i>
    <i class="seed" style="left:72%;top:18px;--r:-14deg"></i>
    <i class="seed" style="left:82%;top:36px;--r:20deg"></i>
    <i class="seed" style="left:33%;top:30px;--r:30deg"></i>
    <i class="seed" style="left:58%;top:32px;--r:-28deg"></i>
    <div class="logo">hoagie<span class="dot">.</span>radar</div>
    <div class="tagline">Only Sandwiches, Near You!</div>
  </header>

  <div class="layer lettuce"></div>
  <div class="layer tomato"></div>
  <div class="layer cheese"><i class="drip d1"></i><i class="drip d2"></i></div>

  <main class="bread">
    <div class="controls">
      <button class="btn btn-main" id="geoBtn">📍 Use my location</button>
      <input type="text" id="locInput" placeholder="or enter an address / ZIP…" style="min-width:190px">
      <button class="btn btn-ghost" id="locBtn">Go</button>
      <button class="btn btn-ghost" id="phillyBtn">Center City Philly</button>
      <span id="geoStatus" class="src"></span>
    </div>

    <div class="controls">
      <label>Radius</label>
      <input type="number" id="radius" value="2.5" min="0.3" max="9.3" step="0.1" style="width:96px">
      <span style="color:var(--ink-soft);font-size:12px">mi</span>
      <button class="btn btn-ghost" id="searchBtn">Sweep</button>
      <span id="status" class="src"></span>
    </div>

    <div class="plate">
      <div id="map"></div>
      <div id="mapFallback"></div>
    </div>

    <div id="slang" class="slang-line"></div>

    <div class="filterbar">
      <input type="text" id="q" placeholder="Search shops, streets, sandwich names… (Enter)">
      <div class="chips" id="chips"></div>
    </div>
    <div id="matchinfo"></div>
    <div id="results"></div>

    <div class="ext" id="ext"></div>

    <div class="keybox">
      Tap a shop for <b>directions &amp; menus</b> — menus open in Google or Yelp.
    </div>
  </main>

  <div class="botbun"></div>

  <div class="modal-veil" id="noMatchModal" hidden>
    <div class="modal">
      <div class="modal-emoji">🥪</div>
      <div class="modal-title">Did you mean a sandwich?</div>
      <div class="modal-sub">Nothing around here matches that.<br>How about a sandwich instead?</div>
      <div class="modal-btns">
        <button class="btn btn-main" id="modalSandwich">Show me sandwiches</button>
        <button class="btn btn-ghost" id="modalDismiss">Never mind</button>
      </div>
    </div>
  </div>

  <footer class="footnote">
    <div class="credit">Built &amp; designed by
      <a href="https://www.linkedin.com/in/jonathancevera/" target="_blank" rel="noopener">Jonathan Cevera</a>
      &nbsp;·&nbsp;
      <a href="https://www.linkedin.com/in/jonathancevera/" target="_blank" rel="noopener" title="LinkedIn" class="li">in</a>
    </div>
    <div class="attrib">Data © OpenStreetMap contributors (ODbL) · map © OpenStreetMap</div>
  </footer>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const $=id=>document.getElementById(id);
let LOC=null;

const TYPE_META={
  cheesesteak:{label:"Cheesesteak",color:"#c0392b"},
  italian:{label:"Italian",color:"#8e44ad"},
  roast_pork:{label:"Roast Pork",color:"#a0522d"},
  roast_beef:{label:"Roast Beef",color:"#8d6e63"},
  pastrami:{label:"Pastrami/Kosher",color:"#5d4037"},
  kosher_deli:{label:"Kosher Deli",color:"#1565c0"},
  turkey:{label:"Turkey",color:"#d97b7b"},
  meatball:{label:"Meatball",color:"#6d4c41"},
  breakfast:{label:"Breakfast",color:"#d4a017"},
  veggie:{label:"Veggie/Vegan",color:"#2e9e4f"},
  cold_cut:{label:"Cold Cut",color:"#e67e22"}
};

let D=null,activeType="all",map=null,mapMode=null,markers=[],userMk=null;

function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function typeColor(r){const t=(r.types||[])[0];return (TYPE_META[t]||{}).color||"#e67e22";}
function typeLabel(r){return (r.types||[]).map(t=>TYPE_META[t]).filter(Boolean);}
function setStatus(m,spin){$("status").innerHTML=(spin?'<span class="spinner"></span> ':'')+m;}

/* ---- location ---- */
function geoSuccess(pos){
  LOC={lat:pos.coords.latitude,lon:pos.coords.longitude};
  $("geoStatus").textContent="got your fix ✓";
  sweep();
}
function geoError(){
  $("geoStatus").textContent="using IP location";
  LOC=null;
  sweep();
}
function useMyLocation(){
  if(navigator.geolocation){
    navigator.geolocation.getCurrentPosition(geoSuccess,geoError,{timeout:10000,enableHighAccuracy:true});
  } else geoError();
}

/* ---- sweep ---- */
async function sweep(){
  const miles=parseFloat($("radius").value||"2.5");
  const radius=Math.round(miles*1609.34);
  const body=LOC?{lat:LOC.lat,lon:LOC.lon,radius}:{radius};
  setStatus("sweeping the radar…",true);
  $("results").innerHTML="";$("slang").innerHTML="";$("ext").innerHTML="";$("matchinfo").innerHTML="";
  try{
    const r=await fetch("/api/radar",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.error){setStatus("Error: "+d.error);return;}
    render(d);
  }catch(e){setStatus("Request failed: "+e);}
}

function render(d){
  D=d;setStatus("");
  const loc=d.location;
  const where=(loc.display||"").split(",").slice(0,3).join(",")||"your area";
  $("status").innerHTML=`<span class="pill">📍 <b>${esc(where)}</b></span>`;
  const sl=d.slang;
  const alts=(d.alts||[]).map(a=>`<span class="pill">${esc(a)}</span>`).join(" ");
  $("slang").innerHTML=`Around here it's a <b>${esc(sl)}</b> &nbsp;${alts}`;
  const L=d.links||{};
  $("ext").innerHTML=
    `<a class="g" href="${L.google}" target="_blank" rel="noopener">🔍 Google Maps · ${esc(sl)}</a>`+
    `<a class="y" href="${L.yelp}" target="_blank" rel="noopener">Yelp · ${esc(sl)}</a>`;
  buildChips();
  initMap();
  plotMap();
  applyFilter();
}

/* ---- map ---- */
function initMap(){
  if(mapMode) return;
  if(typeof L!=="undefined"){
    mapMode="leaflet";
    map=L.map("map");
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      {maxZoom:19,attribution:"© OpenStreetMap contributors"}).addTo(map);
  } else {
    mapMode="radar";
    $("map").style.display="none";
  }
}
function plotMap(){
  if(!D||!mapMode) return;
  if(mapMode==="leaflet"){
    markers.forEach(m=>map.removeLayer(m.marker));markers=[];
    const bounds=[];
    D.results.forEach((r,i)=>{
      bounds.push([r.lat,r.lon]);
      const mk=L.circleMarker([r.lat,r.lon],
        {radius:7,color:"#5c3a10",weight:2,fillColor:typeColor(r),fillOpacity:.95})
        .addTo(map).bindPopup(popup(r));
      mk.on("click",()=>{
        const el=document.querySelector(`.card[data-i="${i}"]`);
        if(el) el.scrollIntoView({behavior:"smooth",block:"center"});
      });
      markers.push({marker:mk,r});
    });
    bounds.push([D.location.lat,D.location.lon]);
    if(userMk) map.removeLayer(userMk);
    userMk=L.circleMarker([D.location.lat,D.location.lon],
      {radius:10,color:"#fff",weight:3,fillColor:"#e8630a",fillOpacity:1})
      .addTo(map).bindPopup("<b>You are here</b>");
    map.fitBounds(bounds,{padding:[24,24],maxZoom:16});
  } else {
    fallbackRadar();
  }
}
function popup(r){
  const feet=Math.round(r.distance_m*3.28084);
  const dist=r.distance_m<804.7?feet+" ft":(r.distance_m/1609.34).toFixed(1)+" mi";
  const maps=`https://www.google.com/maps/search/?api=1&query=${r.lat},${r.lon}`;
  const y=`https://www.yelp.com/search?find_desc=${encodeURIComponent(r.name)}`;
  const menu=menuUrl(r), menuY=menuUrlYelp(r);
  const badges=typeLabel(r).map(m=>`<span class="tchip" style="background:${m.color}">${m.label}</span>`).join("");
  return `<div style="min-width:180px"><div style="font-weight:800;font-size:14px">${esc(r.name)}</div>
    <div class="tchips">${badges}</div>
    <div style="font-size:12px;color:#86653f">${dist} away${r.address?" · "+esc(r.address):""}</div>
    <div style="margin-top:6px;font-size:12px"><a href="${maps}" target="_blank" rel="noopener" style="color:#8a4a12">Directions</a> ·
    <a href="${menu}" target="_blank" rel="noopener" style="color:#8a4a12">Menu</a> ·
    <a href="${y}" target="_blank" rel="noopener" style="color:#8a4a12">Yelp</a></div></div>`;
}
/* offline fallback: a radar plot from relative lat/lon (no tiles needed) */
function fallbackRadar(){
  const el=$("mapFallback");
  const cx=150,cy=105,R=82;
  const lat0=D.location.lat,lon0=D.location.lon;
  const ky=111320,kx=111320*Math.cos(lat0*Math.PI/180);
  let maxd=1;
  const pts=D.results.map(r=>{
    const dx=(r.lon-lon0)*kx,dy=(r.lat-lat0)*ky;
    maxd=Math.max(maxd,Math.hypot(dx,dy));
    return {r,dx,dy};
  });
  let s=`<svg viewBox="0 0 300 210" width="100%" height="210" style="background:#12200f;display:block">`;
  s+=`<text x="10" y="16" fill="#9fd3ab" font-size="10" font-family="sans-serif">radar view · ${D.results.length} spots · tiles offline</text>`;
  [0.34,0.67,1].forEach(f=>{
    s+=`<circle cx="${cx}" cy="${cy}" r="${R*f}" fill="none" stroke="#2f5d3a" stroke-width="1" stroke-dasharray="3 4"/>`;
  });
  s+=`<line x1="${cx-R}" y1="${cy}" x2="${cx+R}" y2="${cy}" stroke="#2f5d3a" stroke-width=".7"/>`;
  s+=`<line x1="${cx}" y1="${cy-R}" x2="${cx}" y2="${cy+R}" stroke="#2f5d3a" stroke-width=".7"/>`;
  pts.forEach(p=>{
    const a=Math.atan2(p.dy,p.dx),d=Math.hypot(p.dx,p.dy)/maxd*R;
    const m=matches(p.r);
    s+=`<circle cx="${cx+d*Math.cos(a)}" cy="${cy-d*Math.sin(a)}" r="${m?5:4}"
      fill="${typeColor(p.r)}" stroke="#fff" stroke-width="1" opacity="${m?1:.45}"/>`;
  });
  s+=`<circle cx="${cx}" cy="${cy}" r="6" fill="#e8630a" stroke="#fff" stroke-width="2"/>`;
  s+=`<text x="${cx+10}" y="${cy-8}" fill="#fff" font-size="9" font-family="sans-serif">you</text>`;
  s+=`</svg>`;
  el.innerHTML=s;
  el.style.display="block";
}

/* ---- menu + search vocab ---- */
/* a menu deep-link for a shop: Google's business menu, plus a Yelp fallback.
   No API key needed — these open the shop's real menu page when Google/Yelp
   have it indexed, and otherwise land on the search for it. */
function menuUrl(r){
  const q=encodeURIComponent(`${r.name} menu`);
  return `https://www.google.com/search?q=${q}`;
}
function menuUrlYelp(r){
  return `https://www.yelp.com/search?find_desc=${encodeURIComponent(r.name+" menu")}`;
}
/* sandwich vocabulary: if the user types one of these, match the shop TYPE
   even when the shop's own name/cuisine doesn't contain the word. */
const SYNONYMS={
  cheesesteak:["cheesesteak","cheese steak","steak","whiz wit","whiz","wit","wiz","provolone","chicken steak","steak sandwich","philly","melt","beef steak","steak hoagie"],
  italian:["italian","italian hoagie","italian deli","italian sub"],
  roast_pork:["roast pork","roasted pork","pork","pork sandwich","broccoli rabe"],
  roast_beef:["roast beef","roastbeef","beef","roast beef sandwich","roast beef hoagie","french dip","au jus"],
  pastrami:["pastrami","pastromi","katz","kosher deli","kosher style","jewish deli","corned beef","corned-beef","reuben","deli rye","deli mustard"],
  kosher_deli:["kosher deli","kosher","4th st deli","fourth st deli","4th street deli","schleisinger","schleinger","kosher style","jewish deli","kosher deli sandwich"],
  turkey:["turkey","turkey hoagie","turkey sub","deli turkey"],
  meatball:["meatball","meatball hoagie","meatball sub","meatball parm"],
  breakfast:["breakfast","egg","eggs","egg sandwich","pork roll","taylor ham","scrapple","sausage","bacon","morning sandwich","brunch"],
  veggie:["veggie","vegan","vegetarian","meatless","garden","plant"],
  cold_cut:["cold cut","deli","sub","sandwich","hoagie","hero","grinder","po boy","po'boy","italian cold cut","wawa","sheetz","roll"]
};

/* ---- filter ---- */
function matches(r){
  const q=$("q").value.trim().toLowerCase();
  if(activeType!=="all" && !(r.types||[]).includes(activeType)) return false;
  if(q){
    const words=q.split(/\s+/);
    const hay=((r.name||"")+" "+(r.cuisine||"")+" "+(r.address||"")+" "+(r.types||[]).join(" ")).toLowerCase();
    // match if the raw text contains the query, OR any sandwich-type synonym maps
    const typeHit=(r.types||[]).some(t=>(SYNONYMS[t]||[]).some(syn=>q.includes(syn)));
    if(!hay.includes(q) && !typeHit) return false;
  }
  return true;
}
function applyFilter(submitted){
  if(!D) return;
  submitted=submitted===true;
  let shown=0;
  markers.forEach(({marker,r})=>{
    const m=matches(r);shown+=m?1:0;
    marker.setStyle(m
      ?{radius:9,fillColor:typeColor(r),color:"#5c3a10",weight:2,fillOpacity:.95}
      :{radius:5,fillColor:"#b3a48c",color:"#8a7a63",weight:1,fillOpacity:.55});
  });
  if(mapMode==="radar") fallbackRadar();
  const list=[];
  D.results.forEach((r,i)=>{if(matches(r)) list.push([r,i]);});
  const cnt=activeType==="all"?"":` · ${TYPE_META[activeType].label}`;
  $("matchinfo").innerHTML=`<span class="src">${list.length} of ${D.results.length} spots${cnt}</span>`;
  $("results").innerHTML=list.length
    ? list.map(([r,i])=>card(r,i)).join("")
    : `<div class="empty">No matches here.<br>Try another type or clear the search.</div>`;
  // only nudge on an explicit search submission that found nothing —
  // never while the user is still typing
  const modal=$("noMatchModal");
  const submittedSearch=submitted && $("q").value.trim().length>0;
  if(list.length===0 && submittedSearch) modal.hidden=false;
  else modal.hidden=true;
}
function buildChips(){
  const counts={};
  D.results.forEach(r=>(r.types||[]).forEach(t=>counts[t]=(counts[t]||0)+1));
  let html=`<button class="chip active" data-type="all">🥪 All sandwiches</button>`;
  for(const [k,m] of Object.entries(TYPE_META)){
    if(!counts[k]) continue;
    html+=`<button class="chip" data-type="${k}" style="--c:${m.color}">`+
          `<span class="swatch" style="background:${m.color}"></span>${m.label}`+
          ` <span class="cnt">${counts[k]}</span></button>`;
  }
  const box=$("chips");
  box.innerHTML=html;
  activeType="all";
  box.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{
    activeType=c.dataset.type;
    box.querySelectorAll(".chip").forEach(x=>x.classList.toggle("active",x===c));
    applyFilter();
  });
}
/* make OSM hours (Mo-Fr 08:00-18:00) human-readable */
function fmtHours(h){
  const days={Mo:"Mon",Tu:"Tue",We:"Wed",Th:"Thu",Fr:"Fri",Sa:"Sat",Su:"Sun"};
  return h
    .replace(/Mo|Tu|We|Th|Fr|Sa|Su/g,d=>days[d]||d)
    .replace(/;/g,"; ")
    .replace(/-/g,"–")
    .replace(/(\d{2}:\d{2})/g,(m,t)=>{  // 24h -> 12h
      let [H,M]=t.split(":").map(Number);
      const ap=H>=12?"pm":"am"; H=H%12||12;
      return `${H}${M?":"+String(M).padStart(2,"0"):""}${ap}`;
    })
    .replace(/\s{2,}/g," ").trim();
}
function card(r,i){
  const dm=r.distance_m;
  // imperial: under half a mile show feet, otherwise miles
  const feet=Math.round(dm*3.28084);
  let dist,unit;
  if(dm<804.7){dist=feet;unit="ft";}
  else{dist=(dm/1609.34).toFixed(1);unit="mi";}
  const maps=`https://www.google.com/maps/search/?api=1&query=${r.lat},${r.lon}`;
  const g=`https://www.google.com/search?q=${encodeURIComponent(r.name+" "+(r.slang||"sandwich"))}`;
  const y=`https://www.yelp.com/search?find_desc=${encodeURIComponent(r.name)}`;
  const badges=typeLabel(r).map(m=>`<span class="tchip" style="background:${m.color}">${m.label}</span>`).join("");
  // dedicated, easy-to-scan lines for the details that matter most
  const addrHtml=r.address?`<div class="row-line">📍 ${esc(r.address)}</div>`:"";
  const hoursHtml=r.hours?`<div class="row-line">🕒 ${esc(fmtHours(r.hours))}</div>`:"";
  const phoneHtml=r.phone?`<div class="row-line">📞 <a href="tel:${esc(r.phone.replace(/[^0-9+]/g,""))}">${esc(r.phone)}</a></div>`:"";
  // secondary details stay in a compact meta line
  const meta=[
    r.cuisine?`<span class="cuisine">${esc(r.cuisine)}</span>`:"",
    r.type?`<span>${esc(r.type)}</span>`:"",
    r.takeaway==="yes"?"<span>✓ takeaway</span>":"",
    r.delivery==="yes"?"<span>✓ delivery</span>":""
  ].filter(Boolean);
  const site=r.website||"";
  const host=site?site.replace(/^https?:\/\//,"").replace(/\/.*$/,""):"";
  if(host) meta.push(`<a class="cuisine" href="${esc(site)}" target="_blank" rel="noopener">🔗 ${esc(host)}</a>`);
  const metaHtml=meta.length?`<div class="meta">${meta.join('<span style="color:#e0cba4"> · </span>')}</div>`:"";
  // inline menu drawer: prefetched items shown inline, else revealed on demand
  let menuHtml;
  if(r.menu && r.menu.length){
    const via=r.menu_via==="site"?"their website":r.menu_via;
    menuHtml=`<div class="menu menu-loaded">
      <div class="menu-h">📋 Menu <span class="menu-via">via ${via}</span></div>
      <div class="menu-items">${r.menu.map(m=>
        `<div class="menu-item"><span class="mi-name">${esc(m.name)}</span>${m.price?`<span class="mi-price">${esc(m.price)}</span>`:""}</div>`
      ).join("")}</div></div>`;
  } else {
    menuHtml=`<div class="menu menu-hidden"></div>`;
  }
  return `<div class="card" data-i="${i}">
    <div class="dist"><b>${dist}</b><span>${unit}</span></div>
    <div class="info">
      <div class="name">${esc(r.name)}</div>
      <div class="tchips">${badges}</div>
      ${addrHtml}
      ${hoursHtml}
      ${phoneHtml}
      ${metaHtml}
      ${menuHtml}
      <div class="links">
        <a href="${maps}" target="_blank" rel="noopener">Directions</a>
        <button class="menu-btn" data-name="${esc(r.name)}" data-web="${esc(site)}">Menu</button>
        ${site?`<a href="${site}" target="_blank" rel="noopener">Website</a>`:""}
      </div>
    </div></div>`;
}

/* ---- wiring ---- */
$("geoBtn").onclick=useMyLocation;
$("phillyBtn").onclick=()=>{
  LOC={lat:39.9526,lon:-75.1652};
  $("geoStatus").textContent="Center City Philly";
  sweep();
};
/* address / ZIP entry — geocode it, then sweep */
async function geocodeAndSweep(){
  const q=$("locInput").value.trim();
  if(!q){ $("geoStatus").textContent="enter an address or ZIP"; return; }
  $("geoStatus").textContent="looking up…";
  try{
    const r=await fetch("/api/geocode",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({q})});
    const d=await r.json();
    if(d.error){ $("geoStatus").textContent=d.error; return; }
    LOC={lat:d.lat,lon:d.lon};
    $("geoStatus").textContent=d.label.split(",").slice(0,2).join(",");
    sweep();
  }catch(e){ $("geoStatus").textContent="lookup failed"; }
}
$("locBtn").onclick=geocodeAndSweep;
$("locInput").addEventListener("keydown",e=>{ if(e.key==="Enter"){e.preventDefault();geocodeAndSweep();} });
$("searchBtn").onclick=sweep;
$("radius").addEventListener("change",sweep);
/* search: filter live as you type, but only nudge with the modal on Enter */
$("q").addEventListener("input",()=>applyFilter(false));
$("q").addEventListener("keydown",e=>{
  if(e.key==="Enter"){ e.preventDefault(); applyFilter(true); }
});
$("q").addEventListener("search",()=>applyFilter(true));  // cleared via the ✕
/* ---- no-match modal ---- */
$("modalSandwich").onclick=()=>{
  $("noMatchModal").hidden=true;
  $("q").value="";                    // clear the search
  // reset type chip to "all"
  activeType="all";
  document.querySelectorAll("#chips .chip").forEach(x=>
    x.classList.toggle("active",x.dataset.type==="all"));
  applyFilter();
};
$("modalDismiss").onclick=()=>{ $("noMatchModal").hidden=true; };
$("noMatchModal").addEventListener("click",e=>{
  if(e.target.id==="noMatchModal") $("noMatchModal").hidden=true;  // click veil to close
});
/* ---- lazy menu loading (Menu button fills the drawer inline) ---- */
function menuItemsHtml(items){
  return `<div class="menu-h">📋 Menu <span class="menu-via">via their website</span></div>
    <div class="menu-items">${items.map(m=>
      `<div class="menu-item"><span class="mi-name">${esc(m.name)}</span>${m.price?`<span class="mi-price">${esc(m.price)}</span>`:""}</div>`
    ).join("")}</div>`;
}
function menuSrcHtml(data){
  const L=data.menu_links||{};
  const opts=[
    L.yelp&&{n:"Yelp",u:L.yelp.url,c:"#d32323"},
    L.google&&{n:"Google",u:L.google.url,c:"#1a73e8"},
    L.grubhub&&{n:"Grubhub",u:L.grubhub.url,c:"#f63440"}
  ].filter(Boolean);
  return `<div class="menu-h">📋 Menu <span class="menu-via">open where it's posted</span></div>
    <div class="menu-src">${opts.map(o=>
      `<a href="${o.u}" target="_blank" rel="noopener" style="border-color:${o.c};color:${o.c}">${o.n}</a>`
    ).join("")}</div>`;
}

document.addEventListener("click",async e=>{
  const btn=e.target.closest(".menu-btn");
  if(!btn) return;
  e.stopPropagation();
  const card=btn.closest(".card");
  const container=card.querySelector(".menu");
  // already loaded? just toggle it open/closed
  if(container.classList.contains("menu-loaded")){
    container.classList.toggle("menu-hidden");
    return;
  }
  if(container.dataset.done==="1"){ container.classList.remove("menu-hidden"); return; }
  btn.textContent="Loading…"; btn.disabled=true;
  const city=(D&&D.location&&D.location.city)||"";
  try{
    const r=await fetch("/api/menu",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:btn.dataset.name,website:btn.dataset.web,city})});
    const data=await r.json();
    if(data.error){ container.innerHTML=`<div class="menu-src">Couldn't load menu.</div>`; }
    else if(data.menu && data.menu.length){ container.innerHTML=menuItemsHtml(data.menu); }
    else { container.innerHTML=menuSrcHtml(data); }
  }catch(err){
    container.innerHTML=`<div class="menu-src">Couldn't load menu.</div>`;
  }
  container.dataset.done="1";
  container.classList.remove("menu-hidden");
  btn.textContent="Menu"; btn.disabled=false;
});

document.addEventListener("click",e=>{
  const c=e.target.closest(".card");
  if(c&&map&&mapMode==="leaflet"&&markers[+c.dataset.i]){
    const m=markers[+c.dataset.i];
    map.flyTo([m.r.lat,m.r.lon],17,{duration:.6});
    m.marker.openPopup();
  }
});

/* ---- launch: show a list immediately, refine to real location after ---- */
function launch(){
  // 1) instantly populate the list + map with the default spot
  LOC={lat:39.9526,lon:-75.1652};
  $("geoStatus").textContent="locating you…";
  sweep();
  // 2) silently swap to the user's real location once it resolves
  if(navigator.geolocation){
    navigator.geolocation.getCurrentPosition(pos=>{
      const la=pos.coords.latitude,lo=pos.coords.longitude;
      // only re-sweep if they've meaningfully moved from the default
      if(Math.abs(la-LOC.lat)>0.01||Math.abs(lo-LOC.lon)>0.01){
        LOC={lat:la,lon:lo};
        $("geoStatus").textContent="got your fix ✓";
        sweep();
      } else {
        $("geoStatus").textContent="got your fix ✓";
      }
    },()=>{ $("geoStatus").textContent="using IP location"; sweep(); },
      {timeout:10000,enableHighAccuracy:true});
  } else { $("geoStatus").textContent="using IP location"; sweep(); }
}
launch();
</script>
</body>
</html>
"""

def main():
    ap = argparse.ArgumentParser(description="hoagie.radar")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5117)))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--open", action="store_true", help="open the browser")
    args = ap.parse_args()
    url = f"http://{args.host}:{args.port}"
    print(f"hoagie.radar → {url}")
    if args.open:
        import webbrowser; webbrowser.open(url)
    # hosting platforms set PORT; bind all interfaces there so the service is
    # reachable. Locally we stay on 127.0.0.1 (nothing exposed to the network).
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
