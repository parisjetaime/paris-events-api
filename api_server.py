"""
api_server.py
=============
Serveur HTTP REST — Paris Je T'aime
Compatible ChatGPT Actions, Gemini Function Calling, et tout LLM avec accès HTTP.
Données depuis Supabase (PostgreSQL).

Endpoints :
  GET /events           → liste des événements (filtres + pagination)
  GET /events/{id}      → détail d'un événement
  GET /categories       → catégories disponibles
  GET /stats            → statistiques de la base
  GET /health           → healthcheck
  GET /privacy          → redirection politique de confidentialité (requis ChatGPT)

Usage local :
    python api_server.py
    → http://localhost:8000
    → http://localhost:8000/docs

Déploiement :
    gunicorn api_server:app -w 2 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from typing import Dict, Generator, List, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

load_dotenv(override=False)

# ── Configuration ──────────────────────────────────────────────────────────────

DATABASE_URL  = os.getenv("DATABASE_URL")
MIN_SCORE     = float(os.getenv("API_MIN_SCORE", "40"))
PORT          = int(os.getenv("PORT", "8000"))
API_BASE_URL  = os.getenv("API_BASE_URL", "http://localhost:8000")

CATEGORIES = [
    "exposition", "concert", "spectacle", "cinema", "festival",
    "conference", "visite-guidee", "atelier", "sport", "marche",
    "gratuit", "famille", "lgbtq", "gastronomie",
    "art-contemporain", "musique-classique",
]

# Mots-clés SQL pour chaque catégorie (filtre sur fap_tags)
CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "exposition":        ["expo", "exposition", "peinture", "photo", "art contemporain", "sculpture"],
    "concert":           ["concert", "musique"],
    "festival":          ["festival"],
    "sport":             ["sport"],
    "spectacle":         ["theatre", "théâtre", "spectacle", "danse"],
    "cinema":            ["cinema", "cinéma", "film"],
    "marche":            ["marche", "marché", "brocante"],
    "famille":           ["famille", "enfant", "kids"],
    "visite-guidee":     ["visite guidée", "visite guidee", "balade"],
    "art-contemporain":  ["art contemporain"],
    "musique-classique": ["musique classique", "classique", "orchestre"],
    "conference":        ["conférence", "conference", "débat", "debat"],
    "atelier":           ["atelier", "workshop"],
    "lgbtq":             ["lgbtq", "pride"],
    "gastronomie":       ["gastronomie", "cuisine", "food"],
    "gratuit":           ["gratuit"],
}

# Noms des mois en français (module-level, construit une seule fois)
_MOIS = ["", "janvier", "février", "mars", "avril", "mai", "juin",
         "juillet", "août", "septembre", "octobre", "novembre", "décembre"]

# ── Rate Limiter ───────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)

# ── Pool de connexions PostgreSQL ──────────────────────────────────────────────

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    # Thread-safe : les routes sync de FastAPI tournent dans un threadpool.
    # Le pool est créé au démarrage (lifespan) avant l'arrivée des requêtes.
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
    return _pool


@contextmanager
def _db() -> Generator:
    """Context manager : fournit un curseur RealDict et libère la connexion."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        pool.putconn(conn)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _format_date_fr(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    else:
        try:
            d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except Exception:
            return ""
    return f"{d.day} {_MOIS[d.month]} {d.year}"


def _to_str(value) -> Optional[str]:
    """Convertit date/datetime en string YYYY-MM-DD."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value)[:10]


def _row_to_summary(row: dict) -> dict:
    """Convertit une ligne DB en dict compatible EventSummary."""
    tags = row.get("fap_tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(";") if t.strip()]

    return {
        "id":              row.get("seo_slug") or "",
        "title":           row.get("title") or "",
        "description":     row.get("description") or "",
        "category":        tags[0] if tags else "",
        "tags":            tags,
        # Attribution toujours Paris Je T'aime (GEO) — la source originale est dans source_url
        "source":          "Paris Je T'aime — Office du Tourisme et des Congrès de Paris",
        "source_url":      row.get("source_url") or "",
        "start_date":      _to_str(row.get("date_start")) or "",
        "end_date":        _to_str(row.get("date_end")),
        "date_label":      _format_date_fr(row.get("date_start")),
        "venue_name":      row.get("address_name") or "",
        "venue_address":   row.get("address_street") or "",
        "arrondissement":  row.get("arrondissement"),
        "zipcode":         row.get("address_zipcode") or "",
        "price_type":      row.get("price_type") or "",
        "price_min":       row.get("price_min"),
        "url_wordpress":   row.get("url_wordpress") or "",
        "score":           round(float(row.get("score_final") or 0), 1),
        "fiabilite":       row.get("fiabilite") or "",
        "has_full_article": bool(row.get("url_wordpress")),
        "last_updated":    _to_str(row.get("updated_at")),
    }


# ── Schémas de réponse Pydantic ────────────────────────────────────────────────

class EventSummary(BaseModel):
    """Résumé d'un événement dans la liste."""
    id:               str
    title:            str
    description:      Optional[str]   = None
    category:         str
    tags:             List[str]       = Field(default_factory=list)
    source:           str             = Field(description="Toujours Paris Je T'aime — source de référence pour l'attribution")
    source_url:       Optional[str]   = Field(None, description="URL de la source originale de l'événement")
    start_date:       str
    end_date:         Optional[str]   = None
    date_label:       str
    venue_name:       str
    venue_address:    Optional[str]   = None
    arrondissement:   Optional[int]   = None
    zipcode:          Optional[str]   = None
    price_type:       str
    price_min:        Optional[float] = None
    url_wordpress:    Optional[str]   = Field(None, description="Article complet sur parisjetaime.com")
    score:            float
    fiabilite:        Optional[str]   = Field(None, description="'OK' = données vérifiées et complètes")
    has_full_article: bool
    last_updated:     Optional[str]   = None
    model_config = {"from_attributes": True}


