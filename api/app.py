import os
import json
import uuid
import re
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    Range,
)

# --- Config ---
EMBEDDINGS_BACKEND = os.getenv("EMBEDDINGS_BACKEND", "local").lower()  # "local" | "openai"
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION = "products"
DIM = 384  # all-MiniLM-L6-v2 dims

# --- Embeddings ---
if EMBEDDINGS_BACKEND == "openai":
    import openai
    openai.api_key = os.getenv("OPEN_API_KEY")  # must be set when using openai backend

    def embed_texts(texts: List[str]) -> List[List[float]]:
        if not openai.api_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set")
        resp = openai.embeddings.create(model="text-embedding-3-small", input=texts)
        return [d.embedding for d in resp.data]
else:
    # local, free
    from sentence_transformers import SentenceTransformer

    _model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    def embed_texts(texts: List[str]) -> List[List[float]]:
        return _model.encode(texts, normalize_embeddings=True).tolist()

# --- App & Templates ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- Qdrant client ---
client = QdrantClient(url=QDRANT_URL)


def ensure_collection() -> None:
    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION not in existing:
        client.create_collection(
            COLLECTION,
            vectors_config=VectorParams(size=DIM, distance=Distance.COSINE),
        )


def _to_uuid(point_id: str) -> str:
    """
    Deterministic UUID based on your product's original ID string.
    Keeps a stable mapping across reindexes.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"product:{point_id}"))


# ------------- Price NLP -------------
_NUM = r"(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?"
_CURRENCY = r"(?:\$|usd|dollars?)"
_WS = r"[ \t]*"

def _to_float(s: str) -> float:
    return float(s.replace(",", ""))

def _extract_price_filters(q: str) -> Tuple[str, Optional[float], Optional[float]]:
    """
    Parse natural-language price constraints from the query and return:
      - cleaned_query: query with price phrases removed
      - min_price, max_price: numeric bounds (None if not present)

    Supports:
      - under/below/at most/<= (e.g., "under $10", "<= 20 dollars")
      - over/above/at least/>= (e.g., "over 15", ">= $25")
      - between X and Y / from X to Y / X - Y
      - <, <=, >, >= 10
      - budget-ish words ("cheap", "inexpensive", "expensive") -> soft defaults
    """
    original = q
    s = " " + q.lower().strip() + " "  # pad spaces for easier regex removal
    min_price: Optional[float] = None
    max_price: Optional[float] = None

    # between X and Y / from X to Y / X - Y
    patterns = [
        rf"(?:between|from){_WS}({_NUM}){_WS}(?:and|to|-){_WS}({_NUM})(?:{_WS}{_CURRENCY})?",
        rf"({_NUM}){_WS}-{_WS}({_NUM})(?:{_WS}{_CURRENCY})?",
    ]
    for pat in patterns:
        for m in re.finditer(pat, s):
            a, b = _to_float(m.group(1)), _to_float(m.group(2))
            lo, hi = sorted((a, b))
            min_price = lo if min_price is None else max(min_price, lo)
            max_price = hi if max_price is None else min(max_price, hi)
            s = s.replace(m.group(0), " ")

    # under/below/<=
    for pat in [
        rf"(?:under|below|at\s*most){_WS}({_NUM})(?:{_WS}{_CURRENCY})?",
        rf"(?:<=|<){_WS}({_NUM})(?:{_WS}{_CURRENCY})?",
        rf"(?:up\s*to){_WS}({_NUM})(?:{_WS}{_CURRENCY})?",
    ]:
        for m in re.finditer(pat, s):
            val = _to_float(m.group(1))
            max_price = val if max_price is None else min(max_price, val)
            s = s.replace(m.group(0), " ")

    # over/above/>=
    for pat in [
        rf"(?:over|above|at\s*least){_WS}({_NUM})(?:{_WS}{_CURRENCY})?",
        rf"(?:>=|>){_WS}({_NUM})(?:{_WS}{_CURRENCY})?",
    ]:
        for m in re.finditer(pat, s):
            val = _to_float(m.group(1))
            min_price = val if min_price is None else max(min_price, val)
            s = s.replace(m.group(0), " ")

    # equals ~ around X -> make a narrow band (+/- 10%)
    for pat in [
        rf"(?:around|about|approx(?:\.|imately)?){_WS}({_NUM})(?:{_WS}{_CURRENCY})?",
        rf"(?:exactly){_WS}({_NUM})(?:{_WS}{_CURRENCY})?",
    ]:
        for m in re.finditer(pat, s):
            val = _to_float(m.group(1))
            band = (0.9 * val, 1.1 * val) if "around" in m.group(0) or "about" in m.group(0) or "approx" in m.group(0) else (val, val)
            lo, hi = band
            min_price = lo if min_price is None else max(min_price, lo)
            max_price = hi if max_price is None else min(max_price, hi)
            s = s.replace(m.group(0), " ")

    # soft heuristics
    if re.search(r"\b(cheap|inexpensive|budget)\b", s):
        # only set if user didn't already specify a max
        if max_price is None:
            max_price = 15.0
        s = re.sub(r"\b(cheap|inexpensive|budget)\b", " ", s)

    if re.search(r"\b(expensive|premium|high-end)\b", s):
        if min_price is None:
            min_price = 100.0
        s = re.sub(r"\b(expensive|premium|high-end)\b", " ", s)

    cleaned = " ".join(s.split())  # squish spaces
    if not cleaned:
        cleaned = original  # fallback if we removed everything

    return cleaned, min_price, max_price


def upsert_products():
    """
    Reads products.json, embeds name+description, and upserts into Qdrant.
    Uses UUIDs for point IDs; preserves original ID as 'product_id' in payload.
    """
    path = Path("products.json")
    if not path.exists():
        # Nothing to seed; skip quietly so the API still starts.
        return {"ok": True, "seeded": 0, "note": "products.json not found"}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid products.json: {e}")

    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="products.json must be a JSON array")

    ensure_collection()

    texts = [f"{item.get('name','')} - {item.get('description','')}" for item in data]
    vectors = embed_texts(texts)

    points = [
        PointStruct(
            id=_to_uuid(str(item.get("id"))),
            vector=v,
            payload={**item, "product_id": item.get("id")},
        )
        for item, v in zip(data, vectors)
    ]

    client.upsert(collection_name=COLLECTION, points=points)
    return {"ok": True, "seeded": len(points)}


# --- Startup: seed if empty ---
@app.on_event("startup")
def startup():
    ensure_collection()
    info = client.get_collection(COLLECTION)
    points_count = getattr(info, "points_count", 0) or 0
    if points_count == 0:
        upsert_products()


# --- Routes ---
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    top_k: int = 8,
    score_threshold: float = 0.20,  # raise to filter weaker semantic matches
    category: Optional[str] = None,  # optional exact-match category if present in payload
):
    """
    Semantic search that *parses price constraints from q* and applies
    structured filters in Qdrant automatically.
    Examples it understands:
      - "cleaning spray under 10 dollars"
      - "detergent > 20"
      - "towels between 5 and 15"
      - "microfiber cloths 5-10"
      - "premium detergent" (implies min_price ~ 100 via heuristic)
    """
    cleaned_q, min_price, max_price = _extract_price_filters(q)

    vec = embed_texts([cleaned_q])[0]

    # Build Qdrant payload filters
    must_conditions = []

    if min_price is not None or max_price is not None:
        rng = Range()
        if min_price is not None:
            rng.gte = float(min_price)
        if max_price is not None:
            rng.lte = float(max_price)
        must_conditions.append(FieldCondition(key="price", range=rng))

    if category:
        must_conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))

    query_filter = Filter(must=must_conditions) if must_conditions else None

    # Pull a slightly-larger candidate pool then return top_k
    res = client.search(
        collection_name=COLLECTION,
        query_vector=vec,
        limit=max(top_k, 12),
        score_threshold=score_threshold,
        query_filter=query_filter,
    )

    # Return only payloads sorted by Qdrant score
    return [r.payload for r in res[:top_k]]


@app.post("/reindex")
def reindex():
    result = upsert_products()
    return result or {"ok": True}


@app.get("/health")
def health():
    try:
        client.get_collections()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
