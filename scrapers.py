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
    """Parse a Tunisian price string into a float TND value.

    Handles: "9,900 DT" -> 9.9 | "1 299,000 TND" -> 1299.0 |
    "249DT000" -> 249.0 (Carrefour) | "131.370" -> 131.37 |
    "1.299,000 DT" -> 1299.0 | promo strings (last amount wins).
    Single source of truth: crawler.py and scrapers_browser.py import
    this function - do NOT duplicate it (audit finding F-12).
    """
    if not raw:
        return None
    cleaned = unicodedata.normalize("NFKD", str(raw)).strip().lower()

    # 1. Format Carrefour (ex: "249DT000")
    carrefour_match = re.search(r'(\d+)\s*(?:dt|tnd|d\.t)\s*(\d{3})', cleaned)
    if carrefour_match:
        return float(f"{carrefour_match.group(1)}.{carrefour_match.group(2)}")

    # 2. Amounts anchored to a currency word. Prefer the LAST match so that
    #    promo noise ("Economisez 20 DT ... payez 45,900 DT") resolves to the
    #    real price. Captures thousands groups: "1 299,000" / "1.299,000".
    amounts = re.findall(
        r'(\d+(?:[ \u00a0.]\d{3})*(?:,\d{2,3})?)\s*(?:dt|tnd|d\.t|dinars?)\b',
        cleaned)
    if amounts:
        s = amounts[-1].replace(" ", "").replace("\u00a0", "")
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(\.\d{3}){2,}", s):
            s = s.replace(".", "")
        try:
            return round(float(s), 3)
        except ValueError:
            pass

    # 3. Fallback: strip currency, take the first bare number
    #    (covers "131.370" with no currency suffix)
    cleaned = re.sub(r'(?:dt|tnd|dinars?|d\.t)', '', cleaned).strip()
    if "," in cleaned:
        cleaned = cleaned.replace(" ", "").replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(" ", "").replace("\u00a0", "")
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

    # 1. Filtre anti-accessoires si non demandé
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
            return False, 0.0

    brand_tokens = [t for t in tokens if t not in model_tokens]
    matched_words = [b for b in brand_tokens if b in full_text]
    if brand_tokens and not matched_words:
        return False, 0.0

    coverage = (len(model_tokens) + len(matched_words)) / len(tokens)
    if coverage < 0.4:
        return False, 0.0

    score = round(0.6 * fuzz.token_set_ratio(q_norm, t_norm) + 0.4 * (coverage * 100), 1)
    return True, score


# ------------------------------------------------------------- HTTP layer

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
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
                timeout=httpx.Timeout(5.0, connect=3.0),
                follow_redirects=True,
            )
            if resp.status_code == 403:
                raise ScraperError(f"HTTP 403 (anti-bot) sur {url}")
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPError, ScraperError) as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.3)
    raise ScraperError(f"Échec sur {url} : {last_exc}")


# --------------------------------------------- extraction robuste du titre

# Textes d'interface a ignorer dans le repli generique
_UI_TEXTS = {"aperçu rapide", "quick view", "favoris", "comparer",
             "ajouter au panier", "add to cart", "détails", "details",
             "acheter", "buy", "voir le produit", "en savoir plus",
             "read more", "favorite_border", "balance"}

