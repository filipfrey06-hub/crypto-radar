"""
Fetcher: RSS / CryptoPanic — news, ogłoszenia polityków, macro events.

Używa feedparser — brak klucza API.
Śledzi: CryptoPanic, CoinTelegraph, Decrypt, TheBlock
Szczególnie wykrywa: regulacje, ogłoszenia polityków, ETF news, CEX listings.
"""
import logging
import re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional

try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False

from .models import SocialSignal
from ..config import cfg

log = logging.getLogger(__name__)


def fetch_crypto_news(lookback_hours: int | None = None) -> list[SocialSignal]:
    """
    Pobierz wiadomości z RSS feedów zdefiniowanych w config.yaml.
    """
    if not FEEDPARSER_AVAILABLE:
        log.warning("feedparser nie jest zainstalowany — RSS fetcher wyłączony")
        return []

    rcfg = cfg["rss"]
    lookback_h = lookback_hours or rcfg["lookback_hours"]
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=lookback_h)).timestamp()
    politician_kw = [k.lower() for k in rcfg["politician_keywords"]]
    narrative_kw = cfg["narratives"]["keywords"]

    all_signals: list[SocialSignal] = []

    for feed_cfg in rcfg["feeds"]:
        try:
            feed = feedparser.parse(feed_cfg["url"])
            feed_name = feed_cfg["name"]
            count = 0

            for entry in feed.entries:
                # Parsuj datę publikacji
                pub_ts = _parse_entry_date(entry)
                if pub_ts and pub_ts < cutoff_ts:
                    continue

                title = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))
                text = f"{title} {summary}".lower()

                # Wykryj czy to political/regulatory news
                is_political = any(kw in text for kw in politician_kw)

                # Wykryj narracje
                matched_kw = []
                narrative_cat = "news"
                for cat, keywords in narrative_kw.items():
                    for kw in keywords:
                        if kw.lower() in text:
                            matched_kw.append(kw)
                            narrative_cat = cat
                            break

                # Priorytetyzuj political news
                if is_political:
                    narrative_cat = "Political"
                    matched_kw.extend([kw for kw in politician_kw if kw in text])

                # Filtruj tylko relevantne (krypto keywords lub political)
                crypto_terms = ["crypto", "bitcoin", "ethereum", "solana", "token", "defi", "nft", "blockchain", "altcoin", "memecoin"]
                if not is_political and not matched_kw and not any(t in text for t in crypto_terms):
                    continue

                signal = SocialSignal(
                    source=f"rss/{feed_name}",
                    title=title,
                    url=entry.get("link", ""),
                    content=summary[:500],
                    score=100 if is_political else 50,  # political = wyższy priorytet
                    keywords_matched=list(set(matched_kw))[:10],
                    narrative_category=narrative_cat,
                    raw={
                        "feed": feed_name,
                        "published": entry.get("published", ""),
                        "published_ts": pub_ts,
                        "is_political": is_political,
                        "author": entry.get("author", ""),
                    }
                )
                all_signals.append(signal)
                count += 1

            log.debug(f"RSS {feed_name}: {count} artykułów")

        except Exception as e:
            log.warning(f"RSS fetch error dla {feed_cfg.get('name', 'unknown')}: {e}")
            continue

    log.info(f"RSS: {len(all_signals)} sygnałów ze wszystkich feedów")
    return all_signals


def fetch_political_signals() -> list[SocialSignal]:
    """
    Specjalny fetch skupiony tylko na sygnałach politycznych/regulacyjnych.
    Używa szerszego okna czasowego (12h) i bardziej agresywnego keyword matchingu.
    """
    all_news = fetch_crypto_news(lookback_hours=12)
    political = [s for s in all_news if s.narrative_category == "Political" or s.raw.get("is_political")]

    if political:
        log.info(f"Znaleziono {len(political)} sygnałów politycznych!")
        for s in political:
            log.info(f"  POLITICAL: {s.title[:80]}")

    return political


def extract_mentioned_tokens(signals: list[SocialSignal]) -> list[str]:
    """
    Wyciągnij wzmiankowane ticki tokenów z RSS signals.
    Szuka wzorców $TICKER w tytułach i treści.
    """
    ticker_pattern = re.compile(r'\$([A-Z]{2,10})\b')
    tickers = []

    for sig in signals:
        text = f"{sig.title} {sig.content}"
        found = ticker_pattern.findall(text.upper())
        tickers.extend(found)

    # Deduplikuj i wyklucz znane stablecoiny/majory
    exclude = {"USD", "USDT", "USDC", "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOT"}
    return list(set(t for t in tickers if t not in exclude))


def _parse_entry_date(entry) -> Optional[float]:
    """Parsuj datę z feed entry → unix timestamp."""
    try:
        # feedparser usually fills published_parsed
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            import calendar
            return float(calendar.timegm(entry.published_parsed))

        # Fallback: spróbuj string
        date_str = entry.get("published", entry.get("updated", ""))
        if date_str:
            dt = parsedate_to_datetime(date_str)
            return dt.timestamp()
    except Exception:
        pass
    return None
