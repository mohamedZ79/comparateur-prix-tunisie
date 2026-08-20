"""
Scrapers asynchrones pour le comparateur de prix tunisien.
Chaque scraper est une coroutine : (query: str) -> list[ProductOffer]
"""
import asyncio
import os
import random
import re
import unicodedata
from dataclasses import dataclass, asdict
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

# ---------------------------------------------------------------- modèle

@dataclass
class ProductOffer:
    source: str
    title: str
    price: Optional[float]          # en TND, valeur numérique triable
    price_raw: str
    url: str
    image: Optional[str]
    availability: str
    match_score: float = 0.0        # score de pertinence (0-100)

    def to_dict(self):
        return asdict(self)


# ------------------------------------------------------- parsing des prix

def parse_tnd_price(raw: str) -> Optional[float]:
    if not raw:
        return None
    cleaned = unicodedata.normalize("NFKD", str(raw)).strip().lower()

    # 1. Format Carrefour (ex: "249DT000")
    carrefour_match = re.search(r'(\d+)\s*(?:dt|tnd|d\.t)\s*(\d{3})', cleaned)
    if carrefour_match:
        return float(f"{carrefour_match.group(1)}.{carrefour_match.group(2)}")

    # 2. Nettoyage des devises et des espaces de milliers (ex: "3 999,000 DT" -> "3999.000")
    cleaned = re.sub(r'(?:dt|tnd|dinars?|d\.t)', '', cleaned).strip()
    
    if "," in cleaned:
        cleaned = cleaned.replace(" ", "").replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(" ", "")
        if re.fullmatch(r"\d{1,3}(\.\d{3}){2,}", cleaned):
            cleaned = cleaned.replace(".", "")

    m = re.search(r'(\d+(?:\.\d+)?)', cleaned)
    if m:
        try:
            return round(float(m.group(1)), 3)
        except ValueError:
            return None
    return None


# --------------------------------------------- filtrage strict & volumes

ACCESSORY_WORDS = {"coque", "etui", "housse", "pochette", "protecteur", "protection",
                   "film", "verre trempe", "verre", "incassable", "cable", "chargeur",
                   "adaptateur", "support", "vitre", "skin", "cache"}

_STOPWORDS = {"de", "du", "la", "le", "les", "des", "pour", "avec", "en", "a", "au", "et", "sur", "sans"}

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r'["\']', '', text)
    return text.strip()

def _clean_sku(text: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]', '', str(text)).lower()

def extract_volume_spec(text: str) -> Optional[str]:
    norm = _normalize(text)
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:l|litre|litres)\b', norm)
    if m:
        return f"{int(float(m.group(1))*1000)}ml"
    m = re.search(r'(\d+)\s*(?:ml|millilitres)\b', norm)
    if m:
        return f"{int(m.group(1))}ml"
    return None

def is_strict_match(query: str, title: str, url: str = "") -> tuple[bool, float]:
    q_norm = _normalize(query)
    t_norm = _normalize(title)

    # 1. Filtre anti-accessoires (si non demandé par l'utilisateur)
    user_wants_acc = any(a in q_norm for a in ACCESSORY_WORDS)
    if not user_wants_acc:
        is_acc = any(re.search(r'\b' + re.escape(a) + r'\b', t_norm) for a in ACCESSORY_WORDS)
        if is_acc:
            return False, 0.0

    # 2. Discrimination des volumes (1L vs 200ml)
    q_vol = extract_volume_spec(q_norm)
    t_vol = extract_volume_spec(t_norm)
    if q_vol and t_vol and q_vol != t_vol:
        return False, 0.0

    # 3. Vérification des tokens du modèle (ex: 's23')
    tokens = [t for t in re.findall(r'[a-z0-9]+', q_norm) if t not in _STOPWORDS and len(t) > 1]
    if not tokens:
        return True, 100.0

    full_text = f"{t_norm} {_normalize(url)}"
    full_sku = _clean_sku(full_text)

    model_tokens = [t for t in tokens if any(c.isdigit() for c in t) or len(t) <= 3]
    for m in model_tokens:
        if _clean_sku(m) not in full_sku:
            return False, 0.0  # Modèle manquant (ex: 's23' non trouvé dans 'j4')

    brand_tokens = [t for t in tokens if t not in model_tokens]
    matched_words = [b for b in brand_tokens if b in full_text]
    if brand_tokens and not matched_words:
        return False, 0.0

    coverage = (len(model_tokens) + len(matched_words)) / len(tokens)
    if coverage < 0.5:
        return False, 0.0

    score = round(0.6 * fuzz.token_set_ratio(q_norm, t_norm) + 0.4 * (coverage * 100), 1)
    return True, score


