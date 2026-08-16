import asyncio
import re
import unicodedata
from typing import List, Optional, Dict
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from rapidfuzz import fuzz

app = FastAPI(title="Méta-Comparateur Prix Tunisie (Multi-Sources)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ProductOffer(BaseModel):
    store: str
    category: str
    title: str
    reference: Optional[str] = None
    price: float
    price_formatted: str
    url: str
    image_url: Optional[str] = None
    in_stock: bool
    similarity_score: float

class SearchResponse(BaseModel):
    query: str
    total_results: int
    active_stores: int
    results: List[ProductOffer]

# -----------------------------------------------------------------------------
# Utilitaires de Normalisation & Parsing de Prix
# -----------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    if not text:
        return ""
    return unicodedata.normalize('NFKD', str(text)).encode('ASCII', 'ignore').decode('utf-8').lower().strip()

def clean_alphanumeric(text: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]', '', str(text)).lower()

def parse_tunisian_price(price_str: str) -> float:
    if not price_str:
        return 0.0
    cleaned = unicodedata.normalize("NFKD", str(price_str)).strip().lower()
    
    # Format Carrefour (ex: "249DT000")
    carrefour_match = re.search(r'(\d+)\s*(?:dt|tnd|d\.t)\s*(\d{3})', cleaned)
    if carrefour_match:
        return float(f"{carrefour_match.group(1)}.{carrefour_match.group(2)}")
    
    carrefour_short = re.search(r'(\d+)\s*(?:dt|tnd|d\.t)\s*(\d+)', cleaned)
    if carrefour_short:
        return float(f"{carrefour_short.group(1)}.{carrefour_short.group(2).ljust(3, '0')[:3]}")

    cleaned = re.sub(r'(dt|tnd|dinars?|d\.t)', '', cleaned)
    cleaned = cleaned.replace(" ", "").replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if match:
        try:
            return round(float(match.group(1)), 3)
        except ValueError:
            return 0.0
    return 0.0

STOPWORDS = {"de", "du", "la", "le", "les", "un", "une", "des", "pour", "en", "et", "a", "avec", "sur", "sans"}

def strict_matcher(query: str, title: str, reference: str = "", url: str = "") -> bool:
    q_norm = normalize_text(query)
    t_norm = normalize_text(title)
    r_norm = normalize_text(reference)
    u_norm = normalize_text(url)
    
    if any(b in t_norm for b in ["resultats", "recherche", "panier", "aucun", "accueil", "connexion"]):
        return False

    full_text = f"{t_norm} {r_norm} {u_norm}"
    full_sku = clean_alphanumeric(full_text)

    tokens = [w for w in re.findall(r'[a-z0-9]+', q_norm) if w not in STOPWORDS and len(w) > 1]
    if not tokens:
        return True

    # 1. Vérification par SKU / Référence
    sku_tokens = [w for w in tokens if any(c.isdigit() for c in w) or len(w) <= 3]
    for sku in sku_tokens:
        clean_sku_token = clean_alphanumeric(sku)
        if clean_sku_token not in full_sku:
            return False

    # 2. Vérification par Mots-clés
    matched = [w for w in tokens if w in full_text]
    return len(matched) >= max(1, int(len(tokens) * 0.5))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
}

