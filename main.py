"""
API PrixTN - recherche sur base PostgreSQL/Supabase.

Corrections et ameliorations d'audit :
  F-04/F-05  match_score REEL (pg_trgm word_similarity, repli rapidfuzz)
             et champ "cached" supprime (remplace par "source")
  F-06       wildcards LIKE echappes + recherche limitee a title/sku
  F-10       CORS en liste blanche (ALLOWED_ORIGINS), GET uniquement
  F-13       lifespan FastAPI (on_event deprecie supprime)
  F-14       pagination (limit/offset) + modeles Pydantic documentes
  F-15       index trigram pg_trgm - recherche tolerante aux fautes
  F-23       /health verifie reellement la base (503 si degrade)
  +          /api/suggest (autocompletion), /api/stats (fraicheur catalogue)
  +          limitation de debit par IP (30 req/min, sans dependance)
"""
import logging
import os
import re
import time
import unicodedata
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from scrapers import is_strict_match

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_SSL = os.getenv("DATABASE_SSL", "require")   # require | disable
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://prixtn.netlify.app,http://localhost:8000,"
        "http://127.0.0.1:8000").split(",") if o.strip()
]
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "30"))      # requetes...
RATE_WINDOW = int(os.getenv("RATE_WINDOW", "60"))    # ...par fenetre (s)
SUGGEST_RATE_LIMIT = int(os.getenv("SUGGEST_RATE_LIMIT", "60"))
TRIGRAM_THRESHOLD = 0.35   # seuil word_similarity pour la tolerance aux fautes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("prixtn")

# ----------------------------------------------------------------- etat

db_pool = None
trigram_ok = False          # pg_trgm disponible et operationnel ?


def clean_query(text: str) -> str:
    clean = re.sub(r'["\']', '', str(text))
    return unicodedata.normalize(
        'NFKD', clean).encode('ASCII', 'ignore').decode('utf-8').strip()


def escape_like(token: str) -> str:
    """Traite l'entree utilisateur comme un literal, pas comme un motif
    LIKE (F-06 : '%' et '_' ne peuvent plus elargir la recherche)."""
    return (token.replace("\\", "\\\\")
                 .replace("%", r"\%")
                 .replace("_", r"\_"))


# ------------------------------------------------------- limiteur de debit

class SlidingWindowLimiter:
    """Limiteur par IP en fenetre glissante, sans dependance externe.
    Note : valable pour un process uvicorn unique (deploiement documente).
    """

    def __init__(self, limit: int, window_s: int):
        self.limit = limit
        self.window = window_s
        self._hits: dict = defaultdict(deque)

    def allow(self, key: str):
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            retry = int(self.window - (now - q[0])) + 1
            return False, retry
        q.append(now)
        if len(self._hits) > 10000:            # nettoyage memoire
            for k in list(self._hits):
                if not self._hits[k]:
                    del self._hits[k]
        return True, self.limit - len(q)


search_limiter = SlidingWindowLimiter(RATE_LIMIT, RATE_WINDOW)
suggest_limiter = SlidingWindowLimiter(SUGGEST_RATE_LIMIT, RATE_WINDOW)


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------- modeles

class Offer(BaseModel):
    source: str
    category: Optional[str] = None
    title: str
    sku: Optional[str] = None
    price: float
    price_raw: Optional[str] = None
    url: str
    image: Optional[str] = None
    availability: str
    match_score: float = 0.0
    updated_at: Optional[str] = None
    price_age_hours: Optional[float] = None


class SearchResponse(BaseModel):
    query: str
    count: int
    total: int = 0
    source: str = "database"
    offers: list[Offer] = []
    error: Optional[str] = None


class SuggestResponse(BaseModel):
    suggestions: list[str] = []


class SourceStat(BaseModel):
    source: str
    products: int
    last_seen: Optional[str] = None


class StatsResponse(BaseModel):
    sources: list[SourceStat]
    stale_rows: int
    trigram: bool


# ------------------------------------------------------------------ SQL

# Recherche avec pg_trgm : tolerance aux fautes (query <% title) + tokens
# exacts (tous les tokens doivent apparaitre, semantique AND preservee),
# score = word_similarity. COUNT(*) OVER() donne le total pour la pagination.
# Parametres : $1..$n = motifs de tokens, $(n+1) = requete trigram,
#              $(n+2) = limit, $(n+3) = offset.
SEARCH_SQL_TRIGRAM = """
SELECT source, category, title, sku, price, price_raw, url, image,
       in_stock, updated_at,
       word_similarity(${q_param}, title) AS match_score,
       COUNT(*) OVER() AS total
FROM products
WHERE (${q_param} <% title)
   OR ({token_conditions})
ORDER BY (NOT in_stock), match_score DESC, price ASC
LIMIT ${limit_param} OFFSET ${offset_param}
"""

