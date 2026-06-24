"""
Fetcher: MetaDAO — propozycje (futarchy), aktywność on-chain.

MetaDAO używa futarchy: zamiast głosowania użytkownicy kupują/sprzedają
tokeny warunkowe (pass/fail) dla każdej propozycji. Wynik rynkowy decyduje.

Strategia pobrania propozycji:
  1. Próba MetaDAO public API (api.metadao.fi)
  2. Próba futarchy.metadao.fi/api
  3. Fallback: Solana RPC → getProgramAccounts dla Autocrat program
     (zwraca liczbę propozycji, bez szczegółów)

Program Autocrat: autoQP9RmUNkzzKRc2U5LS6T4XHAkQDTBQ5u83CVXD6
"""
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ── Stałe ────────────────────────────────────────────────────────────────────

AUTOCRAT_PROGRAM  = "autoQP9RmUNkzzKRc2U5LS6T4XHAkQDTBQ5u83CVXD6"
SOLANA_RPC        = "https://api.mainnet-beta.solana.com"
HEADERS_JSON      = {"Content-Type": "application/json", "User-Agent": "crypto-radar/2.0"}
HEADERS_GET       = {"Accept": "application/json",       "User-Agent": "crypto-radar/2.0"}

# Możliwe URLe API MetaDAO (próbujemy po kolei)
METADAO_API_URLS = [
    "https://api.metadao.fi",
    "https://futarchy.metadao.fi/api",
    "https://meta-dao-alpha.vercel.app/api",
]


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class MetaDAOProposal:
    """Pojedyncza propozycja MetaDAO."""
    proposal_id: str          # unikalny identyfikator
    title: str = ""
    description: str = ""
    state: str = ""           # pending | active | passed | failed | cancelled
    pass_price: float = 0.0   # cena tokenu warunkowego PASS
    fail_price: float = 0.0   # cena tokenu warunkowego FAIL
    created_at: int = 0       # unix timestamp
    ends_at: int = 0          # unix timestamp
    url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class MetaDAOStatus:
    """Pełny status MetaDAO pobierany co godzinę."""
    proposals: list[MetaDAOProposal] = field(default_factory=list)
    active_count: int = 0
    total_count: int = 0
    data_source: str = ""     # "api" | "rpc" | "unavailable"
    ok: bool = False
    fetched_at: int = field(default_factory=lambda: int(time.time()))


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 10) -> Optional[dict | list]:
    try:
        r = requests.get(url, headers=HEADERS_GET, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.debug(f"GET {url} failed: {e}")
        return None


def _solana_rpc(method: str, params: list, timeout: int = 15) -> Optional[dict]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = requests.post(SOLANA_RPC, json=payload, headers=HEADERS_JSON, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.debug(f"Solana RPC {method} failed: {e}")
        return None


# ── Strategia 1: MetaDAO REST API ────────────────────────────────────────────

def _try_metadao_api() -> Optional[list[MetaDAOProposal]]:
    """Próbuj pobrać propozycje z publicznego API MetaDAO."""
    for base_url in METADAO_API_URLS:
        for endpoint in ["/proposals", "/dao/proposals", "/v1/proposals"]:
            data = _get(f"{base_url}{endpoint}", timeout=8)
            if data and isinstance(data, (list, dict)):
                proposals = _parse_api_proposals(data)
                if proposals is not None:
                    log.info(f"MetaDAO API OK: {base_url}{endpoint} — {len(proposals)} propozycji")
                    return proposals

    log.debug("MetaDAO REST API niedostępne — przechodzę do Solana RPC")
    return None


def _parse_api_proposals(data) -> Optional[list[MetaDAOProposal]]:
    """Parsuj odpowiedź API (różne formaty) → lista MetaDAOProposal."""
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ["proposals", "data", "items", "results"]:
            if key in data and isinstance(data[key], list):
                items = data[key]
                break
        else:
            return None  # nieznany format

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            proposal = MetaDAOProposal(
                proposal_id  = str(item.get("id", item.get("proposalId", item.get("pubkey", "?")))),
                title        = str(item.get("title", item.get("description", ""))),
                description  = str(item.get("description", item.get("details", ""))),
                state        = str(item.get("state", item.get("status", ""))).lower(),
                pass_price   = float(item.get("passPrice", item.get("pass_price", 0)) or 0),
                fail_price   = float(item.get("failPrice", item.get("fail_price", 0)) or 0),
                created_at   = int(item.get("createdAt", item.get("created_at", 0)) or 0),
                ends_at      = int(item.get("endsAt", item.get("ends_at", item.get("endTime", 0))) or 0),
                url          = str(item.get("url", "")),
                raw          = item,
            )
            results.append(proposal)
        except Exception as e:
            log.debug(f"MetaDAO proposal parse error: {e}")
            continue

    return results if results else None


# ── Strategia 2: Solana RPC fallback ─────────────────────────────────────────

def _try_solana_rpc() -> Optional[list[MetaDAOProposal]]:
    """
    Policz propozycje przez Solana RPC.
    Nie znamy struktury kont, ale możemy policzyć wszystkie konta Autocrat
    i wykryć wzrost liczby (= nowa propozycja).
    """
    resp = _solana_rpc("getProgramAccounts", [
        AUTOCRAT_PROGRAM,
        {
            "encoding": "base64",
            "withContext": True,
            "dataSlice": {"offset": 0, "length": 8},  # tylko discriminator
        }
    ])

    if not resp or "result" not in resp:
        return None

    result = resp["result"]
    accounts = result.get("value", result) if isinstance(result, dict) else result
    if not isinstance(accounts, list):
        return None

    count = len(accounts)
    log.info(f"MetaDAO Solana RPC: {count} kont w programie Autocrat")

    # Nie możemy zdekodować szczegółów bez IDL, ale możemy zwrócić placeholder
    # z licznikiem do porównania w state.db
    if count > 0:
        return [MetaDAOProposal(
            proposal_id  = f"rpc_count_{count}",
            title        = f"MetaDAO: {count} kont w programie Autocrat",
            state        = "rpc_count",
            raw          = {"count": count, "source": "solana_rpc"},
        )]
    return []


# ── Strategia 3: Transakcje programu (podejście activity-based) ──────────────

def _get_recent_program_activity() -> int:
    """
    Sprawdź ile transakcji miał program Autocrat w ostatnich ~1h.
    Proxy dla aktywności MetaDAO.
    """
    resp = _solana_rpc("getSignaturesForAddress", [
        AUTOCRAT_PROGRAM,
        {"limit": 20}
    ])
    if not resp or "result" not in resp:
        return 0

    sigs = resp["result"]
    if not isinstance(sigs, list):
        return 0

    # Policz transakcje z ostatniej godziny
    cutoff = int(time.time()) - 3600
    recent = [s for s in sigs if isinstance(s, dict) and int(s.get("blockTime", 0) or 0) > cutoff]
    log.info(f"MetaDAO aktywność on-chain (Autocrat): {len(recent)} tx w ostatniej 1h")
    return len(recent)


# ── Główna funkcja ─────────────────────────────────────────────────────────────

def fetch_metadao_status() -> MetaDAOStatus:
    """
    Pobierz pełny status MetaDAO.
    Próbuje API → Solana RPC → zwraca MetaDAOStatus z dostępnymi danymi.
    """
    # Próba 1: REST API
    proposals = _try_metadao_api()
    if proposals is not None:
        active = [p for p in proposals if p.state in ("active", "pending", "open", "voting")]
        # Wzbogać URL propozycji jeśli nie wypełniony
        for p in proposals:
            if not p.url and p.proposal_id not in ("?", ""):
                p.url = f"https://futarchy.metadao.fi/proposals/{p.proposal_id}"

        return MetaDAOStatus(
            proposals    = proposals,
            active_count = len(active),
            total_count  = len(proposals),
            data_source  = "api",
            ok           = True,
        )

    # Próba 2: Solana RPC (tylko licznik + activity)
    rpc_proposals = _try_solana_rpc()
    activity_count = _get_recent_program_activity()

    if rpc_proposals is not None:
        # Dodaj info o aktywności
        if rpc_proposals:
            rpc_proposals[0].raw["recent_txns_1h"] = activity_count

        return MetaDAOStatus(
            proposals    = rpc_proposals,
            active_count = 0,
            total_count  = rpc_proposals[0].raw.get("count", 0) if rpc_proposals else 0,
            data_source  = "rpc",
            ok           = True,
        )

    # Próba 3: Tylko aktywność programu
    if activity_count > 0:
        return MetaDAOStatus(
            proposals    = [],
            active_count = 0,
            total_count  = 0,
            data_source  = "activity_only",
            ok           = True,
        )

    log.warning("MetaDAO: wszystkie metody pobierania danych zawiodły")
    return MetaDAOStatus(ok=False, data_source="unavailable")


def get_proposals_url() -> str:
    """Zwróć URL strony propozycji MetaDAO."""
    return "https://futarchy.metadao.fi/proposals"
