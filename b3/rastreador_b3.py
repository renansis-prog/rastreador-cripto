"""
Rastreador B3 — fundamentos + análise técnica.

1. Fundamentos (Fundamentus, foto atual de todas as ações): nota de 0 a 100
   por valuation, qualidade, saúde financeira, crescimento e dividendos.
2. Análise técnica (Yahoo Finance, preços ajustados por proventos): nota de 0 a 100
   pela mesma lógica do rastreador de criptos, com o Ibovespa no lugar do BTC.
3. Sinal, juntando as duas:
     Compra   fundamentos >= 60 e técnica >= 70  (empresa boa, tendência a favor)
     Observar fundamentos >= 60 e técnica <  70  (empresa boa, momento ainda não)
     Neutra   fundamentos entre 40 e 59
     Evitar   fundamentos <  40

Saída: docs/b3/data.json (lido por docs/b3/index.html).
"""
import io
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / "docs" / "b3" / "data.json"

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) rastreador-b3/1.0"}
FUNDAMENTUS_URL = "https://www.fundamentus.com.br/resultado.php"

LIQUIDEZ_MINIMA = 5_000_000      # R$ negociados por dia (média de 2 meses)
MAX_ACOES = 120                  # as mais líquidas, uma ação por empresa
INDICE = "^BVSP"

# Períodos em pregões (≈ 1 semana, 1 mês, 3 meses)
P_SEMANA, P_MES, P_TRIM = 5, 21, 63

SINAIS = ["Compra", "Observar", "Neutra", "Evitar"]


# ---------------------------------------------------------------- utilidades
def num_br(valor) -> float:
    """'1.234,56%' -> 1234.56 ; vazio -> NaN"""
    if valor is None or (isinstance(valor, float) and np.isnan(valor)):
        return np.nan
    if isinstance(valor, (int, float, np.integer, np.floating)):
        return float(valor)
    s = str(valor).strip().replace("%", "").replace("R$", "").strip()
    if not s or s in {"-", "N/D"}:
        return np.nan
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return np.nan


def chave(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9/]", "", t.lower())


