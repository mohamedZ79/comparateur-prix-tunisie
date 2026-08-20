"""
API du comparateur de prix tunisien.
Lancement : uvicorn main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import time

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from scrapers import search_all

app = FastAPI(title="Comparateur Prix TN", version="0.1.0")

# Le frontend (Netlify ou fichier local) appelle l'API en cross-origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # restreindre au domaine Netlify en production
    allow_methods=["GET"],
)

# ------------------------------------------------ cache mémoire simple (PoC)
# En production : remplacer par Redis (TTL 15-30 min) — voir README.
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 15 * 60               # 15 minutes


@app.get("/api/search")
async def search(q: str = Query(..., min_length=2, max_length=120)):
    key = q.strip().lower()
    now = time.time()

    if key in _cache and now - _cache[key][0] < CACHE_TTL:
        return {**_cache[key][1], "cached": True}

    result = await search_all(key)
    _cache[key] = (now, result)
    return {**result, "cached": False}


@app.get("/api/health")
async def health():
    from scrapers import SCRAPERS, ENABLE_BROWSER_SCRAPERS
    return {
        "status": "ok",
        "scrapers_http": list(SCRAPERS),
        "scrapers_browser": ["mytek", "wamia", "sangour"] if ENABLE_BROWSER_SCRAPERS else "désactivés (ENABLE_BROWSER_SCRAPERS=1 pour activer)",
    }


# Sert le frontend en local (en prod, le front est sur Netlify)
@app.get("/")
async def index():
    return FileResponse("index.html")