# Repli sans pg_trgm : tokens exacts uniquement (comportement historique,
# mais echappe et limite a title/sku), tri prix ; le score reel est
# recalcule cote Python via is_strict_match.
SEARCH_SQL_FALLBACK = """
SELECT source, category, title, sku, price, price_raw, url, image,
       in_stock, updated_at
FROM products
WHERE {token_conditions}
ORDER BY (NOT in_stock), price ASC
LIMIT 400
"""

SUGGEST_SQL_TRIGRAM = """
SELECT title, word_similarity($1, title) AS s
FROM products
WHERE ($1 <% title OR title ILIKE $2)
ORDER BY s DESC, LENGTH(title) ASC
LIMIT $3
"""


def build_token_conditions(n_tokens: int) -> str:
    """(title ILIKE $1 OR sku ILIKE $1) AND (title ILIKE $2 OR ...) - la
    semantique AND par token de la v2, restreinte a title/sku (F-06)."""
    return " AND ".join(
        f"(title ILIKE ${i} OR COALESCE(sku, '') ILIKE ${i})"
        for i in range(1, n_tokens + 1))


# ---------------------------------------------------------------- demarrage

async def _pool_init(conn):
    """Applique le seuil trigram a chaque connexion du pool."""
    try:
        await conn.execute(
            "SELECT set_config('pg_trgm.word_similarity_threshold', "
            f"'{TRIGRAM_THRESHOLD}', false)")
    except Exception:
        pass                                  # base sans pg_trgm : repli


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, trigram_ok
    # ssl="require" pour Supabase ; DATABASE_SSL=disable pour un Postgres
    # local sans TLS
    ssl_arg = None if DATABASE_SSL == "disable" else "require"
    if DATABASE_URL:
        try:
            # detection pg_trgm (peut echouer silencieusement : repli auto)
            probe = await asyncpg.connect(DATABASE_URL, timeout=15.0,
                                          ssl=ssl_arg)
            try:
                try:
                    await probe.execute("CREATE EXTENSION IF NOT EXISTS "
                                        "pg_trgm")
                except Exception:
                    pass                       # droits insuffisants : tant pis
                try:
                    await probe.fetchval("SELECT word_similarity('a', 'a')")
                    trigram_ok = True
                except Exception:
                    trigram_ok = False
            finally:
                await probe.close()

            db_pool = await asyncpg.create_pool(
                DATABASE_URL, min_size=1, max_size=5, timeout=15.0,
                ssl=ssl_arg, init=_pool_init if trigram_ok else None)
            log.info("Connecte a PostgreSQL (pg_trgm: %s)", trigram_ok)
        except Exception as e:
            log.warning("Connexion base de donnees impossible : %s", e)
    else:
        log.warning("DATABASE_URL absent - l'API demarre en mode degrade")
    yield
    if db_pool:
        await db_pool.close()


