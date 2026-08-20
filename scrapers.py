"""
Scrapers asynchrones pour le comparateur de prix tunisien.
Chaque scraper est une coroutine : (query: str) -> list[ProductOffer]
"""
import asyncio
import os
import random
import re
import unicodedata
from dataclasses import dataclass, field, asdict
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
    match_score: float = 0.0        # score de fuzzy matching (0-100)

    def to_dict(self):
        return asdict(self)


# ------------------------------------------------------- parsing des prix

_PRICE_RE = re.compile(r"(\d[\d\s.,]*)")

def parse_tnd_price(raw: str) -> Optional[float]:
    """
    Convertit les formats tunisiens en float :
      "9,900 DT"   -> 9.9      (virgule = séparateur décimal, 3 décimales)
      "1 299,000 TND" -> 1299.0
      "9.900 DT"   -> 9.9      (point décimal, certains sites)
      "1.299 DT"   -> 1299.0   (point = séparateur de milliers, heuristique)
    """
    if not raw:
        return None
    m = _PRICE_RE.search(raw.replace("\xa0", " "))
    if not m:
        return None
    num = m.group(1).strip()
    if "," in num:
        # Virgule = décimale tunisienne ; points/espaces = milliers
        # "1 299,000" -> 1299.0 | "131,370" -> 131.37
        num = num.replace(" ", "").replace(".", "").replace(",", ".")
    else:
        num = num.replace(" ", "")
        # Plusieurs groupes de points -> séparateurs de milliers ("1.234.567")
        if re.fullmatch(r"\d{1,3}(\.\d{3}){2,}", num):
            num = num.replace(".", "")
        # Un seul point -> décimale ("9.900" -> 9.9, "131.370" -> 131.37)
    try:
        return float(num)
    except ValueError:
        return None


# --------------------------------------------------------- fuzzy matching

_STOPWORDS = {"de", "du", "la", "le", "les", "des", "pour", "avec", "en", "a", "au"}

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    tokens = [t for t in text.split() if t not in _STOPWORDS]
    return " ".join(tokens)

def match_score(query: str, title: str) -> float:
    """Score 0-100 : moyenne pondérée token_set (robuste à l'ordre) + partial."""
    q, t = _normalize(query), _normalize(title)
    if not q or not t:
        return 0.0
    return 0.6 * fuzz.token_set_ratio(q, t) + 0.4 * fuzz.partial_ratio(q, t)


def token_coverage(query: str, title: str) -> float:
    """Fraction des tokens de la requête retrouvés dans le titre (0.0 - 1.0).

    Un token est « couvert » s'il apparaît en sous-chaîne, en préfixe
    (pluriels : 'ventilateur'/'ventilateurs') ou en fuzzy >= 85 (petites
    fautes de frappe). Évite les faux positifs type 'stylo gel' remonté
    pour la requête 'cerave gel moussant' (1 seul token commun sur 3).
    """
    q, t = _normalize(query), _normalize(title)
    if not q or not t:
        return 0.0
    t_tokens = t.split()
    covered = 0
    for tok in q.split():
        if (tok in t
                or any(tt.startswith(tok) or tok.startswith(tt) for tt in t_tokens)
                or max((fuzz.ratio(tok, tt) for tt in t_tokens), default=0) >= 85):
            covered += 1
    return covered / len(q.split())


# ------------------------------------------------------------- HTTP layer

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

class ScraperError(Exception):
    """Erreur de scraping (réseau, blocage, structure HTML changée)."""

async def fetch(client: httpx.AsyncClient, url: str, retries: int = 2) -> str:
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
                timeout=httpx.Timeout(25.0, connect=10.0),
                follow_redirects=True,
            )
            if resp.status_code == 403:
                raise ScraperError(f"HTTP 403 (anti-bot) sur {url}")
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPError, ScraperError) as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.8 * (attempt + 1) + random.random())
    raise ScraperError(f"Échec après {retries + 1} tentatives : {last_exc}")


