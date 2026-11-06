"""
Backtest da nota do Rastreador de Criptos.

Pergunta: comprar as moedas de nota alta teria rendido mais que segurar Bitcoin?

Como simula:
  - Histórico diário de até ~1000 dias (Binance; se falhar, CoinGecko com 365 dias).
  - Universo: as moedas do painel atual (docs/data.json); sem ele, os pares USDT mais negociados.
  - Toda semana recalcula a nota de cada moeda usando SÓ os dados disponíveis até aquele dia
    (mesma função do rastreador) e remonta a carteira com pesos iguais.
  - Taxa de 0,1% por operação (taxa padrão da Binance), cobrada sobre o valor girado.

Estratégias comparadas:
  1. BTC: comprar e segurar.
  2. Todas as moedas: média simples do universo, rebalanceada toda semana.
  3. Top 10 da nota.
  4. Top 10 + clima: só investe quando o BTC está em alta (acima da MM200 e MM50 > MM200); senão, caixa.
  5. Top 10 + clima + saída: igual à 4, mas vende no dia em que a moeda fecha abaixo da MM50.

Também mede, por faixa de nota, quanto as moedas renderam nos 7 e 30 dias seguintes.

Saída: docs/backtest.json (lido por docs/backtest.html).
"""
import json
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import rastreador as R

AQUI = Path(__file__).parent
SAIDA = AQUI / "docs" / "backtest.json"
BINANCE = "https://data-api.binance.vision"
TOP_N = 10
REBALANCEAR_A_CADA = 7      # dias
TAXA = 0.001                # 0,1% por operação
AQUECIMENTO = 200           # dias antes do início do teste (para a MM200 do BTC)
MAX_UNIVERSO = 120


# ---------------- Dados ----------------
def http_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "rastreador-cripto/2.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def dia(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def binance_hist(simbolo):
    k = http_json(f"{BINANCE}/api/v3/klines?symbol={simbolo}&interval=1d&limit=1000")
    agora = time.time() * 1000
    return {dia(c[0]): float(c[4]) for c in k if c[6] < agora}


def coingecko_hist(coin_id):
    d = R.get_json(f"/coins/{coin_id}/market_chart", {"vs_currency": "usd", "days": 365, "interval": "daily"})
    out = {}
    for ms, p in d.get("prices", []):
        if p:
            out[dia(ms)] = p      # se houver dois pontos no mesmo dia, fica o último
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out.pop(hoje, None)           # tira o dia ainda aberto
    return out


def universo():
    """Lista de (moeda, coin_id) — do painel atual, se existir."""
    arq = AQUI / "docs" / "data.json"
    if arq.exists():
        try:
            moedas = json.loads(arq.read_text(encoding="utf-8")).get("moedas", [])
            if moedas:
                return [(m["moeda"], m.get("id")) for m in moedas][:MAX_UNIVERSO], "painel atual (CoinGecko)"
        except Exception:  # noqa: BLE001
            pass
    try:
        tickers = http_json(f"{BINANCE}/api/v3/ticker/24hr")
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"Binance indisponível ({e}) e o painel ainda não tem dados. "
                         "Rode primeiro o workflow 'Atualizar rastreador' e depois o Backtest.")
    stables = {s.upper() for s in R.STABLES}
    lista = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in stables or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        if float(t["quoteVolume"]) >= R.VOLUME_MINIMO_USD:
            lista.append((float(t["quoteVolume"]), base))
    lista.sort(reverse=True)
    return [(b, None) for _, b in lista[:MAX_UNIVERSO]], "pares USDT mais negociados na Binance"