app = FastAPI(title="PrixTN API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,             # F-10 : liste blanche
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------- helpers

def row_to_offer(row) -> Offer:
    age_h = None
    if row.get("updated_at") is not None:
        delta = time.time() - row["updated_at"].timestamp()
        age_h = round(max(delta, 0) / 3600, 1)
    score = float(row.get("match_score") or 0.0)
    return Offer(
        source=row["source"],
        category=row.get("category"),
        title=row["title"],
        sku=row.get("sku"),
        price=float(row["price"]),
        price_raw=row.get("price_raw"),
        url=row["url"],
        image=row.get("image"),
        availability="En stock" if row["in_stock"] else "Épuisé",
        match_score=round(score * 100, 1),
        updated_at=(row["updated_at"].isoformat()
                    if row.get("updated_at") else None),
        price_age_hours=age_h,
    )


def fallback_rank(query: str, raw_query: str, rows: list,
                  limit: int, offset: int):
    """Repli sans pg_trgm : score reel via is_strict_match (rapidfuzz)."""
    scored = []
    for r in rows:
        valid, score = is_strict_match(raw_query, r["title"], r["url"])
        if not valid:
            continue
        r = dict(r)
        r["match_score"] = score / 100.0
        scored.append(r)
    scored.sort(key=lambda r: (not r["in_stock"], -r["match_score"],
                               r["price"]))
    total = len(scored)
    return [row_to_offer(r) for r in scored[offset:offset + limit]], total

# ------------------------------------------------------------- endpoints

@app.get("/api/search", response_model=SearchResponse)
@app.get("/api/compare", response_model=SearchResponse,
         include_in_schema=False)
async def search(request: Request,
                 q: str = Query(..., min_length=2, max_length=120),
                 limit: int = Query(40, ge=1, le=100),
                 offset: int = Query(0, ge=0, le=5000)):
    allowed, _ = search_limiter.allow(client_ip(request))
    if not allowed:
        return JSONResponse({"error": "Too many requests"},
                            status_code=429,
                            headers={"Retry-After": "30"})

    if not db_pool:
        return JSONResponse(
            {"query": q, "count": 0, "total": 0, "offers": [],
             "error": "Database connecting..."},
            status_code=503)

    query = clean_query(q)
    tokens = [w for w in query.split() if len(w) > 1][:8]
    if not tokens:
        return SearchResponse(query=q, count=0, total=0)

    patterns = [f"%{escape_like(t)}%" for t in tokens]
    token_conditions = build_token_conditions(len(tokens))

    try:
        async with db_pool.acquire() as conn:
            if trigram_ok:
                n = len(tokens)
                sql = (SEARCH_SQL_TRIGRAM
                       .replace("{token_conditions}", token_conditions)
                       .replace("${q_param}", f"${n + 1}")
                       .replace("${limit_param}", f"${n + 2}")
                       .replace("${offset_param}", f"${n + 3}"))
                rows = await conn.fetch(sql, *patterns, query, limit,
                                        offset)
                offers = [row_to_offer(r) for r in rows]
                total = rows[0]["total"] if rows else 0
            else:
                sql = SEARCH_SQL_FALLBACK.replace(
                    "{token_conditions}", token_conditions)
                rows = await conn.fetch(sql, *patterns)
                offers, total = fallback_rank(query, q, rows,
                                              limit, offset)

        return SearchResponse(query=q, count=len(offers), total=total,
                              source="database", offers=offers)
    except Exception as e:
        log.exception("Erreur recherche '%s'", q)
        return JSONResponse({"query": q, "count": 0, "total": 0,
                             "offers": [], "error": str(e)},
                            status_code=500)


@app.get("/api/suggest", response_model=SuggestResponse)
async def suggest(request: Request,
                  q: str = Query(..., min_length=2, max_length=80)):
    allowed, _ = suggest_limiter.allow(client_ip(request))
    if not allowed:
        return JSONResponse({"suggestions": []},
                            status_code=429,
                            headers={"Retry-After": "30"})
    if not db_pool:
        return SuggestResponse(suggestions=[])

    query = clean_query(q)
    pattern = f"%{escape_like(query)}%"
    try:
        async with db_pool.acquire() as conn:
            if trigram_ok:
                rows = await conn.fetch(SUGGEST_SQL_TRIGRAM, query,
                                        pattern, 8)
                return SuggestResponse(
                    suggestions=[r["title"] for r in rows])
            # repli : ILIKE + score Python
            rows = await conn.fetch(
                "SELECT DISTINCT title FROM products "
                "WHERE title ILIKE $1 LIMIT 40", pattern)
            from rapidfuzz import fuzz
            scored = sorted(
                ((fuzz.token_set_ratio(query, r["title"]), r["title"])
                 for r in rows), reverse=True)[:8]
            return SuggestResponse(suggestions=[t for _, t in scored])
    except Exception as e:
        log.exception("Erreur suggest '%s'", q)
        return SuggestResponse(suggestions=[])


@app.get("/api/stats", response_model=StatsResponse)
async def stats():
    if not db_pool:
        return JSONResponse({"sources": [], "stale_rows": -1,
                             "trigram": trigram_ok}, status_code=503)
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT source, COUNT(*) AS n, MAX(updated_at) AS last "
                "FROM products GROUP BY source ORDER BY n DESC")
            stale = await conn.fetchval(
                "SELECT COUNT(*) FROM products "
                "WHERE updated_at < NOW() - INTERVAL '2 days'")
        return StatsResponse(
            sources=[SourceStat(
                source=r["source"], products=r["n"],
                last_seen=r["last"].isoformat() if r["last"] else None)
                for r in rows],
            stale_rows=stale or 0,
            trigram=trigram_ok)
    except Exception:
        log.exception("Erreur stats")
        return JSONResponse({"sources": [], "stale_rows": -1,
                             "trigram": trigram_ok}, status_code=500)


@app.get("/")
async def serve_index():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"status": "running"}


@app.get("/health")
async def health():
    """Sante REELLE (F-23) : la base est-elle joignable ?"""
    if not db_pool:
        return JSONResponse({"status": "degraded", "db": "down",
                             "trigram": trigram_ok}, status_code=503)
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "db": "up", "trigram": trigram_ok}
    except Exception:
        return JSONResponse({"status": "degraded", "db": "error",
                             "trigram": trigram_ok}, status_code=503)