# Marqueurs "aucun résultat" : si présents, retourner [] au lieu de lever
# une erreur (on distingue "pas de résultat" de "structure HTML cassée").
_NO_RESULT_MARKERS = (
    "bricks-posts-nothing-found",   # Wiki.tn (Bricks/WP Grid Builder)
    "aucun produit",                # PrestaShop / générique FR
    "no products were found",       # WooCommerce
    "aucun résultat",
    "no results",
    "search again what you are looking for",   # Tunisianet (EN)
    "recherchez à nouveau",                    # SBS / PrestaShop FR
    "sorry for the inconvenience",
    "désolé pour le dérangement",
)

def _check_items(items, html: str, site: str, selector: str):
    """Retourne [] si la page indique 'aucun résultat', lève ScraperError sinon."""
    if items:
        return items
    low = html.lower()
    if any(marker in low for marker in _NO_RESULT_MARKERS):
        return []
    raise ScraperError(f"Structure HTML {site} modifiée : aucun '{selector}'")


# ---------------------------------------------------------------- scrapers
# Tunisianet & Spacenet : PrestaShop server-side -> BeautifulSoup suffit.
# Mytek / Jumia : derrière Cloudflare (HTTP 403) -> nécessitent Playwright
# (voir scrapers_browser.py) ou un service type ScraperAPI.