def baixar_tudo():
    moedas, origem_universo = universo()
    try:
        btc = binance_hist("BTCUSDT")
        fonte, baixar = "Binance (até ~1000 dias)", lambda m, cid: binance_hist(m + "USDT")
    except Exception as e:  # noqa: BLE001
        print("Binance indisponível:", e, "→ usando CoinGecko (365 dias)")
        btc = coingecko_hist("bitcoin")
        fonte, baixar = "CoinGecko (365 dias)", lambda m, cid: coingecko_hist(cid) if cid else {}
    hist = {"BTC": btc}
    for i, (m, cid) in enumerate(moedas, 1):
        if m == "BTC":
            continue
        try:
            h = baixar(m, cid)
            if len(h) >= 60:
                hist[m] = h
        except Exception as e:  # noqa: BLE001
            print(f"  ! {m}: {e}")
        if fonte.startswith("Binance"):
            time.sleep(0.15)
        if i % 20 == 0:
            print(f"  {i}/{len(moedas)}")
    return hist, fonte, origem_universo


# ---------------- Simulação ----------------
def series_alinhadas(hist):
    datas = sorted(hist["BTC"])
    idx = {d: i for i, d in enumerate(datas)}
    s = {}
    for m, h in hist.items():
        arr = [None] * len(datas)
        for d, p in h.items():
            if d in idx:
                arr[idx[d]] = p
        s[m] = arr
    return datas, s


def ate(arr, i):
    """Preços contínuos até o dia i (inclusive), do primeiro dia com dado."""
    out = []
    for p in arr[: i + 1]:
        if p is None:
            if out:
                out.append(out[-1])
        else:
            out.append(p)
    return out


def preco(arr, i):
    return arr[i] if i < len(arr) else None


def metricas(curva, datas):
    v0, v1 = curva[0], curva[-1]
    anos = max(len(curva) - 1, 1) / 365
    pico, dd = v0, 0.0
    for v in curva:
        pico = max(pico, v)
        dd = min(dd, v / pico - 1)
    semanas = [curva[i + 7] / curva[i] - 1 for i in range(0, len(curva) - 7, 7)]
    return {
        "retorno_total": (v1 / v0 - 1) * 100,
        "retorno_anual": ((v1 / v0) ** (1 / anos) - 1) * 100 if anos >= 0.5 else None,
        "queda_maxima": dd * 100,
        "semanas_positivas": 100 * sum(1 for x in semanas if x > 0) / len(semanas) if semanas else None,
    }


def selecionar(modo, notas_dia, clima_dia, btc_dia):
    """Carteira-alvo num dia de rebalanceamento: {moeda: peso}. O que faltar para 100% fica em caixa."""
    if modo == "btc":
        return {"BTC": 1.0}
    if modo == "todas":
        return {m: 1 / len(notas_dia) for m in notas_dia} if notas_dia else {}
    if modo == "btc_tendencia":
        return {"BTC": 1.0} if btc_dia["acima_mm200"] else {}
    if modo in ("top", "top_mensal", "top_clima", "top_clima_saida"):
        if modo.startswith("top_clima") and clima_dia != "alta":
            return {}
        ordem = sorted(notas_dia.items(), key=lambda kv: (kv[1]["nota"], kv[1]["ret_30d"]), reverse=True)
        top = [m for m, _ in ordem[:TOP_N]]
        return {m: 1 / len(top) for m in top} if top else {}
    if modo in ("forte_btc", "nucleo"):
        r90_btc = btc_dia["ret_90d"]
        elegiveis = [
            (m, n) for m, n in notas_dia.items()
            if m != "BTC" and n["nota"] >= 60
            and n["vs_btc_30d"] is not None and n["vs_btc_30d"] > 0
            and n["ret_90d"] is not None and r90_btc is not None and n["ret_90d"] > r90_btc
        ]
        elegiveis.sort(key=lambda kv: (kv[1]["nota"], kv[1]["vs_btc_30d"]), reverse=True)
        vagas, base = (TOP_N, 0.0) if modo == "forte_btc" else (5, 0.5)
        escolhidas = [m for m, _ in elegiveis[:vagas]]
        pesos = {m: 0.1 for m in escolhidas}
        pesos["BTC"] = 1 - 0.1 * len(escolhidas)       # vagas vazias ficam em BTC
        if base:
            pesos["BTC"] = max(pesos["BTC"], base)
        return pesos
    raise ValueError(modo)


