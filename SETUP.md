# Crypto Radar — Instrukcja setup

## Co to jest

Agent monitorujący rynek krypto/Solana, który:
- Skanuje nowe tokeny (DexScreener, Pump.fun) co 30 minut
- Monitoruje Reddit, Google Trends, RSS news co godzinę
- Filtruje rug-pully (mint/freeze/LP safety check)
- Ocenia narracje przez Claude AI
- Wysyła alerty na Telegram + ntfy.sh (push)

**NIE kupuje ani nie sprzedaje automatycznie.** Tylko wykrywa i alarmuje.

---

## Setup krok po kroku

### 1. Stwórz Telegram bota

1. Otwórz Telegram, znajdź `@BotFather`
2. Wyślij `/newbot`, podaj nazwę i username (np. `CryptoRadarBot`)
3. Zapisz token (`123456:ABC-DEF...`)
4. Znajdź swój Chat ID: wyślij wiadomość do bota, potem otwórz `https://api.telegram.org/bot<TOKEN>/getUpdates` — szukaj `"chat":{"id":XXXXXX}`

### 2. Skonfiguruj ntfy.sh

1. Pobierz aplikację ntfy na telefon: https://ntfy.sh/
2. Wybierz prywatną nazwę tematu (np. `crypto-radar-filipfrey-2024` — coś losowego)
3. Zasubskrybuj ten temat w aplikacji

### 3. Utwórz Reddit API app

1. Idź na https://www.reddit.com/prefs/apps
2. Kliknij "create another app"
3. Wybierz "script", podaj dowolną nazwę
4. `redirect uri` = `http://localhost:8080`
5. Skopiuj `client_id` (pod nazwą app) i `client_secret`

### 4. Klucz Claude API

1. Idź na https://console.anthropic.com/
2. API Keys → Create Key
3. Skopiuj klucz (zaczyna się od `sk-ant-`)

### 5. Stwórz repozytorium GitHub

```bash
git init
git add .
git commit -m "Initial: Crypto Radar v2"
git remote add origin https://github.com/TWOJ_NICK/crypto-radar.git
git push -u origin main
```

### 6. Dodaj GitHub Secrets

Idź do repo → Settings → Secrets and variables → Actions → New repository secret

Dodaj te secrety:
| Secret | Wartość |
|--------|---------|
| `TELEGRAM_BOT_TOKEN` | Token od BotFather |
| `TELEGRAM_CHAT_ID` | Twój Chat ID |
| `NTFY_TOPIC` | Nazwa tematu ntfy.sh |
| `ANTHROPIC_API_KEY` | Klucz Claude API |
| `REDDIT_CLIENT_ID` | ID Reddit app |
| `REDDIT_CLIENT_SECRET` | Secret Reddit app |
| `REDDIT_USER_AGENT` | `crypto-radar:v2.0 (by u/TwojNick)` |

### 7. Aktywuj GitHub Actions

Idź do repo → Actions → włącz workflows (jeśli pytają o zgodę).

Możesz też ręcznie triggerować każdy workflow przez "Run workflow" żeby przetestować.

---

## Test lokalny (opcjonalny)

```bash
cd crypto-radar
pip install -r requirements.txt
cp .env.example .env
# uzupełnij .env swoimi danymi

python scripts/verify_setup.py    # sprawdź wszystko
python scripts/test_alerts.py     # wyślij testowe alerty
python scripts/fast_scan.py       # uruchom ręcznie
```

---

## Dostosowanie config.yaml

Najważniejsze progi do dostrojenia:

```yaml
safety:
  min_liquidity_usd: 30000     # Zmniejsz (np. 10k) jeśli chcesz więcej early calls
                                # Zwiększ (np. 50k) jeśli za dużo rug-pullów przechodzi

activity:
  volume_spike_multiplier: 3.0  # 2.0 = więcej alertów, 5.0 = mniej ale pewniejsze
  min_score_for_hit: 2          # 1 = bardzo czuły, 3 = konserwatywny

general:
  dry_run: true                 # Ustaw true żeby testować bez wysyłania alertów
```

---

## Jak działają priorytety alertów

- 🔴 **HIGH** → pełny alert Telegram + push ntfy.sh na telefon (natychmiastowy)
- 🟡 **MED** → alert Telegram (bez push)
- ⚪ **LOW** → tylko w dziennym digestcie (8:00 rano)

Cooldown (ten sam token nie będzie alertowany ponownie przez):
- HIGH: 12 godzin
- MED: 24 godziny
- LOW: 48 godzin

---

## Harmonogram skanów

| Job | Częstotliwość | Co robi |
|-----|---------------|---------|
| `fast-scan` | co 30 min | Nowe tokeny Solana, Pump.fun, volume spikes |
| `social-scan` | co 1h | Reddit, Google Trends, RSS news, political signals |
| `whale-scan` | co 1h | Duże transakcje (whale movements) |
| `narrative-scan` | co 6h | Szersze altcoiny, wszystkie narracje |
| `daily-digest` | 8:00 rano | Statystyki + LOW sygnały z nocy |

Szacowane zużycie GitHub Actions free tier: ~1400 min/mies. (limit = 2000 dla public repo).

---

## Rozwiązywanie problemów

**Bot nie wysyła alertów:**
- Sprawdź GitHub Actions → zakładka "Actions" czy joby nie failują
- Sprawdź czy wszystkie Secrets są ustawione
- Uruchom `python scripts/test_alerts.py` lokalnie

**Za dużo fałszywych alertów:**
- Zwiększ `volume_spike_multiplier` z 3.0 na 4.0-5.0
- Zwiększ `min_score_for_hit` z 2 na 3
- Zwiększ `min_liquidity_usd`

**Za mało alertów:**
- Zmniejsz `min_score_for_hit` do 1
- Zmniejsz `min_liquidity_usd`
- Dodaj więcej narracji do `config.yaml → narratives.keywords`

**GitHub Actions przekracza limit minut:**
- Wyłącz `whale-scan` workflow (najmniej krytyczny)
- Zmień `fast-scan` z co 30 min na co 1h (cron: `0 * * * *`)
