# PrixTN — Comparateur de prix tunisien 🇹🇳

Méta-moteur de comparaison de prix pour le marché tunisien. Un **crawler
nocturne** collecte le catalogue des boutiques dans **PostgreSQL (Supabase)**,
et une **API FastAPI** sert les offres triées et scorées — avec tolérance
aux fautes de frappe (`pg_trgm`) et prix vérifiés chaque nuit.

## Architecture (v3)

```
index.html (front autonome)
   │  GET /api/search?q=...            (chemin relatif ; proxy Netlify)
   ▼
FastAPI (main.py) ──► PostgreSQL / Supabase
   │   • recherche trigram (tolérance aux fautes)          ◄── schema.sql
   │   • score de pertinence réel (word_similarity)
   │   • pagination, /api/suggest, /api/stats, /health réel
   ▲
   │  upserts transactionnels, chaque nuit à 03h00 (GitHub Actions)
   │
crawler.py ──► SpaceNet, Tunisianet, Yeswikam, MyCare (rayons PrestaShop)
   │           Sangour (rayons WooCommerce — IP tunisienne requise)
   │           Mytek (API GraphQL publique)
   │           Drest (API Store WooCommerce — 27 000+ produits en JSON) ⭐
   └─► scrapers.py : logique partagée (parser prix, matching strict)
```

## Couverture des boutiques

### Sources du catalogue nocturne

| Boutique | Secteur | Accès | Statut |
|---|---|---|---|
| **Drest** | Parapharmacie & Beauté | **API Store WooCommerce** (JSON public) | ✅ testé — 27 000+ produits |
| **Wamia** | Marketplace (15 rayons) | **API GraphQL Magento** (publique) | ✅ testé — ~23 000 produits |
| Mytek | High-tech | API GraphQL publique | ✅ testé |
| Tunisianet | High-tech / Électro | Rayons PrestaShop | ✅ testé |
| SpaceNet | High-tech / Électro | Rayons PrestaShop | ✅ testé |
| Yeswikam | Parapharmacie | Rayons PrestaShop | ✅ testé |
| MyCare | Parapharmacie | Rayons PrestaShop | ✅ testé |
| Sangour | Maison / Entretien | Rayons WooCommerce | ⚠️ IP tunisienne requise (voir plus bas) |

### Scrapers httpx (recherche live / CLI)

Tunisianet, SpaceNet, Mytek (GraphQL), **Wamia (GraphQL)**, Darty,
Technopro, SBS, Yeswikam, MyCare, **Drest (Store API)** — testés par la
CI toutes les 6 heures.

### Sites protégés par Cloudflare — la solution

Diagnostic vérifié en août 2026 depuis une IP datacenter, en testant
**toutes** les voies d'accès : httpx, curl_cffi (empreintes TLS Chrome /
Safari / Edge / Firefox), API JSON (`/wp-json`, `/graphql`, `/rest`),
sitemaps, flux RSS, services externes (Jina, CORS proxies), et même un
vrai Chromium avec 60 s de patience + clic sur le widget Turnstile.

**Verdict : le challenge est décidé par l'IP, pas par le code.** Les sites
restants — **sangour.tn, tdiscount.tn, scoop.com.tn, graiet.tn,
maalejaudio.tn, affariyet.com** (+ geo-bloqués **bricorama.tn** et
**electrotounes.tn**) — laissent TOUT en 403 depuis une IP datacenter.
Mais **deux des sites "impossibles" ont été débloqués par leur API
interne** (voir plus bas) — le même axe de recherche reste ouvert pour
les autres.

**La solution est l'IP, pas le code** — et le code est prêt :

1. **`PROXY_URL`** (nouveau) : définissez un proxy résidentiel tunisien
   dans `.env` ou le secret CI du même nom. Le crawler (httpx + repli
   curl_cffi) et les scrapers Playwright l'utilisent automatiquement :
   ```bash
   PROXY_URL=http://user:pass@tn-proxy:8080 python crawler.py
   ```