# -----------------------------------------------------------------------------
# Configuration des Boutiques
# -----------------------------------------------------------------------------
STORES_CONFIG: List[Dict[str, str]] = [
    {"name": "Tunisianet", "cat": "High-Tech & Électro", "url": "https://www.tunisianet.com.tn/recherche", "param": "s"},
    {"name": "Maalej Audio", "cat": "Électroménager", "url": "https://maalejaudio.tn/recherche", "param": "s"},
    {"name": "Darty TN", "cat": "Électroménager", "url": "https://darty.tn/recherche", "param": "s"},
    {"name": "Drest", "cat": "Beauté & Électro", "url": "https://drest.tn/recherche", "param": "s"},
    {"name": "Batam", "cat": "Électroménager", "url": "https://batam.com.tn/recherche", "param": "s"},
    {"name": "SpaceNet", "cat": "High-Tech & Maison", "url": "https://spacenet.tn/recherche", "param": "s"},
    {"name": "Wiki", "cat": "High-Tech", "url": "https://www.wiki.tn/recherche", "param": "s"},
    {"name": "T-Discount", "cat": "High-Tech & Électro", "url": "https://tdiscount.tn/recherche", "param": "s"},
    {"name": "Scoop", "cat": "High-Tech", "url": "https://www.scoop.com.tn/recherche", "param": "s"},
    {"name": "TunisiaTech", "cat": "High-Tech", "url": "https://tunisiatech.tn/recherche", "param": "s"},
    {"name": "Electro Tounes", "cat": "Électroménager", "url": "https://electrotounes.tn/recherche", "param": "s"},
    {"name": "Graiet", "cat": "Électroménager", "url": "https://graiet.tn/recherche", "param": "s"},
    {"name": "SBS Informatique", "cat": "Gaming & PC", "url": "https://www.sbsinformatique.com/recherche", "param": "s"},
    {"name": "MegaPC", "cat": "Gaming & PC", "url": "https://megapc.tn/recherche", "param": "s"},
    {"name": "Best Buy Tunisie", "cat": "High-Tech", "url": "https://bestbuytunisie.tn/recherche", "param": "s"},
    {"name": "Affariyet", "cat": "High-Tech & Maison", "url": "https://affariyet.com/recherche", "param": "s"},
    {"name": "Technopro", "cat": "High-Tech", "url": "https://www.technopro-online.com/recherche", "param": "s"},
    {"name": "Wamia", "cat": "Maison & Électro", "url": "https://wamia.tn/recherche", "param": "s"},
    {"name": "Bricorama TN", "cat": "Maison & Bricolage", "url": "https://bricorama.tn/recherche", "param": "s"},
    {"name": "Sangour", "cat": "Maison & Cuisine", "url": "https://sangour.tn/", "param": "s", "wc": True},
    {"name": "Fi-Dar", "cat": "Maison & Cuisine", "url": "https://fidar.tn/", "param": "s", "wc": True},
    {"name": "Paraexpert", "cat": "Parapharmacie", "url": "https://paraexpert.tn/recherche", "param": "s"},
    {"name": "Phyto.tn", "cat": "Parapharmacie", "url": "https://phyto.tn/recherche", "param": "s"},
    {"name": "MyCare", "cat": "Parapharmacie", "url": "https://mycare.tn/recherche", "param": "s"},
    {"name": "Santé Parapharmacie", "cat": "Parapharmacie", "url": "https://santeparapharmacie.tn/recherche", "param": "s"},
    {"name": "Paraforce", "cat": "Parapharmacie", "url": "https://paraforce.tn/recherche", "param": "s"},
]

# -----------------------------------------------------------------------------
# Scraper Asynchrone
# -----------------------------------------------------------------------------
async def scrape_store(semaphore: asyncio.Semaphore, client: httpx.AsyncClient, store: Dict[str, str], query: str) -> List[ProductOffer]:
    results = []
    async with semaphore:
        try:
            params = {"s": query, "post_type": "product"} if store.get("wc") else {store.get("param", "s"): query}
            res = await client.get(store["url"], params=params, headers=HEADERS, timeout=8.0)
            if res.status_code != 200:
                return results

            soup = BeautifulSoup(res.text, "html.parser")
            
            cards = soup.select(
                "article.product-miniature, .product-miniature, .product-item, li.product, "
                ".product-grid-item, div.product-small, .ajax_block_product, .item-product"
            )
            
            for p in cards[:8]:
                title_tag = p.select_one(
                    ".product-title a, h3.product-title a, h2.product-title a, .product-name a, "
                    ".woocommerce-loop-product__title, a.woocommerce-LoopProduct-link, .product-description a, h3 a, h2 a"
                )
                link_tag = p.select_one("a[href]")
                price_tag = p.select_one(
                    ".price, span.price, ins .amount, .woocommerce-Price-amount, [itemprop='price'], .product-price, .current-price"
                )
                img_tag = p.select_one(".thumbnail-container img, .product-thumbnail img, img.product-image-photo, img")
                stock_tag = p.select_one("#stock_availability, .product-availability, .availability span, .out-of-stock")
                ref_tag = p.select_one(".product-reference, .reference, [itemprop='sku'], .sku")

                if (title_tag or link_tag) and price_tag:
                    raw_title = title_tag.get_text(strip=True) if title_tag else ""
                    product_url = title_tag.get("href", "") if title_tag and title_tag.get("href") else (link_tag.get("href", "") if link_tag else "")
                    ref_val = ref_tag.get_text(strip=True).replace("Réf :", "").strip() if ref_tag else ""

                    if not strict_matcher(query=query, title=raw_title, reference=ref_val, url=product_url):
                        continue

                    price_val = parse_tunisian_price(price_tag.get_text(strip=True))
                    img_url = img_tag.get("data-full-size-image-url") or img_tag.get("data-src") or img_tag.get("src") if img_tag else None
                    in_stock = not (stock_tag and any(kw in stock_tag.get_text(strip=True).lower() for kw in ["hors stock", "épuisé", "indisponible", "out of stock"]))

                    if price_val > 0:
                        results.append(ProductOffer(
                            store=store["name"],
                            category=store["cat"],
                            title=raw_title,
                            reference=ref_val if ref_val else None,
                            price=price_val,
                            price_formatted=f"{price_val:.3f} DT",
                            url=product_url,
                            image_url=img_url,
                            in_stock=in_stock,
                            similarity_score=100.0
                        ))

            if results:
                print(f"[{store['name']}] ✓ {len(results)} offre(s) trouvée(s)")

        except Exception:
            pass
    return results

