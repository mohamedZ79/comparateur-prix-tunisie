import asyncio
import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
}

async def test_carrefour():
    print("⏳ Recherche de BRE275 sur Carrefour Tunisie en direct...")
    url = "https://www.carrefour.tn/default/search.html"
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, verify=False) as client:
        res = await client.get(url, params={"query": "BRE275"}, timeout=10.0)
        print(f"Status Code Carrefour : {res.status_code}")
        soup = BeautifulSoup(res.text, "html.parser")
        
        # Afficher le titre et prix trouvés
        for item in soup.select(".product-card, .product-item, .product-miniature, li.product, [class*='product']")[:5]:
            title = item.select_one(".product-title, .title, h2, h3, a[title]")
            price = item.select_one(".price, [class*='price'], .amount")
            if title and price:
                print(f"✅ TROUVÉ : {title.get_text(strip=True)} | Prix : {price.get_text(strip=True)}")

asyncio.run(test_carrefour())