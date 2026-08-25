"""
Crawler PrixTN - ingestion nocturne du catalogue vers PostgreSQL/Supabase.

Corrections d'audit appliquees :
  F-03  verification TLS reactive (verify=True) + timeouts explicites
  F-11  plus de "except: pass" - tout echec est journalise
  F-12  parse_tnd_price importe depuis scrapers.py (source unique)
  F-16  heuristique de rupture de stock (rupture/epuise/out of stock)
  F-17  pages crawlees par lots de 3 en concurrence
  F-18  arret a la premiere page vide (plus de compteurs de pages graves)
  F-19  upserts dans UNE transaction (tout ou rien)
  F-22  source normalisee "Mytek" (alignee sur scrapers.py)
  +     requete Mytek GraphQL par variables (plus d'injection f-string)
  +     PROXY_URL : proxy residentiel tunisien pour les sites Cloudflare
        (sangour, wamia, tdiscount, scoop, graiet, maalej, affariyet)
        et les sites geo-bloques (bricorama, electrotounes) - verifie en
        aout 2026 : ces sites defient TOUTES les requetes datacenter,
        y compris /wp-json et les sitemaps. Une IP residuelle TN passe.
  +     Drest via l'API Store WooCommerce (JSON public, 27 000+ produits,
        decouverte de l'audit : aucun challenge sur /wp-json/wc/store)
  +     repli curl_cffi (empreinte TLS Chrome) sur 403 - utile derriere
        un proxy residentiel, sans navigateur
  +     code de sortie 1 si zero produit collecte (alerte CI automatique)
"""
import asyncio
import html as html_lib
import logging
import os
import sys
from typing import Optional
from urllib.parse import urljoin

import asyncpg
import httpx
from bs4 import BeautifulSoup

from scrapers import parse_tnd_price, extract_title_link

DATABASE_URL = os.getenv("DATABASE_URL")
PROXY_URL = os.getenv("PROXY_URL")            # ex: http://user:pass@tn-proxy:8080
DREST_MAX_PAGES = int(os.getenv("DREST_MAX_PAGES", "300"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("crawler")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "*/*;q=0.8"),
    "Accept-Language": "fr-TN,fr;q=0.9,en-US;q=0.8",
}

try:
    from curl_cffi.requests import AsyncSession as CffiSession
    HAS_CFFI = True
except ImportError:                            # optionnel
    HAS_CFFI = False

# --------------------------------------------------------------- fetcheur

