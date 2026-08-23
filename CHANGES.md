# Journal des modifications — Audit PrixTN (août 2026)

Implémentation complète des recommandations du rapport d'audit
(27 constats), chaque correctif étant **vérifié par des tests réels**
avant livraison.

## Corrections critiques

| ID | Fichier | Correctif | Vérification |
|----|---------|-----------|--------------|
| F-01 | scrapers_browser.py | Import `match_score` → `is_strict_match` (signature + tuple de retour) | `import scrapers_browser` OK (ImportError avant) |
| F-02 | scrapers_browser.py | Shim `scrape_mytek` (KeyError `SITES["mytek"]`) supprimé | Le module s'importe et le CLI tourne |
| F-03 | crawler.py | `verify=True` + timeouts explicites partout | grep : plus aucun `verify=False` |
| F-12 | scrapers.py + crawler.py | Parser de prix unifié — **et corrigé** : la copie du crawler renvoyait **299.0 pour "1 299,000 TND"** (faux prix stockés en production !) et celle de scrapers renvoyait 20.0 sur "Economisez 20 DT ... 45,900 DT" | 15 formats de prix testés (pytest) |

## Corrections hautes

| ID | Correctif | Vérification |
|----|-----------|--------------|
| F-04/F-05 | `match_score` **réel** (pg_trgm `word_similarity`, repli rapidfuzz) et champ `cached` supprimé → `"source": "database"` | E2E : scores 71.4–100 selon pertinence |
| F-06 | Wildcards LIKE échappés (`escape_like`) + recherche limitée à `title`/`sku` (plus de `category`/`source`) | E2E : requête `%%` → 0 résultat (injection bloquée) |
| F-07 | `schema.sql` complet : table `products`, index trigram, `price_history` + trigger, migration Mytek | Chargé sur PostgreSQL 16 réel, trigger vérifié |
| F-08 | CI : ensembles dérivés de `SHOP_CATEGORY` (plus de 7 boutiques fantômes) + **import smoke test** + pytest | `test_every_scraper_has_category` dans la suite |
| F-09 | README réécrit : architecture v3 réelle, matrice de couverture, solution IP tunisienne | — |
| F-10 | CORS liste blanche (`ALLOWED_ORIGINS`), GET uniquement, sans credentials | — |
| F-11 | `except: pass` → `log.exception` + **code de sortie 1 si zéro produit** (alerte email CI) | Crawler journalise, la CI échoue sur crawl vide |

## Corrections moyennes/basses

- **F-13** : hooks `on_event` dépréciés → `lifespan` (asynccontextmanager).
- **F-14** : pagination `limit`/`offset` + modèles Pydantic (`/docs` documenté).
- **F-15** : index GIN `gin_trgm_ops` + recherche trigram indexée (avec détection automatique et **repli Python si `pg_trgm` absent** — repli testé).
- **F-16** : heuristique de rupture de stock (`rupture`, `épuisé`, `out of stock`, classes CSS).
- **F-17/F-18** : crawl par lots de 3 pages concurrentes + arrêt à la première série vide.
- **F-19** : upserts dans UNE transaction (tout ou rien) + mise à jour titre/catégorie/sku.
- **F-20** : URLs d'images validées `http(s)` et échappées (`safeImg`) — vector XSS fermé.
- **F-21** : `API = '/api/search'` relatif (le proxy Netlify existant devient le seul point de config).
- **F-22** : source `"Mytek"` partout + migration SQL.
- **F-23** : `/health` vérifie la base (503 dégradé) + expose `trigram`.
- **F-24** : GUIDE-GITHUB.md supprimé (lien zip mort, arbre obsolète).
- **F-25** : LICENSE MIT ajoutée.
- **F-26** : meta description, Open Graph, favicon SVG inline, `aria-label`, focus visible.
- **F-27** : logging structuré avec niveaux (crawler + API).

## Nouvelles fonctionnalités