async def scrape_tunisianet(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    url = f"https://www.tunisianet.com.tn/recherche?controller=search&s={httpx.QueryParams({'s': query})['s']}"
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    items = _check_items(soup.select("article.product-miniature, div.product-miniature"), html, "Tunisianet", "article.product-miniature, div.product-miniature")
    for item in items[:12]:
        a = item.select_one("h2.product-title a, h3.product-title a")
        price_el = item.select_one("span.price")
        img = item.select_one("div.product-thumbnail img, a.thumbnail img")
        avail_el = item.select_one("div.store-availability-list.stock")
        if not (a and price_el):
            continue
        offers.append(ProductOffer(
            source="Tunisianet",
            title=a.get_text(strip=True),
            price=parse_tnd_price(price_el.get_text()),
            price_raw=price_el.get_text(strip=True),
            url=a.get("href", ""),
            image=(img.get("src") or img.get("data-src")) if img else None,
            availability="En stock" if avail_el else "À vérifier",
        ))
    return offers


async def scrape_spacenet(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    url = f"https://spacenet.tn/recherche?controller=search&s={httpx.QueryParams({'s': query})['s']}"
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    items = _check_items(soup.select("div.product-miniature"), html, "Spacenet", "div.product-miniature")
    for item in items[:12]:
        a = item.select_one("h2.product_name a")
        price_el = item.select_one("span.price")
        img = item.select_one("img.product_image")
        qty = item.select_one("div.product-quantities")
        if not (a and price_el):
            continue
        avail = "En stock" if (qty and "stock" in qty.get_text().lower() and "hors" not in qty.get_text().lower()) else "À vérifier"
        offers.append(ProductOffer(
            source="Spacenet",
            title=a.get_text(strip=True),
            price=parse_tnd_price(price_el.get_text()),
            price_raw=price_el.get_text(strip=True),
            url=a.get("href", ""),
            image=(img.get("data-src") or img.get("src")) if img else None,
            availability=avail,
        ))
    return offers


# Parapharmacies : WooCommerce server-side -> BeautifulSoup suffit.

async def scrape_paraexpert(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    url = f"https://www.paraexpert.tn/?s={httpx.QueryParams({'s': query})['s']}"
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    items = _check_items(soup.select(".c-post-list.product"), html, "ParaExpert", ".c-post-list.product")
    for item in items[:12]:
        a = item.select_one("a.c-post-list__header-link")
        # prix courant : <ins> en promo, sinon le span simple
        price_el = item.select_one("div.c-post-list__price ins span.woocommerce-Price-amount") \
                   or item.select_one("div.c-post-list__price span.woocommerce-Price-amount")
        img = item.select_one("div.c-post-list__thumb img")
        if not (a and price_el):
            continue
        offers.append(ProductOffer(
            source="ParaExpert",
            title=a.get_text(strip=True),
            price=parse_tnd_price(price_el.get_text()),
            price_raw=price_el.get_text(strip=True),
            url=a.get("href", ""),
            image=(img.get("src") or img.get("data-src")) if img else None,
            availability="En stock" if "instock" in (item.get("class") or []) else "À vérifier",
        ))
    return offers


async def scrape_maparatunisie(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    url = f"https://www.maparatunisie.tn/?s={httpx.QueryParams({'s': query})['s']}&post_type=product"
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    items = _check_items(soup.select("div.product-small"), html, "MaParaTunisie", "div.product-small")
    for item in items[:12]:
        a = item.select_one("p.name.product-title a")
        price_el = item.select_one("span.price ins span.woocommerce-Price-amount") \
                   or item.select_one("span.price span.woocommerce-Price-amount")
        img = item.select_one("div.box-image img")
        if not (a and price_el):
            continue
        offers.append(ProductOffer(
            source="MaParaTunisie",
            title=a.get_text(strip=True),
            price=parse_tnd_price(price_el.get_text()),
            price_raw=price_el.get_text(strip=True),
            url=a.get("href", ""),
            image=(img.get("src") or img.get("data-src")) if img else None,
            availability="En stock" if item.select_one("div.add-to-cart-button") else "À vérifier",
        ))
    return offers


async def scrape_wiki(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    """Wiki.tn : WooCommerce + builder Bricks (classes product-card__*)."""
    url = f"https://www.wiki.tn/?s={httpx.QueryParams({'s': query})['s']}&post_type=product"
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    items = _check_items(soup.select("div.product-card--grid"), html, "Wiki", "div.product-card--grid")
    for item in items[:12]:
        a = item.select_one(".product-card__title a")
        price_el = item.select_one(".product-card__price ins span.woocommerce-Price-amount") \
                   or item.select_one(".product-card__price span.woocommerce-Price-amount")
        img = item.select_one(".product-card__image img")
        if not (a and price_el):
            continue
        offers.append(ProductOffer(
            source="Wiki",
            title=a.get_text(strip=True),
            price=parse_tnd_price(price_el.get_text()),
            price_raw=price_el.get_text(strip=True),
            url=a.get("href", ""),
            image=(img.get("src") or img.get("data-src")) if img else None,
            availability="En stock",
        ))
    return offers


async def scrape_tunisiatech(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    """TunisiaTech : PrestaShop (même famille que Tunisianet)."""
    url = f"https://tunisiatech.tn/recherche?controller=search&s={httpx.QueryParams({'s': query})['s']}"
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    items = _check_items(soup.select("article.product-miniature, div.product-miniature"), html, "TunisiaTech", "article.product-miniature, div.product-miniature")
    for item in items[:12]:
        a = item.select_one("h5.product-name a, h2.product-name a, a.product-name")
        price_el = item.select_one("span.price.product-price") or item.select_one("span.price")
        img = item.select_one("img")
        if not (a and price_el):
            continue
        # images lazy-loadées : la vraie URL est dans data-original
        image = None
        if img:
            image = img.get("data-original") or img.get("data-src")
            if not image and img.get("src", "").startswith("http"):
                image = img.get("src")
        offers.append(ProductOffer(
            source="TunisiaTech",
            title=a.get_text(strip=True),
            price=parse_tnd_price(price_el.get_text()),
            price_raw=price_el.get_text(strip=True),
            url=a.get("href", ""),
            image=image,
            availability="En stock",
        ))
    return offers


# ------------------------------------------------------- PrestaShop générique
# Darty, Technopro, SBS Informatique et MyCare partagent la même structure
# PrestaShop 1.7 (article.product-miniature, /recherche?s=...) — validé en live.

async def _scrape_prestashop(query: str, client: httpx.AsyncClient,
                             source: str, search_url: str) -> list[ProductOffer]:
    url = search_url.format(q=httpx.QueryParams({"s": query})["s"])
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    card_sel = "article.product-miniature, div.product-miniature"
    items = _check_items(soup.select(card_sel), html, source, card_sel)
    for item in items[:12]:
        a = item.select_one("h2.product-title a, h3.product-title a, "
                            "h2.product-name a, a.product-name, .product-title a")
        price_el = item.select_one("span.product-price, span.price, .product-price")
        img = item.select_one("img")
        if not (a and price_el):
            continue
        image = None
        if img:
            image = (img.get("data-full-size-image-url") or img.get("data-src")
                     or img.get("data-original"))
            if not image and (img.get("src") or "").startswith("http"):
                image = img.get("src")
        offers.append(ProductOffer(
            source=source,
            title=a.get_text(strip=True),
            price=parse_tnd_price(price_el.get_text()),
            price_raw=price_el.get_text(strip=True),
            url=a.get("href", ""),
            image=image,
            availability="En stock",
        ))
    return offers


async def scrape_darty(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "Darty", "https://darty.tn/recherche?s={q}")


async def scrape_technopro(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "Technopro", "https://www.technopro-online.com/recherche?s={q}")


async def scrape_sbs(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "SBS Informatique", "https://www.sbsinformatique.com/recherche?s={q}")


async def scrape_mycare(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    return await _scrape_prestashop(query, client, "MyCare", "https://mycare.tn/recherche?s={q}")


async def scrape_megapc(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    """MegaPC : front Next.js rendu côté serveur — cartes article.product-card,
    prix dans un span 'text-skin-primary' (format "849 DT"), liens relatifs."""
    url = f"https://megapc.tn/?s={httpx.QueryParams({'s': query})['s']}&post_type=product"
    html = await fetch(client, url)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    items = _check_items(soup.select("article.product-card"), html, "MegaPC", "article.product-card")
    for item in items[:12]:
        a = item.select_one("a[href*='/shop/product/']")
        title = item.get("title") or (a.get_text(strip=True) if a else None)
        price_el = item.select_one("span.text-skin-primary")
        if not (a and title and price_el):
            continue
        # Image Next.js : src = placeholder data:, la vraie URL est dans srcset
        img = item.select_one("img")
        image = None
        if img:
            srcset = img.get("srcset") or ""
            cand = srcset.split(",")[0].split(" ")[0] if srcset else (img.get("src") or "")
            if cand and not cand.startswith("data:"):
                image = cand if cand.startswith("http") else f"https://megapc.tn{cand}"
        badge = item.select_one("span.bg-skin-primary")   # ex. "Dans 10 Jours", "New"
        avail = badge.get_text(strip=True) if badge else "En stock"
        href = a.get("href", "")
        offers.append(ProductOffer(
            source="MegaPC",
            title=title,
            price=parse_tnd_price(price_el.get_text()),
            price_raw=price_el.get_text(strip=True),
            url=href if href.startswith("http") else f"https://megapc.tn{href}",
            image=image,
            availability=avail,
        ))
    return offers


# ---------------------------------------------------------------------------
# Mytek (API GraphQL publique — pas de Cloudflare sur /graphql !)
# Technique validée par le projet mytek-radar : les pages HTML sont protégées
# par Cloudflare, mais l'API GraphQL (moteur OpenSearch) répond en httpx simple.
# ---------------------------------------------------------------------------

MYTEK_GRAPHQL = "https://www.mytek.tn/graphql"
MYTEK_MEDIA = "https://www.mytek.tn/media/catalog/product"

MYTEK_QUERY = """
query ($search: String, $page: Int, $pageSize: Int) {
  opensearchProductSearch(search: $search, page: $page, pageSize: $pageSize) {
    total_count
    items { id sku name price special_price final_price image url manufacturer }
  }
}
"""


async def scrape_mytek(query: str, client: httpx.AsyncClient) -> list[ProductOffer]:
    resp = await client.post(
        MYTEK_GRAPHQL,
        json={"query": MYTEK_QUERY, "variables": {"search": query, "page": 1, "pageSize": 30}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body and not body.get("data"):
        raise ScraperError(f"Mytek GraphQL: {body['errors']}")
    items = (body["data"]["opensearchProductSearch"] or {}).get("items") or []

    offers = []
    for it in items[:20]:
        price = it.get("special_price") or it.get("final_price") or it.get("price")
        title = (it.get("name") or "").strip()
        if not price or float(price) <= 0 or not title:
            continue
        img = it.get("image") or ""
        if img.startswith("/"):
            img = MYTEK_MEDIA + img
        url = it.get("url") or ""
        if url and not url.startswith("http"):
            url = "https://www.mytek.tn/" + url.lstrip("/")
        offers.append(ProductOffer(
            source="Mytek",
            title=title,
            price=round(float(price), 3),
            price_raw=f"{float(price):,.3f} TND",
            url=url,
            image=img or None,
            availability="À vérifier",  # l'API GraphQL n'expose pas le stock
        ))
    return offers


SCRAPERS = {
    "tunisianet": scrape_tunisianet,
    "spacenet": scrape_spacenet,
    "tunisiatech": scrape_tunisiatech,
    "wiki": scrape_wiki,
    "darty": scrape_darty,
    "technopro": scrape_technopro,
    "sbs": scrape_sbs,
    "megapc": scrape_megapc,
    "paraexpert": scrape_paraexpert,
    "maparatunisie": scrape_maparatunisie,
    "mycare": scrape_mycare,
    "mytek": scrape_mytek,
}


# ------------------------------------------------------------- orchestrateur

PER_SITE_TIMEOUT = 30.0   # un site lent ne doit jamais retarder les autres

# Sites derrière Cloudflare Turnstile (Sangour, Wamia, T-Discount…) : nécessitent
# Playwright + une IP résidentielle tunisienne. Activés via ENABLE_BROWSER_SCRAPERS=1.
# (Mytek n'en fait plus partie : son API GraphQL répond en httpx simple.)
ENABLE_BROWSER_SCRAPERS = os.getenv("ENABLE_BROWSER_SCRAPERS", "0") == "1"

async def search_all(query: str, min_score: float = 45.0) -> dict:
    """Lance tous les scrapers en parallèle, normalise, filtre et trie."""
    async with httpx.AsyncClient(http2=False) as client:
        async def guarded(name, fn):
            try:
                return name, await asyncio.wait_for(fn(query, client), timeout=PER_SITE_TIMEOUT), None
            except Exception as exc:                       # un site en panne ne bloque pas les autres
                return name, [], f"{type(exc).__name__}: {exc}" or type(exc).__name__

        tasks = [guarded(n, f) for n, f in SCRAPERS.items()]

        if ENABLE_BROWSER_SCRAPERS:
            async def guarded_browser():
                try:
                    from scrapers_browser import scrape_browser_sites, SITES
                    return await asyncio.wait_for(scrape_browser_sites(query), timeout=120)
                except Exception as exc:
                    # Échec global (navigateur non installé, libs manquantes...) :
                    # on attribue l'erreur à chaque site navigateur pour la transparence
                    try:
                        from scrapers_browser import SITES
                        labels = [cfg["label"] for cfg in SITES.values()]
                    except Exception:
                        labels = ["Mytek", "Wamia", "Sangour"]
                    return [], {l: f"{type(exc).__name__}: {exc}" for l in labels}
            tasks.append(guarded_browser())

        outcomes = await asyncio.gather(*tasks)
        results, errors = [], {}
        for outcome in outcomes:
            if ENABLE_BROWSER_SCRAPERS and len(outcome) == 2:   # (offers, errors) du navigateur
                b_offers, b_errors = outcome
                results.extend(b_offers)
                errors.update(b_errors)
            else:
                name, offers, err = outcome
                results.extend(offers)
                if err:
                    errors[name] = err

    for offer in results:
        offer.match_score = round(match_score(query, offer.title), 1)

    # Déduplication par (boutique, URL) — certains thèmes répètent les produits
    seen, deduped = set(), []
    for o in results:
        key = (o.source, o.url)
        if key not in seen:
            seen.add(key)
            deduped.append(o)

    filtered = [o for o in deduped
                if o.match_score >= min_score
                and token_coverage(query, o.title) >= 0.5
                and o.price is not None]
    filtered.sort(key=lambda o: o.price)
    return {
        "query": query,
        "count": len(filtered),
        "offers": [o.to_dict() for o in filtered],
        "errors": errors,
    }


if __name__ == "__main__":
    import json, sys
    q = sys.argv[1] if len(sys.argv) > 1 else "ventilateur"
    print(json.dumps(asyncio.run(search_all(q)), ensure_ascii=False, indent=2))
