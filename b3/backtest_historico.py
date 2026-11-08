"""
Backtest SEM viés de sobrevivência: em cada mês, o universo é o das 120 ações mais negociadas
NAQUELA época (inclusive empresas que depois saíram da bolsa, quebraram ou foram compradas).

Fontes:
  B3 COTAHIST   preços diários oficiais de todas as ações, desde ANO_INICIAL (sem ajustes)
  Yahoo         preços ajustados por proventos e dividendos, para as ações que ainda existem
  CVM           balanços (ITR/DFP) e cadastro (setor), de todas as empresas do universo histórico
  Banco Central CDI

Diferenças para o backtest_fund.py (que usa as ações de hoje):
  - universo de cada mês pelo volume financeiro dos 2 meses anteriores (uma ação por empresa);
  - valor de mercado = preço da época × número de ações da época (nada de "valor de hoje");
  - ação que sumiu do Yahoo usa o preço da B3, com desdobramentos/grupamentos corrigidos e sem
    dividendos (isso puxa o resultado dessas ações um pouco para baixo, o lado conservador).

Saída: docs/b3/backtest_hist.json
"""
import io
import json
import math
import re
import statistics
import sys
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as BT            # noqa: E402
import backtest_fund as BF       # noqa: E402
import fundamentos_cvm as CVM    # noqa: E402
import rastreador_b3 as T        # noqa: E402

ANO_INICIAL = 2016
DADOS = RAIZ / "b3" / "dados"
FUND_HIST = DADOS / "fundamentos_historico.csv"
SAIDA = RAIZ / "docs" / "b3" / "backtest_hist.json"
UA = {"User-Agent": "Mozilla/5.0 rastreador-b3/1.0"}
URLS_COTAHIST = [
    "https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A{ano}.ZIP",
    "https://www.b3.com.br/pesquisapregao/download?filelist=COTAHIST_A{ano}.ZIP",
]
UNIVERSO_N = 120
LIQ_MIN = 5_000_000
JANELA_LIQ = 42                  # pregões para medir o volume médio
MES = 21
AQUECIMENTO = 260
SETORES_FIN = ("banc", "segur", "intermedia", "arrendamento", "previd", "financ", "capitaliza")


# ---------------------------------------------------------------- COTAHIST
def baixar_cotahist(ano):
    for url in URLS_COTAHIST:
        u = url.format(ano=ano)
        for tentativa in range(3):
            try:
                r = requests.get(u, headers=UA, timeout=300)
                if r.status_code == 404:
                    break
                r.raise_for_status()
                z = zipfile.ZipFile(io.BytesIO(r.content))
                # o arquivo da B3 às vezes vem com outro zip dentro
                nome = z.namelist()[0]
                dado = z.read(nome)
                if dado[:2] == b"PK":
                    z2 = zipfile.ZipFile(io.BytesIO(dado))
                    dado = z2.read(z2.namelist()[0])
                return dado.decode("latin-1")
            except Exception as e:  # noqa: BLE001
                print(f"  COTAHIST {ano} ({u.split('/')[2]}): tentativa {tentativa + 1} falhou ({type(e).__name__}: {e})")
                time.sleep(10 * (tentativa + 1))
    return None


def ler_cotahist(texto):
    """Linhas do mercado à vista, lote padrão, ações (ON/PN/UNT). Preços em R$ por ação."""
    reg = []
    for ln in texto.splitlines():
        if ln[:2] != "01" or ln[10:12] != "02" or ln[24:27] != "010":
            continue
        cod = ln[12:24].strip()
        if not re.fullmatch(r"[A-Z0-9]{4}(3|4|5|6|7|8|11)", cod):
            continue
        fat = int(ln[210:217] or 1) or 1
        p = lambda a, b: int(ln[a:b]) / 100 / fat  # noqa: E731
        reg.append((ln[2:10], cod, p(56, 69), p(69, 82), p(82, 95), p(108, 121), int(ln[170:188]) / 100))
    df = pd.DataFrame(reg, columns=["data", "papel", "open", "high", "low", "close", "volume"])
    df["data"] = pd.to_datetime(df["data"], format="%Y%m%d")
    return df