- **`GET /api/suggest`** — autocomplétion (pg_trgm ou repli rapidfuzz), branchée sur le front avec debounce 250 ms et navigation clavier.
- **`GET /api/stats`** — fraîcheur du catalogue par boutique + lignes périmées (surveillance).
- **Limitation de débit** — 30 req/min par IP (sliding window sans dépendance), 429 + `Retry-After`. **Vérifié E2E : 35e requête → 429.**
- **Âge du prix** — `updated_at` + `price_age_hours` dans la réponse, badges « vu il y a N h » et badge « prix ancien » > 7 jours côté front.
- **Front** : tri pertinence/prix, filtres par boutique (chips), mode sombre automatique, placeholder d'image.
- **Historique des prix** — table `price_history` + trigger : chaque changement de prix est journalisé (base des futures alertes). **Vérifié E2E : re-upsert avec prix modifié → 1 entrée d'historique.**
- **Tolérance aux fautes** — `samsng` (sans le u) trouve les produits Samsung. **Vérifié E2E.**
- **Dockerfile**, **`.env.example`**, **requirements.txt épinglés**.

## Sites bloqués (Cloudflare) — diagnostic et solution

Diagnostic vérifié en août 2026 depuis une IP datacenter :

| Site | httpx | curl_cffi (TLS Chrome) | API JSON / sitemap | Playwright | Verdict |
|---|---|---|---|---|---|
| sangour, wamia, tdiscount, scoop, graiet, maalej, affariyet | 403 | 403 | 403 | bloqué (« Un instant… ») | IP tunisienne requise |
| bricorama, electrotounes | connexion refusée | — | — | — | geo-blocage, IP tunisienne |
| **drest** | 200* | 200 | **200 JSON (27 082 produits)** | n/a | **DÉBLOQUÉ via API Store** |

\* avec User-Agent navigateur.

**Solutions livrées :**
1. **Drest débloqué** : nouveau scraper `scrape_drest` (API Store WooCommerce,
   JSON public, recherche live intégrée) + ingestion nocturne de tout le
   catalogue (`crawl_drest`). Prix en unités mineures convertis
   (`currency_minor_unit`). **Vérifié : 23 offres live pour « gel ».**
2. **`PROXY_URL`** (nouveau, partout) : proxy résidentiel tunisien branché
   sur le crawler (httpx **et** repli curl_cffi) et sur Playwright. Depuis
   une IP résidentielle tunisienne, le challenge Cloudflare se résout seul —
   c'est la seule solution pour les 9 sites restants, et le code est prêt :
   `PROXY_URL=http://… python crawler.py` ou secret CI `PROXY_URL`.
3. **Couche furtive curl_cffi** dans le crawler : repli automatique sur
   empreinte TLS Chrome pour tout 403 (utile derrière proxy, sans navigateur).

## Bugs supplémentaires découverts pendant la vérification (et corrigés)

1. **Parser de prix du crawler faux sur « 1 299,000 TND » → 299.0** — le
   format exact utilisé par Tunisianet : des prix faux étaient stockés.
2. **Scrapers yeswikam, darty, sbs à zéro résultat** : leurs thèmes mettent
   la marque (ou rien) dans le heading et le vrai titre dans une ancre
   simple. Nouvel extracteur générique `extract_title_link` (titre = ancre
   produit la plus descriptive, liens marque/catégorie/UI exclus) + 5 tests
   de régression sur structures HTML réelles. **Résultat : 9/9 scrapers
   OK** (yeswikam 24, darty 6, sbs 12 offres — et spacenet passe de 4 à 8).
3. **GraphQL Mytek sans en-tête `Accept: application/json`** → corps HTML
   non-JSON : corrigé dans `post_json` (205 produits récupérés au test).

## Suite de vérification (tout passe)

```
pytest -q                        → 28 passed
import scrapers, scrapers_browser, crawler, main → OK
9/9 scrapers httpx live          → OK (depuis IP datacenter)
E2E PostgreSQL 16 + pg_trgm      → schema, crawl réel (SpaceNet/Tunisianet/
                                   Mytek/Drest), upserts transactionnels,
                                   trigger price_history
API live                         → /health, search (+fautes, +injection
                                   bloquée, +pagination), /api/suggest,
                                   /api/stats, rate limit 429, front servi
Repli sans pg_trgm               → 91 lignes → 85 résultats scorés Python
```

## Déploiement

1. Exécuter `schema.sql` sur Supabase (idempotent).
2. Configurer `DATABASE_URL` (Render) ; optionnel : `PROXY_URL`,
   `ALLOWED_ORIGINS`, `DATABASE_SSL=disable` (Postgres local).
3. Secret CI `PROXY_URL` (optionnel) pour débloquer les boutiques
   Cloudflare depuis le crawler nocturne.
