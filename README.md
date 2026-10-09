# hoagie.radar
Find sandwich shops near you — in your local slang (hoagie, hero, grinder, po'boy, sub).

A single-file Flask app. Live map, sandwich-type filters, inline menus (Yelp → Google →
Grubhub fallback), imperial distances, and a "Did you mean a sandwich?" nudge.
Data from OpenStreetMap (Overpass → Photon → Nominatim fallback chain).

## Run locally
```bash
pip install -r requirements.txt
python3 main.py --port 5117 --open
```
Opens http://127.0.0.1:5117 and auto-locates you (falls back to an address/ZIP box).

## Deploy
Works on any platform that runs a Python web process (Heroku, Railway, Render, Fly.io…).
`main:app` is the WSGI entrypoint; the app reads `$PORT` and binds `0.0.0.0` when set.

```bash
gunicorn main:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
```

Optional env vars:
- `GOOGLE_PLACES_KEY` — live inline Google Places results (otherwise deep-links only)
- `YELP_API_KEY` — live inline Yelp results (otherwise deep-links only)

## Note on data
No API key is required. Results come from public OpenStreetMap services, which rate-limit
under load — the app falls back across three providers and caches responses for 5 minutes.
A small curated seed covers iconic shops OSM doesn't map (Pat's, Geno's, 4th St Deli…).

Built by Jonathan Cevera — https://www.linkedin.com/in/jonathancevera/
