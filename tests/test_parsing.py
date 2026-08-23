"""Tests unitaires PrixTN - logique pure (parser prix, matching, secu).

Lancement : pytest -q
"""
import pytest

from scrapers import (parse_tnd_price, is_strict_match, SCRAPERS,
                      SHOP_CATEGORY, extract_title_link)
from main import escape_like, clean_query, build_token_conditions


# ------------------------------------------------------------- parse_tnd_price

@pytest.mark.parametrize("raw, expected", [
    # formats reels rencontres sur les boutiques tunisiennes
    ("9,900 DT", 9.9),
    ("1 299,000 TND", 1299.0),          # crawler bogguait : renvoyait 299.0
    ("3 999,000 DT", 3999.0),
    ("1.299,000 DT", 1299.0),
    ("249DT000", 249.0),                # format Carrefour
    ("131.370", 131.37),                # Mytek/Spacenet, 3 decimales sans devise
    ("45,900 DT", 45.9),
    ("899 DT", 899.0),
    ("1299.000", 1299.0),
    ("1 299.000 DT", 1299.0),
    # bruit promotionnel : le DERNIER montant gagne
    ("Economisez 20 DT et payez 45,900 DT", 45.9),
    # cas limites
    ("gratuit", None),
    ("", None),
    (None, None),
    ("Prix : 25 dt", 25.0),
])
def test_parse_tnd_price(raw, expected):
    assert parse_tnd_price(raw) == expected


# ------------------------------------------------------------- is_strict_match

def test_strict_match_rejects_accessory():
    ok, _ = is_strict_match("samsung s23", "Coque Samsung S23 transparente")
    assert ok is False


def test_strict_match_accepts_model():
    ok, score = is_strict_match("samsung s23",
                                "Samsung Galaxy S23 128 Go Noir")
    assert ok is True
    assert score > 50


def test_strict_match_volume_discrimination():
    ok, _ = is_strict_match("cerave 200ml",
                            "Cerave Gel Moussant 473ml")
    assert ok is False


def test_strict_match_model_token_required():
    # le token modele 's24' doit etre present dans le titre
    ok, _ = is_strict_match("samsung s24", "Samsung Galaxy S23")
    assert ok is False


# ------------------------------------------------------------------ main.py

def test_escape_like():
    assert escape_like("100%") == "100\\%"
    assert escape_like("a_b") == "a\\_b"
    assert escape_like("\\") == "\\\\"


def test_escape_like_blocks_wildcard_injection():
    # une requete composee uniquement de '%' ne doit plus matcher tout
    assert escape_like("%") == "\\%"


def test_clean_query():
    assert clean_query("l'aspir'ateur") == "laspirateur"
    assert clean_query("  Samsung   ") == "Samsung"


def test_build_token_conditions_and_semantics():
    sql = build_token_conditions(2)
    # semantique AND preservee entre tokens, OR a l'interieur (title/sku)
    assert " AND " in sql
    assert sql.count("(title ILIKE") == 2
    assert "$1" in sql and "$2" in sql


# --------------------------------------------------------------- registre CI

def test_every_scraper_has_category():
    """La CI derive ses ensembles de SHOP_CATEGORY (F-08) : toute nouvelle
    boutique doit y etre declaree, sinon le smoke-test CI l'ignore."""
    assert set(SCRAPERS) == set(SHOP_CATEGORY), (
        f"SCRAPERS sans categorie : {set(SCRAPERS) ^ set(SHOP_CATEGORY)}")


# ------------------------------------------------------- extract_title_link

# Structures reelles verifiees sur les sites en aout 2026 :
# - themes classiques : titre dans le heading
CLASSIC_CARD = """
<article class="product-miniature">
  <h2 class="product-title"><a href="/p/123-iphone-15">Apple iPhone 15 128 Go Noir</a></h2>
  <span class="price">3 999,000 TND</span>
</article>
"""

# - theme yeswikam : le heading porte la MARQUE, le vrai titre est dans
#   une ancre simple de la carte (regression de janvier 2026 : le scraper
#   yeswikam ne renvoyait plus rien)
BRAND_CARD = """
<article class="product-miniature">
  <a href="/beaute-et-soins/162-svr-xerial-50-e">Aperçu rapide</a>
  <a href="/marque/13-svr" title="SVR">SVR</a>
  <a href="/beaute-et-soins/162-svr-xerial-50-e">SVR Xerial 50 Extreme Creme Pieds 50ml</a>
  <span class="price">38,700 TND</span>
</article>
"""

# - theme darty/sbs : pas de heading utile, titre dans la derniere ancre
PLAIN_CARD = """
<li class="product-item">
  <a href="/ventil/4206-ventilateur-luxell"><img src="x.jpg"></a>
  <a href="#">Favoris</a>
  <a href="/80-ventil">CHAUFFAGE/VENTIL.</a>
  <a href="/ventil/4206-ventilateur-luxell">VENTILATEUR SUR PIED LUXELL KTF-285 - NOIR</a>
  <span class="price">129,000 TND</span>
</li>
"""


@pytest.mark.parametrize("html, expected_title_part", [
    (CLASSIC_CARD, "iPhone 15"),
    (BRAND_CARD, "Xerial 50 Extreme"),
    (PLAIN_CARD, "LUXELL KTF-285"),
])
def test_extract_title_link(html, expected_title_part):
    from bs4 import BeautifulSoup
    card = BeautifulSoup(html, "html.parser").select_one(
        "article, li")
    title, href = extract_title_link(card)
    assert expected_title_part in title
    assert href and not href.startswith("#")
    assert "/marque/" not in href


def test_extract_title_link_brand_card_not_brand():
    from bs4 import BeautifulSoup
    card = BeautifulSoup(BRAND_CARD, "html.parser").select_one("article")
    title, _ = extract_title_link(card)
    assert title != "SVR"          # le heading marque ne doit pas gagner
