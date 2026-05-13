"""
mcp_server.py
=============
Serveur MCP Paris Je T'aime — expose les événements parisiens scorés
aux assistants IA (Claude Desktop, ChatGPT, Gemini).
Données depuis Supabase (PostgreSQL).

Usage :
    python mcp_server.py

Déclaration dans Claude Desktop (claude_desktop_config.json) :
    {
      "mcpServers": {
        "paris-events": {
          "command": "python",
          "args": ["C:/Users/taroua/Documents/paris-events-api/mcp_server.py"]
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import re
import sys
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator


def _log(event: str, data: dict = {}) -> None:
    """Log vers stderr (stdio MCP ne peut pas écrire sur stdout)."""
    print(json.dumps({"event": event, **data}), file=sys.stderr)


load_dotenv(override=False)

# ── Configuration ──────────────────────────────────────────────────────────────

DATABASE_URL = os.getenv("DATABASE_URL")
MIN_SCORE    = float(os.getenv("MCP_MIN_SCORE", "40"))
MAX_RESULTS  = int(os.getenv("MCP_MAX_RESULTS", "20"))
ARTICLES_DIR = Path(__file__).parent / "data" / "ai_articles"

# Noms des mois en français (module-level, construit une seule fois)
_MOIS = ["", "janvier", "février", "mars", "avril", "mai", "juin",
         "juillet", "août", "septembre", "octobre", "novembre", "décembre"]

# Cache des articles JSON chargés depuis le disque
_article_json_cache: Dict[str, Optional[dict]] = {}

CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "exposition":        ["expo", "exposition", "peinture", "photo", "art contemporain", "sculpture"],
    "concert":           ["concert", "musique"],
    "festival":          ["festival"],
    "sport":             ["sport"],
    "theatre":           ["theatre", "théâtre", "spectacle"],
    "danse":             ["danse"],
    "cinema":            ["cinema", "cinéma", "film"],
    "balade":            ["balade urbaine", "balade"],
    "marche":            ["marche", "marché", "brocante"],
    "famille":           ["famille", "enfant", "kids"],
    "visite-guidee":     ["visite guidée", "visite guidee"],
    "art-contemporain":  ["art contemporain"],
    "musique-classique": ["musique classique", "classique", "orchestre"],
    "conference":        ["conférence", "conference", "débat"],
    "atelier":           ["atelier", "workshop"],
    "lgbtq":             ["lgbtq", "pride"],
    "gastronomie":       ["gastronomie", "cuisine"],
    "gratuit":           ["gratuit"],
}

# ── Pool de connexions PostgreSQL ──────────────────────────────────────────────

_pool: Optional[psycopg2.pool.SimpleConnectionPool] = None


def _get_pool() -> psycopg2.pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(1, 5, DATABASE_URL)
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


def _to_iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value)[:10]


def _row_to_event(row: dict) -> dict:
    """Convertit une ligne DB en dict propre pour les outils MCP."""
    tags = row.get("fap_tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(";") if t.strip()]

    return {
        "title":           row.get("title") or "",
        "description":     (row.get("description") or "")[:300],
        "date_start":      _format_date_fr(row.get("date_start")),
        "date_end":        _format_date_fr(row.get("date_end")),
        "date_start_iso":  _to_iso(row.get("date_start")),
        "date_end_iso":    _to_iso(row.get("date_end")),
        "venue":           row.get("address_name") or "",
        "address":         row.get("address_street") or "",
        "zipcode":         row.get("address_zipcode") or "",
        "city":            row.get("address_city") or "Paris",
        "arrondissement":  row.get("arrondissement"),
        "price_type":      row.get("price_type") or "",
        "audience":        row.get("audience") or "",
        "tags":            tags,
        "metro":           "",   # enrichi via article si disponible
        "lien_officiel":   "",
        "url_wordpress":   row.get("url_wordpress") or "",
        "sources":         [s for s in [row.get("source_url")] if s],
        "score":           round(float(row.get("score_final") or 0), 1),
        "fiabilite":       row.get("fiabilite") or "",
        "seo_slug":        row.get("seo_slug") or "",
        "article_complet": bool(row.get("url_wordpress")),
        "last_updated":    _to_iso(row.get("updated_at")),
    }


def _load_article_json(slug: str) -> Optional[dict]:
    """Charge l'article JSON depuis le dossier local (sections, FAQ enrichies). Résultat mis en cache."""
    if slug in _article_json_cache:
        return _article_json_cache[slug]

    result = None
    if ARTICLES_DIR.exists():
        # Correspondance directe par nom de dossier
        direct = ARTICLES_DIR / slug / "article.json"
        if direct.exists():
            try:
                result = json.loads(direct.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Recherche par slug dans les métadonnées
        if result is None:
            for folder in ARTICLES_DIR.iterdir():
                if not folder.is_dir():
                    continue
                json_file = folder / "article.json"
                if not json_file.exists():
                    continue
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    art_slug = data.get("facts", {}).get("slug") or folder.name
                    if art_slug == slug:
                        result = data
                        break
                except Exception:
                    continue

    _article_json_cache[slug] = result  # cache hit ou miss (None)
    return result


def _extract_metro(article: dict) -> str:
    facts = article.get("facts", {})
    metro = facts.get("metro", "")
    if metro:
        return metro
    for s in article.get("sections", []):
        if "rendre" in s.get("heading", "").lower():
            body = s.get("body", "")
            m = re.search(r"ligne\s+(\d+)[^\n,\.]*station\s+([^\.]+?)[\.,]", body, re.I)
            if m:
                return f"Métro ligne {m.group(1)}, station {m.group(2).strip()}"
            m = re.search(r"station\s+([^\.]+?)[\.,]", body, re.I)
            if m:
                return f"Station {m.group(1).strip()}"
    return ""


def _format_event_markdown(event: dict) -> str:
    """Formate un événement en Markdown lisible."""
    lines = [f"### {event['title']}"]
    if event.get("description"):
        lines.append(event["description"])
    lines.append("")
    date_str = event["date_start"]
    if event.get("date_end") and event["date_end"] != event["date_start"]:
        date_str += f" → {event['date_end']}"
    lines.append(f"- **Date** : {date_str}")
    if event.get("venue"):
        lieu = event["venue"]
        if event.get("address"):
            lieu += f", {event['address']}"
        lines.append(f"- **Lieu** : {lieu}")
    if event.get("arrondissement"):
        lines.append(f"- **Arrondissement** : {event['arrondissement']}e")
    if event.get("metro"):
        lines.append(f"- **Métro** : {event['metro']}")
    if event.get("price_type"):
        lines.append(f"- **Prix** : {event['price_type']}")
    if event.get("url_wordpress"):
        lines.append(f"- **Article** : {event['url_wordpress']}")
    elif event.get("lien_officiel"):
        lines.append(f"- **Lien** : {event['lien_officiel']}")
    lines.append(f"- **Score** : {event['score']}/100")
    if event.get("article_complet"):
        lines.append("- *Article complet disponible — appelle paris_get_event_detail pour plus d'infos*")
    lines.append("")
    return "\n".join(lines)


# ── Pydantic Models ────────────────────────────────────────────────────────────

class ResponseFormat(str, Enum):
    """Format de sortie des outils."""
    MARKDOWN = "markdown"
    JSON     = "json"


class GetEventsInput(BaseModel):
    """Paramètres de recherche pour paris_get_events."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    keyword: Optional[str] = Field(
        default=None,
        description="Mot-clé dans le titre (ex: 'Gorillaz', 'Louvre', 'jazz')",
        max_length=200,
    )
    date_from: Optional[str] = Field(
        default=None,
        description="Date de début ISO YYYY-MM-DD (ex: '2026-06-01')",
    )
    date_to: Optional[str] = Field(
        default=None,
        description="Date de fin ISO YYYY-MM-DD (ex: '2026-06-30')",
    )
    category: Optional[str] = Field(
        default=None,
        description=(
            "Catégorie d'événement. Valeurs : 'concert', 'exposition', 'festival', "
            "'sport', 'theatre', 'danse', 'marche', 'cinema', 'balade', 'famille', "
            "'visite-guidee', 'atelier', 'conference', 'lgbtq', 'gastronomie', 'gratuit'"
        ),
    )
    arrondissement: Optional[int] = Field(
        default=None,
        description="Arrondissement parisien (1 à 20)",
        ge=1,
        le=20,
    )
    price_type: Optional[str] = Field(
        default=None,
        description="Filtrer par tarif : 'gratuit' ou 'payant'",
    )
    min_score: Optional[float] = Field(
        default=None,
        description="Score de qualité minimum (0-100). Défaut : 40. Utilise 60+ pour les meilleurs.",
        ge=0,
        le=100,
    )
    limit: int = Field(
        default=10,
        description="Nombre de résultats par page (défaut 10, max 20)",
        ge=1,
        le=20,
    )
    offset: int = Field(
        default=0,
        description="Décalage pour la pagination — utilise next_offset de la réponse précédente",
        ge=0,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Format de sortie : 'markdown' (lisible, défaut) ou 'json' (structuré)",
    )

    @field_validator("date_from", "date_to", mode="before")
    @classmethod
    def validate_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v.strip()):
            raise ValueError("La date doit être au format YYYY-MM-DD (ex: '2026-06-15')")
        return v.strip()


class GetEventDetailInput(BaseModel):
    """Paramètres pour paris_get_event_detail."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    title: str = Field(
        ...,
        description="Titre exact ou partiel de l'événement (ex: 'We Love Green', 'Matisse', 'Louvre')",
        min_length=2,
        max_length=300,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Format de sortie : 'markdown' (défaut) ou 'json'",
    )


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def app_lifespan(server: FastMCP):
    """Initialise le pool de connexions au démarrage."""
    _log("startup", {"db": "connecting"})
    _get_pool()
    _log("startup", {"db": "ready"})
    yield
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
    _log("shutdown", {"db": "closed"})