def carregar_cotahist():
    DADOS.mkdir(parents=True, exist_ok=True)
    partes = []
    for ano in range(ANO_INICIAL, date.today().year + 1):
        texto = baixar_cotahist(ano)
        if texto is None:
            print(f"COTAHIST {ano}: indisponível")
            continue
        df = ler_cotahist(texto)
        print(f"COTAHIST {ano}: {len(df):,} linhas, {df['papel'].nunique()} ações")
        partes.append(df)
    if not partes:
        raise SystemExit("Não consegui baixar o histórico da B3 (COTAHIST).")
    return pd.concat(partes, ignore_index=True)


# ---------------------------------------------------------------- desdobramentos
FATORES = [2, 3, 4, 5, 8, 10, 20, 25, 50, 100, 1000]


def fator_evento(r):
    """Se a variação de um dia parece desdobramento/grupamento, devolve o fator; senão 1."""
    for f in FATORES:
        for alvo in (1 / f, float(f)):
            if abs(math.log(r / alvo)) < 0.08:
                return alvo
    return 1.0


def ajustar_desdobramentos(close, adj_ref=None):
    """Fator acumulado (para trás) que põe o preço bruto na base de hoje, só por desdobramentos.

    Com a série ajustada do Yahoo (adj_ref), um salto bruto que não aparece no ajustado é evento.
    Sem ela, saltos acima de 45% em um dia com proporção típica (1:2, 10:1...) contam como evento."""
    n = len(close)
    fat = np.ones(n)
    acum = 1.0
    for i in range(n - 1, 0, -1):
        fat[i] = acum
        a, b = close[i - 1], close[i]
        if not (a and b) or math.isnan(a) or math.isnan(b):
            continue
        r = b / a
        if adj_ref is not None and adj_ref[i - 1] and adj_ref[i] and not math.isnan(adj_ref[i - 1]) and not math.isnan(adj_ref[i]):
            ra = adj_ref[i] / adj_ref[i - 1]
            q = r / ra
            if abs(math.log(q)) > 0.25:
                ev = fator_evento(q)
                if ev != 1.0:
                    acum /= ev     # ex.: 1:10 (r = 0,1) -> preços anteriores divididos por 10
        elif abs(math.log(r)) > math.log(1.45):
            ev = fator_evento(r)
            if ev != 1.0:
                acum /= ev
    fat[0] = acum
    return fat


# ---------------------------------------------------------------- Yahoo
def baixar_yahoo(tickers):
    import yfinance as yf
    out = {}
    lotes = [tickers[i:i + 80] for i in range(0, len(tickers), 80)]
    for lote in lotes + [[]]:
        simbolos = [t + ".SA" for t in lote] + ([T.INDICE] if not lote else [])
        if not simbolos:
            continue
        try:
            df = yf.download(simbolos, start=f"{ANO_INICIAL}-01-01", interval="1d", auto_adjust=False,
                             actions=True, group_by="ticker", threads=True, progress=False)
        except Exception as e:  # noqa: BLE001
            print("Yahoo falhou:", e)
            continue
        for s in simbolos:
            try:
                x = df[s] if isinstance(df.columns, pd.MultiIndex) and s in df.columns.get_level_values(0) else df
                if "Dividends" not in x:
                    x = x.assign(Dividends=0.0)
                x = x[["Close", "Adj Close", "Dividends"]].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(x) >= 60:
                out["IBOV" if s == T.INDICE else s.replace(".SA", "")] = x
    return out


# ---------------------------------------------------------------- setor (bancos/seguradoras)
def financeiras(cnpjs):
    try:
        r = requests.get("https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv", headers=UA, timeout=120)
        r.raise_for_status()
        cad = pd.read_csv(io.StringIO(r.content.decode("latin-1")), sep=";", dtype=str)
        c_cnpj = next(c for c in cad.columns if c.upper().startswith("CNPJ"))
        c_set = next(c for c in cad.columns if "SETOR" in c.upper())
        cad = cad[cad[c_cnpj].isin(cnpjs)]
        return {c for c, s in zip(cad[c_cnpj], cad[c_set]) if isinstance(s, str) and any(k in s.lower() for k in SETORES_FIN)}
    except Exception as e:  # noqa: BLE001
        print("Cadastro da CVM indisponível:", e)
        return set()


