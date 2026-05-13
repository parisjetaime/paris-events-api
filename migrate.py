"""
migrate.py
==========
Migre les données CSV + articles JSON vers Supabase (PostgreSQL).

Usage :
    python migrate.py
"""

import csv
import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

DATABASE_URL = os.getenv("DATABASE_URL")
SCORED_CSV   = Path(__file__).parent / "data" / "scored_events.csv"
ARTICLES_DIR = Path(__file__).parent / "data" / "ai_articles"

# ── Helpers ────────────────────────────────────────────────────────────────────

def _clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_float(value: str) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _parse_date(value: str):
    if not value:
        return None
    try:
        return value[:19].replace("+00:00", "")
    except Exception:
        return None


def _hash_dedup(title: str, date_start: str, zipcode: str) -> str:
    raw = f"{title.lower().strip()}|{date_start[:10]}|{zipcode}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _calc_statut_date(date_start: str, date_end: str) -> str:
    today = date.today()
    ds = date_start[:10] if date_start else None
    de = date_end[:10] if date_end else ds
    if not ds:
        return "a_venir"
    try:
        from datetime import datetime
        start = datetime.strptime(ds, "%Y-%m-%d").date()
        end   = datetime.strptime(de, "%Y-%m-%d").date() if de else start
        if end < today:
            return "passe"
        elif start <= today <= end:
            return "en_cours"
        else:
            return "a_venir"
    except Exception:
        return "a_venir"


def _calc_departement(zipcode: str) -> str:
    if not zipcode or len(zipcode) < 2:
        return ""
    return zipcode[:2]


def _calc_zone(departement: str) -> str:
    if departement == "75":
        return "Paris intra-muros"
    elif departement in ("92", "93", "94"):
        return "Petite couronne"
    elif departement in ("77", "78", "91", "95"):
        return "Grande couronne"
    return ""


def _calc_arrondissement(zipcode: str):
    if zipcode and len(zipcode) == 5 and zipcode.startswith("75"):
        try:
            n = int(zipcode[-2:])
            return n if 1 <= n <= 20 else None
        except ValueError:
            pass
    return None


def _load_articles() -> dict:
    articles = {}
    if not ARTICLES_DIR.exists():
        return articles
    for folder in ARTICLES_DIR.iterdir():
        if not folder.is_dir():
            continue
        json_file = folder / "article.json"
        if not json_file.exists():
            continue
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            slug = data.get("facts", {}).get("slug") or folder.name
            articles[slug] = data
        except Exception:
            continue
    return articles


def _slug_from_title(title: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFD", title.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:80]


# ── Migration ──────────────────────────────────────────────────────────────────

def migrate():
    print("Connexion à Supabase...")
    conn = psycopg2.connect(DATABASE_URL)
    cur  = conn.cursor()

    print("Chargement des articles...")
    articles = _load_articles()
    print(f"  {len(articles)} articles trouvés")

    print("Lecture du CSV...")
    with SCORED_CSV.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    print(f"  {len(rows)} événements trouvés")

    inserted = 0
    skipped  = 0

    for row in rows:
        title      = row.get("title", "").strip()
        date_start = _parse_date(row.get("date_start", ""))
        date_end   = _parse_date(row.get("date_end", ""))
        zipcode    = row.get("address_zipcode", "").strip()
        slug       = _slug_from_title(title)

        hash_dedup    = _hash_dedup(title, date_start or "", zipcode)
        departement   = _calc_departement(zipcode)
        zone          = _calc_zone(departement)
        arrondissement = _calc_arrondissement(zipcode)
        statut_date   = _calc_statut_date(date_start or "", date_end or "")

        sources    = [s.strip() for s in row.get("sources", "").split("|") if s.strip()]
        fap_tags   = [t.strip() for t in row.get("qfap_tags", "").replace('"', "").split(";") if t.strip()]
        price_type = row.get("price_type", "").strip()
        price_min  = 0.0 if "gratuit" in price_type.lower() else None

        # Article associé
        article    = articles.get(slug)
        url_wp     = article.get("url_wordpress", "") if article else ""
        seo_title  = ""
        seo_meta   = ""
        seo_slug   = slug
        faq_paires = []

        if article:
            facts     = article.get("facts", {})
            seo_title = article.get("seo_title", "")
            seo_meta  = article.get("meta_description", "")
            faq_paires = article.get("faq", [])

        try:
            cur.execute("""
                INSERT INTO events (
                    source_name, source_url, hash_dedup,
                    updated_at, title, description,
                    fap_tags, date_start, date_end,
                    statut_date, address_name, address_street,
                    address_zipcode, address_city, departement,
                    zone_grand_paris, arrondissement, price_type,
                    price_min, audience, score_frequence,
                    score_fraicheur, score_lieu, score_tendance,
                    score_info, score_google_notes, score_final,
                    fiabilite, seo_title, seo_meta_desc,
                    seo_slug, url_wordpress, faq_paires,
                    categorie, statut, is_active
                ) VALUES (
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (hash_dedup) DO UPDATE SET
                    score_final  = EXCLUDED.score_final,
                    statut_date  = EXCLUDED.statut_date,
                    updated_at   = NOW()
            """, (
                sources[0] if sources else "",
                sources[1] if len(sources) > 1 else "",
                hash_dedup,
                _parse_date(row.get("updated_at", "")),
                title,
                _clean_html(row.get("description", "")),
                fap_tags,
                date_start, date_end,
                statut_date,
                row.get("address_name", "").strip(),
                row.get("address_street", "").strip(),
                zipcode,
                row.get("address_city", "").strip(),
                departement, zone, arrondissement,
                price_type, price_min,
                row.get("audience", "").strip(),
                _parse_float(row.get("score_frequence", "0")),
                _parse_float(row.get("score_fraicheur", "0")),
                _parse_float(row.get("score_lieu", "0")),
                _parse_float(row.get("score_tendance", "0")),
                _parse_float(row.get("score_info", "0")),
                _parse_float(row.get("score_google_notes", "0")),
                _parse_float(row.get("score_final", "0")),
                row.get("fiabilite", "").strip(),
                seo_title, seo_meta, seo_slug,
                url_wp,
                json.dumps(faq_paires, ensure_ascii=False),
                "evenement", "publie", True
            ))
            inserted += 1
        except Exception as e:
            print(f"  ⚠️  Erreur sur '{title}': {e}")
            skipped += 1
            conn.rollback()
            continue

        conn.commit()

    cur.close()
    conn.close()

    print(f"\n✅ Migration terminée : {inserted} insérés, {skipped} ignorés")


if __name__ == "__main__":
    migrate()
