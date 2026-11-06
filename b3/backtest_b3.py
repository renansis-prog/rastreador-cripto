"""
Backtest da análise técnica nas ações da B3, contra o CDI e o Ibovespa.

Como simula:
  - Até ~7 anos de preços diários (Yahoo Finance, ajustados por proventos: dividendos entram no retorno).
  - Universo: as ações do painel atual (docs/b3/data.json).
  - Toda semana (ou todo mês) recalcula a nota técnica de cada ação usando só os dados até aquele dia.
  - Dinheiro fora de ações rende 100% do CDI (como um Tesouro Selic).
  - Custo de 0,05% por operação (emolumentos e diferença de compra e venda).

Não testa a nota de FUNDAMENTOS: o Fundamentus só dá a foto de hoje, e usar os números de hoje
para escolher ações no passado inventaria um resultado bom demais. Testar fundamentos exige
os balanços antigos (CVM), que ficam para uma próxima etapa.

Saída: docs/b3/backtest.json (lido por docs/b3/backtest.html).
"""
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest as BT            # noqa: E402  (reaproveita a leitura do Banco Central)
import rastreador_b3 as T        # noqa: E402

SAIDA = RAIZ / "docs" / "b3" / "backtest.json"
TOP_N = 10
SEMANA, MES = 5, 21              # em pregões
TAXA = 0.0005
AQUECIMENTO = 200
PREGOES_ANO = 252


def metricas(curva):
    v0, v1 = curva[0], curva[-1]
    anos = max(len(curva) - 1, 1) / PREGOES_ANO
    pico, dd = v0, 0.0
    for v in curva:
        pico = max(pico, v)
        dd = min(dd, v / pico - 1)
    meses = [curva[i + MES] / curva[i] - 1 for i in range(0, len(curva) - MES, MES)]
    return {
        "retorno_total": (v1 / v0 - 1) * 100,
        "retorno_anual": ((v1 / v0) ** (1 / anos) - 1) * 100 if anos >= 0.5 else None,
        "queda_maxima": dd * 100,
        "meses_positivos": 100 * sum(1 for x in meses if x > 0) / len(meses) if meses else None,
    }


def carregar():
    arq = RAIZ / "docs" / "b3" / "data.json"
    if arq.exists():
        papeis = [a["papel"] for a in json.loads(arq.read_text(encoding="utf-8"))["acoes"]]
        origem = "ações do painel atual"
    else:
        papeis = T.universo(T.coletar_fundamentus())["papel"].tolist()
        origem = "ações mais líquidas do Fundamentus hoje"
    precos = T.baixar_precos(papeis, periodo="7y")
    return precos, origem


def alinhar(precos):
    ibov = precos[T.INDICE]
    datas = [d.strftime("%Y-%m-%d") for d in ibov.index]
    idx = {d: i for i, d in enumerate(datas)}
    S = {}
    for nome, serie in precos.items():
        arr = [None] * len(datas)
        for d, p in zip(serie.index, serie.tolist()):
            k = d.strftime("%Y-%m-%d")
            if k in idx:
                arr[idx[k]] = float(p)
        S["IBOV" if nome == T.INDICE else nome] = arr
    return datas, S


def ate(arr, i):
    out = []
    for p in arr[: i + 1]:
        if p is None:
            if out:
                out.append(out[-1])
        else:
            out.append(p)
    return out


def selecionar(modo, notas, ibov_info):
    """Pesos-alvo {ativo: peso}; o que faltar vai para o CDI."""
    if modo == "cdi":
        return {}
    if modo == "ibov":
        return {"IBOV": 1.0}
    if modo == "ibov_tendencia":
        return {"IBOV": 1.0} if ibov_info["acima_mm200"] else {}
    if modo == "todas":
        return {m: 1 / len(notas) for m in notas} if notas else {}
    if modo in ("top", "top_mensal"):
        ordem = sorted(notas.items(), key=lambda kv: (kv[1]["nota"], kv[1]["ret_1m"]), reverse=True)
        top = [m for m, _ in ordem[:TOP_N]]
        return {m: 1 / len(top) for m in top} if top else {}
    if modo == "forte_ibov":
        r3_ibov = ibov_info["ret_3m"]
        eleg = [(m, n) for m, n in notas.items()
                if n["nota"] >= 70 and n["vs_ibov_1m"] is not None and n["vs_ibov_1m"] > 0
                and n["ret_3m"] is not None and r3_ibov is not None and n["ret_3m"] > r3_ibov]
        eleg.sort(key=lambda kv: (kv[1]["nota"], kv[1]["vs_ibov_1m"]), reverse=True)
        return {m: 1 / TOP_N for m, _ in eleg[:TOP_N]}      # vagas vazias ficam no CDI
    raise ValueError(modo)


def simular(datas, S, cdi_idx, notas, ibov_info, inicio, modo, periodo):
    pos, cdi_val = {}, 100.0
    curva, ultimo = [], None
    for i in range(inicio, len(datas)):
        if i > inicio:
            cdi_val *= cdi_idx[i] / cdi_idx[i - 1]
            for m in list(pos):
                p0, p1 = S[m][i - 1], S[m][i]
                if p0 and p1:
                    pos[m] *= p1 / p0
                elif p0 and not p1 and all(x is None for x in S[m][i:i + 10]):
                    cdi_val += pos.pop(m) * (1 - TAXA)     # ação saiu da bolsa: vende ao último preço
        total = cdi_val + sum(pos.values())
        if ultimo is None or i - ultimo >= periodo:
            ultimo = i
            pesos = selecionar(modo, notas[i], ibov_info[i])
            alvo = {m: total * w for m, w in pesos.items() if w > 0}
            giro = sum(abs(alvo.get(m, 0) - pos.get(m, 0)) for m in set(alvo) | set(pos))
            total -= giro * TAXA
            soma = sum(pesos.values())
            pos = {m: total * w for m, w in pesos.items() if w > 0}
            cdi_val = total * (1 - soma)
        curva.append(cdi_val + sum(pos.values()))
    return curva