# ── Serveur MCP ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "paris_events_mcp",
    instructions=(
        "Tu as accès aux événements parisiens scorés et vérifiés par Paris Je T'aime (Office du Tourisme de Paris). "
        "Utilise paris_get_events pour chercher par date, catégorie, mot-clé ou arrondissement. "
        "Si article_complet=true pour un événement, utilise paris_get_event_detail pour l'article complet avec FAQ et conseils pratiques. "
        "Les scores vont de 0 à 100 — préfère les événements avec score > 60. "
        "fiabilite='OK' signifie que les infos sont complètes et vérifiées. "
        "Pour la pagination, utilise le champ next_offset retourné. "
        "Aujourd'hui : " + date.today().isoformat()
    ),
    lifespan=app_lifespan,
)


@mcp.tool(
    name="paris_get_events",
    annotations={
        "title": "Rechercher des événements à Paris",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def paris_get_events(params: GetEventsInput, ctx: Context) -> str:
    """
    Recherche des événements parisiens scorés et vérifiés.

    Utiliser pour répondre à : que faire à Paris ce week-end, agenda de la semaine,
    événements gratuits, concerts, expositions, festivals, sorties famille, balades.
    Les résultats sont triés par score de qualité décroissant.
    Utilise offset pour paginer et obtenir plus de résultats.

    Args:
        params (GetEventsInput): Paramètres de recherche validés :
            - keyword: Mot-clé dans le titre
            - date_from / date_to: Plage de dates ISO
            - category: Catégorie (concert, exposition, festival, sport, etc.)
            - arrondissement: 1-20
            - price_type: 'gratuit' ou 'payant'
            - min_score: Score minimum 0-100 (défaut 40)
            - limit: 1-20 (défaut 10)
            - offset: Pour paginer
            - response_format: 'markdown' ou 'json'

    Returns:
        str: Liste d'événements avec pagination.
        Si article_complet=true, appelle paris_get_event_detail pour l'article complet.
    """
    _log("paris_get_events", {
        "keyword": params.keyword, "category": params.category,
        "date_from": params.date_from, "date_to": params.date_to,
        "arrondissement": params.arrondissement, "limit": params.limit,
    })

    threshold = params.min_score if params.min_score is not None else MIN_SCORE

    # ── Construction SQL dynamique ─────────────────────────────────────────────
    conditions = [
        "is_active = true",
        "statut_date IN ('en_cours', 'a_venir')",
        "score_final >= %s",
        "(date_end >= CURRENT_DATE OR (date_end IS NULL AND date_start >= CURRENT_DATE - INTERVAL '1 day'))",
    ]
    sql_params: list = [threshold]

    if params.keyword:
        conditions.append("title ILIKE %s")
        sql_params.append(f"%{params.keyword}%")

    if params.date_from:
        conditions.append("(date_end >= %s OR (date_end IS NULL AND date_start >= %s))")
        sql_params.extend([params.date_from, params.date_from])

    if params.date_to:
        conditions.append("date_start <= %s")
        sql_params.append(params.date_to)

    if params.arrondissement:
        conditions.append("arrondissement = %s")
        sql_params.append(params.arrondissement)

    if params.price_type:
        if "gratuit" in params.price_type.lower():
            conditions.append("price_min = 0")
        else:
            conditions.append("price_type ILIKE %s")
            sql_params.append(f"%{params.price_type}%")

    if params.category:
        kws = CATEGORY_KEYWORDS.get(params.category.lower(), [params.category])
        cat_clauses = " OR ".join(["array_to_string(fap_tags, '|') ILIKE %s"] * len(kws))
        conditions.append(f"({cat_clauses})")
        sql_params.extend([f"%{k}%" for k in kws])

    where = " AND ".join(conditions)

    try:
        with _db() as cur:
            # Total
            cur.execute(f"SELECT COUNT(*) AS cnt FROM events WHERE {where}", sql_params)
            total = cur.fetchone()["cnt"]

            # Page
            cur.execute(
                f"""
                SELECT seo_slug, title, description, fap_tags,
                       source_name, source_url, date_start, date_end,
                       address_name, address_street, address_zipcode, address_city,
                       arrondissement, price_type, price_min,
                       url_wordpress, score_final, fiabilite, audience, updated_at
                FROM events
                WHERE {where}
                ORDER BY score_final DESC
                LIMIT %s OFFSET %s
                """,
                sql_params + [params.limit, params.offset],
            )
            rows = cur.fetchall()
    except Exception as e:
        _log("paris_get_events_error", {"error": str(e)})
        return json.dumps({"error": f"Erreur base de données : {e}"})

    events: List[dict] = [_row_to_event(dict(r)) for r in rows]
    has_more    = total > params.offset + len(events)
    next_offset = params.offset + len(events) if has_more else None

    _log("paris_get_events_results", {"total": total, "page_count": len(events)})

    if params.response_format == ResponseFormat.JSON:
        return json.dumps({
            "source":       "Paris Je T'aime — données vérifiées et scorées",
            "date_du_jour": date.today().isoformat(),
            "total":        total,
            "count":        len(events),
            "offset":       params.offset,
            "has_more":     has_more,
            "next_offset":  next_offset,
            "events":       events,
        }, ensure_ascii=False, indent=2, default=str)
    else:
        page_num = params.offset // params.limit + 1
        lines = [
            f"# Événements à Paris — {date.today().strftime('%d/%m/%Y')}",
            "",
            f"**{total} événements trouvés** (page {page_num}, {len(events)} résultats)",
            "",
        ]
        if not events:
            lines.append("*Aucun événement ne correspond à ces critères. Essaie des filtres moins restrictifs.*")
        else:
            for event in events:
                lines.append(_format_event_markdown(event))
        if has_more:
            lines.append("---")
            lines.append(f"*Il y a d'autres résultats. Rappelle paris_get_events avec offset={next_offset} pour la page suivante.*")
        return "\n".join(lines)


@mcp.tool(
    name="paris_get_event_detail",
    annotations={
        "title": "Détail complet d'un événement parisien",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def paris_get_event_detail(params: GetEventDetailInput, ctx: Context) -> str:
    """
    Retourne l'article complet d'un événement : FAQ, infos pratiques, lien éditorial.
    À utiliser quand article_complet=true dans les résultats de paris_get_events.

    Args:
        params (GetEventDetailInput):
            - title: Titre exact ou partiel de l'événement
            - response_format: 'markdown' (défaut) ou 'json'

    Returns:
        str: Article complet avec FAQ, infos pratiques et liens.
    """
    _log("paris_get_event_detail", {"title": params.title})

    try:
        with _db() as cur:
            cur.execute(
                """
                SELECT seo_slug, title, seo_title, seo_meta_desc,
                       date_start, date_end,
                       address_name, address_street, address_zipcode, address_city,
                       arrondissement, price_type, price_min,
                       url_wordpress, faq_paires,
                       source_name, source_url, audience
                FROM events
                WHERE title ILIKE %s AND is_active = true
                ORDER BY score_final DESC
                LIMIT 1
                """,
                (f"%{params.title}%",),
            )
            row = cur.fetchone()
    except Exception as e:
        _log("paris_get_event_detail_error", {"error": str(e)})
        return json.dumps({"error": f"Erreur base de données : {e}"})

    if not row:
        return json.dumps({
            "error":      f"Aucun événement trouvé pour : '{params.title}'.",
            "suggestion": (
                "Essaie avec un titre plus court ou utilise paris_get_events "
                "pour trouver l'événement exact."
            ),
        }, ensure_ascii=False)

    row  = dict(row)
    slug = row.get("seo_slug") or ""

    # Désérialiser la FAQ depuis la DB
    faq_raw = row.get("faq_paires") or []
    if isinstance(faq_raw, str):
        try:
            faq_raw = json.loads(faq_raw)
        except Exception:
            faq_raw = []

    faq = [
        {"question": item.get("question", ""), "reponse": item.get("reponse", item.get("answer", ""))}
        for item in (faq_raw or [])
        if item.get("question")
    ]

    # Enrichissement optionnel depuis l'article JSON local (sections, metro, etc.)
    sections: List[dict] = []
    metro = ""
    lien_officiel = ""
    article_json = _load_article_json(slug)
    if article_json:
        facts         = article_json.get("facts", {})
        metro         = facts.get("metro", "") or _extract_metro(article_json)
        lien_officiel = facts.get("lien_officiel", "")
        sections      = [
            {"titre": s["heading"], "contenu": s["body"]}
            for s in article_json.get("sections", [])
        ]
        # Compléter la FAQ si non présente en DB
        if not faq and article_json.get("faq"):
            faq = article_json["faq"]

    # Adresse complète
    adresse = " ".join(filter(None, [
        row.get("address_street"),
        row.get("address_zipcode"),
        row.get("address_city"),
    ]))

    result: Dict[str, Any] = {
        "title":         row.get("title") or "",
        "h1":            row.get("seo_title") or "",
        "excerpt":       row.get("seo_meta_desc") or "",
        "url_wordpress": row.get("url_wordpress") or "",
        "infos_pratiques": {
            "date":          _format_date_fr(row.get("date_start")),
            "adresse":       adresse,
            "metro":         metro,
            "tarif":         row.get("price_type") or "",
            "lien_officiel": lien_officiel,
        },
        "sections": sections,
        "faq":      faq,
        "sources":  [s for s in [row.get("source_url")] if s],
    }

    _log("paris_get_event_detail_ok", {"title": result["title"]})

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
    else:
        lines = [f"# {result['title']}", ""]
        if result.get("excerpt"):
            lines += [result["excerpt"], ""]
        if result.get("url_wordpress"):
            lines += [f"**Article complet** : {result['url_wordpress']}", ""]

        infos = result["infos_pratiques"]
        lines.append("## Infos pratiques")
        for label, key in [
            ("Date",          "date"),
            ("Adresse",       "adresse"),
            ("Métro",         "metro"),
            ("Tarif",         "tarif"),
            ("Lien officiel", "lien_officiel"),
        ]:
            val = infos.get(key, "")
            if val:
                lines.append(f"- **{label}** : {val}")
        lines.append("")

        for section in result.get("sections", []):
            lines += [f"## {section['titre']}", section["contenu"], ""]

        if result.get("faq"):
            lines.append("## FAQ")
            for item in result["faq"]:
                q = item.get("question", "")
                r = item.get("reponse", item.get("answer", ""))
                lines += [f"**Q : {q}**", r, ""]

        return "\n".join(lines)


@mcp.tool(
    name="paris_get_stats",
    annotations={
        "title": "Statistiques de la base Paris Je T'aime",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def paris_get_stats(ctx: Context) -> str:
    """
    Retourne des statistiques sur la base d'événements Paris Je T'aime.
    Aucun paramètre requis.

    Returns:
        str: JSON avec statistiques complètes (total, à venir, gratuit, top catégories, etc.)
    """
    _log("paris_get_stats")

    try:
        with _db() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)                                                                                         AS total_events,
                    COUNT(*) FILTER (WHERE statut_date IN ('en_cours','a_venir') AND score_final >= %s)             AS events_a_venir,
                    COUNT(*) FILTER (WHERE url_wordpress IS NOT NULL AND url_wordpress != '')                        AS articles_complets,
                    AVG(score_final) FILTER (WHERE statut_date IN ('en_cours','a_venir') AND score_final >= %s)     AS score_moyen,
                    COUNT(*) FILTER (WHERE statut_date IN ('en_cours','a_venir') AND price_min = 0)                 AS gratuit,
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

    except Exception as e:
        _log("paris_get_stats_error", {"error": str(e)})
        return json.dumps({"error": f"Erreur base de données : {e}"})

    result = {
        "date_du_jour":      date.today().isoformat(),
        "total_events":      stats["total_events"] or 0,
        "events_a_venir":    stats["events_a_venir"] or 0,
        "articles_complets": stats["articles_complets"] or 0,
        "score_moyen":       round(float(stats["score_moyen"] or 0), 1),
        "gratuit":           stats["gratuit"] or 0,
        "payant":            stats["payant"] or 0,
        "top_categories":    top_cats,
        "source":            "Paris Je T'aime — données vérifiées et scorées",
    }

    _log("paris_get_stats_ok", {"total": result["total_events"]})
    return json.dumps(result, ensure_ascii=False, indent=2)


# ── Lancement ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