2. **VPS tunisien ou machine locale** : lancez le nightly crawler depuis
   une IP tunisienne (cron local ou GitHub Actions + secret `PROXY_URL`).
3. **Playwright** (dépannage ponctuel) : `scrapers_browser.py` est réparé
   (l'import cassé `match_score` — audit F-01 — et le shim Mytek — F-02)
   et respecte lui aussi `PROXY_URL`.

> ⭐ **Drest débloqué** : son API Store WooCommerce (`/wp-json/wc/store/products`)
> répond en **JSON sans aucun challenge** — 27 000 produits accessibles depuis
> n'importe quelle IP avec un simple User-Agent navigateur.
>
> ⭐ **Wamia débloqué** : son API GraphQL Magento (`/graphql`) échappe
> complètement au challenge Cloudflare qui protège le HTML — recherche live,
> prix, stock et images, ~23 000 produits sur 15 catégories, depuis
> n'importe quelle IP. Deux sites « impossibles » sur huit : les APIs
> internes (Magento GraphQL, WooCommerce Store) sont l'axe d'attaque
> privilégié pour les boutiques restantes.

## Lancement local

```bash
pip install -r requirements.txt
cp .env.example .env            # renseignez DATABASE_URL
psql $DATABASE_URL -f schema.sql   # ou éditeur SQL Supabase
uvicorn main:app --reload --port 8000
# → http://localhost:8000
```

Test CLI des scrapers seuls :

```bash
python scrapers.py "ventilateur orient"       # 9 boutiques httpx (dont Drest)
python scrapers_browser.py "samsung s23"      # boutiques Cloudflare (IP TN)
python crawler.py                              # ingestion nocturne complète
pytest -q                                      # tests unitaires
```

## API

| Endpoint | Description |
|---|---|
| `GET /api/search?q=…&limit=40&offset=0` | Recherche paginée, tri pertinence puis prix, tolérance aux fautes |
| `GET /api/suggest?q=…` | Autocomplétion (8 suggestions) |
| `GET /api/stats` | Fraîcheur du catalogue par boutique (alerte données périmées) |
| `GET /health` | Santé réelle (vérifie la base, 503 si dégradé) |

Réponse `search` : chaque offre inclut `price`, `match_score` **réel**
(0-100), `updated_at` et `price_age_hours` (âge du prix). Plus de score
bidon à 100 ni de flag `cached` fictif (audit F-04/F-05).

Sans `pg_trgm`, l'API retombe automatiquement sur la recherche par tokens
avec scoring Python (rapidfuzz) — comportement historique, mais sécurisé
(wildcards LIKE échappés — F-06).

## Base de données

`schema.sql` versionne le contrat complet : table `products` (clé unique
`source, url`), index trigram, table `price_history` + trigger (chaque
changement de prix est historisé automatiquement), et la migration
`MyTek → Mytek`. Toutes les instructions sont idempotentes.

## Déploiement

- **Backend** : Render / Railway / VPS / Docker
  (`docker build -t prixtn . && docker run -p 8000:8000 prixtn`)
- **Frontend** : servi par l'API ; sur Netlify, `netlify.toml` proxifie
  déjà `/api/*` vers Render (le front appelle `/api/search` en relatif).
- **Secrets CI** : `DATABASE_URL` (crawler nocturne), `PROXY_URL`
  (optionnel — proxy résidentiel tunisien).

## Tests & CI

- `pytest -q` : parser de prix TND (15 formats réels), matching strict,
  échappement des wildcards, cohérence du registre boutiques.
- **Scraper Health Check** (toutes les 6 h) : import smoke test de tous
  les modules + tests unitaires + smoke test par catégorie avec ensembles
  **dérivés du registre** (plus de boutiques fantômes — audit F-08).
- **Daily Catalog Crawler** (03h00) : échoue explicitement si zéro produit
  collecté → email d'alerte GitHub (plus de silence — audit F-11).

## Licence

MIT — voir [LICENSE](LICENSE).
