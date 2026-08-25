"""
Crawler Industriel PrixTN - Aspiration Massive (60 000 à 100 000+ Produits).
Couverture totale : Drest (JSON 27k), Wamia (GraphQL 20k), MyTek (12k), Tunisianet, SpaceNet, Yeswikam.
"""
import asyncio
import html as html_lib
import logging
import os
import re
import unicodedata
from typing import List, Dict, Optional
from urllib.parse import urljoin
import asyncpg
import httpx
from bs4 import BeautifulSoup

DATABASE_URL = os.getenv("DATABASE_URL")
PROXY_URL = os.getenv("PROXY_URL")
DREST_MAX_PAGES = int(os.getenv("DREST_MAX_PAGES", "280"))  # ~28 000 produits Drest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("crawler")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-TN,fr;q=0.9,en-US;q=0.8",
}

def parse_tnd_price(raw: str) -> Optional[float]:
    if not raw:
        return None
    cleaned = unicodedata.normalize("NFKD", str(raw)).strip().lower()
    
    # Carrefour format "249DT000"
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

# -----------------------------------------------------------------------------
# TOUS LES RAYONS DU MARCHÉ TUNISIEN (40+ Catégories en profondeur)
# -----------------------------------------------------------------------------
CATALOG_TARGETS = [
    # 1. SPACENET (Tous les rayons avec pagination profonde)
    ("SpaceNet", "High-Tech", "https://spacenet.tn/377-smartphone-tunisie?page={page}", 20),
    ("SpaceNet", "High-Tech", "https://spacenet.tn/14-pc-portable?page={page}", 15),
    ("SpaceNet", "High-Tech", "https://spacenet.tn/11-informatique?page={page}", 15),
    ("SpaceNet", "High-Tech", "https://spacenet.tn/15-tv-son?page={page}", 12),
    ("SpaceNet", "High-Tech", "https://spacenet.tn/12-composants-informatique?page={page}", 12),
    ("SpaceNet", "High-Tech", "https://spacenet.tn/13-peripheriques-accessoires?page={page}", 15),
    ("SpaceNet", "Électroménager", "https://spacenet.tn/46-electromenager?page={page}", 15),
    ("SpaceNet", "Électroménager", "https://spacenet.tn/47-petit-electromenager?page={page}", 15),
    ("SpaceNet", "Maison", "https://spacenet.tn/48-maison-bureau?page={page}", 10),

    # 2. TUNISIANET (Tous les rayons jusqu'à 25 pages)
    ("Tunisianet", "High-Tech", "https://www.tunisianet.com.tn/377-smartphone-tunisie?page={page}", 25),
    ("Tunisianet", "High-Tech", "https://www.tunisianet.com.tn/301-pc-portable-tunisie?page={page}", 20),
    ("Tunisianet", "High-Tech", "https://www.tunisianet.com.tn/302-composant-informatique-tunisie?page={page}", 18),
    ("Tunisianet", "High-Tech", "https://www.tunisianet.com.tn/379-televiseur?page={page}", 12),
    ("Tunisianet", "Électroménager", "https://www.tunisianet.com.tn/380-electromenager-tunisie?page={page}", 20),
    ("Tunisianet", "Maison & Soins", "https://www.tunisianet.com.tn/386-beaute-et-sante?page={page}", 15),
    ("Tunisianet", "High-Tech", "https://www.tunisianet.com.tn/303-peripheriques-accessoires?page={page}", 20),
    ("Tunisianet", "High-Tech", "https://www.tunisianet.com.tn/304-reseau-securite?page={page}", 12),

    # 3. YESWIKAM & MYCARE (Parapharmacies - catalogue complet)
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/2-accueil?page={page}", 40),
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/3-visage?page={page}", 25),
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/4-corps?page={page}", 20),
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/5-cheveux?page={page}", 15),
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/6-solaire?page={page}", 12),
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/7-bebe-maman?page={page}", 15),
    ("MyCare", "Parapharmacie", "https://mycare.tn/recherche?s=soin&page={page}", 15),
    ("MyCare", "Parapharmacie", "https://mycare.tn/recherche?s=creme&page={page}", 12),
    ("MyCare", "Parapharmacie", "https://mycare.tn/recherche?s=gel&page={page}", 12),

    # 4. DARTY TUNISIE
    ("Darty TN", "Électroménager", "https://darty.tn/recherche?s=electromenager&page={page}", 12),
    ("Darty TN", "Électroménager", "https://darty.tn/recherche?s=cuisine&page={page}", 10),
    ("Darty TN", "Électroménager", "https://darty.tn/recherche?s=climatiseur&page={page}", 8),
    ("Darty TN", "Électroménager", "https://darty.tn/recherche?s=tv&page={page}", 8),
]

