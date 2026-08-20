"""
Scrapers Playwright pour les boutiques derrière Cloudflare Turnstile ou
injoignables en httpx : Sangour, Wamia, T-Discount, Scoop, Graiet,
Maalej Audio, Affariyet, Drest, Bricorama, Electro Tounes
(HTTP 403 sur toute requête non-navigateur, vérifié).

Note : Mytek n'est PAS ici — son API GraphQL publique (/graphql) n'est pas
protégée par Cloudflare, il est scrapé en httpx dans scrapers.py.

⚠️  IMPORTANT — IP : le challenge Turnstile ne se résout PAS depuis une IP
de datacenter (Render, Railway, GitHub Actions...). Ces scrapers doivent
tourner depuis une IP résidentielle tunisienne (machine locale, VPS TN,
ou proxy résidentiel). Depuis une IP tunisienne, le challenge se résout
tout seul en quelques secondes avec un vrai navigateur.

Installation :
    pip install playwright
    playwright install chromium          # build complet, pas seulement le shell

Usage :
    python scrapers_browser.py "ventilateur"              # toutes les boutiques navigateur
    python scrapers_browser.py "ventilateur" sangour      # une seule boutique

Stratégie anti-détection :
  - masquage de navigator.webdriver et autres empreintes d'automatisation
  - vrai Chromium (headless shell = détecté), xvfb-run recommandé en serveur
  - attente active du passage du challenge (titre "Un instant…" -> titre réel)
  - extraction multi-plateforme : on teste les sélecteurs PrestaShop,
    WooCommerce, Magento et OpenCart dans la page, et on garde le jeu qui
    trouve le plus de cartes produit (utile quand le thème exact est inconnu).
"""
import asyncio
import os
import random
import sys

from scrapers import ProductOffer, parse_tnd_price, match_score, ScraperError

HEADLESS = os.getenv("HEADLESS", "1") == "1"   # HEADLESS=0 + xvfb-run = plus fiable

# --------------------------------------------------------------------------
# Configuration des sites
# --------------------------------------------------------------------------
# Chaque site liste des URLs de recherche candidates, essayées dans l'ordre
# jusqu'à trouver des cartes produit (la plateforme exacte de Wamia/Sangour
# n'étant pas vérifiable depuis une IP bloquée, on couvre les 4 CMS courants).
def _candidates(base: str) -> list[str]:
    """URLs de recherche candidates pour les 4 CMS courants, dans l'ordre."""
    return [
        f"{base}/recherche?controller=search&s={{q}}",            # PrestaShop 1.7
        f"{base}/?s={{q}}&post_type=product",                     # WooCommerce
        f"{base}/index.php?route=product/search&search={{q}}",    # OpenCart
        f"{base}/catalogsearch/result/?q={{q}}",                  # Magento
    ]

