"""
Crawler National PrixTN - Intégration Complète des 21 Parapharmacies + High-Tech + Sangour.
"""
import asyncio
import html as html_lib
import logging
import os
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
    
    # Format Carrefour
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
# Fetcher Résilient
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
                    log.info("✅ Débloqué via curl_cffi : %s", url)
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
    # 1. PARAPHARMACIES PRESTASHOP (URLs officielles avec controller=search)
    {"source": "Yeswikam", "category": "Parapharmacie", "url": "https://www.yeswikam.com/2-accueil?page={page}", "max_pages": 40},
    {"source": "Yeswikam", "category": "Parapharmacie", "url": "https://www.yeswikam.com/3-visage?page={page}", "max_pages": 25},
    {"source": "Parashop", "category": "Parapharmacie", "url": "https://www.parashop.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 20},
    {"source": "Pharma-Shop", "category": "Parapharmacie", "url": "https://pharma-shop.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 20},
    {"source": "Parastore", "category": "Parapharmacie", "url": "https://parastore.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 20},
    {"source": "Paralabel", "category": "Parapharmacie", "url": "https://www.paralabel.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 15},
    {"source": "Eden Pharma", "category": "Parapharmacie", "url": "https://edenpharma.tn/fr/recherche?controller=search&s=soin&page={page}", "max_pages": 20},
    {"source": "Phytonat", "category": "Parapharmacie", "url": "https://phytonat.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 15},
    {"source": "Para Fendri", "category": "Parapharmacie", "url": "https://parafendri.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 15},
    {"source": "Para House", "category": "Parapharmacie", "url": "https://www.parahouse.tn/fr/recherche?controller=search&s=soin&page={page}", "max_pages": 15},
    {"source": "Para du Bonheur", "category": "Parapharmacie", "url": "https://paradubonheur.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 15},
    {"source": "La Para du Lac", "category": "Parapharmacie", "url": "https://laparadulac.com/recherche?controller=search&s=soin&page={page}", "max_pages": 15},
    {"source": "Taicir Fendri", "category": "Parapharmacie", "url": "https://www.taicir.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 15},
    {"source": "MyCare", "category": "Parapharmacie", "url": "https://mycare.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 20},
    {"source": "Paraforce", "category": "Parapharmacie", "url": "https://paraforce.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 15},

    # 2. HIGH-TECH & ÉLECTROMÉNAGER PRESTASHOP
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

    {"source": "Batam", "category": "Électroménager", "url": "https://batam.com.tn/recherche?controller=search&s=electromenager&page={page}", "max_pages": 15},
    {"source": "Batam", "category": "Électroménager", "url": "https://batam.com.tn/recherche?controller=search&s=tv&page={page}", "max_pages": 10},
    {"source": "Technopro", "category": "High-Tech", "url": "https://www.technopro-online.com/recherche?controller=search&s=smartphone&page={page}", "max_pages": 15},
    {"source": "Technopro", "category": "High-Tech", "url": "https://www.technopro-online.com/recherche?controller=search&s=pc+portable&page={page}", "max_pages": 15},
    {"source": "SBS Informatique", "category": "Gaming & PC", "url": "https://www.sbsinformatique.com/recherche?controller=search&s=pc+gamer&page={page}", "max_pages": 15},
    {"source": "Darty TN", "category": "Électroménager", "url": "https://darty.tn/recherche?controller=search&s=electromenager&page={page}", "max_pages": 12},
]

# 3. PARAPHARMACIES WOOCOMMERCE (Pagination /page/N/)
WOOCOMMERCE_TARGETS = [
    ("Parapharmacie.tn", "Parapharmacie", "https://parapharmacie.tn/", 20),
    ("MaPara Tunisie", "Parapharmacie", "https://www.maparatunisie.tn/", 20),
    ("MS Para", "Parapharmacie", "https://mspara.com/", 15),
    ("Tunisie Para", "Parapharmacie", "https://tunisiepara.com/", 15),
    ("ParaTunisie", "Parapharmacie", "https://www.paratunisie.com/", 15),
    ("ParaHealth", "Parapharmacie", "https://parahealth.tn/", 15),
    ("Paraepharma", "Parapharmacie", "https://paraepharma.com/", 15),
    ("Skincare Para", "Parapharmacie", "https://skincarepara.com/", 12),
    ("Coquette.tn", "Parapharmacie", "https://www.coquette.tn/", 12),
]

# -----------------------------------------------------------------------------
# Fonctions de Scraping
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
        await asyncio.sleep(0.1)
    log.info("[%s] %s : %d produits", source, category, len(products))
    return products

async def crawl_woocommerce_search(fetcher: Fetcher, source: str, category: str, base_url: str, max_pages: int = 15, batch: int = 3) -> list:
    products, page = [], 1
    while page <= max_pages:
        urls = [f"{base_url}?s=soin&post_type=product" if p == 1 else f"{base_url}page/{p}/?s=soin&post_type=product" for p in range(page, page + batch)]
        results = await asyncio.gather(*(crawl_page(fetcher, source, category, u) for u in urls))
        if not any(results):
            break
        for items in results:
            products.extend(items)
        page += batch
        await asyncio.sleep(0.1)
    log.info("[%s] %s : %d produits", source, category, len(products))
    return products

# -----------------------------------------------------------------------------
# SANGOUR (Extraction via API Store JSON + Rayons Directs)
# -----------------------------------------------------------------------------
async def crawl_sangour(fetcher: Fetcher) -> list:
    log.info("Démarrage du crawl Sangour...")
    products, seen = [], set()
    
    # 1. Tentative API Store JSON (Ultra-rapide)
    for page in range(1, 30):
        body = await fetcher.get_json("https://sangour.tn/wp-json/wc/store/products", params={"per_page": 100, "page": page})
        if not body or not isinstance(body, list):
            break
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
                "source": "Sangour",
                "category": "Maison & Entretien",
                "title": title,
                "sku": it.get("sku") or None,
                "price": price_val,
                "price_raw": f"{price_val:,.3f} TND",
                "url": it.get("permalink") or "",
                "image": images[0].get("src") if images else None,
                "in_stock": bool(it.get("is_in_stock")),
            })

    # 2. Rayons HTML directs de secours
    if not products:
        sangour_urls = [
            "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/javel/",
            "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/sol/",
            "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/vaisselles/",
            "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/linge/",
            "https://sangour.tn/marques/judy/",
            "https://sangour.tn/marques/tramontina/",
            "https://sangour.tn/marques/tefal/",
            "https://sangour.tn/marques/moulinex/",
        ]
        for u in sangour_urls:
            html = await fetcher.get(u)
            if html:
                products.extend(parse_products(html, "Sangour", "Maison & Entretien", u))

    log.info("[Sangour] Total collecté : %d produits", len(products))
    return products

# -----------------------------------------------------------------------------
# Mytek & Wamia & Drest (APIs Massives)
# -----------------------------------------------------------------------------
MYTEK_GRAPHQL = "https://www.mytek.tn/graphql"
MYTEK_MEDIA = "https://www.mytek.tn/media/catalog/product"
MYTEK_QUERY = """
query ($search: String, $page: Int, $pageSize: Int) {
  opensearchProductSearch(search: $search, page: $page, pageSize: $pageSize) {
    items { id sku name price spe