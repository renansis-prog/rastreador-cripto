"""
Rastreador de Criptos — triagem por tendência de preço.

Fonte dos dados: CoinGecko (preço médio de todas as corretoras, market cap e ranking).
A Binance entra só como filtro: analisamos apenas moedas com par em USDT na Binance,
porque é lá que a compra é feita.

Para cada moeda calcula:
  - retornos de 7, 30 e 90 dias e força relativa contra o BTC
  - médias móveis de 20, 50 e 200 dias e a inclinação da MM50
  - RSI de 14 dias (alerta de sobrecompra/sobrevenda)
e dá uma nota de tendência de 0 a 100. Também classifica o "clima" do mercado pelo BTC.

Saída: docs/data.json (lido pelo painel docs/index.html no GitHub Pages).
Chave da API: variável de ambiente COINGECKO_API_KEY (plano Demo, gratuito).
Só usa a biblioteca padrão do Python.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------- Configuração ----------------
API = "https://api.coingecko.com/api/v3"
CHAVE = os.environ.get("COINGECKO_API_KEY", "").strip()
PAUSA = 2.5 if CHAVE else 6.0       # segundos entre chamadas (limite do plano gratuito)

TOP_MARKET_CAP = 300                # só moedas entre as 300 maiores por market cap
VOLUME_MINIMO_USD = 10_000_000      # volume mínimo em 24h (todas as corretoras)
MAX_MOEDAS = 120                    # analisa as N maiores que passaram nos filtros
DIAS_HISTORICO = 220                # precisa de 200+ para a MM200
SAIDA = Path(__file__).parent / "docs" / "data.json"

STABLES = {
    "usdt", "usdc", "fdusd", "tusd", "busd", "dai", "usdp", "usdd", "pyusd", "usde", "usd1",
    "eur", "euri", "aeur", "eurc", "brl", "try", "gbp", "xusd", "bfusd", "rlusd", "usds",
    "susde", "frax", "lusd", "gusd", "paxg", "xaut", "wbtc", "weth", "steth", "wsteth",
    "bnsol", "wbeth", "cbbtc",
}


# ---------------- Acesso à API ----------------
def get_json(caminho: str, params: dict | None = None):
    url = API + caminho + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"accept": "application/json", "User-Agent": "rastreador-cripto/2.0"}
    if CHAVE:
        headers["x-cg-demo-api-key"] = CHAVE
    ultimo_erro = None
    for tentativa in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                dados = json.loads(r.read())
            time.sleep(PAUSA)
            return dados
        except urllib.error.HTTPError as e:
            ultimo_erro = f"HTTP {e.code}"
            if e.code in (401, 403):
                raise RuntimeError("CoinGecko recusou a chave (HTTP %s). Confira o secret COINGECKO_API_KEY." % e.code)
            espera = 65 if e.code == 429 else 5 * (tentativa + 1)   # 429 = limite por minuto
        except Exception as e:  # noqa: BLE001
            ultimo_erro, espera = e, 5 * (tentativa + 1)
        time.sleep(espera)
    raise RuntimeError(f"Falha ao acessar {caminho}: {ultimo_erro}")


def moedas_na_binance() -> set[str]:
    """IDs do CoinGecko das moedas com par em USDT na Binance."""
    ids, pagina = set(), 1
    while pagina <= 40:
        lote = get_json("/exchanges/binance/tickers", {"page": pagina}).get("tickers", [])
        if not lote:
            break
        for t in lote:
            if t.get("target") == "USDT" and t.get("coin_id"):
                ids.add(t["coin_id"])
        pagina += 1
    return ids


def candidatas(na_binance: set[str]):
    mercado = []
    for pagina in range(1, (TOP_MARKET_CAP - 1) // 250 + 2):
        mercado += get_json("/coins/markets", {
            "vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": pagina,
            "price_change_percentage": "24h",
        })
    lista = []
    for m in mercado:
        rank = m.get("market_cap_rank")
        if not rank or rank > TOP_MARKET_CAP:
            continue
        if m["id"] not in na_binance or m["symbol"].lower() in STABLES:
            continue
        if (m.get("total_volume") or 0) < VOLUME_MINIMO_USD:
            continue
        lista.append({
            "id": m["id"], "moeda": m["symbol"].upper(), "nome": m["name"],
            "rank": rank, "market_cap": m.get("market_cap"),
            "volume_24h": m.get("total_volume"), "var_24h": m.get("price_change_percentage_24h"),
        })
    lista.sort(key=lambda x: x["rank"])
    return lista[:MAX_MOEDAS]


def fechamentos(coin_id: str):
    d = get_json(f"/coins/{coin_id}/market_chart",
                 {"vs_currency": "usd", "days": DIAS_HISTORICO, "interval": "daily"})
    precos = [p[1] for p in d.get("prices", []) if p[1]]
    # um preço por dia; o último ponto é o preço atual
    return precos


# ---------------- Indicadores ----------------
def media(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def retorno(v, dias):
    if len(v) <= dias:
        return None
    return (v[-1] / v[-1 - dias] - 1) * 100


def rsi(v, n=14):
    if len(v) <= n:
        return None
    ganhos = perdas = 0.0
    for i in range(1, n + 1):
        d = v[i] - v[i - 1]
        ganhos += max(d, 0)
        perdas += max(-d, 0)
    mg, mp = ganhos / n, perdas / n
    for i in range(n + 1, len(v)):
        d = v[i] - v[i - 1]
        mg = (mg * (n - 1) + max(d, 0)) / n
        mp = (mp * (n - 1) + max(-d, 0)) / n
    return 100.0 if mp == 0 else 100 - 100 / (1 + mg / mp)


PESOS = {
    "acima_mm20": 10, "acima_mm50": 15, "acima_mm200": 20, "mm50_acima_mm200": 15,
    "mm50_subindo": 10, "alta_30d": 10, "alta_90d": 5, "forte_vs_btc": 15,
}


def analisar(closes, btc_ret30=None):
    """Indicadores e nota de tendência (0–100). None se histórico insuficiente."""
    if len(closes) < 60:
        return None
    p = closes[-1]
    mm20, mm50, mm200 = media(closes, 20), media(closes, 50), media(closes, 200)
    mm50_10d = media(closes[:-10], 50)
    r7, r30, r90 = retorno(closes, 7), retorno(closes, 30), retorno(closes, 90)
    vs_btc = None if (btc_ret30 is None or r30 is None) else r30 - btc_ret30

    criterios = {
        "acima_mm20": p > mm20,
        "acima_mm50": p > mm50,
        "acima_mm200": mm200 is not None and p > mm200,
        "mm50_acima_mm200": mm200 is not None and mm50 > mm200,
        "mm50_subindo": mm50_10d is not None and mm50 > mm50_10d,
        "alta_30d": r30 is not None and r30 > 0,
        "alta_90d": r90 is not None and r90 > 0,
        # o próprio BTC não tem com quem comparar: vale a alta de 30 dias
        "forte_vs_btc": (r30 is not None and r30 > 0) if btc_ret30 is None else (vs_btc is not None and vs_btc > 0),
    }
    nota = sum(PESOS[c] for c, ok in criterios.items() if ok)
    classe = "Alta forte" if nota >= 80 else "Alta" if nota >= 60 else "Lateral" if nota >= 40 else "Baixa"

    return {
        "preco": p,
        "ret_7d": r7, "ret_30d": r30, "ret_90d": r90, "vs_btc_30d": vs_btc,
        "mm20": mm20, "mm50": mm50, "mm200": mm200,
        "dist_mm200": (p / mm200 - 1) * 100 if mm200 else None,
        "rsi14": rsi(closes),
        "historico_curto": mm200 is None,
        "criterios": criterios,
        "nota": nota,
        "classe": classe,
        "sparkline": [float(f"{c:.6g}") for c in closes[-90:]],
    }


def clima_do_mercado(btc_closes):
    """Mercado em alta quando o BTC está acima da MM200 e a MM50 está acima da MM200."""
    p, mm50, mm200 = btc_closes[-1], media(btc_closes, 50), media(btc_closes, 200)
    if mm200 is None:
        return {"estado": "indefinido"}
    acima, cruz = p > mm200, mm50 > mm200
    estado = "alta" if acima and cruz else "baixa" if not acima and not cruz else "transicao"
    return {"estado": estado, "btc_acima_mm200": acima, "btc_mm50_acima_mm200": cruz,
            "btc_dist_mm200": (p / mm200 - 1) * 100}


# ---------------- Execução ----------------
def main():
    print("Chave CoinGecko:", "configurada" if CHAVE else "AUSENTE (modo lento, sem chave)")
    na_binance = moedas_na_binance()
    print(f"{len(na_binance)} moedas com par USDT na Binance")
    lista = candidatas(na_binance)
    print(f"{len(lista)} moedas passaram nos filtros (top {TOP_MARKET_CAP}, volume, sem stablecoins)")
    if not lista:
        raise SystemExit("Nenhuma moeda passou nos filtros — algo errado na coleta.")

    btc = fechamentos("bitcoin")
    btc_ret30 = retorno(btc, 30)
    clima = clima_do_mercado(btc)

    resultado = []
    for i, m in enumerate(lista, 1):
        try:
            closes = btc if m["id"] == "bitcoin" else fechamentos(m["id"])
            a = analisar(closes, None if m["id"] == "bitcoin" else btc_ret30)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {m['moeda']}: {e}")
            continue
        if a:
            resultado.append({**m, **a})
        if i % 20 == 0:
            print(f"  {i}/{len(lista)}")

    if len(resultado) < len(lista) // 2:
        raise SystemExit(f"Só {len(resultado)} de {len(lista)} moedas analisadas — dados não salvos.")

    resultado.sort(key=lambda x: (x["nota"], x["ret_30d"] or 0), reverse=True)
    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps({
        "atualizado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fonte": "CoinGecko (filtrado por moedas com par USDT na Binance)",
        "parametros": {"top_market_cap": TOP_MARKET_CAP, "volume_minimo_usd": VOLUME_MINIMO_USD,
                       "max_moedas": MAX_MOEDAS, "pesos": PESOS},
        "mercado": clima,
        "moedas": resultado,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"OK: {len(resultado)} moedas salvas em {SAIDA}")


if __name__ == "__main__":
    main()
