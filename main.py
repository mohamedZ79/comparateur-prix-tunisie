"""
API du comparateur de prix tunisien - Moteur PostgreSQL Supabase IPv4.
"""
import os
import re
import unicodedata
from typing import List, Optional
import asyncpg
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

DATABASE_URL = os.getenv("DATABASE_URL")

app = FastAPI(title="PrixTN API - Base Indexée", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db_pool = None

@app.on_event("startup")
async def startup():
    global db_pool
    if DATABASE_URL:
        try:
            # Connexion sécurisée au Pooler IPv4
            db_pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=5,
                timeout=15.0,
                ssl="require"
            )
            print("✅ Connecté avec succès à Supabase PostgreSQL (IPv4) !")
        except Exception as e:
            print(f"⚠️ Avertissement connexion base de données : {e}")

@app.on_event("shutdown")
async def shutdown():
    global db_pool
    if db_pool:
        await db_pool.close()

def clean_query(text: str) -> str:
    clean = re.sub(r'["\']', '', str(text))
    return unicodedata.normalize('NFKD', clean).encode('ASCII', 'ignore').decode('utf-8').strip()

@app.get("/api/search")
@app.get("/api/compare")
async def search(q: str = Query(..., min_length=2, max_length=120)):
    if not db_pool:
        return {"query": q, "count": 0, "offers": [], "error": "Database connecting..."}

    query = clean_query(q)
    tokens = [w for w in query.split() if len(w) > 1]
    
    sql_conditions = []
    params = []
    
    for i, tok in enumerate(tokens, start=1):
        sql_conditions.append(f"(title ILIKE ${i} OR sku ILIKE ${i} OR category ILIKE ${i} OR source ILIKE ${i})")
        params.append(f"%{tok}%")
        
    where_clause = " AND ".join(sql_conditions) if sql_conditions else "TRUE"
    
    sql = f"""
    SELECT source, category, title, sku, price, price_raw, url, image, in_stock
    FROM products
    WHERE {where_clause}
    ORDER BY (NOT in_stock), price ASC
    LIMIT 40;
    """
    
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            
        offers = [
            {
                "source": r["source"],
                "category": r["category"],
                "title": r["title"],
                "sku": r["sku"],
                "price": float(r["price"]),
                "price_raw": r["price_raw"],
                "url": r["url"],
                "image": r["image"],
                "availability": "En stock" if r["in_stock"] else "Épuisé",
                "match_score": 100.0
            }
            for r in rows
        ]
        
        return {
            "query": q,
            "count": len(offers),
            "offers": offers,
            "cached": True
        }
    except Exception as e:
        return {"query": q, "count": 0, "offers": [], "error": str(e)}

@app.get("/")
async def serve_index():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"status": "running"}

@app.get("/health")
async def health():
    return {"status": "ok"}