def simular(datas, S, notas_por_dia, clima_por_dia, btc_por_dia, mm50_por_dia, inicio, modo, periodo, saida=False):
    caixa, pos = 100.0, {}
    curva, ultimo_reb = [], None
    for i in range(inicio, len(datas)):
        # 1) valoriza as posições do dia anterior até hoje
        if i > inicio:
            for m in list(pos):
                p0, p1 = preco(S[m], i - 1), preco(S[m], i)
                if p0 and p1:
                    pos[m] *= p1 / p0
                elif p0 and not p1:          # moeda sumiu: vende ao último preço
                    caixa += pos.pop(m) * (1 - TAXA)
        total = caixa + sum(pos.values())

        # 2) rebalanceia nos dias marcados
        if ultimo_reb is None or i - ultimo_reb >= periodo:
            ultimo_reb = i
            pesos = selecionar(modo, notas_por_dia[i], clima_por_dia[i], btc_por_dia[i])
            alvo = {m: total * w for m, w in pesos.items() if w > 0}
            giro = sum(abs(alvo.get(m, 0) - pos.get(m, 0)) for m in set(alvo) | set(pos))
            fator = 1 - giro * TAXA / total if total else 1
            pos = {m: v * fator for m, v in alvo.items()}
            caixa = total * fator - sum(pos.values())

        # 3) regra de saída diária
        if saida:
            for m in list(pos):
                mm = mm50_por_dia[i].get(m)
                p = preco(S[m], i)
                if mm and p and p < mm:
                    caixa += pos.pop(m) * (1 - TAXA)
        curva.append(caixa + sum(pos.values()))
    return curva