async def scrape_mytek(semaphore: asyncio.Semaphore, client: httpx.AsyncClient, query: str) -> List[ProductOffer]:
    results = []
    async with semaphore:
        try:
            url = "https://www.mytek.tn/catalogsearch/result/"
            res = await client.get(url, params={"q": query}, headers=HEADERS, timeout=8.0)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                for p in soup.select(".product-item-info")[:6]:
                    title_tag = p.select_one(".product-item-name a")
                    price_tag = p.select_one("[data-price-type='finalPrice'] .price, .price")
                    img_tag = p.select_one(".product-image-photo")
                    stock_tag = p.select_one(".stock.unavailable")
                    ref_tag = p.select_one(".sku, .product-item-sku")

                    if title_tag and price_tag:
                        title = title_tag.get_text(strip=True)
                        product_url = title_tag.get("href", "")
                        ref_val = ref_tag.get_text(strip=True) if ref_tag else ""

                        if not strict_matcher(query=query, title=title, reference=ref_val, url=product_url):
                            continue

                        price_val = parse_tunisian_price(price_tag.get_text(strip=True))
                        img_url = img_tag.get("src") if img_tag else None
                        in_stock = stock_tag is None

                        if price_val > 0:
                            results.append(ProductOffer(
                                store="MyTek",
                                category="High-Tech & Électro",
                                title=title,
                                reference=ref_val if ref_val else None,
                                price=price_val,
                                price_formatted=f"{price_val:.3f} DT",
                                url=product_url,
                                image_url=img_url,
                                in_stock=in_stock,
                                similarity_score=100.0
                            ))
                if results:
                    print(f"[MyTek] ✓ {len(results)} offre(s) trouvée(s)")
        except Exception:
            pass
    return results

# -----------------------------------------------------------------------------
# Endpoint Principal & Orchestration
# -----------------------------------------------------------------------------
@app.get("/api/compare", response_model=SearchResponse)
async def compare(q: str = Query(..., min_length=2)):
    print(f"\n=======================================================")
    print(f"🔍 [Recherche Multi-Boutiques] : '{q}'")
    print(f"=======================================================")
    
    semaphore = asyncio.Semaphore(15)
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=25)

    async with httpx.AsyncClient(follow_redirects=True, verify=False, limits=limits) as client:
        tasks = [scrape_store(semaphore, client, store, q) for store in STORES_CONFIG]
        tasks.append(scrape_mytek(semaphore, client, q))

        grouped_results = await asyncio.gather(*tasks, return_exceptions=True)

    flat_results: List[ProductOffer] = []
    for g in grouped_results:
        if isinstance(g, list):
            flat_results.extend(g)

    sorted_res = sorted(flat_results, key=lambda x: (not x.in_stock, x.price))
    total_stores = len(STORES_CONFIG) + 1

    print(f"📊 Bilan : {len(sorted_res)} offre(s) réelle(s) trouvée(s) sur {total_stores} boutiques.\n")

    return SearchResponse(
        query=q,
        total_results=len(sorted_res),
        active_stores=total_stores,
        results=sorted_res
    )