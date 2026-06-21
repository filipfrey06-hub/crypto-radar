"""
Wspólne modele danych używane przez wszystkie fetchery.
Każdy fetcher zwraca listę TokenCandidate lub SocialSignal.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TokenCandidate:
    """Kandydat tokenowy — wyjście fetchers, wejście do filtrów."""
    # Identyfikacja
    address: str                        # contract/mint address
    symbol: str
    name: str
    chain: str = "solana"

    # Cenowe / płynność
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h: float = 0.0
    market_cap_usd: float = 0.0
    fdv_usd: float = 0.0

    # Wolumen historyczny (do spike detection)
    volume_7d_avg: float = 0.0         # średnie volume/dzień z ostatnich 7d

    # On-chain
    holder_count: int = 0
    top10_holder_pct: float = 0.0      # % supply w top 10 walletach
    mint_disabled: Optional[bool] = None
    freeze_disabled: Optional[bool] = None
    lp_locked: Optional[bool] = None
    lp_lock_pct: float = 0.0           # % LP zablokowanego
    token_age_minutes: float = 0.0     # wiek tokenu w minutach

    # Pump.fun specific
    is_pump_fun: bool = False
    bonding_curve_pct: float = 0.0     # % wypełnienia bonding curve

    # Social
    reddit_mentions_24h: int = 0
    reddit_mentions_7d_avg: float = 0.0
    google_trends_score: int = 0       # 0-100

    # Metadata
    source: str = "unknown"            # skąd pochodzi kandydat
    source_url: str = ""
    extra: dict = field(default_factory=dict)  # dodatkowe dane specyficzne dla źródła

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class SocialSignal:
    """Sygnał społecznościowy — wyjście Reddit/Trends/RSS fetchera."""
    source: str                        # "reddit", "google_trends", "rss"
    title: str
    url: str = ""
    content: str = ""
    score: int = 0                     # upvotes/points/relevance
    mentions: int = 1
    keywords_matched: list = field(default_factory=list)
    narrative_category: str = "unknown"
    raw: dict = field(default_factory=dict)
