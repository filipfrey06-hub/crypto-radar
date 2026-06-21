"""
Fetcher: Reddit — mention spikes, nowe projekty, sentyment.

Używa PRAW (Python Reddit API Wrapper) z OAuth app credentials.
Utwórz aplikację na: https://www.reddit.com/prefs/apps (wybierz "script")

Zmienne środowiskowe:
  REDDIT_CLIENT_ID
  REDDIT_CLIENT_SECRET
  REDDIT_USER_AGENT
"""
import logging
import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import praw
    PRAW_AVAILABLE = True
except ImportError:
    PRAW_AVAILABLE = False

from .models import SocialSignal
from ..config import cfg, env

log = logging.getLogger(__name__)


def _get_reddit() -> Optional["praw.Reddit"]:
    if not PRAW_AVAILABLE:
        log.warning("praw nie jest zainstalowany — Reddit fetcher wyłączony")
        return None
    if not env.REDDIT_CLIENT_ID or not env.REDDIT_CLIENT_SECRET:
        log.warning("Brak REDDIT_CLIENT_ID / SECRET — Reddit fetcher wyłączony")
        return None
    return praw.Reddit(
        client_id=env.REDDIT_CLIENT_ID,
        client_secret=env.REDDIT_CLIENT_SECRET,
        user_agent=env.REDDIT_USER_AGENT,
    )


def fetch_hot_posts(lookback_hours: int | None = None) -> list[SocialSignal]:
    """
    Pobierz hot/new posty z krypto subreddits.
    Zwraca SocialSignal dla każdego pasującego postu.
    """
    reddit = _get_reddit()
    if not reddit:
        return []

    rcfg = cfg["reddit"]
    lookback_h = lookback_hours or rcfg["lookback_hours"]
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=lookback_h)).timestamp()
    subreddits = rcfg["subreddits"]
    narrative_kw = cfg["narratives"]["keywords"]

    signals: list[SocialSignal] = []

    for sub_name in subreddits:
        try:
            sub = reddit.subreddit(sub_name)

            for post in sub.new(limit=100):
                if post.created_utc < cutoff_ts:
                    continue
                if post.upvote_ratio < rcfg["min_upvote_ratio"]:
                    continue
                if post.score < rcfg["min_score"]:
                    continue

                text = f"{post.title} {post.selftext}".lower()
                matched_kw = []
                narrative_cat = "unknown"

                for cat, keywords in narrative_kw.items():
                    for kw in keywords:
                        if kw.lower() in text:
                            matched_kw.append(kw)
                            narrative_cat = cat
                            break

                # Zawsze dodaj jeśli to krypto subreddit (nawet bez keyword match)
                if not matched_kw and sub_name in ["solana", "CryptoMoonShots", "SatoshiStreetBets", "memecoin"]:
                    narrative_cat = "general_crypto"
                    matched_kw = ["subreddit:" + sub_name]

                signals.append(SocialSignal(
                    source=f"reddit/{sub_name}",
                    title=post.title,
                    url=f"https://reddit.com{post.permalink}",
                    content=post.selftext[:500],
                    score=post.score,
                    mentions=1,
                    keywords_matched=matched_kw,
                    narrative_category=narrative_cat,
                    raw={
                        "subreddit": sub_name,
                        "author": str(post.author),
                        "num_comments": post.num_comments,
                        "upvote_ratio": post.upvote_ratio,
                        "created_utc": post.created_utc,
                        "flair": post.link_flair_text,
                    }
                ))

        except Exception as e:
            log.warning(f"Reddit fetch error dla r/{sub_name}: {e}")
            continue

    log.info(f"Reddit: {len(signals)} sygnałów z {len(subreddits)} subreddits")
    return signals


def count_token_mentions(token_name: str, symbol: str, lookback_hours: int = 24) -> dict:
    """
    Zlicz wzmianki konkretnego tokenu na Reddit.
    Zwraca {'mentions_24h': int, 'top_posts': list}
    """
    reddit = _get_reddit()
    if not reddit:
        return {"mentions_24h": 0, "top_posts": []}

    cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp()
    search_terms = [token_name.lower(), f"${symbol.lower()}", symbol.lower()]
    mentions = 0
    top_posts = []

    try:
        # Szukaj w top krypto subredditach
        combined = reddit.subreddit("+".join(cfg["reddit"]["subreddits"]))
        for post in combined.search(f"{token_name} OR ${symbol}", time_filter="day", limit=50):
            if post.created_utc < cutoff_ts:
                continue
            text = f"{post.title} {post.selftext}".lower()
            if any(term in text for term in search_terms):
                mentions += 1
                if len(top_posts) < 5:
                    top_posts.append({
                        "title": post.title,
                        "score": post.score,
                        "url": f"https://reddit.com{post.permalink}",
                    })
    except Exception as e:
        log.warning(f"Reddit mention count error dla {token_name}: {e}")

    return {"mentions_24h": mentions, "top_posts": top_posts}


def extract_ticker_mentions(signals: list[SocialSignal]) -> Counter:
    """
    Z listy sygnałów Reddit wyciągnij tickers ($SOL, $PEPE itd.) i policz wzmianki.
    Pomocne do wykrywania emerging tickers bez wcześniejszej listy.
    """
    ticker_pattern = re.compile(r'\$([A-Z]{2,10})\b')
    counter: Counter = Counter()

    for sig in signals:
        text = f"{sig.title} {sig.content}"
        tickers = ticker_pattern.findall(text.upper())
        counter.update(tickers)

    return counter
