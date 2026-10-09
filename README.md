# 🥪 hoagie.radar

Find sandwich shops near you — in your local slang (**hoagie** / **hero** / **grinder** /
**po'boy** / **sub**), automatically picked from your location.

A single-file Flask app styled like an actual sandwich (bun, lettuce, tomato, cheese).
No database, no required API keys.

## What it does

- **Finds you** — uses browser geolocation on load, or you can type any **address / ZIP /
  city** into the search box. Falls back to IP if location's denied.
- **Live map** — every spot plotted on an OpenStreetMap map, color-coded by sandwich type,
  with a "you are here" pin. Dims non-matching spots when you filter.
- **Sandwich-type filters** — All · Cheesesteak · Italian · Roast Pork · Roast Beef ·
  Pastrami/Kosher · Kosher Deli · Turkey · Meatball · Breakfast · Veggie/Vegan · Cold Cut.
- **Search by sandwich name** — type `steak`, `whiz wit`, `pork roll`, `pastrami`, `kosher`,
  `po'boy`… and it matches by sandwich vocabulary, not just the shop's name.
- **Details on every result** — name, type badges, address, hours (human-readable), phone
  (tap-to-call), cuisine, and website link.
- **Inline menus** — tap **Menu** to pull the menu inline: scrapes the shop's own site first,
  then falls back to Yelp → Google → Grubhub links.
- **Imperial distances** — feet under half a mile, miles beyond.
- **"Did you mean a sandwich?"** — press Enter on a search that finds nothing and it nudges
  you back toward sandwiches.

## Data

No API key required. Results come from public OpenStreetMap services via a fallback chain:
**Overpass → Photon → Nominatim**, with a 5-minute response cache so repeat visits are instant
and the rate-limited public APIs aren't hammered. A small curated seed covers iconic shops OSM
doesn't map (Pat's, Geno's, 4th St Deli, Schleisinger's…).

Optional env vars for live inline results (otherwise everything deep-links out):
- `GOOGLE_PLACES_KEY` — live Google Places results
- `YELP_API_KEY` — live Yelp results

## Run locally

```bash
pip install -r requirements.txt
python3 main.py --port 5117 --open     # or just: ./run.sh
```

Opens http://127.0.0.1:5117 and auto-locates you.

## Deploy

Any platform that runs a Python web process (Railway, Render, Fly.io, Heroku…).
`main:app` is the WSGI entrypoint.

```bash
gunicorn main:app --bind 0.0.0.0:5117 --workers 2 --timeout 120
```

> Note: the `Procfile`/`nixpacks.toml` bind to **port 5117** to match this deployment's
> configured Railway port. If your host assigns a dynamic `$PORT` instead, change the bind
> to `0.0.0.0:$PORT`.

Built by **Jonathan Cevera** — https://www.linkedin.com/in/jonathancevera/
