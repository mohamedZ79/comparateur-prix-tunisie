# Guide de déploiement PrixTN (v3)

Déploiement complet en ~15 minutes : base Supabase, API Render, front
Netlify, crawler nocturne GitHub Actions.

---

## 1. Base de données — Supabase (5 min)

1. Créez un projet sur [supabase.com](https://supabase.com) (plan gratuit
   suffisant).
2. **SQL Editor** → collez le contenu de `schema.sql` → **Run**.
   - Crée la table `products` (clé unique `source, url`)
   - Active `pg_trgm` + index trigram (recherche tolérante aux fautes)
   - Crée `price_history` + trigger (historique automatique des prix)
   - Applique la migration `MyTek → Mytek`
   - Idempotent : peut être rejoué sans danger.
3. **Settings → Database → Connection string → URI** (choisir la chaine
   du **pooler IPv4**, `db.<ref>.supabase.co` si vous êtes sur un PaaS
   sans IPv6) → conservez-la pour l'étape 2.

> Vérification : la requête `SELECT word_similarity('a', 'a');` doit
> renvoyer `1`. L'API affiche ensuite `"trigram": true` sur `/health`.

## 2. API — Render (5 min)

1. [render.com](https://render.com) → **New → Web Service** → connectez
   le dépôt GitHub.
2. Configuration :
   - **Environment** : `Python 3`
   - **Build Command** : `pip install -r requirements.txt`
   - **Start Command** : `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. **Environment variables** :

   | Variable | Valeur | Obligatoire |
   |---|---|---|
   | `DATABASE_URL` | `postgresql://postgres:[password]@aws-0-eu-...pooler.supabase.com:5432/postgres` | ✅ |
   | `ALLOWED_ORIGINS` | `https://votre-site.netlify.app,https://votre-custom-domain.tn` | ✅ |
   | `DATABASE_SSL` | (défaut `require`, convient à Supabase ; `disable` uniquement pour un Postgres local) | — |

4. Déployez → vérifiez `https://votre-app.onrender.com/health` :
   ```json
   {"status": "ok", "db": "up", "trigram": true}
   ```

## 3. Frontend — Netlify (2 min)

Le front est déjà configuré : il appelle `/api/search` en **chemin
relatif**, et `netlify.toml` proxifie `/api/*` vers Render. Il suffit de :

1. [netlify.com](https://netlify.com) → **Add new site → Deploy manually**
   → glissez le dossier du dépôt (ou connectez le repo GitHub).
2. Dans **Site settings → Build & deploy** : le `netlify.toml` du dépôt
   fait le reste. **Modifiez l'URL Render dans `netlify.toml`** si
   différente de `api-comparateur-tunisie.onrender.com`.
3. Ajoutez l'URL Netlify finale dans `ALLOWED_ORIGINS` côté Render.

> Aucune URL codée en dur dans le HTML (audit F-21) : le proxy Netlify
> est le seul point de configuration.

## 4. Crawler nocturne — GitHub Actions (3 min)

Le workflow `.github/workflows/daily-crawler.yml` tourne chaque nuit à
03h00 UTC automatiquement. Il ne manque que le secret :

1. Repo GitHub → **Settings → Secrets and variables → Actions**.
2. **New repository secret** :

   | Secret | Valeur |
   |---|---|
   | `DATABASE_URL` | la même chaîne que Render |
   | `PROXY_URL` | *(optionnel)* `http://user:pass@proxy-tunisien:port` |

3. **Actions → Daily Catalog Crawler → Run workflow** pour un premier
   test immédiat. Le job échoue volontairement (email d'alerte) s'il
   collecte zéro produit (audit F-11).

Après le premier crawl, `https://votre-app.onrender.com/api/stats`
confirme les volumes par boutique :
```json
{"sources": [{"source": "Mytek", "products": 440}, ...],
 "stale_rows": 0, "trigram": true}
```

## 5. Débloquer Sangour, Wamia, T-Discount… (optionnel)

Diagnostic vérifié (août 2026) : ces 7 boutiques Cloudflare + 2
geo-bloquées refusent **toutes** les requêtes venant d'une IP datacenter
(pages, API JSON, sitemaps, flux). Deux solutions, déjà câblées dans le
code :

### Option A — Proxy résidentiel tunisien (recommandé)

1. Souscrivez un proxy résidentiel avec sortie **Tunisie** (ex. services
   type IPRoyal/Bright Data/Webshare, quelques dollars/mois).
2. Ajoutez le secret CI `PROXY_URL` (ci-dessus) et la même variable
   d'environnement côté Render si vous lancez le crawler localement.
3. Le crawler (httpx + repli curl_cffi) et les scrapers Playwright
   l'utilisent automatiquement — aucune modification de code.

### Option B — Machine/VPS en Tunisie

Lancez le nightly depuis une IP tunisienne :
```bash
DATABASE_URL=postgresql://... python crawler.py        # chaque nuit via cron
# ou avec Docker :
docker run --rm -e DATABASE_URL=... -e PROXY_URL=... prixtn python crawler.py
```

### Test ponctuel avec Playwright (dépannage)

```bash
pip install playwright && playwright install chromium
PROXY_URL=http://user:pass@proxy:port python scrapers_browser.py "ventilateur" sangour
```

## 6. Vérifications finales

| Vérification | Attendu |
|---|---|
| `GET /health` | `{"status": "ok", "db": "up", "trigram": true}` |
| `GET /api/search?q=samsung` | offres triées, `match_score` réel, `price_age_hours` |
| `GET /api/search?q=samsng` | tolérance aux fautes : résultats Samsung malgré la faute |
| `GET /api/stats` | volumes par boutique, `stale_rows` proche de 0 après un crawl |
| Actions GitHub « Scraper Health Check » | 9/9 scrapers verts toutes les 6 h |
| Actions GitHub « Daily Catalog Crawler » | vert ; rouge = email d'alerte |

## Résumé des secrets

| Endroit | Secret | Usage |
|---|---|---|
| Render | `DATABASE_URL`, `ALLOWED_ORIGINS` | API |
| GitHub Actions | `DATABASE_URL`, `PROXY_URL` (optionnel) | Crawler nocturne |
| Netlify | aucun | Le front est statique, proxy inclus |

Bon déploiement ! 🚀