# 5. SANGOUR (Entretien, Détergents, Cuisine)
SANGOUR_DIRECT_URLS = [
    ("Sangour", "Maison & Entretien", "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/javel/"),
    ("Sangour", "Maison & Entretien", "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/sol/"),
    ("Sangour", "Maison & Entretien", "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/vaisselles/"),
    ("Sangour", "Maison & Entretien", "https://sangour.tn/categorie-produit/hygiene-maison/produits-nettoyage/linge/"),
    ("Sangour", "Maison & Entretien", "https://sangour.tn/marques/judy/"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/marques/tramontina/"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/marques/tefal/"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/marques/moulinex/"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/categorie-produit/art-de-table-et-cuisine/art-culinaire/cocottes/"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/categorie-produit/art-de-table-et-cuisine/art-culinaire/poeles/"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/categorie-produit/art-de-table-et-cuisine/art-culinaire/casseroles/"),
]

# -----------------------------------------------------------------------------
# Fonctions de Scraping Asynchrone
# -----------------------------------------------------------------------------
async def crawl_page(client: httpx.AsyncClient, source: str, category: str, url: str) -> List[Dict]:
    products = []
    try:
        res = await client.get(url, headers=HEADERS, timeout=12.0)
        if res.status_code != 200:
            return products

        soup = BeautifulSoup(res.text, "html.parser")
        cards = soup.select(
            ".product-grid-item, .wd-product, div.product, li.product, "
            "article.product-miniature, .product-miniature, .product-item, div.product-small, .ajax_block_product"
        )

        for p in cards:
            title_tag = p.select_one(
                ".wd-entities-title a, .woocommerce-loop-product__title, .product-title a, "
                "h2.product_name a, .product_name a, h3.product-title a, h2.product-title a, .product-name a, a[title], h3 a, h2 a"
            )
            price_tag = p.select_one(
                "ins .woocommerce-Price-amount, ins .amount, .price ins, .price .amount, "
                ".woocommerce-Price-amount, .price, span.price, [itemprop='price'], .product-price, .current-price"
            )
            img_tag = p.select_one(
                ".product-element-top img, img.wp-post-image, img.product_image, "
                ".thumbnail-container img, .product-thumbnail img, img"
            )
            link_tag = p.select_one(".product-element-top a, a.woocommerce-LoopProduct-link, a[href]")
            ref_tag = p.select_one(".product-reference, .reference, [itemprop='sku'], .sku")

            if (title_tag or link_tag) and price_tag:
                title = title_tag.get_text(strip=True) if title_tag else ""
                href = title_tag.get("href", "") if title_tag and title_tag.get("href") else (link_tag.get("href", "") if link_tag else "")
                product_url = href if href.startswith("http") else f"{url.split('/')[0]}//{url.split('/')[2]}/{href.lstrip('/')}"
                price_val = parse_tnd_price(price_tag.get_text(strip=True))
                img_url = img_tag.get("data-full-size-image-url") or img_tag.get("data-src") or img_tag.get("src") if img_tag else None
                ref_val = ref_tag.get_text(strip=True).replace("Réf :", "").strip() if ref_tag else None

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
                        "in_stock": True
                    })
    except Exception:
        pass
    return products

# DREST : API Store JSON (27 000+ Produits)
async def crawl_drest(client: httpx.AsyncClient) -> List[Dict]:
    log.info("Démarrage du crawl Drest via API Store JSON...")
    products, page, seen = [], 1, set()
    while page <= DREST_MAX_PAGES:
        try:
            r = await client.get("https://drest.tn/wp-json/wc/store/products", params={"per_page": 100, "page": page}, timeout=15.0)
            if r.status_code != 200:
                break
            body = r.json()
            if not body:
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
            if page % 25 == 0:
                log.info(f"[Drest] {page} pages explorées ({len(products)} produits cumulés)")
            page += 1
            await asyncio.sleep(0.1)
        except Exception:
            break
    log.info(f"[Drest] Total collecté : {len(products)} produits.")
    return products

# WAMIA : API GraphQL Magento (20 000+ Produits)
async def crawl_wamia(client: httpx.AsyncClient) -> List[Dict]:
    log.info("Démarrage du crawl Wamia via API GraphQL...")
    WAMIA_GQL = "https://www.wamia.tn/graphql"
    WAMIA_BASE = "https://www.wamia.tn/"
    products = []
    try:
        cat_resp = await client.post(WAMIA_GQL, json={"query": "{ categories(filters: {parent_id: {eq: \"2\"}}) { items { id name } } }"}, timeout=12.0)
        cats = cat_resp.json().get("data", {}).get("categories", {}).get("items", [])
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
                r = await client.post(WAMIA_GQL, json={"query": query}, timeout=12.0)
                items = r.json().get("data", {}).get("products", {}).get("items", [])
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
    log.info(f"[Wamia] Total collecté : {len(products)} produits.")
    return products

