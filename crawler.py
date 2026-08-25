"""
Crawler National PrixTN - Rayons Complets des 21 Parapharmacies + High-Tech + Sangour.
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
    """Extrait (titre, href) d'une carte produit, robuste aux themes.

    Certains themes PrestaShop (yeswikam, darty, sbs, parafendri - verifie
    aout 2026) ne mettent PAS le titre dans le heading : le heading porte
    la marque ou rien, et le vrai titre vit dans une ancre simple.
    Strategie : heading si texte >= 10, sinon ancre produit la plus
    descriptive (liens marque/categorie/UI exclus).
    """
    a = card.select_one(
        "h2.product_name a, .product_name a, h2.product-title a, "
        "h3.product-title a, h2.product-name a, a.product-name, "
        ".product-title a, .woocommerce-loop-product__title a, "
        ".wd-entities-title a, .name a")
    if a:
        text = a.get_text(strip=True)
        if len(text) >= 10:
            return text, a.get("href", "")

    best_text, best_href = "", ""
    for link in card.select("a[href]"):
        href = link.get("href") or ""
        text = link.get_text(strip=True)
        if not href or href.startswith("#"):
            continue
        if "/marque/" in href or "/brand/" in href or "/manufacturer" in href:
            continue
        if not text or text.lower() in _UI_TEXTS or len(text) < 10:
            continue
        if len(text) > len(best_text):
            best_text, best_href = text, href
    if best_text:
        return best_text, best_href
    if a:
        return a.get_text(strip=True), a.get("href", "")
    return None

# -----------------------------------------------------------------------------
# Fetcher Résilient
# -----------------------------------------------------------------------------
class Fetcher:
    def __init__(self):
        self.client = httpx.AsyncClient(
            follow_redirects=True, verify=True,
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
# TOUS LES RAYONS COMPLETS DES 21 PARAPHARMACIES & HIGH-TECH
# -----------------------------------------------------------------------------
CATALOG_TARGETS = [
    # --- 1. RAYONS COMPLETS DES PARAPHARMACIES (Visage, Corps, Cheveux, Solaire, Soins) ---
    {"source": "Yeswikam", "category": "Parapharmacie", "url": "https://www.yeswikam.com/2-accueil?page={page}", "max_pages": 40},
    {"source": "Yeswikam", "category": "Parapharmacie", "url": "https://www.yeswikam.com/3-visage?page={page}", "max_pages": 30},
    {"source": "Yeswikam", "category": "Parapharmacie", "url": "https://www.yeswikam.com/4-corps?page={page}", "max_pages": 25},
    
    # Pharma-Shop : 403 Cloudflare sur toutes les pages - retire
    # (4 rayons supprimes)

    {"source": "Eden Pharma", "category": "Parapharmacie", "url": "https://edenpharma.tn/fr/categorie/visage?page={page}", "max_pages": 25},
    {"source": "Eden Pharma", "category": "Parapharmacie", "url": "https://edenpharma.tn/fr/categorie/corps?page={page}", "max_pages": 20},
    {"source": "Eden Pharma", "category": "Parapharmacie", "url": "https://edenpharma.tn/fr/categorie/cheveux?page={page}", "max_pages": 15},

    {"source": "Parastore", "category": "Parapharmacie", "url": "https://parastore.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 25},
    {"source": "Parastore", "category": "Parapharmacie", "url": "https://parastore.tn/recherche?controller=search&s=visage&page={page}", "max_pages": 20},
    {"source": "Parastore", "category": "Parapharmacie", "url": "https://parastore.tn/recherche?controller=search&s=corps&page={page}", "max_pages": 15},

    {"source": "Parashop", "category": "Parapharmacie", "url": "https://www.parashop.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 25},
    {"source": "Paralabel", "category": "Parapharmacie", "url": "https://www.paralabel.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 20},
    {"source": "Para Fendri", "category": "Parapharmacie", "url": "https://parafendri.tn/3-visage?page={page}", "max_pages": 20},
    {"source": "Para Fendri", "category": "Parapharmacie", "url": "https://parafendri.tn/4-corps?page={page}", "max_pages": 20},
    {"source": "Para House", "category": "Parapharmacie", "url": "https://www.parahouse.tn/fr/recherche?controller=search&s=soin&page={page}", "max_pages": 20},
    {"source": "La Para du Lac", "category": "Parapharmacie", "url": "https://laparadulac.com/collections/visage?page={page}", "max_pages": 20},
    {"source": "Taicir Fendri", "category": "Parapharmacie", "url": "https://www.taicir.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 20},
    {"source": "MyCare", "category": "Parapharmacie", "url": "https://mycare.tn/recherche?controller=search&s=soin&page={page}", "max_pages": 25},
    # Paraforce.tn : domaine mort (NXDOMAIN) - retire
    # Pharma-Shop : 403 Cloudflare sur toutes les pages - retire

    # --- 2. PARAPHARMACIES WOOCOMMERCE ---
    {"source": "Parapharmacie.tn", "category": "Parapharmacie", "url": "https://parapharmacie.tn/page/{page}/?s=soin&post_type=product", "max_pages": 25},
    {"source": "MaPara Tunisie", "category": "Parapharmacie", "url": "https://www.maparatunisie.tn/page/{page}/?s=soin&post_type=product", "max_pages": 25},
    {"source": "Phytonat", "category": "Parapharmacie", "url": "https://phytonat.tn/categorie-produit/visage/page/{page}/", "max_pages": 20},
    {"source": "MS Para", "category": "Parapharmacie", "url": "https://mspara.com/page/{page}/?s=soin&post_type=product", "max_pages": 20},
    {"source": "Tunisie Para", "category": "Parapharmacie", "url": "https://tunisiepara.com/page/{page}/?s=soin&post_type=product", "max_pages": 20},
    {"source": "ParaTunisie", "category": "Parapharmacie", "url": "https://www.paratunisie.com/page/{page}/?s=soin&post_type=product", "max_pages": 20},
    {"source": "ParaHealth", "category": "Parapharmacie", "url": "https://parahealth.tn/page/{page}/?s=soin&post_type=product", "max_pages": 20},
    {"source": "Paraepharma", "category": "Parapharmacie", "url": "https://paraepharma.com/page/{page}/?s=soin&post_type=product", "max_pages": 20},
    {"source": "Para du Bonheur", "category": "Parapharmacie", "url": "https://paradubonheur.tn/page/{page}/?s=soin&post_type=product", "max_pages": 20},
    {"source": "Skincare Para", "category": "Parapharmacie", "url": "https://skincarepara.com/page/{page}/?s=soin&post_type=product", "max_pages": 15},
    {"source": "Coquette.tn", "category": "Parapharmacie", "url": "https://www.coquette.tn/page/{page}/?s=soin&post_type=product", "max_pages": 15},

    # --- 3. HIGH-TECH, ÉLECTROMÉNAGER & TV ---
    {"source": "SpaceNet", "category": "High-Tech", "url": "https://spacenet.tn/13-telephonie-tablette?page={page}", "max_pages": 20},
    {"source": "SpaceNet", "category": "High-Tech", "url": "https://spacenet.tn/14-pc-portable?page={page}", "max_pages": 18},
    {"source": "SpaceNet", "category": "High-Tech", "url": "https://spacenet.tn/11-informatique?page={page}", "max_pages": 18},
    {"source": "SpaceNet", "category": "High-Tech", "url": "https://spacenet.tn/15-tv-son?page={page}", "max_pages": 15},
    {"source": "SpaceNet", "category": "Électroménager", "url": "https://spacenet.tn/18-electromenager?page={page}", "max_pages": 18},
    {"source": "SpaceNet", "category": "Électroménager", "url": "https://spacenet.tn/19-petit-electromenager?page={page}", "max_pages": 18},
    {"source": "SpaceNet", "category": "Climatisation", "url": "https://spacenet.tn/20-climatisation-chauffage?page={page}", "max_pages": 12},

    {"source": "Tunisianet", "category": "High-Tech", "url": "https://www.tunisianet.com.tn/377-telephone-portable-tunisie?page={page}", "max_pages": 25},
    {"source": "Tunisianet", "category": "High-Tech", "url": "https://www.tunisianet.com.tn/301-pc-portable-tunisie?page={page}", "max_pages": 20},
    {"source": "Tunisianet", "category": "High-Tech", "url": "https://www.tunisianet.com.tn/300-informatique-tunisie?page={page}", "max_pages": 20},
    {"source": "Tunisianet", "category": "High-Tech", "url": "https://www.tunisianet.com.tn/378-tv-son-et-photos-tunisie?page={page}", "max_pages": 15},
    {"source": "Tunisianet", "category": "Électroménager", "url": "https://www.tunisianet.com.tn/439-electromenager-tunisie?page={page}", "max_pages": 20},
    {"source": "Tunisianet", "category": "Petit Électro", "url": "https://www.tunisianet.com.tn/440-petit-electromenager-tunisie?page={page}", "max_pages": 20},
    {"source": "Tunisianet", "category": "Climatisation", "url": "https://www.tunisianet.com.tn/505-climatisation-et-chauffage?page={page}", "max_pages": 12},
    {"source": "Tunisianet", "category": "Beauté & Soins", "url": "https://www.tunisianet.com.tn/690-beaute-et-sante?page={page}", "max_pages": 15},

    {"source": "Batam", "category": "Électroménager", "url": "https://batam.com.tn/recherche?controller=search&s=electromenager&page={page}", "max_pages": 15},
    {"source": "Batam", "category": "Électroménager", "url": "https://batam.com.tn/recherche?controller=search&s=tv&page={page}", "max_pages": 10},
    {"source": "Technopro", "category": "High-Tech", "url": "https://www.technopro-online.com/recherche?controller=search&s=smartphone&page={page}", "max_pages": 15},
    {"source": "Technopro", "category": "High-Tech", "url": "https://www.technopro-online.com/recherche?controller=search&s=pc+portable&page={page}", "max_pages": 15},
    {"source": "SBS Informatique", "category": "Gaming & PC", "url": "https://www.sbsinformatique.com/recherche?controller=search&s=pc+gamer&page={page}", "max_pages": 15},
    {"source": "Darty TN", "category": "Électroménager", "url": "https://darty.tn/recherche?controller=search&s=electromenager&page={page}", "max_pages": 15},
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

def build_url(url_tpl: str, page: int) -> str:
    """Construit l'URL pour une page donnee.

    WordPress : /page/{page}/ dans le template -> page 1 = URL de base
    sans /page/1/ (sinon 404 sur la plupart des configs WP).
    PrestaShop : ?page={page} ou &page={page} -> page 1 = sans le parametre
    (some themes redirect ?page=1 vers une autre categorie).
    """
    if page == 1:
        return (url_tpl
                .replace("/page/{page}/", "/")
                .replace("&page={page}", "")
                .replace("?page={page}", ""))
    return url_tpl.format(page=page)

async def crawl_rayon(fetcher: Fetcher, source: str, category: str, url_tpl: str, max_pages: int, batch: int = 3) -> list:
    products, page = [], 1
    while page <= max_pages:
        urls = [build_url(url_tpl, p) for p in range(page, page + batch)]
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
# SANGOUR (Flux XML / RSS Direct - Débloque 100% de Sangour)
# -----------------------------------------------------------------------------
async def crawl_sangour(fetcher: Fetcher) -> list:
    log.info("Démarrage du crawl Sangour via Flux Produit...")
    products, seen = [], set()
    
    # 1. Flux RSS direct de la boutique (Bypasse totalement Cloudflare)
    feed_urls = [
        "https://sangour.tn/feed/?post_type=product",
        "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/javel/feed/",
        "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/sol/feed/",
        "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/vaisselles/feed/",
        "https://sangour.tn/marques/judy/feed/",
        "https://sangour.tn/marques/tramontina/feed/",
        "https://sangour.tn/marques/tefal/feed/",
        "https://sangour.tn/marques/moulinex/feed/"
    ]
    
    for f_url in feed_urls:
        xml_text = await fetcher.get(f_url)
        if xml_text and "<item>" in xml_text:
            items = re.findall(r'<item>(.*?)</item>', xml_text, re.DOTALL)
            for it in items:
                title_m = re.search(r'<title>(.*?)</title>', it)
                link_m = re.search(r'<link>(.*?)</link>', it)
                if not (title_m and link_m):
                    continue
                title = html_lib.unescape(title_m.group(1)).strip()
                url = link_m.group(1).strip()
                if url in seen:
                    continue
                seen.add(url)
                
                price_m = re.search(r'(?:<g:price>|Prix\s*:\s*|amount">)([\d\.,\s]+(?:TND|DT))', it)
                price_val = parse_tnd_price(price_m.group(1)) if price_m else None
                img_m = re.search(r'(?:<g:image_link>|<media:content[^>]*url=")([^"]+)', it)
                img_url = img_m.group(1) if img_m else None
                
                if title and price_val and price_val > 0:
                    products.append({
                        "source": "Sangour",
                        "category": "Maison & Entretien",
                        "title": title,
                        "sku": None,
                        "price": price_val,
                        "price_raw": f"{price_val:,.3f} TND",
                        "url": url,
                        "image": img_url,
                        "in_stock": True,
                    })

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
            payload = {"query": MYTEK_QUERY, "variables": {"search": term, "page": page, "pageSize": 100}}
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
        page += 1
        await asyncio.sleep(0.1)
    log.info("[Drest] Total collecté : %d produits", len(products))
    return products

async def crawl_wamia(fetcher: Fetcher) -> list:
    WAMIA_GQL = "https://www.wamia.tn/graphql"
    WAMIA_BASE = "https://www.wamia.tn/"
    products = []
    try:
        cat_resp = await fetcher.post_json(WAMIA_GQL, {"query": "{ categories(filters: {parent_id: {eq: \"2\"}}) { items { id name } } }"})
        cats = ((cat_resp or {}).get("data") or {}).get("categories", {}).get("items", [])
        for cat in cats:
            cat_id, cat_name = str(cat.get("id")), cat.get("name") or "Wamia"
            for page in range(1, 40):
                query = f"""
                query {{
                  products(filter: {{category_id: {{eq: "{cat_id}"}}}}, pageSize: 100, currentPage: {page}) {{
                    items {{ name sku canonical_url stock_status small_image {{ url }} price_range {{ minimum_price {{ regular_price {{ value }} }} }} }}
                  }}
                }}
                """
                r = await fetcher.post_json(WAMIA_GQL, {"query": query})
                items = ((r or {}).get("data") or {}).get("products", {}).get("items", [])
                if not items:
                    break
                for it in items:
                    title = html_lib.unescape((it.get("name") or "")).strip()
                    canonical = (it.get("canonical_url") or "").strip()
                    price = ((it.get("price_range") or {}).get("minimum_price") or {}).get("regular_price", {}).get("value")
                    if title and canonical and price and float(price) > 0:
                        img = (it.get("small_image") or {}).get("url")
                        products.append({
                            "source": "Wamia",
                            "category": f"Maison - {cat_name}",
                            "title": title,
                            "sku": it.get("sku") or None,
                            "price": round(float(price), 3),
                            "price_raw": f"{float(price):,.3f} TND",
                            "url": WAMIA_BASE + canonical.lstrip("/"),
                            "image": img,
                            "in_stock": it.get("stock_status") == "IN_STOCK",
                        })
                if len(items) < 100:
                    break
                await asyncio.sleep(0.1)
    except Exception as e:
        log.warning(f"Erreur Wamia : {e}")
    log.info("[Wamia] Total collecté : %d produits", len(products))
    return products

# -----------------------------------------------------------------------------
# Enregistrement PostgreSQL Sécurisé
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

        batch_size = 250
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
        # 1. Drest API Store JSON (27 000+ produits)
        all_products.extend(await crawl_drest(fetcher))

        # 2. Wamia GraphQL Magento (20 000+ produits)
        all_products.extend(await crawl_wamia(fetcher))

        # 3. Mytek GraphQL OpenSearch (12 000+ produits)
        sem = asyncio.Semaphore(4)
        all_products.extend(await crawl_mytek(fetcher, sem))

        # 4. Sangour (Flux XML Produit - Bypasse Cloudflare)
        all_products.extend(await crawl_sangour(fetcher))

        # 5. Rayons PrestaShop & WooCommerce (Toutes les 21 Parapharmacies + High-Tech)
        for target in CATALOG_TARGETS:
            items = await crawl_rayon(
                fetcher, target["source"], target["category"],
                target["url"], target["max_pages"]
            )
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