# ------------------------------------------------------------- HTTP layer

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

class ScraperError(Exception):
    pass

async def fetch(client: httpx.AsyncClient, url: str, retries: int = 1) -> str:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": random.choice(USER_AGENTS),
                    "Accept-Language": "fr-TN,fr;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=httpx.Timeout(7.0, connect=4.0),
                follow_redirects=True,
            )
            if resp.status_code == 403:
                raise ScraperError(f"HTTP 403 (anti-bot) sur {url}")
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPError, ScraperError) as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.5)
    raise ScraperError(f"Échec sur {url} : {last_exc}")


# ---------------------------------------------------------------- scrapers

async def _scrape_prestashop(query: str, client: httpx.AsyncClient, source: str, search_url: str) -> list[ProductOffer]:
    clean_q = re.sub(r'["\']', '', query).strip()
    url = search_url.format(q=httpx.QueryParams({"s": clean_q})["s"])
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    
    card_sel = "article.product-miniature, div.product-miniature, .product-miniature, .product-item, li.product"
    items = soup.select(card_sel)
    
    for item in items[:15]:
        # ✅ Inclusion des sélecteurs SpaceNet (h2.product_name) et standards PrestaShop
        a = item.select_one(
            "h2.product_name a, .product_name a, h2.product-title a, h3.product-title a, "
            "h2.product-name a, a.product-name, .product-title a, .name a, h3 a, h2 a, a[title]"
        )
        price_el = item.select_one("span.product-price, span.price, .product-price, .price, [itemprop='price'], ins .amount")
        img = item.select_one("img.product_image, .thumbnail-container img, .product-thumbnail img, img")
        
        if not (a and price_el):
            continue
            
        title = a.get_text(strip=True)
        href = a.get("href", "")
        
        valid, score = is_strict_match(query, title, href)
        if not valid:
            continue

        image = None
        if img:
            image = (img.get("data-full-size-image-url") or img.get("data-src") or img.get("data-original") or img.get("src"))
        
        price_val = parse_tnd_price(price_el.get_text())
        if price_val and price_val > 0:
            offers.append(ProductOffer(
                source=source,
                title=title,
                price=price_val,
                price_raw=f"{price_val:,.3f} TND",
                url=href,
                image=image,
                availability="En stock",
                match_score=score
            ))
    return offers

async def scrape_spacenet(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "Spacenet", "https://spacenet.tn/recherche?s={q}")

async def scrape_tunisianet(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "Tunisianet", "https://www.tunisianet.com.tn/recherche?s={q}")