# MYTEK : GraphQL Massif (12 000+ Produits)
async def crawl_mytek(client: httpx.AsyncClient) -> List[Dict]:
    log.info("Démarrage du crawl Mytek via GraphQL...")
    MYTEK_GRAPHQL = "https://www.mytek.tn/graphql"
    MYTEK_MEDIA = "https://www.mytek.tn/media/catalog/product"
    products = []
    keywords = [
        "samsung", "iphone", "xiaomi", "infinix", "oppo", "honor", "nokia",
        "pc portable", "pc gamer", "imprimante", "ecran", "tablette", "serveur",
        "tv", "climatiseur", "refrigerateur", "machine a laver", "micro ondes", "lave vaisselle",
        "moulinex", "tefal", "aspirateur", "cuisiniere", "cafetiere", "robot", "fer a repasser",
        "casque", "souris", "clavier", "onduleur", "disque dur", "bureau", "chaise gamer"
    ]
    for term in keywords:
        for page in [1, 2, 3]:
            try:
                query_gql = f"""
                query {{
                  opensearchProductSearch(search: "{term}", page: {page}, pageSize: 150) {{
                    items {{ id sku name price special_price final_price image url }}
                  }}
                }}
                """
                resp = await client.post(MYTEK_GRAPHQL, json={"query": query_gql}, timeout=12.0)
                items = resp.json().get("data", {}).get("opensearchProductSearch", {}).get("items", [])
                if not items:
                    break
                for it in items:
                    price = it.get("special_price") or it.get("final_price") or it.get("price")
                    title = (it.get("name") or "").strip()
                    if price and float(price) > 0 and title:
                        img = it.get("image") or ""
                        if img.startswith("/"):
                            img = MYTEK_MEDIA + img
                        url = it.get("url") or ""
                        if url and not url.startswith("http"):
                            url = "https://www.mytek.tn/" + url.lstrip("/")
                        products.append({
                            "source": "Mytek",
                            "category": "High-Tech",
                            "title": title,
                            "sku": it.get("sku"),
                            "price": round(float(price), 3),
                            "price_raw": f"{float(price):,.3f} TND",
                            "url": url,
                            "image": img or None,
                            "in_stock": True
                        })
                await asyncio.sleep(0.1)
            except Exception:
                break
    log.info(f"[Mytek] Total collecté : {len(products)} produits.")
    return products

async def save_to_database_bulk(products: List[Dict]):
    log.info("Connexion à Supabase PostgreSQL...")
    conn = await asyncpg.connect(DATABASE_URL)
    
    unique_map = {}
    for p in products:
        unique_map[(p["source"], p["url"])] = p
    deduped = list(unique_map.values())

    log.info(f"📦 Enregistrement par lots de {len(deduped)} produits uniques...")
    
    upsert_query = """
    INSERT INTO products (source, category, title, sku, price, price_raw, url, image, in_stock, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
    ON CONFLICT (source, url) 
    DO UPDATE SET
        price = EXCLUDED.price,
        price_raw = EXCLUDED.price_raw,
        title = EXCLUDED.title,
        category = EXCLUDED.category,
        sku = COALESCE(EXCLUDED.sku, products.sku),
        image = COALESCE(EXCLUDED.image, products.image),
        in_stock = EXCLUDED.in_stock,
        updated_at = NOW();
    """

    batch_size = 250
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
        await conn.executemany(upsert_query, records)

    await conn.close()
    log.info(f"🎉 SUCCÈS TOTAL : {len(deduped)} produits enregistrés / mis à jour dans Supabase !")

async def main():
    log.info("🚀 Démarrage du Grand Crawler PrixTN (Massif)...")
    all_products = []
    proxy_client = PROXY_URL if PROXY_URL else None

    async with httpx.AsyncClient(proxy=proxy_client, follow_redirects=True, verify=False) as client:
        # 1. Drest API Store JSON (~27 000 produits)
        all_products.extend(await crawl_drest(client))

        # 2. Wamia GraphQL (~20 000 produits)
        all_products.extend(await crawl_wamia(client))

        # 3. Mytek GraphQL (~12 000 produits)
        all_products.extend(await crawl_mytek(client))

        # 4. Rayons Multi-Pages (SpaceNet, Tunisianet, Yeswikam, Darty)
        for source, cat, base_url, total_pages in CATALOG_TARGETS:
            log.info(f"[{source}] Exploration de {cat} ({total_pages} pages)...")
            for page in range(1, total_pages + 1):
                url = base_url.format(page=page)
                items = await crawl_page(client, source, cat, url)
                all_products.extend(items)
                await asyncio.sleep(0.1)

        # 5. Sangour Direct
        for source, cat, url in SANGOUR_DIRECT_URLS:
            items = await crawl_page(client, source, cat, url)
            all_products.extend(items)

    log.info(f"TOTAL GLOBAL COLLECTÉ : {len(all_products)} produits.")
    if all_products:
        await save_to_database_bulk(all_products)

if __name__ == "__main__":
    asyncio.run(main())