SITES = {
    "sangour": {
        "label": "Sangour",
        # WooCommerce + thème Woodmart — validé par le scraper Sangoor-radar
        # (github.com/mohamedZ79/Sangoor-radar) : cartes .product-grid-item,
        # titres .wd-entities-title, images lazy-loadées via data-wood-src.
        "search_urls": [
            "https://sangour.tn/?s={q}&post_type=product",
            "https://sangour.tn/recherche?controller=search&s={q}",
        ],
    },
    "wamia":        {"label": "Wamia",          "search_urls": _candidates("https://www.wamia.tn")},
    "tdiscount":    {"label": "T-Discount",     "search_urls": _candidates("https://tdiscount.tn")},
    "scoop":        {"label": "Scoop",          "search_urls": _candidates("https://www.scoop.com.tn")},
    "graiet":       {"label": "Graiet",         "search_urls": _candidates("https://graiet.tn")},
    "maalej":       {"label": "Maalej Audio",   "search_urls": _candidates("https://maalejaudio.tn")},
    "affariyet":    {"label": "Affariyet",      "search_urls": _candidates("https://affariyet.com")},
    "drest":        {"label": "Drest",          "search_urls": _candidates("https://drest.tn")},
    # Injoignables depuis une IP étrangère (geo-blocage probable) — à tester depuis la Tunisie
    "bricorama":    {"label": "Bricorama",      "search_urls": _candidates("https://bricorama.tn")},
    "electrotounes": {"label": "Electro Tounes", "search_urls": _candidates("https://electrotounes.tn")},
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

# Script d'initialisation : efface les empreintes typiques de Playwright
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['fr-TN', 'fr', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = {runtime: {}};
"""

# Extraction multi-plateforme exécutée DANS la page : renvoie le premier jeu
# de sélecteurs qui trouve >= 2 cartes, avec les données déjà extraites.
EXTRACT_JS = """
(maxItems) => {
  const text = (el, sel) => { const n = el.querySelector(sel); return n ? n.textContent.trim() : null; };
  const attr = (el, sel, a) => { const n = el.querySelector(sel); return n ? n.getAttribute(a) : null; };
  const imgOf = (el) => {
    const img = el.querySelector('img');
    if (!img) return null;
    return img.getAttribute('data-wood-src')     // Woodmart (Sangour) — validé
        || img.getAttribute('data-src') || img.getAttribute('data-lazy-src')
        || img.getAttribute('data-image') || img.getAttribute('src');
  };
  const SETS = [
    { card: '.product-grid-item',                            // WooCommerce + Woodmart (Sangour — validé)
      title: '.wd-entities-title a, h3 a, .woocommerce-loop-product__title',
      price: 'ins .woocommerce-Price-amount, .price',
      link: '.wd-entities-title a, a[href*="/product/"]' },
    { card: 'article.product-miniature',                     // PrestaShop 1.7
      title: '.product-title a, h2 a, h3 a', price: '.product-price-and-shipping .price, span.price',
      link: '.product-title a, h2 a, h3 a' },
    { card: 'li.product-item, .product-item-info',           // Magento 2
      title: 'a.product-item-link', price: '.price-box .price, span.price',
      link: 'a.product-item-link' },
    { card: 'li.product, .product-small',                    // WooCommerce classique
      title: '.woocommerce-loop-product__title, .product-title a, h2 a, h3 a, .name a',
      price: 'ins .woocommerce-Price-amount, .price',
      link: 'a.woocommerce-LoopProduct-link, .product-title a, h2 a, h3 a, .name a' },
    { card: '.product-layout, .product-thumb',                 // OpenCart
      title: 'h4 a, .caption a', price: '.price, .price-new', link: 'h4 a, .caption a' },
    { card: '.product-card, [class*="product-card"]',          // thèmes custom (Bricks...)
      title: 'a[href*="produit"], a[href*="product"], .product-card__title a, h2 a, h3 a',
      price: '[class*="price"]', link: 'a[href*="produit"], a[href*="product"]' },
  ];
  for (const s of SETS) {
    const cards = [...document.querySelectorAll(s.card)].slice(0, maxItems);
    if (cards.length < 2) continue;
    const out = [];
    for (const c of cards) {
      const title = text(c, s.title);
      const price = text(c, s.price);
      const link = attr(c, s.link, 'href');
      if (title && price && link) {
        out.push({ title, price_raw: price, url: link, image: imgOf(c),
                   oos: !!c.querySelector('.out-of-stock, .unavailable, [class*="rupture"]') });
      }
    }
    if (out.length >= 2) return { selector: s.card, items: out };
  }
  return null;
}
"""

CHALLENGE_TITLES = ("un instant", "just a moment", "attention required")


async def _new_context(pw):
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
              "--disable-dev-shm-usage", "--disable-infobars"],
    )
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        locale="fr-TN",
        viewport={"width": 1366, "height": 900},
        timezone_id="Africa/Tunis",
    )
    await context.add_init_script(STEALTH_JS)
    return browser, context


async def _wait_challenge_passed(page, timeout_s: int = 30) -> bool:
    """Attend que Cloudflare laisse passer (le titre change)."""
    for _ in range(timeout_s // 2):
        title = (await page.title()).lower()
        if not any(t in title for t in CHALLENGE_TITLES):
            return True
        await page.wait_for_timeout(2000)
    return False


async def _scrape_site(context, key: str, cfg: dict, query: str) -> list[ProductOffer]:
    label = cfg["label"]
    page = await context.new_page()
    try:
        for url_tpl in cfg["search_urls"]:
            url = url_tpl.format(q=query.replace(" ", "+"))
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                continue                                    # URL candidate invalide -> suivante

            if not await _wait_challenge_passed(page):
                raise ScraperError(
                    f"{label} : challenge Cloudflare non résolu — IP datacenter ? "
                    "Lancez depuis une IP tunisienne (voir README).")

            # Laisse le JS produit se charger, puis extraction multi-plateforme
            await page.wait_for_timeout(2500 + random.randint(0, 1500))
            data = await page.evaluate(EXTRACT_JS, 12)
            if not data:
                continue                                    # sélecteurs KO -> URL candidate suivante

            offers = []
            for it in data["items"]:
                offers.append(ProductOffer(
                    source=label,
                    title=it["title"],
                    price=parse_tnd_price(it["price_raw"]),
                    price_raw=it["price_raw"],
                    url=it["url"] if it["url"].startswith("http") else url.rsplit("/", 1)[0] + "/" + it["url"].lstrip("/"),
                    image=it.get("image"),
                    availability="Rupture" if it.get("oos") else "En stock",
                ))
            return offers

        raise ScraperError(f"{label} : aucune URL de recherche n'a retourné de produits "
                           f"(structure du site changée ?)")
    finally:
        await page.close()


async def scrape_browser_sites(query: str, sites: list[str] | None = None) -> tuple[list[ProductOffer], dict]:
    """Scrape les sites Cloudflare en parallèle dans un seul navigateur.
    Retourne (offres, erreurs_par_site) — même contrat que search_all."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise ScraperError("playwright non installé : pip install playwright && playwright install chromium")

    keys = sites or list(SITES)
    async with async_playwright() as pw:
        browser, context = await _new_context(pw)
        try:
            async def guarded(k):
                try:
                    return k, await asyncio.wait_for(
                        _scrape_site(context, k, SITES[k], query), timeout=90), None
                except Exception as exc:
                    return k, [], f"{type(exc).__name__}: {exc}"

            outcomes = await asyncio.gather(*(guarded(k) for k in keys))
        finally:
            await browser.close()

    offers, errors = [], {}
    for k, site_offers, err in outcomes:
        offers.extend(site_offers or [])
        if err:
            errors[SITES[k]["label"]] = err
    return offers, errors


# Rétro-compatibilité : l'ancienne entrée Mytek reste importable
async def scrape_mytek(query: str) -> list[ProductOffer]:
    offers, errors = await scrape_browser_sites(query, sites=["mytek"])
    if errors:
        raise ScraperError(next(iter(errors.values())))
    return offers


if __name__ == "__main__":
    import json
    q = sys.argv[1] if len(sys.argv) > 1 else "ventilateur"
    only = [sys.argv[2]] if len(sys.argv) > 2 else None
    offers, errors = asyncio.run(scrape_browser_sites(q, sites=only))
    for o in offers:
        o.match_score = round(match_score(q, o.title), 1)
    print(json.dumps({
        "count": len(offers),
        "offers": [o.to_dict() for o in offers],
        "errors": errors,
    }, ensure_ascii=False, indent=2))