async def scrape_wiki(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    clean_q = re.sub(r'["\']', '', query).strip()
    url = f"https://www.wiki.tn/?s={httpx.QueryParams({'s': clean_q})['s']}&post_type=product"
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    for item in soup.select("div.product-card--grid, li.product")[:15]:
        a = item.select_one(".product-card__title a, h2.woocommerce-loop-product__title a, h3 a")
        price_el = item.select_one(".product-card__price ins span.woocommerce-Price-amount, .product-card__price span.woocommerce-Price-amount, .price")
        img = item.select_one(".product-card__image img, img")
        if not (a and price_el):
            continue
        title = a.get_text(strip=True)
        href = a.get("href", "")
        valid, score = is_strict_match(query, title, href)
        if not valid:
            continue
        price_val = parse_tnd_price(price_el.get_text())
        if price_val and price_val > 0:
            offers.append(ProductOffer(
                source="Wiki",
                title=title,
                price=price_val,
                price_raw=f"{price_val:,.3f} TND",
                url=href,
                image=img.get("src") if img else None,
                availability="En stock",
                match_score=score
            ))
    return offers

async def scrape_darty(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "Darty", "https://darty.tn/recherche?s={q}")

async def scrape_technopro(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "Technopro", "https://www.technopro-online.com/recherche?s={q}")

async def scrape_sbs(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "SBS Informatique", "https://www.sbsinformatique.com/recherche?s={q}")

async def scrape_yeswikam(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "Yeswikam", "https://www.yeswikam.com/recherche?s={q}")

async def scrape_mycare(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "MyCare", "https://mycare.tn/recherche?s={q}")

async def scrape_paraexpert(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    clean_q = re.sub(r'["\']', '', query).strip()
    url = f"https://www.paraexpert.tn/?s={httpx.QueryParams({'s': clean_q})['s']}"
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    for item in soup.select(".c-post-list.product, li.product")[:15]:
        a = item.select_one("a.c-post-list__header-link, h2 a, h3 a")
        price_el = item.select_one("ins span.woocommerce-Price-amount, span.woocommerce-Price-amount, .price")
        img = item.select_one("img")
        if not (a and price_el):
            continue
        title = a.get_text(strip=True)
        href = a.get("href", "")
        valid, score = is_strict_match(query, title, href)
        if not valid:
            continue
        price_val = parse_tnd_price(price_el.get_text())
        if price_val and price_val > 0:
            offers.append(ProductOffer(
                source="ParaExpert",
                title=title,
                price=price_val,
                price_raw=f"{price_val:,.3f} TND",
                url=href,
                image=img.get("src") if img else None,
                availability="En stock",
                match_score=score
            ))
    return offers

# --- MYTEK GRAPHQL ---
MYTEK_GRAPHQL = "https://www.mytek.tn/graphql"
MYTEK_MEDIA = "https://www.mytek.tn/media/catalog/product"
MYTEK_QUERY = """
query ($search: String, $page: Int, $pageSize: Int) {
  opensearchProductSearch(search: $search, page: $page, pageSize: $pageSize) {
    items { id sku name price special_price final_price image url }
  }
}
"""

async def scrape_mytek(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    clean_q = re.sub(r'["\']', '', query).strip()
    resp = await client.post(
        MYTEK_GRAPHQL,
        json={"query": MYTEK_QUERY, "variables": {"search": clean_q, "page": 1, "pageSize": 35}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    resp.raise_for_status()
    body = resp.json()
    items = (body.get("data", {}).get("opensearchProductSearch") or {}).get("items") or []

    offers = []
    for it in items:
        price = it.get("special_price") or it.get("final_price") or it.get("price")
        title = (it.get("name") or "").strip()
        url = it.get("url") or ""
        
        valid, score = is_strict_match(query, title, url)
        if not valid or not price or float(price) <= 0:
            continue

        img = it.get("image") or ""
        if img.startswith("/"):
            img = MYTEK_MEDIA + img
        if url and not url.startswith("http"):
            url = "https://www.mytek.tn/" + url.lstrip("/")
        
        offers.append(ProductOffer(
            source="Mytek",
            title=title,
            price=round(float(price), 3),
            price_raw=f"{float(price):,.3f} TND",
            url=url,
            image=img or None,
            availability="En stock",
            match_score=score
        ))
    return offers


SCRAPERS = {
    "spacenet": scrape_spacenet,
    "mytek": scrape_mytek,
    "tunisianet": scrape_tunisianet,
    "yeswikam": scrape_yeswikam,
    "wiki": scrape_wiki,
    "darty": scrape_darty,
    "technopro": scrape_technopro,
    "sbs": scrape_sbs,
    "mycare": scrape_mycare,
    "paraexpert": scrape_paraexpert,
}

# ------------------------------------------------------------- orchestrateur

PER_SITE_TIMEOUT = 7.0   # ✅ Réduit à 7s pour une réponse ultra-rapide sans bloquer l'utilisateur

async def search_all(query: str) -> dict:
    async with httpx.AsyncClient(http2=False) as client:
        async def guarded(name, fn):
            try:
                return name, await asyncio.wait_for(fn(query, client), timeout=PER_SITE_TIMEOUT), None
            except Exception as exc:
                return name, [], f"{type(exc).__name__}"

        tasks = [guarded(n, f) for n, f in SCRAPERS.items()]
        outcomes = await asyncio.gather(*tasks)
        
        results, errors = [], {}
        for outcome in outcomes:
            name, offers, err = outcome
            results.extend(offers)
            if err and not offers:
                errors[name] = err

    # Déduplication
    seen, deduped = set(), []
    for o in results:
        key = (o.source, o.url)
        if key not in seen:
            seen.add(key)
            deduped.append(o)

    # Tri par prix le moins cher
    deduped.sort(key=lambda o: (o.price is None, o.price))
    
    return {
        "query": query,
        "count": len(deduped),
        "offers": [o.to_dict() for o in deduped],
        "errors": errors,
    }