class Fetcher:
    """GET resilient : httpx d'abord, repli curl_cffi (TLS Chrome) sur 403.

    Derriere PROXY_URL (residentiel tunisien), ce double etage suffit pour
    la plupart des sites Cloudflare sans lancer de navigateur.
    """

    def __init__(self):
        self.client = httpx.AsyncClient(
            follow_redirects=True, verify=True,          # F-03
            timeout=httpx.Timeout(15.0, connect=10.0),
            headers=HEADERS,
            proxy=PROXY_URL,
        )
        self._cffi = None

    async def _cffi_session(self):
        if self._cffi is None:
            self._cffi = CffiSession(impersonate="chrome",
                                     proxy=PROXY_URL, timeout=20)
        return self._cffi

    async def get(self, url: str) -> Optional[str]:
        """Renvoie le HTML ou None. Journalise les echecs (F-11)."""
        status = None
        try:
            r = await self.client.get(url)
            status = r.status_code
            if r.status_code == 200:
                return r.text
        except httpx.HTTPError as e:
            log.warning("httpx error %s : %s", url, type(e).__name__)

        # 403/429/503 = anti-bot probable -> tentative curl_cffi
        if status in (403, 429, 503) and HAS_CFFI:
            try:
                s = await self._cffi_session()
                r2 = await s.get(url, allow_redirects=True)
                if r2.status_code == 200:
                    log.info("OK via curl_cffi (Chrome TLS) : %s", url)
                    return r2.text
                status = r2.status_code
            except Exception as e:
                log.warning("curl_cffi error %s : %s", url,
                            type(e).__name__)
        elif status is not None and status != 404:
            log.warning("HTTP %s sur %s", status, url)
        return None

    async def get_json(self, url: str, params: dict = None) -> Optional[list]:
        """GET JSON avec repli curl_cffi (utilise pour l'API Drest)."""
        try:
            r = await self.client.get(url, params=params,
                                      headers={"Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            status = r.status_code
        except (httpx.HTTPError, ValueError):
            status = None
        if status in (403, 429, 503) and HAS_CFFI:
            try:
                s = await self._cffi_session()
                r2 = await s.get(url, params=params, allow_redirects=True)
                if r2.status_code == 200:
                    return r2.json()
            except Exception as e:
                log.warning("curl_cffi JSON error %s : %s", url,
                            type(e).__name__)
        return None

    async def post_json(self, url: str, payload: dict) -> Optional[dict]:
        """POST JSON avec repli curl_cffi (utilise pour GraphQL Mytek)."""
        json_headers = {"Content-Type": "application/json",
                        "Accept": "application/json"}
        try:
            r = await self.client.post(url, json=payload,
                                       headers=json_headers)
            if r.status_code == 200:
                return r.json()
        except (httpx.HTTPError, ValueError):
            pass
        if HAS_CFFI:
            try:
                s = await self._cffi_session()
                r2 = await s.post(url, json=payload, headers=json_headers)
                if r2.status_code == 200:
                    return r2.json()
            except Exception as e:
                log.warning("curl_cffi POST error %s : %s", url,
                            type(e).__name__)
        return None

    async def close(self):
        await self.client.aclose()
        if self._cffi is not None:
            await self._cffi.close()

# -------------------------------------------------------- rupture de stock

OOS_MARKERS = ("rupture de stock", "en rupture", "épuisé", "epuise",
               "out of stock", "sold out", "unavailable")

def detect_in_stock(card) -> bool:
    """Heuristique de disponibilite (F-16) : True sauf marqueur explicite."""
    el = card.select_one(
        ".out-of-stock, .unavailable, [class*='rupture'], "
        "[class*='epuise'], [class*='outofstock'], .stock.unavailable")
    if el is not None:
        return False
    text = card.get_text(" ", strip=True).lower()
    return not any(m in text for m in OOS_MARKERS)

# ------------------------------------------------------------- rayons

CATALOG_TARGETS = [
    # SpaceNet (Smartphones, PC, Électroménager) - PrestaShop
    {"source": "SpaceNet", "category": "High-Tech",
     "url": "https://spacenet.tn/377-smartphone-tunisie?page={page}",
     "max_pages": 12},
    {"source": "SpaceNet", "category": "High-Tech",
     "url": "https://spacenet.tn/14-pc-portable?page={page}", "max_pages": 10},
    {"source": "SpaceNet", "category": "Électroménager",
     "url": "https://spacenet.tn/46-electromenager?page={page}",
     "max_pages": 10},
    {"source": "SpaceNet", "category": "Électroménager",
     "url": "https://spacenet.tn/47-petit-electromenager?page={page}",
     "max_pages": 10},

    # Tunisianet - PrestaShop
    {"source": "Tunisianet", "category": "High-Tech",
     "url": "https://www.tunisianet.com.tn/377-smartphone-tunisie?page={page}",
     "max_pages": 15},
    {"source": "Tunisianet", "category": "High-Tech",
     "url": "https://www.tunisianet.com.tn/301-pc-portable-tunisie?page={page}",
     "max_pages": 12},
    {"source": "Tunisianet", "category": "Électroménager",
     "url": "https://www.tunisianet.com.tn/380-electromenager-tunisie?page={page}",
     "max_pages": 12},
    {"source": "Tunisianet", "category": "Maison & Soins",
     "url": "https://www.tunisianet.com.tn/386-beaute-et-sante?page={page}",
     "max_pages": 10},

    # Yeswikam & MyCare (parapharmacies) - PrestaShop
    {"source": "Yeswikam", "category": "Parapharmacie",
     "url": "https://www.yeswikam.com/2-accueil?page={page}", "max_pages": 20},
    {"source": "Yeswikam", "category": "Parapharmacie",
     "url": "https://www.yeswikam.com/recherche?s=soin&page={page}",
     "max_pages": 10},
    {"source": "MyCare", "category": "Parapharmacie",
     "url": "https://mycare.tn/recherche?s=soin&page={page}", "max_pages": 10},
]

# Sangour : rayons WooCommerce (pagination /page/N/) - necessite une IP
# tunisienne (proxy PROXY_URL ou execution locale) : Cloudflare defie
# toutes les requetes datacenter, verifie aout 2026.
SANGOUR_RAYONS = [
    ("Maison & Entretien", "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/javel/"),
    ("Maison & Entretien", "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/sol/"),
    ("Maison & Entretien", "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/vaisselles/"),
    ("Maison & Entretien", "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/linge/"),
    ("Maison & Entretien", "https://sangour.tn/marques/judy/"),
    ("Maison & Cuisine", "https://sangour.tn/marques/tramontina/"),
    ("Maison & Cuisine", "https://sangour.tn/marques/tefal/"),
    ("Maison & Cuisine", "https://sangour.tn/marques/moulinex/"),
    ("Maison & Cuisine", "https://sangour.tn/categorie-produit/art-de-table-et-cuisine/art-culinaire/cocottes/"),
    ("Maison & Cuisine", "https://sangour.tn/categorie-produit/art-de-table-et-cuisine/art-culinaire/poeles/"),
    ("Maison & Cuisine", "https://sangour.tn/categorie-produit/art-de-table-et-cuisine/art-culinaire/casseroles/"),
]

# ------------------------------------------------------- parsing de cartes

def parse_products(html: str, source: str, category: str,
                   page_url: str) -> list:
    """Extrait les produits d'une page de rayon (PrestaShop ou WooCommerce)."""
    products = []
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(
        ".product-grid-item, .wd-product, div.product, li.product, "
        "article.product-miniature, .product-miniature, .product-item, "
        "div.product-small, .ajax_block_product")

    for p in cards:
        extracted = extract_title_link(p)
        price_tag = p.select_one(
            "ins .woocommerce-Price-amount, ins .amount, .price ins, "
            ".price .amount, .woocommerce-Price-amount, .price, span.price, "
            "[itemprop='price'], .product-price, .current-price")
        img_tag = p.select_one(
            ".product-element-top img, img.wp-post-image, img.product_image, "
            ".thumbnail-container img, .product-thumbnail img, img")
        ref_tag = p.select_one(".product-reference, .reference, "
                               "[itemprop='sku'], .sku")

        if not (extracted and price_tag):
            continue

        title, href = extracted
        product_url = urljoin(page_url, href)
        price_val = parse_tnd_price(price_tag.get_text(strip=True))
        img_url = (img_tag.get("data-full-size-image-url") or
                   img_tag.get("data-src") or img_tag.get("src")
                   ) if img_tag else None
        ref_val = (ref_tag.get_text(strip=True).replace("Réf :", "").strip()
                   if ref_tag else None)
        in_stock = detect_in_stock(p)               # F-16

        if title and price_val and price_val > 0:
            products.append({
                "source": source,
                "category": category,
                "title": title,
                "sku": ref_val,
                "price": price_val,
                "price_raw": f"{price_val:,.3f} TND",
                "url": product_url,
                "image": img_url,
                "in_stock": in_stock,
            })
    return products

# ------------------------------------------------------------- crawl rayon

async def crawl_page(fetcher: Fetcher, source: str, category: str,
                     url: str) -> list:
    html = await fetcher.get(url)
    if html is None:
        return []
    return parse_products(html, source, category, url)

async def crawl_rayon_prestashop(fetcher: Fetcher, source: str,
                                 category: str, url_tpl: str,
                                 max_pages: int,
                                 batch: int = 3) -> list:
    """Rayon PrestaShop (?page=N) : lots de 3 pages concurrentes (F-17),
    arret a la premiere serie vide (F-18)."""
    products, page = [], 1
    while page <= max_pages:
        urls = [url_tpl.format(page=p) for p in range(page, page + batch)]
        results = await asyncio.gather(
            *(crawl_page(fetcher, source, category, u) for u in urls))
        if not any(results):
            break
        for items in results:
            products.extend(items)
        page += batch
        await asyncio.sleep(0.2)
    log.info("[%s] %s : %d produits", source, category, len(products))
    return products

async def crawl_rayon_woocommerce(fetcher: Fetcher, source: str,
                                  category: str, base_url: str,
                                  max_pages: int = 10,
                                  batch: int = 3) -> list:
    """Rayon WooCommerce (/page/N/) : meme strategie par lots."""
    products, page = [], 1
    while page <= max_pages:
        urls = []
        for p in range(page, page + batch):
            urls.append(base_url if p == 1 else f"{base_url}page/{p}/")
        results = await asyncio.gather(
            *(crawl_page(fetcher, source, category, u) for u in urls))
        if not any(results):
            break
        for items in results:
            products.extend(items)
        page += batch
        await asyncio.sleep(0.2)
    log.info("[%s] %s : %d produits", source, category, len(products))
    return products

# ------------------------------------------------------------------ Mytek

MYTEK_GRAPHQL = "https://www.mytek.tn/graphql"
MYTEK_MEDIA = "https://www.mytek.tn/media/catalog/product"

MYTEK_QUERY = """
query ($search: String, $page: Int, $pageSize: Int) {
  opensearchProductSearch(search: $search, page: $page, pageSize: $pageSize) {
    items { id sku name price special_price final_price image url }
  }
}
"""

MYTEK_KEYWORDS = [
    "samsung", "iphone", "xiaomi", "infinix", "oppo", "honor",
    "pc portable", "pc gamer", "imprimante", "ecran", "tablette",
    "tv", "climatiseur", "refrigerateur", "machine a laver", "micro ondes",
    "moulinex", "tefal", "aspirateur", "cuisiniere", "cafetiere",
]

async def crawl_mytek(fetcher: Fetcher, sem: asyncio.Semaphore) -> list:
    """Catalogue Mytek via GraphQL - requete parametree (anti-injection),
    mots-cles en concurrence limitee, arret par mot-cle a la page courte."""
    products = []

    async def crawl_keyword(term: str):
        page, local = 1, []
        while page <= 15:
            payload = {
                "query": MYTEK_QUERY,
                "variables": {"search": term, "page": page, "pageSize": 100},
            }
            async with sem:
                body = await fetcher.post_json(MYTEK_GRAPHQL, payload)
            if not body:
                break
            items = ((body.get("data") or {})
                     .get("opensearchProductSearch") or {}).get("items") or []
            if not items:
                break
            for it in items:
                price = (it.get("special_price") or it.get("final_price")
                         or it.get("price"))
                title = (it.get("name") or "").strip()
                if not title or price is None or float(price) <= 0:
                    continue
                img = it.get("image") or ""
                if img.startswith("/"):
                    img = MYTEK_MEDIA + img
                url = it.get("url") or ""
                if url and not url.startswith("http"):
                    url = "https://www.mytek.tn/" + url.lstrip("/")
                local.append({
                    "source": "Mytek",            # F-22 : orthographe unique
                    "category": "High-Tech",
                    "title": title,
                    "sku": it.get("sku"),
                    "price": round(float(price), 3),
                    "price_raw": f"{float(price):,.3f} TND",
                    "url": url,
                    "image": img or None,
                    "in_stock": True,
                })
            if len(items) < 100:
                break
            page += 1
            await asyncio.sleep(0.15)
        log.info("[Mytek] mot-cle %-15s : %d produits", term, len(local))
        return local

    results = await asyncio.gather(
        *(crawl_keyword(t) for t in MYTEK_KEYWORDS))
    for r in results:
        products.extend(r)
    return products

# ------------------------------------------------------------------ Wamia

# Decouverte de l'audit aout 2026 : l'API GraphQL Magento de wamia.tn
# (/graphql) echappe au challenge Cloudflare qui protege le HTML. On ingere
# le catalogue (~23 000 produits sur 15 categories) en parcourant les
# categories puis la pagination de chaque rayon.
WAMIA_GRAPHQL = "https://www.wamia.tn/graphql"
WAMIA_BASE = "https://www.wamia.tn/"

WAMIA_CATEGORIES_QUERY = """
query {
  categories(filters: {parent_id: {eq: "2"}}) {
    items { id name product_count }
  }
}
"""

WAMIA_CATEGORY_PRODUCTS_QUERY = """
query ($filter: ProductAttributeFilterInput, $pageSize: Int!, $page: Int!) {
  products(filter: $filter, pageSize: $pageSize, currentPage: $page) {
    total_count
    items {
      name
      sku
      canonical_url
      stock_status
      small_image { url }
      price_range { minimum_price { regular_price { value currency } } }
    }
  }
}
"""

async def _wamia_gql(fetcher: Fetcher, query: str, variables: dict):
    body = await fetcher.post_json(
        WAMIA_GRAPHQL, {"query": query, "variables": variables})
    if not body:
        return None
    return (body.get("data") or {})

async def crawl_wamia(fetcher: Fetcher, max_pages_per_cat: int = 50) -> list:
    """Catalogue Wamia via GraphQL : categories puis pagination."""
    data = await _wamia_gql(fetcher, WAMIA_CATEGORIES_QUERY, {})
    cats = ((data or {}).get("categories") or {}).get("items") or []
    if not cats:
        log.warning("[Wamia] categories inaccessibles - API GraphQL "
                    "indisponible ?")
        return []

    products = []
    for cat in cats:
        cat_id, cat_name = str(cat.get("id")), cat.get("name") or "Wamia"
        page, got = 1, 0
        while page <= max_pages_per_cat:
            data = await _wamia_gql(
                fetcher, WAMIA_CATEGORY_PRODUCTS_QUERY,
                {"filter": {"category_id": {"eq": cat_id}},
                 "pageSize": 100, "page": page})
            items = ((data or {}).get("products") or {}).get("items") or []
            if not items:
                break
            for it in items:
                title = html_lib.unescape((it.get("name") or "")).strip()
                canonical = (it.get("canonical_url") or "").strip()
                price_obj = (((it.get("price_range") or {})
                              .get("minimum_price") or {})
                             .get("regular_price") or {})
                price = price_obj.get("value")
                if not title or not canonical or price is None:
                    continue
                try:
                    price_val = round(float(price), 3)
                except (TypeError, ValueError):
                    continue
                if price_val <= 0:
                    continue
                img = ((it.get("small_image") or {}).get("url") or
                       "").strip() or None
                products.append({
                    "source": "Wamia",
                    "category": f"Marketplace - {cat_name}",
                    "title": title,
                    "sku": it.get("sku") or None,
                    "price": price_val,
                    "price_raw": f"{price_val:,.3f} TND",
                    "url": WAMIA_BASE + canonical.lstrip("/"),
                    "image": img,
                    "in_stock": it.get("stock_status") == "IN_STOCK",
                })
                got += 1
            if len(items) < 100:
                break
            page += 1
            await asyncio.sleep(0.2)
        log.info("[Wamia] %-28s : %d produits", cat_name, got)
        await asyncio.sleep(0.3)
    log.info("[Wamia] total : %d produits", len(products))
    return products

# ------------------------------------------------------------------ Drest

async def crawl_drest(fetcher: Fetcher) -> list:
    """Catalogue Drest via l'API Store WooCommerce (JSON public).
    ~27 000 produits, prix en unites mineures (currency_minor_unit)."""
    products, page = [], 1
    seen = set()
    while page <= DREST_MAX_PAGES:
        body = await fetcher.get_json(
            "https://drest.tn/wp-json/wc/store/products",
            params={"per_page": 100, "page": page})
        if not body:
            break
        new_count = 0
        for it in body:
            pid = it.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            title = html_lib.unescape((it.get("name") or "")).strip()
            prices = it.get("prices") or {}
            minor = int(prices.get("currency_minor_unit") or 0)
            raw_price = prices.get("price") or prices.get("regular_price")
            if not title or raw_price is None:
                continue
            try:
                price_val = round(int(raw_price) / (10 ** minor), 3)
            except (ValueError, ZeroDivisionError):
                continue
            if price_val <= 0:
                continue
            images = it.get("images") or []
            products.append({
                "source": "Drest",
                "category": "Parapharmacie & Beauté",
                "title": title,
                "sku": it.get("sku") or None,
                "price": price_val,
                "price_raw": f"{price_val:,.3f} TND",
                "url": it.get("permalink") or "",
                "image": images[0].get("src") if images else None,
                "in_stock": bool(it.get("is_in_stock")),
            })
            new_count += 1
        if new_count == 0:
            break
        if page % 25 == 0:
            log.info("[Drest] page %d : %d produits cumules", page,
                     len(products))
        page += 1
        await asyncio.sleep(0.25)
    log.info("[Drest] total : %d produits", len(products))
    return products

# ------------------------------------------------------------ sauvegarde

UPSERT_QUERY = """
INSERT INTO products (source, category, title, sku, price, price_raw, url,
                      image, in_stock, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
ON CONFLICT (source, url)
DO UPDATE SET
    price      = EXCLUDED.price,
    price_raw  = EXCLUDED.price_raw,
    title      = EXCLUDED.title,
    category   = EXCLUDED.category,
    sku        = COALESCE(EXCLUDED.sku, products.sku),
    image      = COALESCE(EXCLUDED.image, products.image),
    in_stock   = EXCLUDED.in_stock,
    updated_at = NOW();
"""

async def save_to_database_bulk(products: list):
    log.info("Connexion a PostgreSQL...")
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        unique_map = {}
        for p in products:
            unique_map[(p["source"], p["url"])] = p
        deduped = list(unique_map.values())
        log.info("Enregistrement de %d produits (sur %d collectes)",
                 len(deduped), len(products))

        batch_size = 200
        async with conn.transaction():            # F-19 : tout ou rien
            for i in range(0, len(deduped), batch_size):
                batch = deduped[i:i + batch_size]
                records = [
                    (
                        str(p["source"])[:100],
                        str(p["category"])[:100],
                        str(p["title"]),
                        str(p["sku"])[:140] if p.get("sku") else None,
                        p["price"],
                        str(p["price_raw"])[:50] if p.get("price_raw") else None,
                        p["url"],
                        p.get("image"),
                        bool(p.get("in_stock", True))
                    )
                    for p in batch
                ]
                await conn.executemany(UPSERT_QUERY, records)
        log.info("SUCCES : %d produits enregistres dans Supabase !", len(deduped))
    finally:
        await conn.close()

# ------------------------------------------------------------------- main

async def main():
    log.info("Demarrage du crawler PrixTN")
    if PROXY_URL:
        log.info("Proxy residentiel actif : %s", PROXY_URL)
    all_products = []
    fetcher = Fetcher()
    try:
        # 1. Rayons PrestaShop (SpaceNet, Tunisianet, Yeswikam, MyCare)
        for target in CATALOG_TARGETS:
            items = await crawl_rayon_prestashop(
                fetcher, target["source"], target["category"],
                target["url"], target["max_pages"])
            all_products.extend(items)
            await asyncio.sleep(0.3)

        # 2. Rayons Sangour (WooCommerce) - IP tunisienne requise
        sangour_total = 0
        for category, url in SANGOUR_RAYONS:
            items = await crawl_rayon_woocommerce(
                fetcher, "Sangour", category, url, max_pages=10)
            sangour_total += len(items)
            all_products.extend(items)
            await asyncio.sleep(0.3)
        if sangour_total == 0:
            log.warning("Sangour : 0 produit - IP tunisienne absente ? "
                        "Definissez PROXY_URL (proxy residentiel TN) ou "
                        "lancez depuis une IP tunisienne.")

        # 3. Mytek GraphQL (source unique "Mytek")
        sem = asyncio.Semaphore(4)
        all_products.extend(await crawl_mytek(fetcher, sem))

        # 4. Wamia via GraphQL Magento (nouveau, decouverte audit 2026 :
        #    l'API echappe au challenge Cloudflare, ~23 000 produits)
        all_products.extend(await crawl_wamia(fetcher))

        # 5. Drest via API Store (nouveau, decouverte audit 2026)
        all_products.extend(await crawl_drest(fetcher))
    finally:
        await fetcher.close()

    log.info("TOTAL GLOBAL : %d produits", len(all_products))
    if not all_products:
        log.error("AUCUN produit collecte - les boutiques ont-elles "
                  "change de structure ? (alerte CI)")
        sys.exit(1)                                # alerte CI (F-11)
    if not DATABASE_URL:
        log.error("DATABASE_URL absent - rien a sauvegarder")
        sys.exit(1)
    await save_to_database_bulk(all_products)

if __name__ == "__main__":
    asyncio.run(main())
