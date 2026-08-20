# 🚀 Guide : publier PrixTN sur GitHub

## Étape 0 — Récupérer le code

Téléchargez [prixtn.zip](avfs:///tasklet/agent/home/prixtn.zip) et décompressez-le.
Vous obtenez :

```
prixtn/
├── .github/workflows/scraper-health.yml   # CI : test des scrapers toutes les 6 h
├── .gitignore
├── README.md
├── requirements.txt                        # dépendances Python
├── scrapers.py                             # 6 scrapers async + parsing prix + fuzzy matching
├── scrapers_browser.py                     # Mytek via Playwright (anti-Cloudflare)
├── main.py                                 # API FastAPI + cache 15 min
└── index.html                              # frontend
```

## Étape 1 — Créer le dépôt sur GitHub

1. Connectez-vous sur [github.com](https://github.com) → bouton **New** (ou https://github.com/new)
2. **Repository name** : `prixtn` (ou `price-comparator-tn`)
3. Visibilité : **Private** recommandé (vos sélecteurs CSS restent discrets)
4. ⚠️ **Ne cochez PAS** « Add a README » (le projet en a déjà un)
5. **Create repository**

## Étape 2 — Envoyer le code (2 options)

### Option A — Ligne de commande (recommandée)

```bash
cd prixtn                # le dossier décompressé

git init -b main
git add -A
git commit -m "PrixTN : comparateur de prix tunisien — 6 scrapers live, API FastAPI, front, CI"

# Remplacez VOTRE-PSEUDO par votre nom d'utilisateur GitHub :
git remote add origin https://github.com/VOTRE-PSEUDO/prixtn.git
git push -u origin main
```

Git vous demandera de vous authentifier : utilisez un **Personal Access Token**
(Settings → Developer settings → Personal access tokens → Tokens (classic) →
cochez `repo`) comme mot de passe. Ou installez [GitHub CLI](https://cli.github.com/)
(`gh auth login` puis les mêmes commandes).

### Option B — Sans rien installer (interface web)

1. Sur la page du dépôt vide, cliquez **uploading an existing file**
2. Glissez-déposez **tous les fichiers** du dossier (y compris `.github` et `.gitignore`)
3. **Commit changes**

> Astuce : pour glisser le dossier `.github` (caché), affichez les fichiers cachés
> (Cmd+Shift+. sur Mac, Ctrl+H sur Linux).

## Étape 3 — Vérifier la CI (GitHub Actions)

1. Onglet **Actions** de votre dépôt → le workflow *Scraper Health Check* apparaît
2. Cliquez **Run workflow** pour un premier test immédiat
3. ✅ Vert = les 6 scrapers répondent. ❌ Rouge = une boutique a changé son HTML
   (vous recevez un e-mail GitHub automatiquement)

> Note : Sangour, Technopro, ElectroTounes et Sotunas semblent géo-bloqués —
> les runners GitHub (USA/Europe) pourraient ne pas y accéder non plus.
> Les scrapers actuels (Tunisianet, Spacenet, etc.) passent sans problème.

## Étape 4 — Déployer

### Backend API → [Render](https://render.com) (gratuit)

1. **New → Web Service** → connectez votre dépôt GitHub
2. Build Command : `pip install -r requirements.txt`
3. Start Command : `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Chaque `git push` redéploie automatiquement ✅

### Frontend → [Netlify](https://netlify.com) (gratuit)

1. Dans `index.html`, remplacez `const API_URL = ""` par
   `const API_URL = "https://votre-app.onrender.com"`
2. **Add new site → Deploy manually** → glissez `index.html`
3. (CORS est déjà ouvert côté API via `allow_origins=["*"]`)

## Étape 5 — Étendre la couverture marché

Pour ajouter une boutique (ex: Wamia via Playwright, ou un site PrestaShop
accessible uniquement depuis la Tunisie) :

1. Copiez un scraper existant dans `scrapers.py`, adaptez les 4-5 sélecteurs CSS
2. Ajoutez-le au dictionnaire `SCRAPERS`
3. `git push` → la CI le teste automatiquement

Bon build ! 🛒