def extract_title_link(card) -> Optional[tuple]:
    """Extrait (titre, href) d'une carte produit, robuste aux themes.

    Certains themes PrestaShop (yeswikam, darty, sbs - verifie aout 2026)
    ne mettent PAS le titre du produit dans le heading : le heading porte
    la marque ("SVR", "VICHY") ou rien, et le vrai titre vit dans une
    ancre simple de la carte. Strategie :
      1. selecteurs de titres classiques (PrestaShop/Woo) si texte >= 10,
      2. sinon repli generique : l'ancre produit au texte le plus long
         (liens marque/categorie/UI exclus),
      3. en dernier recours, le heading court.
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


# ---------------------------------------------------------------- scrapers

async def _scrape_prestashop(query: str, client: httpx.AsyncClient, source: str, search_url: str) -> list[ProductOffer]:
    clean_q = re.sub(r'["\']', '', query).strip()
    url = search_url.format(q=httpx.QueryParams({"s": clean_q})["s"])
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    
    # Sélecteur universel de cartes
    card_sel = "article.product-miniature, div.product-miniature, .product-miniature, .product-item, li.product"
    items = soup.select(card_sel)
    
    # ✅ Analyse TOUTES les cartes de la page (pas de limitation à 15 pour ne pas rater les téléphones !)
    for item in items:
        extracted = extract_title_link(item)
        price_el = item.select_one("span.product-price, span.price, .product-price, .price, [itemprop='price'], ins .amount")
        img = item.select_one("img.product_image, .thumbnail-container img, .product-thumbnail img, img")

        if not (extracted and price_el):
            continue

        title, href = extracted
        
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
    return await _scrape_prestashop(query, client, "Spacenet", "https://spacenet.tn/recherche?controller=search&s={q}")

async def scrape_tunisianet(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "Tunisianet", "https://www.tunisianet.com.tn/recherche?controller=search&s={q}")

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

# --- DREST - WooCommerce Store API (JSON public, pas de Cloudflare) ---
# Decouverte de l'audit 2026 : drest.tn expose l'API Store WooCommerce
# (https://drest.tn/wp-json/wc/store/products) qui repond en JSON sans
# challenge. Necessite seulement un User-Agent navigateur. 27 000+ produits.
import html as _html  # noqa: E402

DREST_STORE_API = "https://drest.tn/wp-json/wc/store/products"

async def _drest_fetch(query: str, client: httpx.AsyncClient) -> list:
    """Interroge l'API Store Drest : httpx, puis repli curl_cffi (TLS
    Chrome) - certains CDN filtrent httpx selon l'IP/geolocalisation
    (observe depuis les runners GitHub US, aout 2026)."""
    clean_q = re.sub(r'["\']', '', query).strip()
    params = {"search": clean_q, "per_page": 24}
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-TN,fr;q=0.9,en;q=0.8",
    }
    # tentative 1 : httpx (le client passe peut-etre par un proxy residentiel)
    try:
        resp = await client.get(DREST_STORE_API, params=params,
                                headers=headers, timeout=12.0)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    # tentative 2 : curl_cffi avec empreinte TLS Chrome
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession(impersonate="chrome", timeout=15) as s:
            r2 = await s.get(DREST_STORE_API, params=params,
                             headers=headers, allow_redirects=True)
            if r2.status_code == 200:
                return r2.json()
    except Exception:
        pass
    return []

async def scrape_drest(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    items = await _drest_fetch(query, client)

    offers = []
    for it in items:
        title = _html.unescape((it.get("name") or "")).strip()
        prices = it.get("prices") or {}
        minor = int(prices.get("currency_minor_unit") or 0)
        raw_price = prices.get("price") or prices.get("regular_price")
        url = it.get("permalink") or ""
        if not title or not raw_price:
            continue
        try:
            price_val = round(int(raw_price) / (10 ** minor), 3)
        except (ValueError, ZeroDivisionError):
            continue
        if price_val <= 0:
            continue

        valid, score = is_strict_match(query, title, url)
        if not valid:
            continue

        images = it.get("images") or []
        offers.append(ProductOffer(
            source="Drest",
            title=title,
            price=price_val,
            price_raw=f"{price_val:,.3f} TND",
            url=url,
            image=images[0].get("src") if images else None,
            availability="En stock" if it.get("is_in_stock") else "Rupture",
            match_score=score,
        ))
    return offers

# --- SANGOUR (WooCommerce Store API, behind Cloudflare => needs PROXY_URL) ---
SANGOUR_STORE_API = "https://sangour.tn/wp-json/wc/store/products"
_SANGOUR_PROXY = os.getenv("PROXY_URL")  # IP residentielle ES/FR requise

async def _sangour_fetch(query: str, client: httpx.AsyncClient) -> list:
    """Store API Sangour. Cloudflare bloque les IP datacenter ; on envoie
    d'abord via le client httpx fourni (qui peut avoir un proxy), puis repli
    curl_cffi (TLS Chrome) - le tout tente de passer par PROXY_URL si present."""
    params = {"search": query, "per_page": 30}
    headers = {"Accept": "application/json"}
    # tentative 1 : httpx (peut porter un proxy si l'API server a PROXY_URL)
    try:
        r = await client.get(SANGOUR_STORE_API, params=params, headers=headers)
        if r.status_code == 200:
            return r.json() or []
    except Exception:
        pass
    # tentative 2 : curl_cffi avec proxy explicite si dispo
    try:
        from curl_cffi.requests import AsyncSession
        kw = {"impersonate": "chrome124", "timeout": 15, "headers": headers}
        if _SANGOUR_PROXY:
            kw["proxy"] = _SANGOUR_PROXY
        async with AsyncSession(**kw) as s:
            r2 = await s.get(SANGOUR_STORE_API, params=params, allow_redirects=True)
            if r2.status_code == 200:
                return r2.json() or []
    except Exception:
        pass
    return []

async def scrape_sangour(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    items = await _sangour_fetch(query, client)
    offers = []
    for it in items:
        title = _html.unescape((it.get("name") or "")).strip()
        prices = it.get("prices") or {}
        minor = int(prices.get("currency_minor_unit") or 0)
        raw_price = prices.get("price") or prices.get("regular_price")
        url = it.get("permalink") or ""
        if not title or not raw_price:
            continue
        try:
            if minor:
                price_val = round(int(raw_price) / (10 ** minor), 3)
            else:
                # Sangour peut renvoyer le prix en centimes sans minor_unit
                price_val = round(int(raw_price) / 100, 3)
        except (ValueError, ZeroDivisionError):
            continue
        if price_val <= 0:
            continue
        valid, score = is_strict_match(query, title, url)
        if not valid:
            continue
        images = it.get("images") or []
        offers.append(ProductOffer(
            source="Sangour",
            title=title,
            price=price_val,
            price_raw=f"{price_val:,.3f} TND",
            url=url,
            image=images[0].get("src") if images else None,
            availability="En stock" if it.get("is_in_stock", True) else "Rupture",
            match_score=score,
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

# --- WAMIA GRAPHQL (Magento) ---
# Decouverte de l'audit aout 2026 : wamia.tn (marketplace) est derriere
# Cloudflare pour le HTML, mais son API GraphQL Magento sur /graphql repond
# SANS challenge depuis n'importe quelle IP. Recherche live + prix + stock.
# Schema verifie : minimum_price.regular_price (Money), canonical_url,
# stock_status, small_image.url.
WAMIA_GRAPHQL = "https://www.wamia.tn/graphql"
WAMIA_BASE = "https://www.wamia.tn/"
WAMIA_SEARCH_QUERY = """
query ($search: String!) {
  products(search: $search, pageSize: 40) {
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

async def scrape_wamia(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    clean_q = re.sub(r'["\']', '', query).strip()
    try:
        resp = await client.post(
            WAMIA_GRAPHQL,
            json={"query": WAMIA_SEARCH_QUERY,
                  "variables": {"search": clean_q}},
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": random.choice(USER_AGENTS)},
            timeout=10.0,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        return []

    items = ((body.get("data") or {}).get("products") or {}).get("items") or []
    offers = []
    for it in items:
        title = _html.unescape((it.get("name") or "")).strip()
        canonical = (it.get("canonical_url") or "").strip()
        price_obj = (((it.get("price_range") or {})
                      .get("minimum_price") or {})
                     .get("regular_price") or {})
        price = price_obj.get("value")
        if not title or not canonical or price is None or float(price) <= 0:
            continue

        url = WAMIA_BASE + canonical.lstrip("/")
        valid, score = is_strict_match(query, title, url)
        if not valid:
            continue

        img = ((it.get("small_image") or {}).get("url") or "").strip() or None
        offers.append(ProductOffer(
            source="Wamia",
            title=title,
            price=round(float(price), 3),
            price_raw=f"{float(price):,.3f} TND",
            url=url,
            image=img,
            availability=("En stock"
                          if it.get("stock_status") == "IN_STOCK"
                          else "Rupture"),
            match_score=score,
        ))
    return offers

async def scrape_mytek(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    clean_q = re.sub(r'["\']', '', query).strip()
    resp = await client.post(
        MYTEK_GRAPHQL,
        json={"query": MYTEK_QUERY, "variables": {"search": clean_q, "page": 1, "pageSize": 60}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=5.0
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
    "wamia": scrape_wamia,
    "tunisianet": scrape_tunisianet,
    "yeswikam": scrape_yeswikam,
    "darty": scrape_darty,
    "technopro": scrape_technopro,
    "sbs": scrape_sbs,
    "mycare": scrape_mycare,
    "drest": scrape_drest,
    "sangour": scrape_sangour,
}

# Categorie de chaque boutique - source de verite unique pour la CI
# (audit F-08 : scraper-health.yml listait des boutiques fantomes ;
#  la CI derive maintenant ses ensembles depuis ce dictionnaire).
SHOP_CATEGORY = {
    "spacenet": "electronics",
    "mytek": "electronics",
    "wamia": "marketplace",
    "tunisianet": "electronics",
    "darty": "electronics",
    "technopro": "electronics",
    "sbs": "electronics",
    "yeswikam": "parapharmacie",
    "mycare": "parapharmacie",
    "drest": "parapharmacie",
    "sangour": "maison",
}

# ------------------------------------------------------------- orchestrateur

PER_SITE_TIMEOUT = 5.0   # ✅ Réponse ultra-rapide en 2-3 secondes

async def search_all(query: str) -> dict:
    # PROXY_URL est necessaire pour Sangour (Cloudflare IP-block) ; si present
    # sur le serveur API, on le passe au client httpx partage pour qu'il soit
    # utilise par tous les scrapers (les autres boutiques l'ignoreront sans effet).
    _proxy = os.getenv("PROXY_URL") or None
    async with httpx.AsyncClient(http2=False, proxy=_proxy, timeout=8.0) as client:
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

    # Tri du moins cher au plus cher
    deduped.sort(key=lambda o: (o.price is None, o.price))
    
    return {
        "query": query,
        "count": len(deduped),
        "offers": [o.to_dict() for o in deduped],
        "errors": errors,
    }