# PrixTN — Comparateur de prix tunisien 🇹🇳

Méta-moteur de recherche de prix en temps réel pour le marché tunisien
(inspiré de primini.tn). L'utilisateur saisit un produit, le backend interroge
simultanément les boutiques en ligne et renvoie les offres triées par prix.

## Boutiques couvertes

### httpx — fonctionnent de partout (12 boutiques)

| Boutique | Secteur | Plateforme | Statut |
|---|---|---|---|
| **Mytek** | High-tech | **API GraphQL** (OpenSearch) | ✅ testé — voir note ci-dessous |
| Tunisianet | High-tech | PrestaShop | ✅ testé |
| Spacenet | High-tech | PrestaShop | ✅ testé |
| TunisiaTech | High-tech / Électroménager | PrestaShop | ✅ testé |
| Wiki.tn | High-tech / Électroménager | WooCommerce/Bricks | ✅ testé |
| Darty Tunisie | Électroménager | PrestaShop | ✅ testé |
| Technopro | High-tech | PrestaShop | ✅ testé |
| SBS Informatique | Gaming / PC | PrestaShop | ✅ testé |
| MegaPC | Gaming / PC | Next.js (SSR) | ✅ testé |
| ParaExpert | Parapharmacie | WooCommerce | ✅ testé |
| MaParaTunisie | Parapharmacie | WooCommerce/Flatsome | ✅ testé |
| MyCare | Parapharmacie | PrestaShop | ✅ testé |

> **💡 Mytek sans navigateur.** Les pages HTML de Mytek sont protégées par
> Cloudflare, mais son **API GraphQL publique** (`/graphql`, moteur
> OpenSearch) ne l'est pas : le scraper l'interroge directement en httpx
> (technique validée par [mytek-radar](https://github.com/mohamedZ79/mytek-radar)).
> Résultat : Mytek fonctionne de partout, sans Playwright ni IP tunisienne.

### Playwright — IP tunisienne requise (10 boutiques)

| Boutique | Secteur | Statut |
|---|---|---|
| Sangour | Maison / Entretien | 🧪 prêt — sélecteurs **validés** (WooCommerce + Woodmart, d'après [Sangoor-radar](https://github.com/mohamedZ79/Sangoor-radar)) |
| Wamia | Marketplace | 🧪 prêt — Cloudflare Turnstile |
| T-Discount | High-tech / Électro | 🧪 prêt — Cloudflare (403 en httpx) |
| Scoop | High-tech | 🧪 prêt — Cloudflare (403 en httpx) |
| Graiet | Électroménager | 🧪 prêt — Cloudflare (403 en httpx) |
| Maalej Audio | Électroménager | 🧪 prêt — Cloudflare (403 en httpx) |
| Affariyet | High-tech / Maison | 🧪 prêt — Cloudflare (403 en httpx) |
| Drest | Beauté / Électro | 🧪 prêt — résultats rendus en JS |
| Bricorama | Bricolage | ⏳ injoignable depuis l'étranger — à tester depuis la Tunisie |
| Electro Tounes | Électroménager | ⏳ injoignable depuis l'étranger — à tester depuis la Tunisie |

> **⚠️ La règle d'or Cloudflare.** Les 10 sites Playwright sont derrière un
> challenge **Cloudflare Turnstile** qui ne se résout **pas depuis une IP de
> datacenter** (Render, Railway, GitHub Actions...). Depuis une **IP
> résidentielle tunisienne**, le challenge se résout tout seul en quelques
> secondes avec le vrai Chromium embarqué. Conséquence : hébergez le backend
> en Tunisie (VPS/local) ou passez par un proxy résidentiel pour ces sources.
> Les 12 scrapers httpx, eux, fonctionnent de partout.

### Sites écartés (diagnostic août 2026)

- **Batam** : domaine mort (en vente)
- **Fi-Dar, Phyto.tn, Santé Parapharmacie, Paraforce** : domaines inexistants (NXDOMAIN)
- **PhytoShop.tn** : prix absents du HTML (chargés en JS) — à ajouter plus tard en Playwright
- **Best Buy Tunisie** : application JS sans recherche server-side
- **Jumia** : exclu volontairement

## Lancement local

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# → http://localhost:8000
```

Test CLI des scrapers seuls :

```bash
python scrapers.py "ventilateur orient"          # les 12 boutiques httpx (dont Mytek)
python scrapers_browser.py "samsung s23"         # les 10 boutiques Playwright
python scrapers_browser.py "samsung s23" sangour # une seule boutique navigateur
```

### Activer les scrapers navigateur (Sangour, Wamia…)

```bash
pip install playwright
playwright install chromium          # build complet (le "headless shell" est détecté)
playwright install-deps chromium     # libs système (Linux)

# Depuis une IP tunisienne :
ENABLE_BROWSER_SCRAPERS=1 uvicorn main:app --port 8000

# Sur un serveur sans écran, le mode fenêtré sous X virtuel passe mieux le challenge :
HEADLESS=0 ENABLE_BROWSER_SCRAPERS=1 xvfb-run -a uvicorn main:app --port 8000
```

Les scrapers navigateur partagent **un seul Chromium** (un onglet par boutique,
en parallèle), masquent les empreintes d'automatisation (`navigator.webdriver`,
plugins, locale fr-TN, fuseau Africa/Tunis) et détectent automatiquement la
plateforme de chaque site (sélecteurs PrestaShop / WooCommerce / Woodmart /
Magento / OpenCart essayés dans l'ordre — le markup des boutiques non
vérifiables depuis l'étranger sera confirmé au premier run tunisien).

## Architecture

```
index.html (front) ──▶ FastAPI /api/search?q=...
                           ├─ cache mémoire 15 min (→ Redis en prod)
                           └─ asyncio.gather ─▶ scrapers.py (12 boutiques httpx en parallèle,
                              │                    timeout 30 s/boutique, erreurs isolées)
                              └─ scrapers_browser.py (10 boutiques Cloudflare, 1 Chromium,
                                                      activé via ENABLE_BROWSER_SCRAPERS=1)
```

- **Parsing prix TND** : `9,900 DT` → `9.9`, `1 299,000 TND` → `1299.0`, `131.370` → `131.37`
- **Fuzzy matching** : rapidfuzz (`token_set_ratio` + `partial_ratio`, seuil 45/100)
  **+ couverture des tokens** : au moins 50 % des mots de la requête doivent
  apparaître dans le titre (évite les faux positifs type « stylo gel » pour
  « cerave gel moussant »)
- **Résilience** : distinction « 0 résultat » vs « structure HTML cassée » (ScraperError)
- **CI** : GitHub Actions teste tous les scrapers toutes les 6 h

## Ajouter une boutique

1. Identifier la plateforme (PrestaShop → `/recherche?controller=search&s=`,
   WooCommerce → `/?s=&post_type=product`).
2. Écrire `async def scrape_x(query, client) -> list[ProductOffer]` dans `scrapers.py`
   (copier un scraper existant et adapter 4 sélecteurs CSS).
3. L'enregistrer dans le dict `SCRAPERS`. C'est tout — l'API, le cache,
   le tri et la CI l'incluent automatiquement.

## Déploiement

- **Backend** : Render / Railway / VPS (build : `pip install -r requirements.txt`,
  start : `uvicorn main:app --host 0.0.0.0 --port $PORT`)
- **Frontend** : `index.html` est servi par l'API ; pour Netlify, changer
  `API_URL` dans `index.html` vers l'URL publique de l'API.