def main():
    hist, fonte, origem_universo = baixar_tudo()
    print(f"{len(hist)} moedas com histórico · fonte: {fonte}")
    datas, S = series_alinhadas(hist)
    inicio = AQUECIMENTO if len(datas) > AQUECIMENTO + 60 else 60
    if len(datas) - inicio < 60:
        raise SystemExit("Histórico curto demais para um backtest útil.")

    # Pré-calcula, para cada dia do teste, nota de cada moeda, clima e MM50
    notas, clima, mm50, btc_info = {}, {}, {}, {}
    btc_all = S["BTC"]
    for i in range(inicio, len(datas)):
        btc = ate(btc_all, i)
        btc_r30 = R.retorno(btc, 30)
        clima[i] = R.clima_do_mercado(btc)["estado"]
        mm200_btc = R.media(btc, 200)
        btc_info[i] = {"ret_90d": R.retorno(btc, 90), "acima_mm200": mm200_btc is not None and btc[-1] > mm200_btc}
        notas[i], mm50[i] = {}, {}
        reb = (i - inicio) % REBALANCEAR_A_CADA == 0
        for m, arr in S.items():
            if arr[i] is None:
                continue
            c = ate(arr, i)
            if len(c) >= 50:
                mm50[i][m] = R.media(c, 50)
            if reb:
                a = R.analisar(c, None if m == "BTC" else btc_r30)
                if a:
                    notas[i][m] = {"nota": a["nota"], "ret_30d": a["ret_30d"] or 0, "classe": a["classe"],
                                   "vs_btc_30d": a["vs_btc_30d"], "ret_90d": a["ret_90d"]}
        # nos dias sem rebalanceamento a simulação não lê as notas

    # (id, nome, como funciona, rebalancear a cada N dias, regra de saída na MM50)
    estrategias = [
        ("btc", "BTC (comprar e segurar)", "Compra Bitcoin no início e não mexe mais.", REBALANCEAR_A_CADA, False),
        ("todas", "Todas as moedas (média)", "Pesos iguais em todas as moedas do universo, rebalanceado toda semana.", REBALANCEAR_A_CADA, False),
        ("top", f"Top {TOP_N} da nota", f"As {TOP_N} moedas de maior nota, trocadas toda semana.", REBALANCEAR_A_CADA, False),
        ("top_clima_saida", f"Top {TOP_N} + clima + saída na MM50", "Igual ao Top 10, mas só investe com o BTC em alta e vende a moeda que fecha abaixo da MM50.", REBALANCEAR_A_CADA, True),
        ("btc_tendencia", "BTC só em tendência", "Fica em Bitcoin quando ele está acima da MM200; abaixo, fica em caixa. Checado toda semana.", REBALANCEAR_A_CADA, False),
        ("forte_btc", "Força vs BTC obrigatória", "Até 10 altcoins com nota 60+ que superam o BTC em 30 e 90 dias, 10% cada; vagas vazias ficam em BTC.", REBALANCEAR_A_CADA, False),
        ("nucleo", "Núcleo BTC + satélites", "50% sempre em BTC; o resto em até 5 altcoins do critério anterior (10% cada).", REBALANCEAR_A_CADA, False),
        ("top_mensal", f"Top {TOP_N} mensal", f"As {TOP_N} de maior nota, trocadas a cada 4 semanas.", 4 * REBALANCEAR_A_CADA, False),
    ]
    curvas, tabela = {}, []
    for k, nome, regra, periodo, saida in estrategias:
        c = simular(datas, S, notas, clima, btc_info, mm50, inicio, k, periodo, saida)
        curvas[k] = c
        tabela.append({"id": k, "nome": nome, "regra": regra, **metricas(c, datas)})
        print(f"  {nome}: {tabela[-1]['retorno_total']:.1f}%  queda máx {tabela[-1]['queda_maxima']:.1f}%")

    # Poder de previsão: retorno seguinte por faixa de nota
    faixas = {f: {"7": [], "30": [], "vs_btc_30": []} for f in ["Alta forte", "Alta", "Lateral", "Baixa"]}
    for i, ns in notas.items():
        if not ns:
            continue
        for h in (7, 30):
            j = i + h
            if j >= len(datas):
                continue
            b0, b1 = btc_all[i], btc_all[j]
            for m, n in ns.items():
                classe = n["classe"]
                p0, p1 = S[m][i], S[m][j]
                if p0 and p1:
                    r = (p1 / p0 - 1) * 100
                    faixas[classe][str(h)].append(r)
                    if h == 30 and m != "BTC" and b0 and b1:
                        faixas[classe]["vs_btc_30"].append(r - (b1 / b0 - 1) * 100)
    resumo_faixas = []
    for f, d in faixas.items():
        def est(v):
            return {"media": statistics.mean(v), "mediana": statistics.median(v),
                    "positivos": 100 * sum(1 for x in v if x > 0) / len(v), "n": len(v)} if v else None
        resumo_faixas.append({"faixa": f, "d7": est(d["7"]), "d30": est(d["30"]), "vs_btc_30": est(d["vs_btc_30"])})

    dias_alta = sum(1 for e in clima.values() if e == "alta")
    passo = max(1, (len(datas) - inicio) // 300)   # até ~300 pontos por curva no gráfico
    SAIDA.write_text(json.dumps({
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fonte": fonte, "universo": origem_universo, "moedas": len(S),
        "periodo": {"inicio": datas[inicio], "fim": datas[-1], "dias": len(datas) - inicio},
        "parametros": {"top_n": TOP_N, "rebalancear_dias": REBALANCEAR_A_CADA, "taxa": TAXA},
        "clima_dias_em_alta_pct": 100 * dias_alta / len(clima),
        "estrategias": tabela,
        "curvas": {"datas": datas[inicio::passo] + ([datas[-1]] if (len(datas) - 1 - inicio) % passo else []),
                   **{k: [round(v, 2) for v in (c[::passo] + ([c[-1]] if (len(c) - 1) % passo else []))]
                      for k, c in curvas.items()}},
        "faixas": resumo_faixas,
    }, ensure_ascii=False), encoding="utf-8")
    print("OK:", SAIDA)


if __name__ == "__main__":
    main()