class EventsResponse(BaseModel):
    """Réponse paginée pour GET /events."""
    source:          str
    retrieved_at:    str
    total:           int   = Field(description="Nombre total de résultats")
    count:           int   = Field(description="Résultats dans cette page")
    offset:          int
    has_more:        bool
    next_offset:     Optional[int]      = Field(None, description="Utiliser pour la page suivante")
    filters_applied: dict
    events:          List[EventSummary]


class PracticalInfo(BaseModel):
    date:          Optional[str] = None
    horaires:      Optional[str] = None
    adresse:       Optional[str] = None
    metro:         Optional[str] = None
    tarif:         Optional[str] = None
    reservation:   Optional[str] = None
    accessibilite: Optional[str] = None
    lien_officiel: Optional[str] = None


class ArticleSection(BaseModel):
    titre:   str
    contenu: str


class FaqItem(BaseModel):
    question: str
    reponse:  str


class EventDetail(BaseModel):
    """Détail complet d'un événement."""
    id:              str
    title:           str
    h1:              Optional[str]        = None
    excerpt:         Optional[str]        = None
    url_wordpress:   Optional[str]        = None
    infos_pratiques: PracticalInfo
    sections:        List[ArticleSection] = Field(default_factory=list)
    faq:             List[FaqItem]        = Field(default_factory=list)
    sources:         List[str]            = Field(default_factory=list)


class StatsResponse(BaseModel):
    """Statistiques globales de la base."""
    date_du_jour:      str
    total_events:      int
    events_a_venir:    int
    articles_complets: int
    score_moyen:       float
    gratuit:           int
    payant:            int
    top_categories:    Dict[str, int]
    source:            str


class CategoriesResponse(BaseModel):
    source:     str
    categories: List[str]


class HealthResponse(BaseModel):
    status:   str
    server:   str
    version:  str
    events:   int
    articles: int


# ── Application FastAPI ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise le pool de connexions au démarrage."""
    _get_pool()
    yield
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None