# ---------------------------------------------------------------- principal
def main():
    cot = carregar_cotahist()
    datas_all = sorted(cot["data"].unique())
    datas = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in datas_all]
    idx = {d: i for i, d in enumerate(datas)}
    n = len(datas)
    cot["i"] = cot["data"].dt.strftime("%Y-%m-%d").map(idx)

    # volume médio móvel por ação -> universo de cada mês (uma ação por empresa)
    vol = cot.pivot_table(index="i", columns="papel", values="volume", aggfunc="sum").reindex(range(n)).fillna(0)
    liq = vol.rolling(JANELA_LIQ, min_periods=20).mean()
    inicio = AQUECIMENTO
    rebal = list(range(inicio, n, MES))
    universo = {}
    for i in rebal:
        linha = liq.iloc[i - 1].dropna()
        linha = linha[linha >= LIQ_MIN].sort_values(ascending=False)
        escolhidos, empresas = [], set()
        for papel in linha.index:
            if papel[:4] in empresas:
                continue
            empresas.add(papel[:4])
            escolhidos.append(papel)
            if len(escolhidos) >= UNIVERSO_N:
                break
        universo[i] = escolhidos
    todos = sorted({p for v in universo.values() for p in v})
    print(f"{len(rebal)} meses · {len(todos)} ações diferentes passaram pelo universo "
          f"(média de {statistics.mean(len(v) for v in universo.values()):.0f} por mês)")

    # fundamentos da CVM de todas as empresas que passaram pelo universo
    CVM.main(prefixos={p[:4] for p in todos}, saida=FUND_HIST, todos_os_anos=True)
    fund = pd.read_csv(FUND_HIST, parse_dates=["data", "disponivel_em"])
    BF.FUND = FUND_HIST
    por_emp = BF.preparar_fund()
    fin = financeiras(set(fund["CNPJ_CIA"]))
    emp_fin = {e for e, c in zip(fund["empresa"], fund["CNPJ_CIA"]) if c in fin}
    print(f"{len(por_emp)} empresas com balanço · {len(emp_fin)} bancos/seguradoras")

    # preços: B3 (bruto) + Yahoo (ajustado) quando a ação ainda existe
    yh = baixar_yahoo(todos)
    print(f"Yahoo: {len(yh) - 1} das {len(todos)} ações ainda têm histórico ajustado")
    if "IBOV" not in yh:
        raise SystemExit("Sem Ibovespa no Yahoo.")
    brutos = {p: g.set_index("i") for p, g in cot[cot["papel"].isin(set(todos) | {x for x in cot["papel"].unique() if x[:4] in {t[:4] for t in todos}})].groupby("papel")}

    def serie(g, col):
        return g[col].reindex(range(n)).astype(float).tolist()

    P, sem_yahoo = {}, 0
    for p in todos:
        g = brutos.get(p)
        if g is None:
            continue
        raw = serie(g, "close")
        y = yh.get(p)
        adj_ref = None
        if y is not None:
            yd = {d.strftime("%Y-%m-%d"): (c, a, dv) for d, c, a, dv in zip(y.index, y["Close"], y["Adj Close"], y["Dividends"])}
            adj_ref = [yd.get(d, (None, None, None))[1] for d in datas]
            adj_ref = [float("nan") if v is None else v for v in adj_ref]
        else:
            sem_yahoo += 1
        fat = ajustar_desdobramentos(raw, adj_ref)
        P[p] = {
            "raw": raw,
            "close": [c / f if c == c else c for c, f in zip(raw, fat)],
            "open": [c / f if c == c else c for c, f in zip(serie(g, "open"), fat)],
            "high": [c / f if c == c else c for c, f in zip(serie(g, "high"), fat)],
            "low": [c / f if c == c else c for c, f in zip(serie(g, "low"), fat)],
        }
        if adj_ref is not None and sum(1 for v in adj_ref if v == v) > 0.8 * sum(1 for v in raw if v == v):
            # retorno total pelo Yahoo nos dias em que ele tem dado; buracos herdam a variação da B3
            adj, ult_a, ult_c = [], None, None
            for a, c in zip(adj_ref, P[p]["close"]):
                if a == a:
                    adj.append(a); ult_a, ult_c = a, c
                elif c == c and ult_a is not None:
                    adj.append(ult_a * c / ult_c)
                else:
                    adj.append(float("nan"))
            P[p]["adj"] = adj
            P[p]["div"] = [yd.get(d, (0, 0, 0))[2] or 0.0 for d in datas]
        else:
            P[p]["adj"] = P[p]["close"]
            P[p]["div"] = [0.0] * n
    ib = yh["IBOV"]
    ibd = {d.strftime("%Y-%m-%d"): a for d, a in zip(ib.index, ib["Adj Close"])}
    ib_adj, ult = [], None
    for d in datas:
        ult = ibd.get(d, ult)
        ib_adj.append(ult if ult is not None else float("nan"))
    P["IBOV"] = {"adj": ib_adj, "close": ib_adj, "open": ib_adj, "high": ib_adj, "low": ib_adj, "raw": ib_adj, "div": [0.0] * n}
    print(f"Preços montados: {len(P) - 1} ações ({sem_yahoo} só com dados da B3)")

    cdi = BT.bcb_serie(12, datas[0], datas[-1])
    cdi_idx, v = [], 100.0
    for k, d in enumerate(datas):
        if k and d in cdi:
            v *= 1 + cdi[d] / 100
        cdi_idx.append(v)

    # preço bruto de uma ação ON/PN da mesma empresa (para valor de mercado de units)
    def preco_valor(papel, i):
        if not papel.endswith("11"):
            return P[papel]["raw"][i]
        for suf in ("3", "4", "5", "6"):
            g = brutos.get(papel[:4] + suf)
            if g is not None and i in g.index:
                return float(g.at[i, "close"])
        return None

    ent, ibov_ok, n_aval = {}, {}, []
    for i in range(inicio, n):
        s_ib = [x for x in ib_adj[: i + 1] if x == x]
        mm = T.media(s_ib, 200)
        ibov_ok[i] = mm is not None and s_ib[-1] > mm
        if i not in universo:
            continue
        ib_mes = T.retorno(s_ib, T.P_MES)
        ent[i] = {}
        for papel in universo[i]:
            if papel not in P:
                continue
            emp = papel[:4]
            g = por_emp.get(emp)
            c = P[papel]["close"][i]
            if g is None or not c or c != c:
                continue
            b = BF.ultimo_balanco(g, datas[i])
            if b is None or BF.vazio(b.get("acoes_bruto")) or not b.get("acoes_bruto"):
                continue
            pv = preco_valor(papel, i)
            if not pv or pv != pv:
                continue
            # escala do número de ações (milhares x unidades): a que deixa o P/VP mais perto de 1
            if not b["pl"] or b["pl"] <= 0:
                continue
            mc = min((pv * b["acoes_bruto"] * 1000.0 ** k for k in (-1, 0, 1)),
                     key=lambda m: abs(math.log10(m / b["pl"])))
            divs = sum(P[papel]["div"][max(0, i - 251): i + 1])
            dy = divs / c * 100 if c else 0
            f = BF.indicadores(b, mc, emp in emp_fin, dy)
            nf = BF.nota_pit(f, emp in emp_fin)
            adj = [x for x in P[papel]["adj"][: i + 1] if x == x]
            tec = T.analisar(adj, ib_mes)
            if not tec:
                continue
            hlc = pd.DataFrame({"High": P[papel]["high"][max(0, i - 60): i + 1], "Low": P[papel]["low"][max(0, i - 60): i + 1],
                                "Close": P[papel]["close"][max(0, i - 60): i + 1]}).dropna()
            atr_i = T.atr(hlc)
            ent[i][papel] = {"nf": nf, "nt": tec["nota_tec"], "sinal": T.sinal_antigo(nf, tec["nota_tec"]),
                             "atr": atr_i, "atr_pct": atr_i / c * 100 if atr_i else None,
                             "alerta": bool(T.alertas(f, atr_i / c * 100 if atr_i else None, emp in emp_fin))}
        n_aval.append(len(ent[i]))
    print(f"Ações avaliadas por mês: média {statistics.mean(n_aval):.0f} (mín {min(n_aval)}, máx {max(n_aval)})")

    estrategias = [
        ("cdi", "CDI", "Renda fixa: 100% do CDI.", False),
        ("ibov", "Ibovespa (segurar)", "Compra o índice e não mexe.", False),
        ("todas", "Todas as ações da época (média)", "Pesos iguais nas ações do universo de cada mês, inclusive as que depois saíram da bolsa.", False),
        ("fund_top10", "Compra atual (top 10 fundamentos)", "As 10 maiores notas de fundamentos (60+) entre as mais negociadas da época, revistas todo mês.", False),
        ("fund_top10_stops", "Compra atual + stops", "Igual, com stop loss (2 ATR) e stop gain (4 ATR) checados todo dia.", True),
        ("risco_stops", "Compra atual + stops + tamanho por risco", "Igual, com o tamanho sugerido no painel (1% da carteira em risco por ação, máx. 15%).", True),
    ]
    tabela, curvas, stats = [], {}, None
    for k, nome, regra, st in estrategias:
        c, trades = BF.simular(k, datas, P, cdi_idx, ent, ibov_ok, inicio, stops=st)
        curvas[k] = c
        tabela.append({"id": k, "nome": nome, "regra": regra, **BF.metricas(c)})
        print(f"  {nome}: {tabela[-1]['retorno_anual']:.1f}% a.a. · queda máx {tabela[-1]['queda_maxima']:.1f}%")
        if k == "fund_top10_stops" and trades:
            def resumo(lst):
                return {"n": len(lst), "ret_medio": statistics.mean(t["ret"] for t in lst)} if lst else {"n": 0, "ret_medio": None}
            stats = {m: resumo([t for t in trades if t["motivo"] == m]) for m in ("gain", "loss", "troca")}
            stats["total"] = resumo(trades)
            stats["acerto"] = 100 * sum(t["ret"] > 0 for t in trades) / len(trades)

    faixas = {"80+": [], "60–79": [], "40–59": [], "abaixo de 40": []}
    vs = {k: [] for k in faixas}
    for i, e in ent.items():
        j = i + MES
        if j >= n:
            continue
        rib = ib_adj[j] / ib_adj[i] - 1
        for m, x in e.items():
            a0, a1 = P[m]["adj"][i], P[m]["adj"][j]
            if a1 != a1 or not a1:     # saiu da bolsa no mês: último preço conhecido
                a1 = next((v for v in reversed(P[m]["adj"][i:j + 1]) if v == v and v), None)
            if not (a0 and a1) or a0 != a0:
                continue
            fx = "80+" if x["nf"] >= 80 else "60–79" if x["nf"] >= 60 else "40–59" if x["nf"] >= 40 else "abaixo de 40"
            faixas[fx].append((a1 / a0 - 1) * 100)
            vs[fx].append((a1 / a0 - 1 - rib) * 100)
    resumo_f = [{"faixa": f, "media": statistics.mean(v), "mediana": statistics.median(v),
                 "positivos": 100 * sum(x > 0 for x in v) / len(v), "n": len(v),
                 "vs_ibov_media": statistics.mean(vs[f]), "vs_ibov_mediana": statistics.median(vs[f])}
                for f, v in faixas.items() if v]

    # comparação com o teste que usa as ações de hoje, se existir
    comp = None
    antigo = RAIZ / "docs" / "b3" / "backtest_fund.json"
    if antigo.exists():
        a = json.loads(antigo.read_text(encoding="utf-8"))
        comp = {e["id"]: {"nome": e["nome"], "retorno_anual": e["retorno_anual"], "queda_maxima": e["queda_maxima"]}
                for e in a["estrategias"] if e["id"] in ("todas", "fund_top10", "fund_top10_stops", "risco_stops")}

    passo = max(1, (n - inicio) // 300)
    def amostra(c):
        return [round(x, 2) for x in c[::passo]] + ([round(c[-1], 2)] if (len(c) - 1) % passo else [])
    SAIDA.write_text(json.dumps({
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fonte": "B3 COTAHIST (preços e volume de todas as ações) + Yahoo (proventos) + CVM (balanços) + Banco Central (CDI)",
        "acoes": len(todos), "acoes_so_b3": sem_yahoo, "media_avaliadas": statistics.mean(n_aval),
        "periodo": {"inicio": datas[inicio], "fim": datas[-1], "pregoes": n - inicio},
        "parametros": {"taxa": BF.TAXA, "top_n": BF.TOP_N, "universo": UNIVERSO_N},
        "media_compra_por_mes": statistics.mean(sum(1 for x in e.values() if x["nf"] >= 60) for e in ent.values() if e),
        "estrategias": tabela, "stops": stats, "faixas": resumo_f, "comparacao_acoes_de_hoje": comp,
        "curvas": {"datas": datas[inicio:][::passo] + ([datas[-1]] if (n - 1 - inicio) % passo else []),
                   **{k: amostra(c) for k, c in curvas.items()}},
        "conferencia": [],
    }, ensure_ascii=False), encoding="utf-8")
    print("OK:", SAIDA)


if __name__ == "__main__":
    main()
