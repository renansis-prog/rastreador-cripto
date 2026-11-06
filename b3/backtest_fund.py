"""
Backtest dos fundamentos e do sinal de Compra, sem olhar o futuro.

Em cada fim de mês do teste, para cada ação:
  - fundamentos: só balanços já disponíveis naquela data (90 dias após o trimestre), com
    preço e dividendos daquele dia -> P/L, P/VP, EV/EBIT, ROE, ROIC, margem, dívida, crescimento, DY
    -> mesma nota de 0 a 100 do painel;
  - técnica: mesma nota do painel, com preços até aquele dia;
  - sinal: Compra / Observar / Neutra / Evitar, pelas mesmas regras.

Estratégias (definidas antes de ver os resultados):
  cdi, ibov                  réguas
  fund_top10                 10 maiores notas de fundamentos, troca mensal (= sinal de Compra atual)
  fund_top10_stops           igual, com stop loss (2 ATR) e stop gain (4 ATR) checados todo dia
  sinal_compra               ações em Compra (até 10, 10% cada), resto no CDI
  sinal_compra_stops         igual, mas sai no stop loss (2 ATR) ou stop gain (4 ATR), checado todo dia
  fund_tendencia             fundamentos 60+ (10 maiores), só com o Ibovespa acima da MM200; senão CDI

Valor de mercado no passado = valor de hoje (P/VP do Fundamentus × patrimônio do último balanço)
ajustado pela variação do preço e do número de ações. Isso funciona também para units (BPAC11, TAEE11).

Saída: docs/b3/backtest_fund.json
"""
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as BT            # noqa: E402
import rastreador_b3 as T        # noqa: E402

FUND = RAIZ / "b3" / "dados" / "fundamentos_trimestrais.csv"
PAINEL = RAIZ / "docs" / "b3" / "data.json"
SAIDA = RAIZ / "docs" / "b3" / "backtest_fund.json"
MES = 21
TAXA = 0.0005
TOP_N = 10
AQUECIMENTO = 260                 # pregões antes do início (MM200 e 12 meses de dividendos)


# ---------------------------------------------------------------- preços
def baixar(tickers):
    import yfinance as yf
    simbolos = [t + ".SA" for t in tickers] + [T.INDICE]
    df = None
    for tentativa in range(3):
        try:
            df = yf.download(simbolos, period="10y", interval="1d", auto_adjust=False, actions=True,
                             group_by="ticker", threads=True, progress=False)
            if df is not None and not df.empty:
                break
        except Exception as e:  # noqa: BLE001
            print("Yahoo falhou:", e)
    if df is None or df.empty:
        raise RuntimeError("Yahoo sem dados")
    datas = df.index
    out = {}
    for s in simbolos:
        try:
            x = df[s]
        except KeyError:
            continue
        if x["Close"].notna().sum() < 300:
            continue
        nome = "IBOV" if s == T.INDICE else s.replace(".SA", "")
        out[nome] = {
            "close": x["Close"].tolist(),            # ajustado só por desdobramentos
            "adj": x["Adj Close"].tolist(),          # ajustado também por proventos (retorno total)
            "open": x["Open"].tolist(), "high": x["High"].tolist(), "low": x["Low"].tolist(),
            "div": (x["Dividends"].fillna(0).tolist() if "Dividends" in x else [0.0] * len(datas)),
        }
    return [d.strftime("%Y-%m-%d") for d in datas], out


