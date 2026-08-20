"""
Robot d'Indexation (Crawler) pour PrixTN.
Scrape les catalogues marchands tunisiens et remplit la base de données Supabase.
Lancement manuel : python crawler.py
"""
import asyncio
import os
import re
import unicodedata
from typing import List, Dict, Optional
import httpx
from bs4 import BeautifulSoup
import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:[YOUR-PASSWORD]@db.lmhbpzvxmumucdsjdurb.supabase.co:5432/postgres")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
}

# -----------------------------------------------------------------------------
# Parsing et Normalisation des Prix Tunisiens
# -----------------------------------------------------------------------------
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
# Catalogues Cibles à Indexer (Maison, High-Tech, Parapharmacie, Entretien)
# -----------------------------------------------------------------------------
CATALOG_TARGETS = [
    # High-Tech & Téléphonie
    ("SpaceNet", "High-Tech", "https://spacenet.tn/recherche?controller=search&s=smartphone"),
    ("SpaceNet", "High-Tech", "https://spacenet.tn/recherche?controller=search&s=samsung"),
    ("SpaceNet", "High-Tech", "https://spacenet.tn/recherche?controller=search&s=pc+portable"),
    ("Tunisianet", "High-Tech", "https://www.tunisianet.com.tn/recherche?controller=search&s=samsung"),
    ("Tunisianet", "High-Tech", "https://www.tunisianet.com.tn/recherche?controller=search&s=smartphone"),
    ("Wiki", "High-Tech", "https://www.wiki.tn/?s=samsung&post_type=product"),
    ("Darty TN", "Électroménager", "https://darty.tn/recherche?s=philips"),
    ("Darty TN", "Électroménager", "https://darty.tn/recherche?s=moulinex"),
    
    # Maison, Entretien & Détergents (Sangour Judy, Tramontina...)
    ("Sangour", "Maison & Entretien", "https://sangour.tn/?s=judy"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/?s=tramontina"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/?s=tefal"),
    ("Wamia", "Maison & Électro", "https://wamia.tn/recherche?s=cuisine"),

    # Parapharmacies (Yeswikam, Paraexpert, MyCare...)
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/recherche?s=svr"),
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/recherche?s=bioderma"),
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/recherche?s=cerave"),
    ("Paraexpert", "Parapharmacie", "https://paraexpert.tn/?s=svr"),
    ("MyCare", "Parapharmacie", "https://mycare.tn/recherche?s=svr"),
    ("Phyto.tn", "Parapharmacie", "https://phyto.tn/recherche?s=gel"),
]

# -----------------------------------------------------------------------------
# Scrapers d'indexation
# -----------------------------------------------------------------------------
async def crawl_url(client: httpx.AsyncClient, source: str, category: str, url: str) -> List[Dict]:
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
    except Exception as e:
        print(f"Erreur sur {url} : {e}")
    return products

# Scraper GraphQL MyTek pour indexer tout le catalogue High-Tech
async def crawl_mytek(client: httpx.AsyncClient) -> List[Dict]:
    products = []
    try:
        MYTEK_GRAPHQL = "https://www.mytek.tn/graphql"
        MYTEK_MEDIA = "https://www.mytek.tn/media/catalog/product"
        query_gql = """
        query {
          opensearchProductSearch(search: "samsung", page: 1, pageSize: 60) {
            items { id sku name price special_price final_price image url }
          }
        }
        """
        resp = await client.post(MYTEK_GRAPHQL, json={"query": query_gql}, headers={"Content-Type": "application/json"}, timeout=12.0)
        items = resp.json().get("data", {}).get("opensearchProductSearch", {}).get("items", [])
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
                    "source": "MyTek",
                    "category": "High-Tech",
                    "title": title,
                    "sku": it.get("sku"),
                    "price": round(float(price), 3),
                    "price_raw": f"{float(price):,.3f} TND",
                    "url": url,
                    "image": img or None,
                    "in_stock": True
                })
    except Exception as e:
        print(f"Erreur MyTek : {e}")
    return products

# -----------------------------------------------------------------------------
# Enregistrement dans Supabase PostgreSQL
# -----------------------------------------------------------------------------
async def save_to_database(products: List[Dict]):
    print(f"\n💾 Connexion à Supabase PostgreSQL...")
    conn = await asyncpg.connect(DATABASE_URL)
    
    saved_count = 0
    upsert_query = """
    INSERT INTO products (source, category, title, sku, price, price_raw, url, image, in_stock, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
    ON CONFLICT (source, url) 
    DO UPDATE SET
        price = EXCLUDED.price,
        price_raw = EXCLUDED.price_raw,
        in_stock = EXCLUDED.in_stock,
        image = COALESCE(EXCLUDED.image, products.image),
        updated_at = NOW();
    """

    for p in products:
        try:
            await conn.execute(
                upsert_query,
                p["source"], p["category"], p["title"], p["sku"],
                p["price"], p["price_raw"], p["url"], p["image"], p["in_stock"]
            )
            saved_count += 1
        except Exception as e:
            print(f"Erreur insertion {p['title']} : {e}")

    await conn.close()
    print(f"✅ {saved_count} produits enregistrés / mis à jour dans Supabase avec succès !")

async def main():
    print("🚀 Démarrage du Crawler d'Indexation PrixTN...\n")
    all_products = []
    
    async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
        # Crawl des catalogues
        for source, cat, url in CATALOG_TARGETS:
            print(f"[{source}] Indexation de {url}...")
            items = await crawl_url(client, source, cat, url)
            all_products.extend(items)
            print(f" -> {len(items)} articles collectés.")
            await asyncio.sleep(0.5)

        # Crawl MyTek
        print(f"[MyTek] Indexation du catalogue High-Tech...")
        mytek_items = await crawl_mytek(client)
        all_products.extend(mytek_items)
        print(f" -> {len(mytek_items)} articles MyTek collectés.")

    print(f"\n📦 Total collecté : {len(all_products)} produits.")
    if all_products:
        await save_to_database(all_products)

if __name__ == "__main__":
    asyncio.run(main())
