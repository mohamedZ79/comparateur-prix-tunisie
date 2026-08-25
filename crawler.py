"""
Crawler National PrixTN - Intégration Complète de TOUTES les Parapharmacies + High-Tech + Sangour.
"""
import asyncio
import html as html_lib
import logging
import os
import random
import re
import sys
import unicodedata
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin

import asyncpg
import httpx
from bs4 import BeautifulSoup

DATABASE_URL = os.getenv("DATABASE_URL")
PROXY_URL = os.getenv("PROXY_URL")
DREST_MAX_PAGES = int(os.getenv("DREST_MAX_PAGES", "300"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
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
except ImportError:
    HAS_CFFI = False

# -----------------------------------------------------------------------------
# Parsing de Prix & Titres
# -----------------------------------------------------------------------------
def parse_tnd_price(raw: str) -> Optional[float]:
    if not raw:
        return None
    cleaned = unicodedata.normalize("NFKD", str(raw)).strip().lower()
    
    # Format Carrefour (ex: "249DT000")
    m_c = re.search(r'(\d+)\s*(?:dt|tnd|d\.t)\s*(\d{3})', cleaned)
    if m_c:
        return float(f"{m_c.group(1)}.{m_c.group(2)}")
        
    tnd_m = re.findall(r'(\d+[\s\.,]+\d{2,3})\s*(?:dt|tnd|d\.t|dinars?)', cleaned)
    if tnd_m:
        val_str = tnd_m[-1].replace(" ", "").replace(",", ".")
        try:
            return round(float(val_str), 3)
        except ValueError:
            pass

    cleaned_num = re.sub(r'(?:dt|tnd|dinars?|d\.t)', '', cleaned).strip()
    if "," in cleaned_num:
        cleaned_num = cleaned_num.replace(" ", "").replace(".", "").replace(",", ".")
    else:
        cleaned_num = cleaned_num.replace(" ", "")
        if re.fullmatch(r"\d{1,3}(\.\d{3}){2,}", cleaned_num):
            cleaned_num = cleaned_num.replace(".", "")

    m = re.search(r'(\d+(?:\.\d+)?)', cleaned_num)
    if m:
        try:
            return round(float(m.group(1)), 3)
        except ValueError:
            return None
    return None

def extract_title_link(card) -> Optional[Tuple[str, str]]:
    a = card.select_one(
        "h2.product_name a, .product_name a, h2.product-title a, h3.product-title a, "
        "h2.product-name a, a.product-name, .product-title a, .name a, h3 a, h2 a, a[title], "
        ".wd-entities-title a, .woocommerce-loop-product__title, a.woocommerce-LoopProduct-link, "
        ".c-post-list__header-link, p.name.product-title a"
    )
    if a:
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if title and href:
            return title, href
    link = card.select_one("a[href]")
    if link:
        title = link.get_text(strip=True) or card.get("title", "")
        href = link.get("href", "")
        if title and href:
            return title, href
    return None

# -----------------------------------------------------------------------------
# Fetcher Résilient avec repli Chrome TLS (Contourne Cloudflare)
# -----------------------------------------------------------------------------
class Fetcher:
    def __init__(self):
        self.client = httpx.AsyncClient(
            follow_redirects=True, verify=False,
            timeout=httpx.Timeout(15.0, connect=10.0),
            headers=HEADERS,
            proxy=PROXY_URL if PROXY_URL else None,
        )
        self._cffi = None

    async def _cffi_session(self):
        if self._cffi is None and HAS_CFFI:
            self._cffi = CffiSession(impersonate="chrome124", proxy=PROXY_URL if PROXY_URL else None, timeout=20)
        return self._cffi

    async def get(self, url: str) -> Optional[str]:
        status = None
        try:
            r = await self.client.get(url)
            status = r.status_code
            if r.status_code == 200:
                return r.text
        except httpx.HTTPError as e:
            log.warning("httpx error %s : %s", url, type(e).__name__)

        if status in (403, 429, 503) and HAS_CFFI:
            try:
                s = await self._cffi_session()
                r2 = await s.get(url, allow_redirects=True)
                if r2.status_code == 200:
                    log.info("✅ Débloqué via curl_cffi (Chrome TLS) : %s", url)
                    return r2.text
                status = r2.status_code
            except Exception as e:
                log.warning("curl_cffi error %s : %s", url, type(e).__name__)
        elif status is not None and status != 404:
            log.warning("HTTP %s sur %s", status, url)
        return None

    async def get_json(self, url: str, params: dict = None) -> Optional[list]:
        try:
            r = await self.client.get(url, params=params, headers={"Accept": "application/json"})
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
                log.warning("curl_cffi JSON error %s : %s", url, type(e).__name__)
        return None

    async def post_json(self, url: str, payload: dict) -> Optional[dict]:
        json_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            r = await self.client.post(url, json=payload, headers=json_headers)
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
                log.warning("curl_cffi POST error %s : %s", url, type(e).__name__)
        return None

    async def close(self):
        await self.client.aclose()
        if self._cffi is not None:
            await self._cffi.close()

# -----------------------------------------------------------------------------
# TOUTES LES 21 PARAPHARMACIES + HIGH-TECH DU MARCHÉ TUNISIEN
# -----------------------------------------------------------------------------
CATALOG_TARGETS = [
    # --- 1. LE RÉSEAU NATIONAL DES PARAPHARMACIES (21 Enseignes) ---
    {"source": "Yeswikam", "category": "Parapharmacie", "url": "https://www.yeswikam.com/2-accueil?page={page}", "max_pages": 40},
    {"source": "Parashop", "category": "Parapharmacie", "url": "https://www.parashop.tn/recherche?s=soin&page={page}", "max_pages": 20},
    {"source": "Pharma-Shop", "category": "Parapharmacie", "url": "https://pharma-shop.tn/recherche?s=soin&page={page}", "max_pages": 20},
    {"source": "Parastore", "category": "Parapharmacie", "url": "https://parastore.tn/recherche?s=soin&page={page}", "max_pages": 20},
    {"source": "Paralabel", "category": "Parapharmacie", "url": "https://www.paralabel.tn/recherche?s=soin&page={page}", "max_pages": 15},
    {"source": "Eden Pharma", "category": "Parapharmacie", "url": "https://edenpharma.tn/recherche?s=soin&page={page}", "max_pages": 20},
    {"source": "Phytonat", "category": "Parapharmacie", "url": "https://phytonat.tn/recherche?s=soin&page={page}", "max_pages": 15},
    {"source": "Para Fendri", "category": "Parapharmacie", "url": "https://parafendri.tn/recherche?s=soin&page={page}", "max_pages": 15},
    {"source": "Para House", "category": "Parapharmacie", "url": "https://www.parahouse.tn/fr/recherche?s=soin&page={page}", "max_pages": 15},
    {"source": "Para du Bonheur", "category": "Parapharmacie", "url": "https://paradubonheur.tn/recherche?s=soin&page={page}", "max_pages": 15},
    {"source": "La Para du Lac", "category": "Parapharmacie", "url": "https://laparadulac.com/recherche?s=soin&page={page}", "max_pages": 15},
    {"source": "Taicir Fendri", "category": "Parapharmacie", "url": "https://www.taicir.tn/recherche?s=soin&page={page}", "max_pages": 15},
    {"source": "MyCare", "category": "Parapharmacie", "url": "https://mycare.tn/recherche?s=soin&page={page}", "max_pages": 20},
    {"source": "Paraforce", "category": "Parapharmacie", "url": "https://paraforce.tn/recherche?s=soin&page={page}", "max_pages": 15},
    
    # Parapharmacies WooCommerce
    {"source": "Parapharmacie.tn", "category": "Parapharmacie", "url": "https://parapharmacie.tn/?s=soin&post_type=product&paged={page}", "max_pages": 20},
    {"source": "MaPara Tunisie", "category": "Parapharmacie", "url": "https://www.maparatunisie.tn/?s=soin&post_type=product&paged={page}", "max_pages": 20},
    {"source": "MS Para", "category": "Parapharmacie", "url": "https://mspara.com/?s=soin&post_type=product&paged={page}", "max_pages": 15},
    {"source": "Tunisie Para", "category": "Parapharmacie", "url": "https://tunisiepara.com/?s=soin&post_type=product&paged={page}", "max_pages": 15},
    {"source": "ParaTunisie", "category": "Parapharmacie", "url": "https://www.paratunisie.com/?s=soin&post_type=product&paged={page}", "max_pages": 15},
    {"source": "ParaHealth", "category": "Parapharmacie", "url": "https://parahealth.tn/?s=soin&post_type=product&paged={page}", "max_pages": 15},
    {"source": "Paraepharma", "category": "Parapharmacie", "url": "https://paraepharma.com/?s=soin&post_type=product&paged={page}", "max_pages": 15},
    {"source": "Skincare Para", "category": "Parapharmacie", "url": "https://skincarepara.com/?s=soin&post_type=product&paged={page}", "max_pages": 12},
    {"source": "Coquette.tn", "category": "Parapharmacie", "url": "https://www.coquette.tn/?s=soin&post_type=product&paged={page}", "max_pages": 12},

    # --- 2. HIGH-TECH, PC, TÉLÉPHONIE & ÉLECTROMÉNAGER ---
    {"source": "SpaceNet", "category": "High-Tech", "url": "https://spacenet.tn/13-telephonie-tablette?page={page}", "max_pages": 15},
    {"source": "SpaceNet", "category": "High-Tech", "url": "https://spacenet.tn/14-pc-portable?page={page}", "max_pages": 15},
    {"source": "SpaceNet", "category": "High-Tech", "url": "https://spacenet.tn/11-informatique?page={page}", "max_pages": 15},
    {"source": "SpaceNet", "category": "High-Tech", "url": "https://spacenet.tn/15-tv-son?page={page}", "max_pages": 12},
    {"source": "SpaceNet", "category": "Électroménager", "url": "https://spacenet.tn/18-electromenager?page={page}", "max_pages": 15},
    {"source": "SpaceNet", "category": "Électroménager", "url": "https://spacenet.tn/19-petit-electromenager?page={page}", "max_pages": 15},
    {"source": "SpaceNet", "category": "Climatisation", "url": "https://spacenet.tn/20-climatisation-chauffage?page={page}", "max_pages": 10},
    {"source": "SpaceNet", "category": "Beauté & Soins", "url": "https://spacenet.tn/22-beaute-sante?page={page}", "max_pages": 10},

    {"source": "Tunisianet", "category": "High-Tech", "url": "https://www.tunisianet.com.tn/377-telephone-portable-tunisie?page={page}", "max_pages": 20},
    {"source": "Tunisianet", "category": "High-Tech", "url": "https://www.tunisianet.com.tn/301-pc-portable-tunisie?page={page}", "max_pages": 18},
    {"source": "Tunisianet", "category": "High-Tech", "url": "https://www.tunisianet.com.tn/300-informatique-tunisie?page={page}", "max_pages": 18},
    {"source": "Tunisianet", "category": "High-Tech", "url": "https://www.tunisianet.com.tn/378-tv-son-et-photos-tunisie?page={page}", "max_pages": 15},
    {"source": "Tunisianet", "category": "Électroménager", "url": "https://www.tunisianet.com.tn/439-electromenager-tunisie?page={page}", "max_pages": 18},
    {"source": "Tunisianet", "category": "Petit Électro", "url": "https://www.tunisianet.com.tn/440-petit-electromenager-tunisie?page={page}", "max_pages": 18},
    {"source": "Tunisianet", "category": "Climatisation", "url": "https://www.tunisianet.com.tn/505-climatisation-et-chauffage?page={page}", "max_pages": 12},
    {"source": "Tunisianet", "category": "Beauté & Soins", "url": "https://www.tunisianet.com.tn/690-beaute-et-sante?page={page}", "max_pages": 15},

    {"source": "Batam", "category": "Électroménager", "url": "https://batam.com.tn/recherche?s=electromenager&page={page}", "max_pages": 15},
    {"source": "Batam", "category": "Électroménager", "url": "https://batam.com.tn/recherche?s=tv&page={page}", "max_pages": 10},
    {"source": "Technopro", "category": "High-Tech", "url": "https://www.technopro-online.com/recherche?s=smartphone&page={page}", "max_pages": 15},
    {"source": "Technopro", "category": "High-Tech", "url": "https://www.technopro-online.com/recherche?s=pc+portable&page={page}", "max_pages": 15},
    {"source": "SBS Informatique", "category": "Gaming & PC", "url": "https://www.sbsinformatique.com/recherche?s=pc+gamer&page={page}", "max_pages": 15},
    {"source": "Darty TN", "category": "Électroménager", "url": "https://darty.tn/recherche?s=electromenager&page={page}", "max_pages": 12},
]

# --- 3. SANGOUR (Détergents, Judy, Javel, Cuisine Tramontina, Tefal) ---
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

# -----------------------------------------------------------------------------
# Fonctions de Scraping Asynchrone
# -----------------------------------------------------------------------------
OOS_MARKERS = ("rupture de stock", "en rupture", "épuisé", "epuise", "out of stock", "sold out", "unavailable")

def detect_in_stock(card) -> bool:
    el = card.select_one(".out-of-stock, .unavailable, [class*='rupture'], [class*='epuise'], [class*='outofstock'], .stock.unavailable")
    if el is not None:
        return False
    text = card.get_text(" ", strip=True).lower()
    return not any(m in text for m in OOS_MARKERS)

def parse_products(html: str, source: str, category: str, page_url: str) -> list:
    products = []
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(
        ".product-grid-item, .wd-product, div.product, li.product, "
        "article.product-miniature, .product-miniature, .product-item, "
        "div.product-small, .ajax_block_product, .c-post-list"
    )

    for p in cards:
        extracted = extract_title_link(p)
        price_tag = p.select_one(
            "ins .woocommerce-Price-amount, ins .amount, .price ins, "
            ".price .amount, .woocommerce-Price-amount, .price, span.price, "
            "[itemprop='price'], .product-price, .current-price"
        )
        img_tag = p.select_one(
            ".product-element-top img, img.wp-post-image, img.product_image, "
            ".thumbnail-container img, .product-thumbnail img, img, .c-post-list__thumb img, .box-image img"
        )
        ref_tag = p.select_one(".product-reference, .reference, [itemprop='sku'], .sku")

        if not (extracted and price_tag):
            continue

        title, href = extracted
        product_url = urljoin(page_url, href)
        price_val = parse_tnd_price(price_tag.get_text(strip=True))
        img_url = (img_tag.get("data-full-size-image-url") or img_tag.get("data-src") or img_tag.get("src")) if img_tag else None
        ref_val = (ref_tag.get_text(strip=True).replace("Réf :", "").strip() if ref_tag else None)
        in_stock = detect_in_stock(p)

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

async def crawl_page(fetcher: Fetcher, source: str, category: str, url: str) -> list:
    html = await fetcher.get(url)
    if html is None:
        return []
    return parse_products(html, source, category, url)

async def crawl_rayon_prestashop(fetcher: Fetcher, source: str, category: str, url_tpl: str, max_pages: int, batch: int = 3) -> list:
    products, page = [], 1
    while page <= max_pages:
        urls = [url_tpl.format(page=p) for p in range(page, page + batch)]
        results = await asyncio.gather(*(crawl_page(fetcher, source, category, u) for u in urls))
        if not any(results):
            break
        for items in results:
            products.extend(items)
        page += batch
        await asyncio.sleep(0.15)
    log.info("[%s] %s : %d produits", source, category, len(products))
    return products

async def crawl_rayon_woocommerce(fetcher: Fetcher, source: str, category: str, base_url: str, max_pages: int = 10, batch: int = 3) -> list:
    products, page = [], 1
    while page <= max_pages:
        urls = [base_url if p == 1 else f"{base_url}page/{p}/" for p in range(page, page + batch)]
        results = await asyncio.gather(*(crawl_page(fetcher, source, category, u) for u in urls))
        if not any(results):
            break
        for items in results:
            products.extend(items)
        page += batch
        await asyncio.sleep(0.15)
    log.info("[%s] %s : %d produits", source, category, len(products))
    return products

# -----------------------------------------------------------------------------
# Mytek (API GraphQL OpenSearch - 12 000+ Produits)
# -----------------------------------------------------------------------------
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
    "moulinex", "tefal", "aspirateur", "cuisiniere", "cafetiere", "robot",
    "casque", "souris", "clavier", "disque dur", "bureau", "onduleur"
]

async def crawl_mytek(fetcher: Fetcher, sem: asyncio.Semaphore) -> list:
    products = []

    async def crawl_keyword(term: str):
        page, local = 1, []
        while page <= 12:
            payload = {
                "query": MYTEK_QUERY,
                "variables": {"search": term, "page": page, "pageSize": 100},
            }
            async with sem:
                body = await fetcher.post_json(MYTEK_GRAPHQL, payload)
            if not body:
                break
            items = ((body.get("data") or {}).get("opensearchProductSearch") or {}).get("items") or []
            if not items:
                break
            for it in items:
                price = it.get("special_price") or it.get("final_price") or it.get("price")
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
                    "source": "Mytek",
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
            await asyncio.sleep(0.1)
        log.info("[Mytek] mot-clé %-15s : %d produits", term, len(local))
        return local

    results = await asyncio.gather(*(crawl_keyword(t) for t in MYTEK_KEYWORDS))
    for r in results:
        products.extend(r)
    return products

# -----------------------------------------------------------------------------
# Wamia (API GraphQL Magento - 20 000+ Produits)
# -----------------------------------------------------------------------------
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

async def crawl_wamia(fetcher: Fetcher, max_pages_per_cat: int = 40) -> list:
    body = await fetcher.post_json(WAMIA_GRAPHQL, {"query": WAMIA_CATEGORIES_QUERY, "variables": {}})
    cats = ((body or {}).get("data") or {}).get("categories", {}).get("items", [])
    if not cats:
        log.warning("[Wamia] Catégories inaccessibles")
        return []

    products = []
    for cat in cats:
        cat_id, cat_name = str(cat.get("id")), cat.get("name") or "Wamia"
        page, got = 1, 0
        while page <= max_pages_per_cat:
            data = await fetcher.post_json(
                WAMIA_GRAPHQL,
                {"query": WAMIA_CATEGORY_PRODUCTS_QUERY, "variables": {"filter": {"category_id": {"eq": cat_id}}, "pageSize": 100, "page": page}}
            )
            items = ((data or {}).get("data") or {}).get("products", {}).get("items", [])
            if not items:
                break
            for it in items:
                title = html_lib.unescape((it.get("name") or "")).strip()
                canonical = (it.get("canonical_url") or "").strip()
                price = ((it.get("price_range") or {}).get("minimum_price") or {}).get("regular_price", {}).get("value")
                if not title or not canonical or price is None:
                    continue
                try:
                    price_val = round(float(price), 3)
                except (TypeError, ValueError):
                    continue
                if price_val <= 0:
                    continue
                img = ((it.get("small_image") or {}).get("url") or "").strip() or None
                products.append({
                    "source": "Wamia",
                    "category": f"Maison - {cat_name}",
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
            await asyncio.sleep(0.1)
        log.info("[Wamia] %-25s : %d produits", cat_name, got)
    log.info("[Wamia] Total collecté : %d produits", len(products))
    return products

# -----------------------------------------------------------------------------
# Drest (API Store WooCommerce JSON - 27 000+ Produits)
# -----------------------------------------------------------------------------
async def crawl_drest(fetcher: Fetcher) -> list:
    products, page, seen = [], 1, set()
    while page <= DREST_MAX_PAGES:
        body = await fetcher.get_json("https://drest.tn/wp-json/wc/store/products", params={"per_page": 100, "page": page})
        if not body or not isinstance(body, list):
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
            except Exception:
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
            log.info("[Drest] page %d : %d produits cumulés", page, len(products))
        page += 1
        await asyncio.sleep(0.1)
    log.info("[Drest] Total collecté : %d produits", len(products))
    return products

# -----------------------------------------------------------------------------
# Enregistrement PostgreSQL Sécurisé (Anti-Truncation)
# -----------------------------------------------------------------------------
UPSERT_QUERY = """
INSERT INTO products (source, category, title, sku, price, price_raw, url, image, in_stock, updated_at)
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
    log.info("Connexion à PostgreSQL Supabase...")
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        unique_map = {}
        for p in products:
            unique_map[(p["source"], p["url"])] = p
        deduped = list(unique_map.values())
        log.info("Enregistrement de %d produits uniques (sur %d collectés)", len(deduped), len(products))

        batch_size = 200
        async with conn.transaction():
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
        log.info("🎉 SUCCÈS : %d produits enregistrés dans Supabase !", len(deduped))
    finally:
        await conn.close()

# -----------------------------------------------------------------------------
# Main Orchestrateur
# -----------------------------------------------------------------------------
async def main():
    log.info("Démarrage du Grand Crawler PrixTN (National Complet)")
    if PROXY_URL:
        log.info("Proxy actif : %s", PROXY_URL)
    all_products = []
    fetcher = Fetcher()
    try:
        # 1. Drest via API Store JSON (27 000+ produits)
        all_products.extend(await crawl_drest(fetcher))

        # 2. Wamia via GraphQL Magento (20 000+ produits)
        all_products.extend(await crawl_wamia(fetcher))

        # 3. Mytek via GraphQL OpenSearch (12 000+ produits)
        sem = asyncio.Semaphore(4)
        all_products.extend(await crawl_mytek(fetcher, sem))

        # 4. Rayons PrestaShop & WooCommerce (SpaceNet, Tunisianet, 21 Parapharmacies, Batam, Technopro...)
        for target in CATALOG_TARGETS:
            items = await crawl_rayon_prestashop(
                fetcher, target["source"], target["category"],
                target["url"], target["max_pages"]
            )
            all_products.extend(items)
            await asyncio.sleep(0.1)

        # 5. Rayons Sangour (avec curl_cffi Chrome TLS + Proxy)
        for category, url in SANGOUR_RAYONS:
            items = await crawl_rayon_woocommerce(fetcher, "Sangour", category, url, max_pages=10)
            all_products.extend(items)
            await asyncio.sleep(0.1)

    finally:
        await fetcher.close()

    log.info("TOTAL GLOBAL COLLECTÉ : %d produits", len(all_products))
    if not all_products:
        log.error("Aucun produit collecté.")
        sys.exit(1)
    if not DATABASE_URL:
        log.error("DATABASE_URL absent.")
        sys.exit(1)
        
    await save_to_database_bulk(all_products)

if __name__ == "__main__":
    asyncio.run(main())