def limpo(v):
    """NaN/inf -> None para o JSON."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return None if (np.isnan(f) or np.isinf(f)) else f


# ---------------------------------------------------------------- Fundamentus
# nome interno -> chave normalizada da coluna no Fundamentus
COLUNAS = {
    "papel": "papel", "cotacao": "cotacao", "pl": "p/l", "pvp": "p/vp", "dy": "divyield",
    "ev_ebit": "ev/ebit", "mrg_liq": "mrgliq", "roic": "roic", "roe": "roe",
    "liquidez": "liq2meses", "patrimonio": "patrimliq", "div_pat": "divliq/patrim",
    "cresc_rec_5a": "crescrec5a",
}


def coletar_fundamentus() -> pd.DataFrame:
    r, erro = None, None
    for tentativa in range(4):                  # o site às vezes demora para responder
        try:
            r = requests.get(FUNDAMENTUS_URL, headers=UA, timeout=45)
            r.raise_for_status()
            break
        except requests.RequestException as e:
            erro, r = e, None
            print(f"Fundamentus: tentativa {tentativa + 1} falhou ({type(e).__name__}); tentando de novo")
            time.sleep(20 * (tentativa + 1))
    if r is None:
        raise RuntimeError(f"Fundamentus não respondeu: {erro}")
    r.encoding = "iso-8859-1"
    # lê tudo como texto e converte com num_br: deixar o pandas adivinhar os números
    # multiplicava por 100 colunas como "0,58"
    cab = next(t for t in pd.read_html(io.StringIO(r.text)) if any(chave(c) == "papel" for c in t.columns)).columns
    tabelas = pd.read_html(io.StringIO(r.text), thousands=None, converters={c: str for c in cab})
    df = next(t for t in tabelas if any(chave(c) == "papel" for c in t.columns))
    mapa = {}
    for c in df.columns:
        k = chave(c)
        for nome, alvo in COLUNAS.items():
            if k == alvo:
                mapa[c] = nome
    faltando = set(COLUNAS) - set(mapa.values())
    if faltando:
        raise RuntimeError(f"Colunas não encontradas no Fundamentus: {sorted(faltando)} · colunas: {list(df.columns)}")
    df = df.rename(columns=mapa)[list(COLUNAS)]
    for c in df.columns:
        if c != "papel":
            df[c] = df[c].map(num_br)
    df["papel"] = df["papel"].astype(str).str.strip().str.upper()
    if len(df) < 200:
        raise RuntimeError(f"Fundamentus devolveu só {len(df)} linhas; a página pode ter mudado.")
    return df


def universo(fund: pd.DataFrame) -> pd.DataFrame:
    df = fund[(fund["liquidez"] >= LIQUIDEZ_MINIMA) & (fund["cotacao"] > 0)].copy()
    # uma ação por empresa (PETR3 e PETR4 contam como a mesma): fica a mais líquida
    df["empresa"] = df["papel"].str[:4]
    df = df.sort_values("liquidez", ascending=False).drop_duplicates("empresa")
    return df.head(MAX_ACOES).reset_index(drop=True)


# ---------------------------------------------------------------- nota fundamental
# (critério, pontos, texto curto, aplica-se a bancos/seguradoras?)
CRITERIOS_FUND = [
    ("pl_ok", 15, "P/L entre 0 e 15", True),
    ("pvp_ok", 10, "P/VP até 2", True),
    ("ev_ebit_ok", 15, "EV/EBIT entre 0 e 10", False),
    ("roe_ok", 15, "ROE de 15%+", True),
    ("roic_ok", 10, "ROIC de 10%+", False),
    ("margem_ok", 10, "Margem líquida de 10%+", False),
    ("divida_ok", 10, "Dívida líquida até 1× o patrimônio", False),
    ("cresc_ok", 10, "Receita crescendo 5%+ ao ano (5 anos)", True),
    ("dy_ok", 5, "Dividendos de 4%+", True),
]


def nota_fundamental(f: dict):
    """Pontuação proporcional aos critérios que se aplicam à empresa."""
    # Bancos e seguradoras aparecem com EV/EBIT, ROIC, margem e dívida zerados no Fundamentus
    financeira = all((f.get(k) or 0) == 0 for k in ("ev_ebit", "roic", "div_pat"))
    v = lambda k: f.get(k) if f.get(k) is not None and not np.isnan(f.get(k)) else None  # noqa: E731
    pl, pvp, ev, roe, roic, mrg, div, cr, dy = (v(k) for k in
        ("pl", "pvp", "ev_ebit", "roe", "roic", "mrg_liq", "div_pat", "cresc_rec_5a", "dy"))
    ok = {
        "pl_ok": pl is not None and 0 < pl <= 15,
        "pvp_ok": pvp is not None and 0 < pvp <= 2,
        "ev_ebit_ok": ev is not None and 0 < ev <= 10,
        "roe_ok": roe is not None and roe >= 15,
        "roic_ok": roic is not None and roic >= 10,
        "margem_ok": mrg is not None and mrg >= 10,
        "divida_ok": div is not None and div <= 1,      # negativa = mais caixa que dívida
        "cresc_ok": cr is not None and cr >= 5,
        "dy_ok": dy is not None and dy >= 4,
    }
    ganhos = possiveis = 0
    criterios = {}
    for k, pts, _, banco in CRITERIOS_FUND:
        if financeira and not banco:
            criterios[k] = None           # não se aplica
            continue
        possiveis += pts
        ganhos += pts if ok[k] else 0
        criterios[k] = ok[k]
    nota = round(100 * ganhos / possiveis) if possiveis else 0
    if pl is not None and pl <= 0:       # empresa com prejuízo nos últimos 12 meses
        nota = min(nota, 30)
    return nota, criterios, financeira


# ---------------------------------------------------------------- preços e técnica
def baixar_precos(tickers, periodo="15mo", ohlc=None):
    """Fechamentos ajustados por ação. Se `ohlc` for um dict, guarda nele máxima/mínima/fechamento (para o ATR)."""
    import yfinance as yf
    simbolos = [t + ".SA" for t in tickers] + [INDICE]
    df = None
    for tentativa in range(3):
        try:
            df = yf.download(simbolos, period=periodo, interval="1d", auto_adjust=True,
                             group_by="ticker", threads=True, progress=False)
            if df is not None and not df.empty:
                break
        except Exception as e:  # noqa: BLE001
            print("Yahoo falhou:", e)
        time.sleep(10 * (tentativa + 1))
    if df is None or df.empty:
        raise RuntimeError("Yahoo Finance não devolveu preços.")
    out = {}
    for s in simbolos:
        try:
            serie = df[s]["Close"].dropna()
        except KeyError:
            continue
        if len(serie) >= 60:
            out[s.replace(".SA", "")] = serie
            if ohlc is not None:
                try:
                    ohlc[s.replace(".SA", "")] = df[s][["High", "Low", "Close"]].dropna()
                except KeyError:
                    pass
    return out


# ---------------------------------------------------------------- níveis de saída
MULT_ATR_STOP = 2.0      # stop loss a 2 ATRs abaixo da cotação
RISCO_RETORNO = 2.0      # stop gain a 2× a distância do stop loss (4 ATRs acima)


def atr(hlc, n=14):
    """Average True Range de Wilder: a variação média diária da ação, em R$."""
    if hlc is None or len(hlc) <= n:
        return None
    h, l, c = hlc["High"].tolist(), hlc["Low"].tolist(), hlc["Close"].tolist()
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[:n]) / n
    for x in tr[n:]:
        a = (a * (n - 1) + x) / n
    return a


def niveis(preco, atr14, f):
    """Stop loss/gain por regra e preço justo de Graham."""
    out = {}
    if preco and atr14:
        stop = preco - MULT_ATR_STOP * atr14
        gain = preco + RISCO_RETORNO * MULT_ATR_STOP * atr14
        out.update({"atr14": atr14, "atr_pct": atr14 / preco * 100,
                    "stop_loss": stop, "stop_loss_pct": (stop / preco - 1) * 100,
                    "stop_gain": gain, "stop_gain_pct": (gain / preco - 1) * 100})
    pl, pvp = f.get("pl"), f.get("pvp")
    if preco and pl and pvp and pl > 0 and pvp > 0:
        # Graham: raiz(22,5 × LPA × VPA), com LPA = preço/PL e VPA = preço/PVP (cotação do Fundamentus)
        cot = f.get("cotacao") or preco
        justo = (22.5 * (cot / pl) * (cot / pvp)) ** 0.5
        out.update({"graham": justo, "graham_pct": (justo / preco - 1) * 100})
    return out


def media(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def retorno(v, n):
    return (v[-1] / v[-1 - n] - 1) * 100 if len(v) > n else None


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


PESOS_TEC = {
    "acima_mm20": 10, "acima_mm50": 15, "acima_mm200": 20, "mm50_acima_mm200": 15,
    "mm50_subindo": 10, "alta_1m": 10, "alta_3m": 5, "forte_vs_ibov": 15,
}


def analisar(closes, ibov_ret_mes=None):
    """Nota técnica (0–100) com os mesmos critérios das criptos, em pregões. None se histórico curto."""
    if len(closes) < 60:
        return None
    p = closes[-1]
    mm20, mm50, mm200 = media(closes, 20), media(closes, 50), media(closes, 200)
    mm50_ant = media(closes[:-10], 50)
    r_sem, r_mes, r_tri = retorno(closes, P_SEMANA), retorno(closes, P_MES), retorno(closes, P_TRIM)
    vs_ibov = None if (ibov_ret_mes is None or r_mes is None) else r_mes - ibov_ret_mes
    crit = {
        "acima_mm20": p > mm20,
        "acima_mm50": p > mm50,
        "acima_mm200": mm200 is not None and p > mm200,
        "mm50_acima_mm200": mm200 is not None and mm50 > mm200,
        "mm50_subindo": mm50_ant is not None and mm50 > mm50_ant,
        "alta_1m": r_mes is not None and r_mes > 0,
        "alta_3m": r_tri is not None and r_tri > 0,
        "forte_vs_ibov": (r_mes is not None and r_mes > 0) if ibov_ret_mes is None else (vs_ibov is not None and vs_ibov > 0),
    }
    nota = sum(PESOS_TEC[c] for c, ok in crit.items() if ok)
    return {
        "preco": p, "ret_1s": r_sem, "ret_1m": r_mes, "ret_3m": r_tri, "vs_ibov_1m": vs_ibov,
        "mm50": mm50, "mm200": mm200, "dist_mm200": (p / mm200 - 1) * 100 if mm200 else None,
        "rsi14": rsi(closes), "historico_curto": mm200 is None,
        "criterios_tec": crit, "nota_tec": nota,
    }


def sinal(nf, nt):
    if nf >= 60:
        return "Compra" if nt >= 70 else "Observar"
    return "Neutra" if nf >= 40 else "Evitar"


def clima(ibov):
    p, mm50, mm200 = ibov[-1], media(ibov, 50), media(ibov, 200)
    if mm200 is None:
        return {"estado": "indefinido"}
    acima, cruz = p > mm200, mm50 > mm200
    estado = "alta" if acima and cruz else "baixa" if not acima and not cruz else "transicao"
    return {"estado": estado, "ibov": p, "ibov_dist_mm200": (p / mm200 - 1) * 100,
            "ibov_ret_1m": retorno(ibov, P_MES), "ibov_ret_3m": retorno(ibov, P_TRIM)}


# ---------------------------------------------------------------- execução
def main():
    fund = coletar_fundamentus()
    uni = universo(fund)
    print(f"Fundamentus: {len(fund)} ações · {len(uni)} no universo (liquidez ≥ R$ {LIQUIDEZ_MINIMA/1e6:.0f} mi/dia)")

    hlc = {}
    precos = baixar_precos(uni["papel"].tolist(), ohlc=hlc)
    if INDICE not in precos:
        raise SystemExit("Sem dados do Ibovespa no Yahoo.")
    ibov = precos[INDICE].tolist()
    ibov_mes = retorno(ibov, P_MES)
    print(f"Yahoo: preços de {len(precos) - 1} ações")

    acoes = []
    for _, row in uni.iterrows():
        f = {k: (None if pd.isna(row[k]) else float(row[k])) for k in COLUNAS if k != "papel"}
        nf, crit_f, financeira = nota_fundamental(f)
        serie = precos.get(row["papel"])
        tec = analisar(serie.tolist(), ibov_mes) if serie is not None else None
        nt = tec["nota_tec"] if tec else 0
        acoes.append({
            "papel": row["papel"], "financeira": financeira,
            "fund": {k: limpo(v) for k, v in f.items()},
            "nota_fund": nf, "criterios_fund": crit_f,
            **({k: limpo(v) if not isinstance(v, dict) else v for k, v in tec.items()} if tec else {"nota_tec": 0, "sem_precos": True}),
            "sparkline": [round(float(x), 2) for x in serie.tolist()[-120:]] if serie is not None else [],
            **{k: limpo(v) for k, v in niveis(tec["preco"] if tec else None, atr(hlc.get(row["papel"])), f).items()},
            "sinal": sinal(nf, nt),
            "nota_final": round((nf + nt) / 2),
        })

    if sum(1 for a in acoes if not a.get("sem_precos")) < len(acoes) // 2:
        raise SystemExit("Menos da metade das ações veio com preço — dados não salvos.")

    ordem = {s: i for i, s in enumerate(SINAIS)}
    acoes.sort(key=lambda a: (ordem[a["sinal"]], -a["nota_final"]))
    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps({
        "atualizado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pregao": precos[INDICE].index[-1].strftime("%Y-%m-%d"),
        "fonte": "Fundamentus (fundamentos) + Yahoo Finance (preços ajustados por proventos)",
        "parametros": {"liquidez_minima": LIQUIDEZ_MINIMA, "max_acoes": MAX_ACOES,
                       "pesos_tec": PESOS_TEC, "mult_atr_stop": MULT_ATR_STOP, "risco_retorno": RISCO_RETORNO,
                       "criterios_fund": [{"id": k, "pontos": p, "texto": t, "bancos": b} for k, p, t, b in CRITERIOS_FUND]},
        "mercado": {k: limpo(v) if not isinstance(v, str) else v for k, v in clima(ibov).items()},
        "acoes": acoes,
    }, ensure_ascii=False), encoding="utf-8")
    contagem = {s: sum(1 for a in acoes if a["sinal"] == s) for s in SINAIS}
    print(f"OK: {len(acoes)} ações · {contagem}")


if __name__ == "__main__":
    sys.exit(main())
