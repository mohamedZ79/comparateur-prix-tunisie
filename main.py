"""
API du comparateur de prix tunisien.
Lancement : uvicorn main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import os
import time

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from scrapers import search_all

app = FastAPI(title="Comparateur Prix TN", version="0.1.0")

# ✅ Configuration CORS complète (autorise Netlify, smartphones et requêtes cross-origin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------ cache mémoire simple (PoC)
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 15 * 60  # 15 minutes


@app.get("/api/search")
@app.get("/api/compare")
async def search(q: str = Query(..., min_length=2, max_length=120)):
    key = q.strip().lower()
    now = time.time()

    # 1. Vérification du cache mémoire
    if key in _cache and now - _cache[key][0] < CACHE_TTL:
        return {**_cache[key][1], "cached": True}

    # 2. Exécution du scraping en direct sur toutes les boutiques
    result = await search_all(q)

    # 3. Mise en cache du résultat
    _cache[key] = (now, result)
    return {**result, "cached": False}


@app.get("/")
async def serve_frontend():
    """Sert directement index.html si quelqu'un ouvre l'adresse de l'API Render dans son navigateur"""
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"status": "ok", "message": "PrixTN API is running"}


@app.get("/health")
async def health_check():
    """Endpoint ultra-léger pour le ping UptimeRobot"""
    return {"status": "healthy"}
