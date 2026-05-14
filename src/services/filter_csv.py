import os
import time
import pandas as pd
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

_BATCH_SIZE = 16   # textos por chamada à Moderation API
_BATCH_DELAY = 22.0  # pausa entre batches — respeita limite de ~3 RPM em contas free

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


def _is_rate_limit(e: Exception) -> bool:
    msg = str(e).lower()
    return "429" in msg or "rate limit" in msg or "too many" in msg


def _score_batch(texts: list[str]) -> list[float]:
    """Envia um lote de textos em uma única chamada com retry em caso de 429."""
    max_retries = 2
    wait = 5.0
    for attempt in range(max_retries):
        try:
            result = _get_client().moderations.create(input=texts)
            scores = []
            for item in result.results:
                s = item.category_scores
                scores.append(float(max(
                    s.harassment,
                    s.harassment_threatening,
                    s.hate,
                    s.hate_threatening,
                    s.sexual,
                    s.sexual_minors,
                    s.violence,
                )))
            flagged = sum(1 for sc in scores if sc >= 0.7)
            print(f"🔍 OpenAI Moderation: {len(texts)} textos analisados | scores: {[round(sc, 3) for sc in scores]} | flagged: {flagged}")
            return scores
        except Exception as e:
            if _is_rate_limit(e) and attempt < max_retries - 1:
                sleep_time = wait * (2 ** attempt)  # 2s → 4s → 8s → 16s
                print(f"⚠️  Rate limit (tentativa {attempt + 1}/{max_retries}), aguardando {sleep_time:.0f}s...")
                time.sleep(sleep_time)
            else:
                if _is_rate_limit(e):
                    print("⚠️  Rate limit persistente após todas as tentativas, retornando 0.0.")
                else:
                    print(f"⚠️  Erro no lote: {e}")
                return [0.0] * len(texts)
    return [0.0] * len(texts)


def get_toxicity_score(text: str) -> float:
    """Calcula score de toxicidade (0.0-1.0) para um único texto."""
    if not text or not text.strip():
        return 0.0
    return _score_batch([text])[0]


def apply_filter(
    df: pd.DataFrame,
    threshold: float = 0.7,
    delay: float = 0.0,
    verbose: bool = False
) -> pd.DataFrame:
    """Adiciona colunas 'toxicity' e 'flagged' para todos os comentários.

    Envia em batches de _BATCH_SIZE para a Moderation API com retry em 429.
    Retorna o DataFrame completo com scores — a filtragem é responsabilidade do chamador.
    """
    if df.empty:
        return df

    df = df.copy()
    texts = df["comment"].fillna("").astype(str).tolist()
    total = len(texts)

    if verbose:
        print(f"⏳ Analisando toxicidade ({total} comentários, batches de {_BATCH_SIZE})...")

    scores = []
    for i in range(0, total, _BATCH_SIZE):
        batch = texts[i : i + _BATCH_SIZE]
        scores.extend(_score_batch(batch))
        if verbose:
            print(f"   ✓ {min(i + _BATCH_SIZE, total)}/{total}")
        if i + _BATCH_SIZE < total:
            time.sleep(_BATCH_DELAY)

    df["toxicity"] = scores
    df["flagged"] = df["toxicity"] >= threshold
    return df


def _generate_output_path(input_path: str, suffix: str = "_flagged") -> str:
    directory = os.path.dirname(input_path)
    filename = os.path.basename(input_path)
    name_without_ext = filename.replace(".csv", "")
    return os.path.join(directory, f"{name_without_ext}{suffix}.csv")


def filter_csv_file(
    input_path: str,
    threshold: float = 0.7,
    delay: float = 0.1
) -> dict:
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Arquivo não encontrado: {input_path}")

    df = pd.read_csv(input_path)
    total = len(df)
    print(f"\n📄 Processando: {input_path}")
    print(f"   Total de comentários: {total}")

    df_scored = apply_filter(df, threshold=threshold, delay=delay, verbose=True)
    filtered_df = df_scored[df_scored["flagged"]].reset_index(drop=True)

    safe = total - len(filtered_df)
    print(f"\n✅ Análise concluída:")
    print(f"   ✓ Seguros: {safe}")
    print(f"   ✗ Ofensivos (threshold={threshold}): {len(filtered_df)}")

    flagged_path = None
    if len(filtered_df) > 0:
        flagged_path = _generate_output_path(input_path)
        filtered_df.to_csv(flagged_path, index=False)
        print(f"   🚨 Arquivo salvo: {flagged_path}")
    else:
        print(f"   ℹ️  Nenhum comentário ofensivo encontrado")

    return {
        "total": total,
        "safe": safe,
        "flagged": len(filtered_df),
        "threshold": threshold,
        "flagged_path": flagged_path,
    }


if __name__ == "__main__":
    import sys
    input_csv = sys.argv[1]
    threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.7
    filter_csv_file(input_csv, threshold=threshold)
