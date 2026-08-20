"""
Crawler Haute Capacité PrixTN - Couverture Complète du Marché Tunisien.
Aspire des milliers de produits et met à jour Supabase automatiquement.
"""
import asyncio
import os
import re
import unicodedata
from typing import List, Dict, Optional
import httpx
from bs4 import BeautifulSoup
import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
}

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

# -----------------------------------------------------------------------------
# TOUS LES RAYONS DU MARCHÉ TUNISIEN
# -----------------------------------------------------------------------------
CATALOG_TARGETS = [
    # 1. SPACENET (High-Tech, Électro, PC, TV)
    ("SpaceNet", "High-Tech", "https://spacenet.tn/377-smartphone-tunisie?page={page}", 5),
    ("SpaceNet", "High-Tech", "https://spacenet.tn/14-pc-portable?page={page}", 5),
    ("SpaceNet", "Électroménager", "https://spacenet.tn/46-electromenager?page={page}", 5),
    ("SpaceNet", "High-Tech", "https://spacenet.tn/recherche?s=samsung&page={page}", 4),
    ("SpaceNet", "High-Tech", "https://spacenet.tn/recherche?s=iphone&page={page}", 3),

    # 2. TUNISIANET (Smartphones, PC, TV, Électro, Soins)
    ("Tunisianet", "High-Tech", "https://www.tunisianet.com.tn/377-smartphone-tunisie?page={page}", 5),
    ("Tunisianet", "High-Tech", "https://www.tunisianet.com.tn/301-pc-portable-tunisie?page={page}", 5),
    ("Tunisianet", "Électroménager", "https://www.tunisianet.com.tn/379-televiseur?page={page}", 4),
    ("Tunisianet", "High-Tech", "https://www.tunisianet.com.tn/recherche?s=samsung+galaxy&page={page}", 4),
    ("Tunisianet", "Électroménager", "https://www.tunisianet.com.tn/recherche?s=moulinex&page={page}", 3),
    ("Tunisianet", "Électroménager", "https://www.tunisianet.com.tn/recherche?s=climatiseur&page={page}", 3),

    # 3. YESWIKAM & PARAPHARMACIES (SVR, CeraVe, Bioderma, La Roche Posay...)
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/2-accueil?page={page}", 8),
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/recherche?s=svr&page={page}", 4),
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/recherche?s=bioderma&page={page}", 3),
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/recherche?s=cerave&page={page}", 3),
    ("Yeswikam", "Parapharmacie", "https://www.yeswikam.com/recherche?s=la+roche+posay&page={page}", 3),
    ("MyCare", "Parapharmacie", "https://mycare.tn/recherche?s=soin&page={page}", 4),
    ("MyCare", "Parapharmacie", "https://mycare.tn/recherche?s=gel&page={page}", 4),

    # 4. DARTY & ÉLECTROMÉNAGER
    ("Darty TN", "Électroménager", "https://darty.tn/recherche?s=moulinex&page={page}", 4),
    ("Darty TN", "Électroménager", "https://darty.tn/recherche?s=philips&page={page}", 4),
    ("Darty TN", "Électroménager", "https://darty.tn/recherche?s=tefal&page={page}", 3),
    ("Darty TN", "Électroménager", "https://darty.tn/recherche?s=electromenager&page={page}", 4),
]

# 5. SANGOUR (Entretien, Judy, Détergents, Cuisine Tramontina, Tefal)
SANGOUR_TARGETS = [
    ("Sangour", "Maison & Entretien", "https://sangour.tn/?s=judy"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/?s=tramontina"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/?s=tefal"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/?s=moulinex"),
    ("Sangour", "Maison & Cuisine", "https://sangour.tn/?s=cocotte"),
    ("Sangour", "Maison & Entretien", "https://sangour.tn/?s=detergent"),
    ("Sangour", "Maison & Entretien", "https://sangour.tn/?s=javel"),
    ("Sangour", "Maison & Entretien", "https://sangour.tn/?s=vaisselle"),
]

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

async def crawl_mytek(client: httpx.AsyncClient) -> List[Dict]:
    products = []
    try:
        MYTEK_GRAPHQL = "https://www.mytek.tn/graphql"
        MYTEK_MEDIA = "https://www.mytek.tn/media/catalog/product"
        
        # Aspire tous les rayons High-Tech & Électro de MyTek
        keywords = ["samsung", "iphone", "xiaomi", "pc portable", "tv", "climatiseur", "moulinex", "refrigerateur"]
        for term in keywords:
            query_gql = f"""
            query {{
              opensearchProductSearch(search: "{term}", page: 1, pageSize: 80) {{
                items {{ id sku name price special_price final_price image url }}
              }}
            }}
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
            await asyncio.sleep(0.3)
    except Exception as e:
        print(f"Erreur MyTek : {e}")
    return products

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
        except Exception:
            pass

    await conn.close()
    print(f"🎉 SUCCÈS : {saved_count} produits enregistrés / mis à jour dans votre base Supabase !")

async def main():
    print("🚀 Démarrage du Grand Crawler PrixTN...\n")
    all_products = []
    
    async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
        # 1. Catégories Multi-Pages (SpaceNet, Tunisianet, Yeswikam, Darty)
        for source, cat, base_url, total_pages in CATALOG_TARGETS:
            print(f"[{source}] Indexation de {cat} ({total_pages} pages)...")
            for page in range(1, total_pages + 1):
                url = base_url.format(page=page)
                items = await crawl_page(client, source, cat, url)
                all_products.extend(items)
                await asyncio.sleep(0.2)

        # 2. Sangour Direct (Judy, Tramontina, Tefal, Moulinex...)
        for source, cat, url in SANGOUR_TARGETS:
            print(f"[{source}] Indexation de {url}...")
            items = await crawl_page(client, source, cat, url)
            all_products.extend(items)
            await asyncio.sleep(0.2)

        # 3. MyTek GraphQL (Centaines d'articles)
        print(f"[MyTek] Indexation approfondie du catalogue...")
        mytek_items = await crawl_mytek(client)
        all_products.extend(mytek_items)

    print(f"\n📦 Total collecté : {len(all_products)} produits.")
    if all_products:
        await save_to_database(all_products)

if __name__ == "__main__":
    asyncio.run(main())
