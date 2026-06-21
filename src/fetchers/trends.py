"""
Fetcher: Google Trends — wykrywanie spike'ów zainteresowania.

Używa pytrends (nieoficjalny wrapper Google Trends).
Nie wymaga klucza API, ale ma agresywny rate limit — używaj rzadko (max co godzinę).

UWAGA: Google Trends zwraca relative interest (0-100), nie absolutne liczby.
Spike definiujemy jako: aktualny wynik >= spike_threshold (default 70).
"""
import logging
import time
from typing import Optional

try:
    from pytrends.request import TrendReq
    PYTRENDS_AVAILABLE = True
except ImportError:
    PYTRENDS_AVAILABLE = False

from .models import SocialSignal
from ..config import cfg

log = logging.getLogger(__name__)


def _get_pytrends() -> Optional["TrendReq"]:
    if not PYTRENDS_AVAILABLE:
        log.warning("pytrends nie jest zainstalowany — Google Trends fetcher wyłączony")
        return None
    return TrendReq(hl="en-US", tz=360, timeout=(10, 25), retries=2, backoff_factor=0.5)


def fetch_trending_searches() -> list[SocialSignal]:
    """
    Pobierz trending searches z Google Trends dla słów kluczowych z config.yaml.
    Zwraca SocialSignal dla każdego spike'ującego keyword.
    """
    pt = _get_pytrends()
    if not pt:
        return []

    tcfg = cfg["google_trends"]
    keywords = tcfg["keywords_to_track"]
    threshold = tcfg["spike_threshold"]
    timeframe = tcfg["timeframe"]
    signals: list[SocialSignal] = []

    # pytrends obsługuje max 5 keywords na raz
    chunks = [keywords[i:i+5] for i in range(0, len(keywords), 5)]

    for chunk in chunks:
        try:
            pt.build_payload(chunk, timeframe=timeframe, geo="")
            df = pt.interest_over_time()

            if df is None or df.empty:
                continue

            # Ostatni punkt = aktualny trend score
            latest = df.iloc[-1]

            for kw in chunk:
                if kw not in df.columns:
                    continue
                score = int(latest.get(kw, 0))
                if score >= threshold:
                    signals.append(SocialSignal(
                        source="google_trends",
                        title=f"Google Trends spike: '{kw}'",
                        url=f"https://trends.google.com/trends/explore?q={kw.replace(' ', '+')}",
                        score=score,
                        keywords_matched=[kw],
                        narrative_category=_classify_keyword(kw),
                        raw={"keyword": kw, "trends_score": score, "timeframe": timeframe}
                    ))
                    log.info(f"Google Trends spike: '{kw}' = {score}/100")

            time.sleep(1.5)  # rate limit protection

        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "too many" in err_str.lower():
                log.debug("Google Trends: rate limit (429) — IP serwera zablokowany przez Google, pomijam")
            else:
                log.warning(f"Google Trends error dla chunk {chunk}: {e}")
            time.sleep(5)
            continue

    log.info(f"Google Trends: {len(signals)} spike'ów wykrytych")
    return signals


def check_token_trend(token_name: str, symbol: str) -> dict:
    """
    Sprawdź trend dla konkretnego tokenu.
    Zwraca {'score': int, 'is_spike': bool, 'related': list}
    """
    pt = _get_pytrends()
    if not pt:
        return {"score": 0, "is_spike": False, "related": []}

    threshold = cfg["google_trends"]["spike_threshold"]
    keywords = [token_name[:30], f"${symbol}"]  # max 30 znaków dla Trends

    try:
        pt.build_payload(keywords, timeframe="now 7-d", geo="")
        df = pt.interest_over_time()

        if df is None or df.empty:
            return {"score": 0, "is_spike": False, "related": []}

        # Sprawdź czy jest wzrost (ostatnie 24h vs poprzednie 6d)
        score = int(df[token_name[:30]].iloc[-1]) if token_name[:30] in df.columns else 0

        # Related queries — mogą wskazywać co ludzi interesuje w kontekście tokenu
        related_data = pt.related_queries()
        related = []
        for kw in keywords:
            if kw in related_data and related_data[kw].get("top") is not None:
                top_df = related_data[kw]["top"]
                if top_df is not None and not top_df.empty:
                    related.extend(top_df["query"].head(5).tolist())

        return {
            "score": score,
            "is_spike": score >= threshold,
            "related": related[:10]
        }
    except Exception as e:
        log.warning(f"Google Trends token check error dla {token_name}: {e}")
        return {"score": 0, "is_spike": False, "related": []}


def _classify_keyword(kw: str) -> str:
    """Przypisz keyword do kategorii narracyjnej."""
    kw_lower = kw.lower()
    for cat, keywords in cfg["narratives"]["keywords"].items():
        if any(k in kw_lower for k in keywords):
            return cat
    return "general_crypto"