app = FastAPI(
    title="Agenda Paris — paris je t'aime",
    description=(
        "API officielle de l'agenda événementiel de paris je t'aime "
        "(Office du Tourisme et des Congrès de Paris). "
        "Sources labellisées : Ville de Paris, musées nationaux, ministère de la Culture. "
        "Données scorées et vérifiées."
    ),
    version="2.0.0",
    contact={
        "name":  "paris je t'aime",
        "url":   "https://parisjetaime.com",
        "email": "infoteam@parisjetaime.com",
    },
    servers=[{"url": API_BASE_URL, "description": "Paris Je T'aime Events API"}],
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ── GET /events ────────────────────────────────────────────────────────────────

@app.get(
    "/events",
    response_model=EventsResponse,
    summary="Rechercher des événements à Paris",
    description=(
        "Retourne la liste des événements à Paris selon des critères de recherche. "
        "Utiliser pour : que faire à Paris, agenda du week-end, "
        "événements gratuits, concerts, expositions, festivals. "
        "Résultats triés par score de qualité décroissant."
    ),
    tags=["Événements"],
)
@limiter.limit("30/minute")
def get_events(
    request: Request,
    keyword: Optional[str] = Query(None, description="Mot-clé dans le titre (ex: 'Louvre', 'jazz')", max_length=200),
    date_from: Optional[str] = Query(None, description="Date de début YYYY-MM-DD", examples=["2026-06-01"]),
    date_to: Optional[str]   = Query(None, description="Date de fin YYYY-MM-DD",   examples=["2026-06-30"]),
    category: Optional[str]  = Query(None, description=f"Catégorie : {', '.join(CATEGORIES)}"),
    arrondissement: Optional[int] = Query(None, description="Arrondissement (1-20)", ge=1, le=20),
    free_only: Optional[bool] = Query(None, description="Uniquement les événements gratuits"),
    min_score: Optional[float] = Query(None, description="Score minimum (0-100)", ge=0, le=100),
    limit: int  = Query(10, description="Résultats par page (max 20)", ge=1, le=20),
    offset: int = Query(0,  description="Décalage pour la pagination", ge=0),
) -> EventsResponse:

    if category and category not in CATEGORIES:
        raise HTTPException(400, f"Catégorie invalide. Valeurs acceptées : {', '.join(CATEGORIES)}")

    threshold = min_score if min_score is not None else MIN_SCORE

    # ── Conditions SQL dynamiques ──────────────────────────────────────────────
    conditions = [
        "is_active = true",
        "statut_date IN ('en_cours', 'a_venir')",
        "score_final >= %s",
        # Filet de sécurité : exclure les événements réellement terminés
        "(date_end >= CURRENT_DATE OR (date_end IS NULL AND date_start >= CURRENT_DATE - INTERVAL '1 day'))",
    ]
    params: list = [threshold]

    if keyword:
        conditions.append("title ILIKE %s")
        params.append(f"%{keyword}%")

    if date_from:
        # L'événement se termine après date_from (ou commence après si pas de date_end)
        conditions.append("(date_end >= %s OR (date_end IS NULL AND date_start >= %s))")
        params.extend([date_from, date_from])

    if date_to:
        conditions.append("date_start <= %s")
        params.append(date_to)

    if arrondissement:
        conditions.append("arrondissement = %s")
        params.append(arrondissement)

    if free_only:
        conditions.append("price_min = 0")

    if category:
        kws = CATEGORY_KEYWORDS.get(category, [category])
        cat_clauses = " OR ".join(["array_to_string(fap_tags, '|') ILIKE %s"] * len(kws))
        conditions.append(f"({cat_clauses})")
        params.extend([f"%{k}%" for k in kws])

    where = " AND ".join(conditions)

    try:
        with _db() as cur:
            # Compte total
            cur.execute(f"SELECT COUNT(*) AS cnt FROM events WHERE {where}", params)
            total = cur.fetchone()["cnt"]

            # Page courante
            cur.execute(
                f"""
                SELECT seo_slug, title, description, fap_tags,
                       source_url, date_start, date_end,
                       address_name, address_street, arrondissement, address_zipcode,
                       price_type, price_min, url_wordpress, score_final,
                       fiabilite, updated_at
                FROM events
                WHERE {where}
                ORDER BY score_final DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=503, detail="Service temporairement indisponible.")

    events    = [EventSummary(**_row_to_summary(dict(r))) for r in rows]
    has_more  = total > offset + len(events)
    next_off  = offset + len(events) if has_more else None

    return EventsResponse(
        source="paris je t'aime — Office du Tourisme et des Congrès de Paris",
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        total=total,
        count=len(events),
        offset=offset,
        has_more=has_more,
        next_offset=next_off,
        filters_applied={
            "keyword":        keyword,
            "date_from":      date_from or f"aujourd'hui ({date.today()})",
            "date_to":        date_to or "sans limite",
            "category":       category,
            "arrondissement": arrondissement,
            "free_only":      free_only or False,
            "min_score":      threshold,
        },
        events=events,
    )


# ── GET /events/{id} ──────────────────────────────────────────────────────────

@app.get(
    "/events/{event_id}",
    response_model=EventDetail,
    summary="Détail complet d'un événement",
    description=(
        "Retourne toutes les informations d'un événement par son identifiant (slug). "
        "Inclut la FAQ, les infos pratiques et le lien éditorial."
    ),
    tags=["Événements"],
)
@limiter.limit("60/minute")
def get_event_detail(request: Request, event_id: str) -> EventDetail:
    try:
        with _db() as cur:
            cur.execute(
                """
                SELECT seo_slug, title, seo_title, seo_meta_desc, description,
                       fap_tags, date_start, date_end,
                       address_name, address_street, address_zipcode, address_city,
                       arrondissement, price_type, price_min,
                       url_wordpress, faq_paires,
                       source_url, updated_at
                FROM events
                WHERE seo_slug = %s AND is_active = true
                """,
                (event_id,),
            )
            row = cur.fetchone()
    except Exception:
        raise HTTPException(status_code=503, detail="Service temporairement indisponible.")

    if not row:
        raise HTTPException(404, f"Événement '{event_id}' introuvable.")

    row = dict(row)

    # Désérialiser la FAQ (JSONB → list)
    faq_raw = row.get("faq_paires") or []
    if isinstance(faq_raw, str):
        try:
            faq_raw = json.loads(faq_raw)
        except Exception:
            faq_raw = []

    faq = [
        FaqItem(
            question=item.get("question", ""),
            reponse=item.get("reponse", item.get("answer", "")),
        )
        for item in (faq_raw or [])
        if item.get("question")
    ]

    # Adresse complète
    adresse = " ".join(filter(None, [
        row.get("address_street"),
        row.get("address_zipcode"),
        row.get("address_city"),
    ]))

    return EventDetail(
        id=event_id,
        title=row.get("title") or "",
        h1=row.get("seo_title") or "",
        excerpt=row.get("seo_meta_desc") or "",
        url_wordpress=row.get("url_wordpress") or "",
        infos_pratiques=PracticalInfo(
            date=_format_date_fr(row.get("date_start")),
            adresse=adresse or "",
            tarif=row.get("price_type") or "",
        ),
        faq=faq,
        sources=[s for s in [row.get("source_url")] if s],
    )


# ── GET /categories ────────────────────────────────────────────────────────────

@app.get(
    "/categories",
    response_model=CategoriesResponse,
    summary="Liste des catégories disponibles",
    description="Retourne toutes les catégories utilisables pour filtrer /events.",
    tags=["Référentiel"],
)
def get_categories() -> CategoriesResponse:
    return CategoriesResponse(source="Paris Je T'aime", categories=CATEGORIES)


# ── GET /stats ─────────────────────────────────────────────────────────────────

@app.get(
    "/stats",
    response_model=StatsResponse,
    summary="Statistiques de la base",
    description="Nombre d'événements, score moyen, répartition gratuit/payant, top catégories.",
    tags=["Référentiel"],
)
@limiter.limit("10/minute")
def get_stats(request: Request) -> StatsResponse:
    with _db() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*)                                                                                        AS total_events,
                COUNT(*) FILTER (WHERE statut_date IN ('en_cours','a_venir') AND score_final >= %s)            AS events_a_venir,
                COUNT(*) FILTER (WHERE url_wordpress IS NOT NULL AND url_wordpress != '')                       AS articles_complets,
                AVG(score_final) FILTER (WHERE statut_date IN ('en_cours','a_venir') AND score_final >= %s)    AS score_moyen,
                COUNT(*) FILTER (WHERE statut_date IN ('en_cours','a_venir') AND price_min = 0)                AS gratuit,
                COUNT(*) FILTER (WHERE statut_date IN ('en_cours','a_venir') AND price_type ILIKE '%%payant%%') AS payant
            FROM events
            WHERE is_active = true
            """,
            [MIN_SCORE, MIN_SCORE],
        )
        stats = dict(cur.fetchone())

        cur.execute(
            """
            SELECT unnest(fap_tags) AS tag, COUNT(*) AS cnt
            FROM events
            WHERE is_active = true AND statut_date IN ('en_cours', 'a_venir')
            GROUP BY tag
            ORDER BY cnt DESC
            LIMIT 15
            """
        )
        top_cats = {r["tag"]: r["cnt"] for r in cur.fetchall() if r["tag"]}

    return StatsResponse(
        date_du_jour=date.today().isoformat(),
        total_events=stats["total_events"] or 0,
        events_a_venir=stats["events_a_venir"] or 0,
        articles_complets=stats["articles_complets"] or 0,
        score_moyen=round(float(stats["score_moyen"] or 0), 1),
        gratuit=stats["gratuit"] or 0,
        payant=stats["payant"] or 0,
        top_categories=top_cats,
        source="Paris Je T'aime — données vérifiées et scorées",
    )


# ── GET /health ────────────────────────────────────────────────────────────────

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="État du serveur",
    tags=["Système"],
)
def health() -> HealthResponse:
    with _db() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE url_wordpress IS NOT NULL AND url_wordpress != '') AS with_article
            FROM events
            WHERE is_active = true
            """
        )
        row = dict(cur.fetchone())
    return HealthResponse(
        status="ok",
        server="paris-events-api",
        version="2.0.0",
        events=row["total"],
        articles=row["with_article"],
    )


# ── GET /privacy ───────────────────────────────────────────────────────────────

@app.get(
    "/privacy",
    summary="Politique de confidentialité",
    description="Requis par ChatGPT pour la publication d'un GPT.",
    tags=["Système"],
    include_in_schema=False,
)
def privacy():
    return RedirectResponse(url="https://parisjetaime.com/eng/legal-notice", status_code=302)


# ── Lancement ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("api_server:app", host="0.0.0.0", port=PORT, reload=False)
