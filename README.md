# Manuel Semantic Search Site

A tiny website with a centered search bar that performs **semantic search** over your products.  
Runs locally on macOS using **Docker**: a **FastAPI** backend + **Qdrant** vector database + a minimal HTML UI.

---

## Features

- 🔎 Natural-language search (embeddings) over product name & description  
- 🧠 Local embeddings by default (`all-MiniLM-L6-v2`) — free, works fully offline  
- 🔁 Easy reindex endpoint to ingest/refresh products  
- 🧱 Minimal UI in one HTML file; simple to restyle  
- 🐳 One `docker compose up` to run everything

---

## Architecture (simple)

```
Browser (index.html)
    │  GET /            ──> FastAPI serves page
    │  GET /search?q=.. ──> FastAPI embeds query -> Qdrant search
    │                     <── JSON product results (payloads)
FastAPI (app.py)
    └── Qdrant (vectors + payloads)
```

---

## Prerequisites

- macOS with **Docker Desktop** installed and running  
- (Optional) **curl** for quick API testing

---

## Project Structure

```
semantic-search-site/
├─ docker-compose.yml
├─ api/
│  ├─ Dockerfile
│  ├─ requirements.txt
│  ├─ app.py
│  ├─ products.json           # sample data (replace with your own)
│  └─ templates/
│     └─ index.html           # minimal centered search UI
```

---

## Getting Started (Quickstart)

From the project root:

```bash
# 1) Build and start containers
docker compose up --build

# 2) Open the site
open http://localhost:8000
```

Type something like **“soft towels for hotels”** and you’ll get semantic results from `products.json`.

> On first run, the API creates a `products` collection in Qdrant and seeds it with the sample data.

---

## Configuration

Environment variables (set inside `docker-compose.yml`):

- `QDRANT_URL` — default `http://qdrant:6333` (internal Docker hostname)
- `EMBEDDINGS_BACKEND` — `local` (default) or `openai`
- `OPENAI_API_KEY` — required only if `EMBEDDINGS_BACKEND=openai`

---

## Indexing Your Data

### Option A: JSON (easiest)
Replace `api/products.json` with your own data:

```json
[
  {"id": "p1", "name": "Item Name", "description": "Details...", "price": 12.34, "url": "/product/p1"},
  ...
]
```

Then either:
```bash
# restart containers OR
curl -X POST http://localhost:8000/reindex
```

### Option B: Pull from your SQL DB (optional)
1) Add your DB container (e.g., Postgres/MySQL) to `docker-compose.yml`.  
2) In `api/app.py`, modify `upsert_products()` to query rows and build payloads:
```python
# example shape:
payload = {"id": row.id, "name": row.name, "description": row.desc, "price": row.price, "url": row.url}
```
3) Rebuild & run:
```bash
docker compose up --build
```

---

## API Endpoints

- `GET /`  
  Returns the search page (centered search bar).

- `GET /search?q=your+query&top_k=8`  
  Performs semantic search and returns payloads.

  **Example:**
  ```bash
  curl "http://localhost:8000/search?q=soft%20spa%20towels&top_k=5"
  ```

- `POST /reindex`  
  Re-embeds and upserts all products from `products.json`.

  **Example:**
  ```bash
  curl -X POST http://localhost:8000/reindex
  ```

---

## Switching Embeddings (Local ↔ OpenAI)

### Local (default)
Pros: free, offline.  
Cons: larger image (Torch), slower cold start.

Nothing to do — it's already set.

### OpenAI (lighter image, possibly better quality)
1) In `api/requirements.txt`, **remove**:
```
sentence-transformers
torch
```
2) In `docker-compose.yml`, set:
```yaml
environment:
  - EMBEDDINGS_BACKEND=openai
  - OPENAI_API_KEY=sk-...your key...
```
3) In `api/Dockerfile`, remove the block that pre-downloads the local model.  
4) Rebuild:
```bash
docker compose up --build
```

---

## Styling the UI

Edit `api/templates/index.html`:
- The page is pure HTML/CSS/JS; no framework required.
- Adjust colors, fonts, layout, or add a logo.
- The script calls `/search` and renders results as cards.

---

## Development Workflow

- Rebuild after Python dependency changes:
  ```bash
  docker compose up --build
  ```
- Hot reload (optional): add `--reload` to the `uvicorn` command in `api/Dockerfile` and mount the code directory as a volume in `docker-compose.yml`. For a simple, reproducible setup, the default is fine.

---

## Data Model (payloads)

Each product is stored as:
```json
{
  "id": "p123",
  "name": "Product Name",
  "description": "Text used for embeddings",
  "price": 19.99,
  "url": "/product/p123"
}
```

- Embedding text = `"{name} - {description}"`  
- Qdrant stores vectors + payloads. Search returns top matches with full payload.

---

## Common Tasks

- **Change top-k results:** pass `top_k` in `/search` (default 8).
- **Filter by fields (advanced):** Extend the search call in `app.py` with Qdrant filters (e.g., by category, availability).
- **Add fields:** Just include extra keys in your payload; the UI shows name/description/price by default—update HTML to display more.

---

## Troubleshooting

- **“Site doesn’t load”**  
  Ensure Docker Desktop is running. Make sure port `8000` isn’t used by another process.

- **Qdrant not reachable**  
  The API refers to Qdrant via `http://qdrant:6333` (Docker network). Don’t change this unless you know what you’re doing.

- **Very slow first query**  
  Local model may cold-load. Subsequent queries are faster. Consider switching to OpenAI embeddings for faster startup.

- **No results**  
  Confirm `products.json` has data and that `/reindex` succeeded.

- **Torch wheel install is slow**  
  That’s normal on first build. It’s cached afterward. If this is a blocker, switch to OpenAI embeddings.

---

## Security Notes

- This repo is for local use. Do **not** expose it to the public Internet without:
  - Auth (at least basic auth or a gateway)
  - Rate limiting
  - HTTPS/Proxy
  - Input validation/log redaction if you add write endpoints

---

## Scaling Ideas (later)

- Replace the HTML page with Next.js or your existing app; keep this FastAPI as an internal “semantic-search” service.
- Add batch upserts and partial updates.
- Add category/brand filters using Qdrant’s structured filters.
- Persist Qdrant volume to keep indexes between runs (already configured via `qdrant_data` volume).

---

## License

MIT

---

## Commands Reference

```bash
# Build & run (foreground logs)
docker compose up --build

# Stop
docker compose down

# Reindex after changing products.json
curl -X POST http://localhost:8000/reindex

# Test search
curl "http://localhost:8000/search?q=laundry%20detergent"
```search?q=laundry%20detergent"