def limpa(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


def vazio(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def n0(v):
    return 0.0 if vazio(v) else float(v)


# ---------------------------------------------------------------- fundamentos no tempo
def preparar_fund():
    f = pd.read_csv(FUND, parse_dates=["data", "disponivel_em"])
    f = f.sort_values(["empresa", "data"])
    por_emp = {}
    for emp, g in f.groupby("empresa"):
        g = g.reset_index(drop=True)
        for c in ("receita", "ebit", "lucro"):
            g[c + "_ttm"] = g[c].rolling(4, min_periods=4).sum()
            # só vale se os 4 trimestres forem seguidos (~1 ano)
            ok = (g["data"] - g["data"].shift(3)).dt.days.between(250, 300)
            g.loc[~ok, c + "_ttm"] = np.nan
        g["receita_ttm_5a"] = g["receita_ttm"].shift(20)
        ok5 = (g["data"] - g["data"].shift(20)).dt.days.between(1780, 1870)
        g.loc[~ok5, "receita_ttm_5a"] = np.nan
        g["acoes"] = g["acoes"].replace(0, np.nan).ffill()
        por_emp[emp] = g
    return por_emp


def ultimo_balanco(g, dia):
    disp = g[g["disponivel_em"] <= pd.Timestamp(dia)]
    disp = disp.dropna(subset=["pl"])
    return None if disp.empty else disp.iloc[-1]


def indicadores(b, mc, financeira, dy):
    """Os mesmos indicadores do Fundamentus, calculados com o balanço b e o valor de mercado mc."""
    pl = b["pl"]
    luc, rec, ebit = b.get("lucro_ttm"), b.get("receita_ttm"), b.get("ebit_ttm")
    divida, caixa = n0(b.get("divida")), n0(b.get("caixa_total"))
    f = {"pl": None, "pvp": None, "ev_ebit": None, "roe": None, "roic": None, "mrg_liq": None,
         "div_pat": None, "cresc_rec_5a": None, "dy": dy}
    if pl and pl > 0:
        f["pvp"] = mc / pl
        if luc is not None and not math.isnan(luc):
            f["roe"] = luc / pl * 100
    if luc is not None and not math.isnan(luc) and luc != 0:
        f["pl"] = mc / luc
    if financeira:
        f.update({"ev_ebit": 0.0, "roic": 0.0, "div_pat": 0.0})
    else:
        if ebit is not None and not math.isnan(ebit) and ebit != 0:
            f["ev_ebit"] = (mc + divida - caixa) / ebit
            cap = pl + divida - caixa
            if cap and cap > 0:
                f["roic"] = ebit * 0.66 / cap * 100
        if rec and not math.isnan(rec) and rec > 0 and luc is not None and not math.isnan(luc):
            f["mrg_liq"] = luc / rec * 100
        if pl and pl > 0:
            f["div_pat"] = (divida - caixa) / pl
    r5 = b.get("receita_ttm_5a")
    if rec and r5 and not math.isnan(rec) and not math.isnan(r5) and r5 > 0 and rec > 0:
        f["cresc_rec_5a"] = ((rec / r5) ** (1 / 5) - 1) * 100
    return {k: limpa(v) for k, v in f.items()}


def nota_pit(f, financeira):
    """Nota de fundamentos como no painel; critério sem dado no período (ex.: crescimento de 5 anos) fica de fora."""
    ignorar = set()
    if f["cresc_rec_5a"] is None:
        ignorar.add("cresc_ok")
    if not financeira and f["mrg_liq"] is None:
        ignorar.add("margem_ok")
    nota, crit, _ = T.nota_fundamental({k: (np.nan if v is None else v) for k, v in f.items()})
    if not ignorar:
        return nota
    ganhos = possiveis = 0
    for k, pts, _, banco in T.CRITERIOS_FUND:
        if crit.get(k) is None or k in ignorar:
            continue
        possiveis += pts
        ganhos += pts if crit[k] else 0
    n = round(100 * ganhos / possiveis) if possiveis else 0
    if f["pl"] is not None and f["pl"] <= 0:
        n = min(n, 30)
    return n


# ---------------------------------------------------------------- simulação
def simular(modo, datas, P, cdi_idx, ent, ibov_ok, inicio, stops=False):
    """ent[i] = {papel: {"nf","nt","sinal","atr"}} nos dias de rebalanceamento."""
    cdi_val, pos = 100.0, {}          # pos[papel] = {"v": valor, "stop": x, "gain": y}
    curva, trades = [], []
    reb = set(ent)
    for i in range(inicio, len(datas)):
        if i > inicio:
            cdi_val *= cdi_idx[i] / cdi_idx[i - 1]
            for m in list(pos):
                a0, a1 = P[m]["adj"][i - 1], P[m]["adj"][i]
                if not (a0 and a1) or math.isnan(a0) or math.isnan(a1):
                    continue
                p = pos[m]
                if stops and m != "IBOV":
                    lo, hi, o = P[m]["low"][i], P[m]["high"][i], P[m]["open"][i]
                    if vazio(o):
                        o = P[m]["close"][i]
                    saida, motivo = None, None
                    # abriu além do nível: sai na abertura; senão, sai no próprio nível.
                    # Se o dia tocou os dois, assume o pior (stop loss).
                    if o <= p["stop"]:
                        saida, motivo = o, "loss"
                    elif lo <= p["stop"]:
                        saida, motivo = p["stop"], "loss"
                    elif o >= p["gain"]:
                        saida, motivo = o, "gain"
                    elif hi >= p["gain"]:
                        saida, motivo = p["gain"], "gain"
                    if saida is not None:
                        # valor até o fechamento anterior × (preço de saída / fechamento anterior)
                        valor = p["v"] * saida / P[m]["close"][i - 1] * (1 - TAXA)
                        trades.append({"motivo": motivo, "ret": (valor / p["v0"] - 1) * 100})
                        cdi_val += valor
                        del pos[m]
                        continue
                p["v"] *= a1 / a0
        total = cdi_val + sum(p["v"] for p in pos.values())
        if i in reb:
            alvo = escolher(modo, ent[i], ibov_ok[i])
            novos = {}
            giro = 0.0
            for m, w in alvo.items():
                v = total * w
                antigo = pos.get(m)
                giro += abs(v - (antigo["v"] if antigo else 0))
                if antigo:
                    novos[m] = {**antigo, "v": v}
                else:
                    c, atr = P[m]["close"][i] if m in P else None, ent[i].get(m, {}).get("atr")
                    novos[m] = {"v": v, "v0": v,
                                "stop": c - 2 * atr if (c and atr) else -1,
                                "gain": c + 4 * atr if (c and atr) else 1e18}
            for m in pos:
                if m not in alvo:
                    giro += pos[m]["v"]
                    if stops:
                        trades.append({"motivo": "troca", "ret": (pos[m]["v"] / pos[m]["v0"] - 1) * 100})
            total -= giro * TAXA
            fator = total / (cdi_val + sum(p["v"] for p in pos.values())) if total else 1
            soma = sum(alvo.values())
            pos = {m: {**p, "v": p["v"] * fator} for m, p in novos.items()}
            cdi_val = total * (1 - soma)
        curva.append(cdi_val + sum(p["v"] for p in pos.values()))
    return curva, trades


def escolher(modo, e, ibov_ok):
    if modo == "cdi":
        return {}
    if modo == "ibov":
        return {"IBOV": 1.0}
    if modo == "todas":
        return {m: 1 / len(e) for m in e} if e else {}
    if modo in ("fund_top10", "fund_top10_stops"):
        # mesma regra do sinal de Compra do painel: 10 maiores notas, só com nota 60+; vagas vazias no CDI
        top = sorted(((m, x) for m, x in e.items() if x["nf"] >= 60),
                     key=lambda kv: (kv[1]["nf"], kv[1]["nt"]), reverse=True)[:TOP_N]
        return {m: 1 / TOP_N for m, _ in top}
    if modo in ("sinal_compra", "sinal_compra_stops"):
        c = [(m, x) for m, x in e.items() if x["sinal"] == "Compra"]   # regra antiga
        c.sort(key=lambda kv: kv[1]["nf"] + kv[1]["nt"], reverse=True)
        return {m: 1 / TOP_N for m, _ in c[:TOP_N]}
    if modo == "fund_tendencia":
        if not ibov_ok:
            return {}
        c = sorted(((m, x) for m, x in e.items() if x["nf"] >= 60), key=lambda kv: (kv[1]["nf"], kv[1]["nt"]), reverse=True)[:TOP_N]
        return {m: 1 / TOP_N for m, _ in c}
    raise ValueError(modo)


def metricas(curva):
    v0, v1 = curva[0], curva[-1]
    anos = (len(curva) - 1) / 252
    pico, dd = v0, 0.0
    for v in curva:
        pico = max(pico, v)
        dd = min(dd, v / pico - 1)
    meses = [curva[i + MES] / curva[i] - 1 for i in range(0, len(curva) - MES, MES)]
    return {"retorno_total": (v1 / v0 - 1) * 100, "retorno_anual": ((v1 / v0) ** (1 / anos) - 1) * 100,
            "queda_maxima": dd * 100, "meses_positivos": 100 * sum(x > 0 for x in meses) / len(meses)}


def main():
    painel = json.loads(PAINEL.read_text(encoding="utf-8"))
    info = {a["papel"]: a for a in painel["acoes"]}
    fund = preparar_fund()
    datas, P = baixar(list(info))
    print(f"{len(P) - 1} ações com preço · {len(datas)} pregões · {len(fund)} empresas com balanço")

    # valor de mercado de hoje (P/VP do Fundamentus × patrimônio do último balanço)
    base = {}
    for papel, a in info.items():
        g = fund.get(papel[:4])
        if papel not in P or g is None or not a["fund"].get("pvp"):
            continue
        b = ultimo_balanco(g, datas[-1])
        if b is None or vazio(b["acoes"]) or not b["acoes"] or b["pl"] <= 0:
            continue
        close_hoje = next(x for x in reversed(P[papel]["close"]) if x and not math.isnan(x))
        base[papel] = {"mc": a["fund"]["pvp"] * b["pl"], "close": close_hoje, "acoes": b["acoes"]}
    print(f"{len(base)} ações com balanço + preço + P/VP atual")

    cdi = BT.bcb_serie(12, datas[0], datas[-1])
    cdi_idx, v = [], 100.0
    for k, d in enumerate(datas):
        if k and d in cdi:
            v *= 1 + cdi[d] / 100
        cdi_idx.append(v)

    ib = P["IBOV"]["adj"]
    inicio = AQUECIMENTO
    ultimo_reb = max(k for k in range(inicio, len(datas)) if (k - inicio) % MES == 0)
    ent, ibov_ok, conf = {}, {}, []
    for i in range(inicio, len(datas)):
        serie_ib = [x for x in ib[: i + 1] if x and not math.isnan(x)]
        mm200 = T.media(serie_ib, 200)
        ibov_ok[i] = mm200 is not None and serie_ib[-1] > mm200
        if (i - inicio) % MES:
            continue
        ib_mes = T.retorno(serie_ib, T.P_MES)
        ent[i] = {}
        for papel, bs in base.items():
            c = P[papel]["close"][i]
            if not c or math.isnan(c):
                continue
            b = ultimo_balanco(fund[papel[:4]], datas[i])
            if b is None or vazio(b["acoes"]) or not b["acoes"]:
                continue
            mc = bs["mc"] * (c / bs["close"]) * (b["acoes"] / bs["acoes"])
            divs = sum(P[papel]["div"][max(0, i - 251): i + 1])
            dy = divs / c * 100
            fin = info[papel]["financeira"]
            f = indicadores(b, mc, fin, dy)
            nf = nota_pit(f, fin)
            adj = [x for x in P[papel]["adj"][: i + 1] if x and not math.isnan(x)]
            tec = T.analisar(adj, ib_mes)
            if not tec:
                continue
            hlc = pd.DataFrame({"High": P[papel]["high"][max(0, i - 60): i + 1],
                                "Low": P[papel]["low"][max(0, i - 60): i + 1],
                                "Close": P[papel]["close"][max(0, i - 60): i + 1]}).dropna()
            nt = tec["nota_tec"]
            ent[i][papel] = {"nf": nf, "nt": nt, "sinal": T.sinal_antigo(nf, nt), "atr": T.atr(hlc)}
            if i == ultimo_reb:
                conf.append({"papel": papel, "pl_calc": f["pl"], "pl_fundamentus": info[papel]["fund"].get("pl"),
                             "roe_calc": f["roe"], "roe_fundamentus": info[papel]["fund"].get("roe"),
                             "dy_calc": dy, "dy_fundamentus": info[papel]["fund"].get("dy")})
    n_compra = statistics.mean(sum(1 for x in e.values() if x["sinal"] == "Compra") for e in ent.values())
    print(f"{len(ent)} meses · média de {n_compra:.1f} ações em Compra por mês")

    estrategias = [
        ("cdi", "CDI", "Renda fixa: 100% do CDI.", False),
        ("ibov", "Ibovespa (segurar)", "Compra o índice e não mexe.", False),
        ("todas", "Todas as ações (média)", "Pesos iguais em todas as ações do universo, todo mês. Mostra o efeito de usar só as ações que existem hoje.", False),
        ("fund_top10", "Top 10 fundamentos (Compra atual)", "A regra atual do sinal de Compra: as 10 maiores notas de fundamentos (60+), 10% cada, revistas todo mês; vagas vazias no CDI.", False),
        ("fund_top10_stops", "Top 10 fundamentos + stops", "A regra atual do painel com os stops: vende no stop loss (2 ATR abaixo da entrada) ou no stop gain (4 ATR acima), checado todo dia; o dinheiro espera no CDI até a próxima troca.", True),
        ("sinal_compra", "Regra antiga (fund. 60+ e téc. 70+)", "Regra anterior do painel: fundamentos 60+ e técnica 70+, até 10 com 10% cada; vagas vazias no CDI.", False),
        ("sinal_compra_stops", "Regra antiga + stops", "Regra anterior com stop loss e stop gain.", True),
        ("fund_tendencia", "Fundamentos + Ibovespa em tendência", "Até 10 ações com fundamentos 60+, só quando o Ibovespa está acima da MM200; senão, CDI.", False),
    ]
    tabela, curvas, stats = [], {}, None
    for k, nome, regra, st in estrategias:
        c, trades = simular(k, datas, P, cdi_idx, ent, ibov_ok, inicio, stops=st)
        curvas[k] = c
        tabela.append({"id": k, "nome": nome, "regra": regra, **metricas(c)})
        print(f"  {nome}: {tabela[-1]['retorno_anual']:.1f}% a.a. · queda máx {tabela[-1]['queda_maxima']:.1f}%")
        if k == "fund_top10_stops" and trades:
            def resumo(lst):
                return {"n": len(lst), "ret_medio": statistics.mean(t["ret"] for t in lst)} if lst else {"n": 0, "ret_medio": None}
            stats = {m: resumo([t for t in trades if t["motivo"] == m]) for m in ("gain", "loss", "troca")}
            stats["total"] = resumo(trades)
            stats["acerto"] = 100 * sum(t["ret"] > 0 for t in trades) / len(trades)

    # poder de previsão da nota de fundamentos (retorno total nos 21 pregões seguintes)
    faixas = {"80+": [], "60–79": [], "40–59": [], "abaixo de 40": []}
    vs = {k: [] for k in faixas}
    for i, e in ent.items():
        j = i + MES
        if j >= len(datas):
            continue
        rib = ib[j] / ib[i] - 1
        for m, x in e.items():
            a0, a1 = P[m]["adj"][i], P[m]["adj"][j]
            if not (a0 and a1) or math.isnan(a0) or math.isnan(a1):
                continue
            f = "80+" if x["nf"] >= 80 else "60–79" if x["nf"] >= 60 else "40–59" if x["nf"] >= 40 else "abaixo de 40"
            faixas[f].append((a1 / a0 - 1) * 100)
            vs[f].append((a1 / a0 - 1 - rib) * 100)
    resumo_f = [{"faixa": f, "media": statistics.mean(v), "mediana": statistics.median(v),
                 "positivos": 100 * sum(x > 0 for x in v) / len(v), "n": len(v),
                 "vs_ibov_media": statistics.mean(vs[f]), "vs_ibov_mediana": statistics.median(vs[f])}
                for f, v in faixas.items() if v]

    passo = max(1, (len(datas) - inicio) // 300)
    def amostra(c):
        return [round(x, 2) for x in c[::passo]] + ([round(c[-1], 2)] if (len(c) - 1) % passo else [])
    SAIDA.write_text(json.dumps({
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fonte": "CVM (balanços) + Yahoo Finance (preços e proventos) + Banco Central (CDI)",
        "acoes": len(base), "periodo": {"inicio": datas[inicio], "fim": datas[-1], "pregoes": len(datas) - inicio},
        "media_compra_por_mes": n_compra,
        "parametros": {"taxa": TAXA, "top_n": TOP_N},
        "estrategias": tabela, "stops": stats, "faixas": resumo_f,
        "curvas": {"datas": datas[inicio:][::passo] + ([datas[-1]] if (len(datas) - 1 - inicio) % passo else []),
                   **{k: amostra(c) for k, c in curvas.items()}},
        "conferencia": sorted(conf, key=lambda x: x["papel"]),
    }, ensure_ascii=False), encoding="utf-8")
    # conferência com o Fundamentus de hoje
    ok = [x for x in conf if x["pl_calc"] and x["pl_fundamentus"]]
    if ok:
        dif = statistics.median(abs(x["pl_calc"] / x["pl_fundamentus"] - 1) for x in ok if x["pl_fundamentus"])
        print(f"Conferência: diferença mediana do P/L calculado vs Fundamentus = {dif * 100:.0f}% ({len(ok)} ações)")
    print("OK:", SAIDA)


if __name__ == "__main__":
    main()
