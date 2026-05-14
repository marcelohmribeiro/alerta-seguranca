"""
Formas de acionar pelo Terminal e iniciar a operação
CLI do MVP: roda o pipeline completo para YouTube OU Reddit.
- YouTube por vídeo:            --video-id <ID>
- Reddit por post específico:    --reddit-submission <ID>
- Reddit busca inteligente:      --reddit-search-auto  (usa vocabulário para montar queries)
"""

import argparse
import logging
from typing import List, Tuple

from common.config import load_settings, get_env
from common.models import CommentRecord
from services.vocab_client import fetch_vocab
from ingestion.youtube import fetch_comments, normalize_comment
from ingestion.reddit import fetch_submission_comments
from ingestion.reddit_util import extract_submission_id
from preprocess.text import Preprocessor
from rules.filter import compile_regex_patterns, apply_rules
from semantic.encoder import SemanticEncoder
from classify.aggregator import aggregate_risk
from services.filter_csv import get_toxicity_score
from storage.supabase_client import (
    save_records as sb_save,
    delete_older_than as sb_ttl,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")


def resolve_source(args) -> Tuple[str, str]:
    if args.video_id:
        return "youtube", args.video_id
    if args.reddit_submission:
        return "reddit", args.reddit_submission
    raise SystemExit("Informe --video-id (YouTube) ou --reddit-submission (Reddit).")


def _queries_from_vocab(keywords: List[str], examples: List[str], max_terms: int = 20) -> List[str]:
    def _san(s: str) -> str:
        s = (s or "").strip()
        return f"\"{s}\"" if " " in s else s

    raw_terms = [_san(k) for k in keywords] + [_san(e) for e in examples]

    seen = set()
    uniq: List[str] = []
    for t in raw_terms:
        tl = t.lower()
        if tl and tl not in seen:
            uniq.append(t)
            seen.add(tl)

    chunk = 5
    queries: List[str] = []
    upto = min(len(uniq), max_terms)
    for i in range(0, upto, chunk):
        group = uniq[i:i + chunk]
        queries.append(" OR ".join(group))
    return queries



def run_pipeline(args) -> List[CommentRecord]:
    settings = load_settings()
    nlp_cfg = settings.nlp
    thr = settings.thresholds
    svc = settings.services
    storage = settings.storage

    logger.info("Buscando vocabulário na Vocab API...")
    vocab = fetch_vocab()
    keywords = vocab.get("keywords_explicit", [])
    examples = vocab.get("examples_implicit", [])
    regex_map = vocab.get("regex_patterns", {})
    regex_compiled = compile_regex_patterns(regex_map)
    logger.info("Vocab OK. KW=%d | EX=%d | Regex=%d", len(keywords), len(examples), len(regex_compiled))

    pre = Preprocessor(nlp_cfg.get("spacy_model", "pt_core_news_sm"))
    enc = SemanticEncoder("paraphrase-multilingual-MiniLM-L12-v2", examples)

    if getattr(args, "reddit_search_auto", False):
        platform = "reddit_auto"

        def _san(s: str) -> str:
            return (s or "").strip().lower()

        raw_terms = [_san(k) for k in keywords] + [_san(e) for e in examples]
        raw_terms = [t for t in raw_terms if t]  # remove vazios
        # limita o total de termos para não estourar consultas:
        max_terms = getattr(args, "reddit_max_terms", 50)
        queries = raw_terms[:max_terms]

        subreddits = [s.strip() for s in (getattr(args, "subreddits", "all") or "all").split(",") if s.strip()]
        logger.info("Reddit busca inteligente → subreddits=%s | queries=%d", ",".join(subreddits), len(queries))

        from ingestion.reddit_search import search_and_collect_comments
        raw = search_and_collect_comments(
            queries=queries,
            subreddits=subreddits,
            limit_per_query=getattr(args, "reddit_posts_per_query", 10),
            time_filter=getattr(args, "reddit_time_filter", "week"),
            sort=getattr(args, "reddit_search_sort", "relevance"),
            per_submission_limit=getattr(args, "reddit_per_submission_limit", 200),
            max_total=getattr(args, "limit_total", 1000),
        )

    else:
        platform, source_id = resolve_source(args)

        # Se for YouTube, exige a chave
        if platform == "youtube":
            youtube_key = get_env("YOUTUBE_API_KEY")
            if not youtube_key:
                raise SystemExit("Defina YOUTUBE_API_KEY no .env para usar YouTube.")

            logger.info("Coletando comentários do YouTube (video_id=%s)...", source_id)
            raw = fetch_comments(
                video_id=source_id,
                page_size=getattr(args, "page_size", 50),
                max_pages=getattr(args, "max_pages", 3),
            )

        elif platform == "reddit":
            try:
                source_id = extract_submission_id(source_id)  
            except ValueError as e:
                raise SystemExit(f"[Reddit] {e}")

            logger.info("Coletando comentários do Reddit (submission_id=%s)...", source_id)
            raw = fetch_submission_comments(
                submission_id=source_id,
                limit=getattr(args, "limit", 200),
                sort=getattr(args, "reddit_sort", "top"),
                only_root=getattr(args, "reddit_only_root", False),
            )

        else:
            raise SystemExit(f"Plataforma '{platform}' não suportada.")

    logger.info("Total bruto: %d", len(raw))

    results: List[CommentRecord] = []
    for item in raw:
        if platform in ("youtube",):
            comment_id, payload = normalize_comment(item)
        else:
            comment_id = item["comment_id"]
            payload = item

        text = payload.get("text") or ""
        preprocessed = pre.preprocess(text)

        hits = apply_rules(text, preprocessed, keywords, regex_compiled)
        sem_score = enc.score(preprocessed)

        perspective_score = None
        if bool(svc.get("perspective_enabled", False)):
            perspective_score = get_toxicity_score(text)

        final_score, label = aggregate_risk(
            rule_hits=hits,
            semantic_score=sem_score,
            thresholds=thr,
            perspective_sexual=perspective_score,
            perspective_weight=float(svc.get("perspective_weight", 0.4)),
        )

        rec = CommentRecord(
            platform="reddit" if platform == "reddit_auto" else platform,  
            source_id=payload.get("source_id") or source_id,
            comment_id=comment_id,
            author=payload.get("author"),
            text=text,
            preprocessed=preprocessed,
            rule_hits=hits,
            semantic_score=sem_score,
            perspective_sexual=perspective_score,
            final_score=final_score,
            classification=label,
            extras={
                "likeCount": payload.get("likeCount", 0),
                "publishedAt": payload.get("publishedAt"),
                "permalink": payload.get("permalink"),
            },
        )
        results.append(rec)
    if args.persist:
        logger.info("Persistindo no Supabase...")
        try:
            ttl_days = int(storage.get("ttl_days", 30))
            deleted = sb_ttl(days=ttl_days)
            logger.info("[Supabase] TTL: removidos %d docs antigos (ttl_days=%d).", deleted, ttl_days)
        except Exception as e:
            logger.warning("[Supabase] TTL falhou (ignorado): %s", e)

        created, _ = sb_save(results)
        logger.info("[Supabase] Gravados %d documentos (upsert).", created)

    sus = sum(1 for r in results if r.classification == "suspeito")
    aten = sum(1 for r in results if r.classification == "atencao")
    ok = sum(1 for r in results if r.classification == "ok")
    logger.info("Resumo → suspeito=%d | atencao=%d | ok=%d", sus, aten, ok)

    return results


def main():
    parser = argparse.ArgumentParser(description="Pipeline de análise de comentários (YouTube/Reddit).")

    parser.add_argument("--video-id", help="YouTube: ID do vídeo (ex.: dQw4w9WgXcQ)")
    parser.add_argument("--reddit-submission", help="Reddit: ID do post (base36 da URL /comments/<ID>/)")
    parser.add_argument("--reddit-search-auto", action="store_true",
                        help="Reddit: usa o vocabulário para buscar posts e coletar comentários dos resultados.")

    # YouTube
    parser.add_argument("--page-size", type=int, default=50, help="YouTube: tamanho da página (default=50)")
    parser.add_argument("--max-pages", type=int, default=3, help="YouTube: número máximo de páginas (default=3)")

    # Reddit (post específico)
    parser.add_argument("--limit", type=int, default=200, help="Reddit (submission): máx. de comentários (default=200)")
    parser.add_argument("--reddit-sort",
                        choices=["new", "top", "best", "controversial", "old", "qa"],
                        default="new",
                        help="Reddit (submission): ordenação dos comentários (default=new)")
    parser.add_argument("--reddit-only-root", action="store_true",
                        help="Reddit (submission): apenas comentários top-level (sem respostas)")

    # Reddit (busca inteligente)
    parser.add_argument("--subreddits", default="all",
                        help="Reddit (auto): lista separada por vírgula de subreddits. Ex.: brasil,paisefilhos")
    parser.add_argument("--reddit-posts-per-query", type=int, default=15,
                        help="Reddit (auto): quantos posts por query (default=15)")
    parser.add_argument("--reddit-per-submission-limit", type=int, default=80,
                        help="Reddit (auto): comentários por post (default=80)")
    parser.add_argument("--reddit-time-filter",
                        choices=["day", "week", "month", "year", "all"],
                        default="week",
                        help="Reddit (auto): janela de tempo da busca (default=week)")
    parser.add_argument("--reddit-search-sort",
                        choices=["new", "top", "relevance", "comments"],
                        default="new",
                        help="Reddit (auto): ordenação de busca (default=new)")
    parser.add_argument("--reddit-max-terms", type=int, default=20,
                        help="Reddit (auto): máx. de termos do vocabulário usados para gerar queries (default=20)")
    parser.add_argument("--limit-total", type=int, default=300,
                        help="Reddit (auto): teto global de comentários coletados (default=300)")

    parser.add_argument("--persist", action="store_true", help="Persiste resultados no Firestore")

    args = parser.parse_args()

    if not (args.video_id or args.reddit_submission or args.reddit_search_auto):
        parser.error("Escolha uma fonte: --video-id OU --reddit-submission OU --reddit-search-auto")

    run_pipeline(args)


if __name__ == "__main__":
    main()
