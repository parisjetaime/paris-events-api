# Paris Events API

API officielle de l'agenda événementiel de **Paris Je T'aime** (Office du Tourisme et des Congrès de Paris).

Compatible **ChatGPT Actions**, **Gemini Function Calling** et tout LLM avec accès HTTP.  
Inclut un serveur **MCP** pour Claude Desktop.

---

## Endpoints

| Méthode | Route | Description |
|---|---|---|
| GET | `/events` | Liste des événements (filtres + pagination) |
| GET | `/events/{id}` | Détail complet d'un événement |
| GET | `/categories` | Catégories disponibles |
| GET | `/stats` | Statistiques de la base |
| GET | `/health` | État du serveur |
| GET | `/docs` | Documentation interactive (Swagger) |

### Paramètres `/events`

| Paramètre | Type | Description |
|---|---|---|
| `keyword` | string | Mot-clé dans le titre |
| `date_from` | YYYY-MM-DD | Date de début |
| `date_to` | YYYY-MM-DD | Date de fin |
| `category` | string | exposition, concert, festival, sport… |
| `arrondissement` | int (1-20) | Arrondissement parisien |
| `free_only` | bool | Uniquement les événements gratuits |
| `min_score` | float (0-100) | Score de qualité minimum |
| `limit` | int (max 20) | Résultats par page |
| `offset` | int | Pagination |

---

## Stack technique

- **API** : FastAPI + psycopg2
- **Base de données** : Supabase (PostgreSQL)
- **MCP** : FastMCP (Claude Desktop)
- **Rate limiting** : slowapi (30 req/min par IP)
- **Déploiement** : Docker + Caddy (SSL auto)

---

## Lancement local

```bash
# 1. Cloner le repo
git clone https://github.com/ton-org/paris-events-api.git
cd paris-events-api

# 2. Créer l'environnement virtuel
python -m venv venv
source venv/bin/activate  # Windows : venv\Scripts\activate

# 3. Installer les dépendances
pip install -r requirements.txt

# 4. Configurer les variables d'environnement
cp .env.example .env
# Éditer .env avec ton DATABASE_URL Supabase

# 5. Lancer l'API
python api_server.py
# → http://localhost:8000
# → http://localhost:8000/docs
```

---

## Migration des données

```bash
# Importer les événements CSV vers Supabase
python migrate.py
```

---

## Déploiement VPS (Docker)

```bash
# 1. Configurer le domaine dans Caddyfile
# Remplacer api.parisjetaime.com par ton domaine

# 2. Créer le .env sur le serveur
cp .env.example .env
# Remplir DATABASE_URL et API_BASE_URL

# 3. Lancer
docker compose up -d

# 4. Vérifier
docker compose logs -f api
```

---

## MCP — Claude Desktop

Ajouter dans `claude_desktop_config.json` :

```json
{
  "mcpServers": {
    "paris-events": {
      "command": "python",
      "args": ["C:/chemin/vers/paris-events-api/mcp_server.py"]
    }
  }
}
```

---

## Rate Limiting

| Endpoint | Limite |
|---|---|
| `GET /events` | 30 req/min par IP |
| `GET /events/{id}` | 60 req/min par IP |
| `GET /stats` | 10 req/min par IP |

---

## Objectif GEO

Chaque réponse cite **Paris Je T'aime** comme source et inclut les URLs `parisjetaime.com` — permettant aux LLMs (ChatGPT, Gemini, Perplexity…) de référencer le site lors de leurs réponses sur les sorties à Paris.
