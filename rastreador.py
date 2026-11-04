"""
Rastreador de Criptos — triagem por tendência de preço (dados públicos da Binance).

Para cada par em USDT com liquidez mínima, baixa ~220 candles diários e calcula:
  - retornos de 7, 30 e 90 dias
  - médias móveis de 20, 50 e 200 dias e a inclinação da MM50
  - RSI de 14 dias (só como alerta de sobrecompra/sobrevenda)
e dá uma nota de tendência de 0 a 100.

Saída: docs/data.json (lido pelo painel docs/index.html no GitHub Pages).
Só usa a biblioteca padrão do Python.
"""
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------- Configuração ----------------
BASES = [
    "https://data-api.binance.vision",   # espelho de dados públicos (funciona no GitHub Actions)
    "https://api.binance.com",
]
QUOTE = "USDT"
VOLUME_MINIMO_USDT = 10_000_000   # volume mínimo nas últimas 24h
MAX_MOEDAS = 120                   # analisa as N mais negociadas que passaram no filtro
CANDLES = 220                      # dias de histórico (precisa de 200+ para a MM200)
SAIDA = Path(__file__).parent / "docs" / "data.json"

STABLES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD", "PYUSD", "USDE", "USD1",
    "EUR", "EURI", "AEUR", "BRL", "TRY", "GBP", "XUSD", "BFUSD", "RLUSD", "PAXG",
}
SUFIXOS_ALAVANCADOS = ("UP", "DOWN", "BULL", "BEAR")


# ---------------- Acesso à API ----------------
def get_json(caminho: str):
    ultimo_erro = None
    for base in BASES:
        for tentativa in range(3):
            try:
                req = urllib.request.Request(base + caminho, headers={"User-Agent": "rastreador-cripto/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read())
            except Exception as e:  # noqa: BLE001
                ultimo_erro = e
                time.sleep(1.5 * (tentativa + 1))
    raise RuntimeError(f"Falha ao acessar {caminho}: {ultimo_erro}")


def candidatas():
    tickers = get_json("/api/v3/ticker/24hr")
    lista = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith(QUOTE):
            continue
        base = s[: -len(QUOTE)]
        if base in STABLES or base.endswith(SUFIXOS_ALAVANCADOS):
            continue
        vol = float(t["quoteVolume"])
        if vol < VOLUME_MINIMO_USDT or float(t["lastPrice"]) <= 0:
            continue
        lista.append({"simbolo": s, "moeda": base, "volume_24h": vol,
                      "var_24h": float(t["priceChangePercent"])})
    lista.sort(key=lambda x: x["volume_24h"], reverse=True)
    return lista[:MAX_MOEDAS]


def fechamentos(simbolo: str):
    k = get_json(f"/api/v3/klines?symbol={simbolo}&interval=1d&limit={CANDLES}")
    # remove o candle do dia corrente (ainda aberto)
    agora_ms = time.time() * 1000
    return [float(c[4]) for c in k if c[6] < agora_ms]


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
    ganhos, perdas = 0.0, 0.0
    for i in range(1, n + 1):
        d = v[i] - v[i - 1]
        ganhos += max(d, 0)
        perdas += max(-d, 0)
    mg, mp = ganhos / n, perdas / n
    for i in range(n + 1, len(v)):
        d = v[i] - v[i - 1]
        mg = (mg * (n - 1) + max(d, 0)) / n
        mp = (mp * (n - 1) + max(-d, 0)) / n
    if mp == 0:
        return 100.0
    return 100 - 100 / (1 + mg / mp)


def analisar(closes):
    """Devolve indicadores e nota de tendência (0–100). None se histórico insuficiente."""
    if len(closes) < 60:
        return None
    p = closes[-1]
    mm20, mm50, mm200 = media(closes, 20), media(closes, 50), media(closes, 200)
    mm50_10d = media(closes[:-10], 50)
    r7, r30, r90 = retorno(closes, 7), retorno(closes, 30), retorno(closes, 90)

    criterios = {
        "acima_mm20": p > mm20,                                   # 15
        "acima_mm50": p > mm50,                                   # 20
        "acima_mm200": mm200 is not None and p > mm200,           # 20
        "mm50_acima_mm200": mm200 is not None and mm50 > mm200,   # 15
        "mm50_subindo": mm50_10d is not None and mm50 > mm50_10d, # 10
        "alta_30d": r30 is not None and r30 > 0,                  # 10
        "alta_90d": r90 is not None and r90 > 0,                  # 10
    }
    pesos = {"acima_mm20": 15, "acima_mm50": 20, "acima_mm200": 20, "mm50_acima_mm200": 15,
             "mm50_subindo": 10, "alta_30d": 10, "alta_90d": 10}
    nota = sum(pesos[c] for c, ok in criterios.items() if ok)

    if nota >= 80:
        classe = "Alta forte"
    elif nota >= 60:
        classe = "Alta"
    elif nota >= 40:
        classe = "Lateral"
    else:
        classe = "Baixa"

    r = rsi(closes)
    return {
        "preco": p,
        "ret_7d": r7, "ret_30d": r30, "ret_90d": r90,
        "mm20": mm20, "mm50": mm50, "mm200": mm200,
        "dist_mm200": (p / mm200 - 1) * 100 if mm200 else None,
        "rsi14": r,
        "historico_curto": mm200 is None,
        "criterios": criterios,
        "nota": nota,
        "classe": classe,
        "sparkline": [round(c, 10) for c in closes[-90:]],
    }


# ---------------- Execução ----------------
def main():
    lista = candidatas()
    print(f"{len(lista)} pares passaram no filtro de volume")
    resultado = []
    for i, m in enumerate(lista, 1):
        try:
            a = analisar(fechamentos(m["simbolo"]))
        except Exception as e:  # noqa: BLE001
            print(f"  ! {m['simbolo']}: {e}")
            continue
        if a:
            resultado.append({**m, **a})
        if i % 20 == 0:
            print(f"  {i}/{len(lista)}")
        time.sleep(0.1)

    resultado.sort(key=lambda x: (x["nota"], x["ret_30d"] or 0), reverse=True)
    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps({
        "atualizado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parametros": {"volume_minimo_usdt": VOLUME_MINIMO_USDT, "max_moedas": MAX_MOEDAS},
        "moedas": resultado,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"OK: {len(resultado)} moedas salvas em {SAIDA}")


if __name__ == "__main__":
    main()