def main():
    precos, origem = carregar()
    datas, S = alinhar(precos)
    print(f"{len(S) - 1} ações com preço · {len(datas)} pregões ({datas[0]} a {datas[-1]})")
    inicio = AQUECIMENTO
    if len(datas) - inicio < 250:
        raise SystemExit("Histórico curto demais.")

    cdi = BT.bcb_serie(12, datas[0], datas[-1])
    cdi_idx, v = [], 100.0
    for k, d in enumerate(datas):
        if k > 0 and d in cdi:
            v *= 1 + cdi[d] / 100
        cdi_idx.append(v)

    ibov_all = S["IBOV"]
    notas, ibov_info = {}, {}
    for i in range(inicio, len(datas)):
        ib = ate(ibov_all, i)
        mm200 = T.media(ib, 200)
        ibov_info[i] = {"acima_mm200": mm200 is not None and ib[-1] > mm200, "ret_3m": T.retorno(ib, T.P_TRIM)}
        notas[i] = {}
        if (i - inicio) % SEMANA:
            continue
        ib_mes = T.retorno(ib, T.P_MES)
        for m, arr in S.items():
            if m == "IBOV" or arr[i] is None:
                continue
            a = T.analisar(ate(arr, i), ib_mes)
            if a:
                notas[i][m] = {"nota": a["nota_tec"], "ret_1m": a["ret_1m"] or 0, "ret_3m": a["ret_3m"],
                               "vs_ibov_1m": a["vs_ibov_1m"]}

    estrategias = [
        ("cdi", "CDI", "Renda fixa: 100% do CDI.", SEMANA),
        ("ibov", "Ibovespa (segurar)", "Compra o índice (na prática, um ETF como o BOVA11) e não mexe.", SEMANA),
        ("ibov_tendencia", "Ibovespa só em tendência", "Fica no Ibovespa quando ele está acima da MM200; abaixo, vai para o CDI.", SEMANA),
        ("todas", "Todas as ações (média)", "Pesos iguais em todas as ações do universo, rebalanceado toda semana.", SEMANA),
        ("top", f"Top {TOP_N} técnica", f"As {TOP_N} ações de maior nota técnica, trocadas toda semana.", SEMANA),
        ("top_mensal", f"Top {TOP_N} técnica mensal", f"As {TOP_N} de maior nota técnica, trocadas uma vez por mês.", MES),
        ("forte_ibov", "Força vs Ibovespa obrigatória", "Até 10 ações com nota técnica 70+ que superam o Ibovespa em 1 e 3 meses, 10% cada; vagas vazias no CDI.", SEMANA),
    ]
    curvas, tabela = {}, []
    for k, nome, regra, periodo in estrategias:
        c = simular(datas, S, cdi_idx, notas, ibov_info, inicio, k, periodo)
        curvas[k] = c
        tabela.append({"id": k, "nome": nome, "regra": regra, **metricas(c)})
        print(f"  {nome}: {tabela[-1]['retorno_anual'] or 0:.1f}% ao ano · queda máx {tabela[-1]['queda_maxima']:.1f}%")

    # Poder de previsão da nota técnica: retorno no mês seguinte, por faixa
    faixas = {"80+": [], "60–79": [], "40–59": [], "abaixo de 40": []}
    vs = {k: [] for k in faixas}
    for i, ns in notas.items():
        j = i + MES
        if not ns or j >= len(datas):
            continue
        ib0, ib1 = ibov_all[i], ibov_all[j]
        for m, n in ns.items():
            p0, p1 = S[m][i], S[m][j]
            if not (p0 and p1):
                continue
            f = "80+" if n["nota"] >= 80 else "60–79" if n["nota"] >= 60 else "40–59" if n["nota"] >= 40 else "abaixo de 40"
            r = (p1 / p0 - 1) * 100
            faixas[f].append(r)
            if ib0 and ib1:
                vs[f].append(r - (ib1 / ib0 - 1) * 100)
    resumo = []
    for f, v in faixas.items():
        if v:
            resumo.append({"faixa": f, "media": statistics.mean(v), "mediana": statistics.median(v),
                           "positivos": 100 * sum(1 for x in v if x > 0) / len(v), "n": len(v),
                           "vs_ibov_media": statistics.mean(vs[f]) if vs[f] else None,
                           "vs_ibov_mediana": statistics.median(vs[f]) if vs[f] else None})

    passo = max(1, (len(datas) - inicio) // 300)
    def amostra(c):
        return [round(x, 2) for x in c[::passo]] + ([round(c[-1], 2)] if (len(c) - 1) % passo else [])
    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps({
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fonte": "Yahoo Finance (preços ajustados) + Banco Central (CDI)",
        "universo": origem, "acoes": len(S) - 1,
        "periodo": {"inicio": datas[inicio], "fim": datas[-1], "pregoes": len(datas) - inicio},
        "parametros": {"top_n": TOP_N, "taxa": TAXA},
        "estrategias": tabela,
        "curvas": {"datas": datas[inicio:][::passo] + ([datas[-1]] if (len(datas) - 1 - inicio) % passo else []),
                   **{k: amostra(c) for k, c in curvas.items()}},
        "faixas": resumo,
    }, ensure_ascii=False), encoding="utf-8")
    print("OK:", SAIDA)


if __name__ == "__main__":
    main()
