import hashlib
import os
from datetime import datetime, timedelta
from typing import List

from supabase import create_client, Client

_client = None


def get_client() -> Client:
    global _client
    if _client is None:
        key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_ANON_KEY"]
        _client = create_client(os.environ["SUPABASE_URL"], key)
    return _client


def save_records(records: List) -> tuple[int, int]:
    """Upsert de CommentRecord completos (pipeline main.py --persist)."""
    client = get_client()
    rows = []
    for r in records:
        if hasattr(r, "model_dump"):
            doc = r.model_dump()
        elif hasattr(r, "__dict__"):
            doc = r.__dict__
        else:
            doc = dict(r)

        rows.append({
            "id": f"{doc['platform']}:{doc['source_id']}:{doc['comment_id']}",
            "platform": doc["platform"],
            "source_id": doc.get("source_id"),
            "comment_id": doc.get("comment_id"),
            "author": doc.get("author"),
            "text": doc["text"],
            "preprocessed": doc.get("preprocessed"),
            "rule_hits": doc.get("rule_hits", []),
            "semantic_score": doc.get("semantic_score"),
            "toxicity_score": doc.get("perspective_sexual"),
            "final_score": doc.get("final_score"),
            "classification": doc.get("classification"),
            "flagged": (doc.get("final_score") or 0.0) >= 0.9,
            "extras": doc.get("extras", {}),
        })

    client.table("comments").upsert(rows).execute()
    return len(records), 0


def save_raw_comments(platform: str, identifier: str, df) -> int:
    """Upsert de comentários crus do scraper API (text + scores OpenAI)."""
    client = get_client()
    rows = []
    for _, row in df.iterrows():
        text = str(row.get("comment") or "")
        cid = hashlib.sha1(f"{platform}:{identifier}:{text}".encode()).hexdigest()[:16]
        rows.append({
            "id": f"{platform}:{identifier}:{cid}",
            "platform": platform,
            "source_id": identifier,
            "comment_id": cid,
            "text": text,
            "toxicity_score": row.get("toxicity"),
            "flagged": bool(row.get("flagged", False)),
        })

    if rows:
        client.table("comments").upsert(rows).execute()
    return len(rows)


def delete_older_than(days: int) -> int:
    client = get_client()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    result = client.table("comments").delete().lt("ingested_at", cutoff).execute()
    return len(result.data) if result.data else 0
