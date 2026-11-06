"""
Histórico de fundamentos das empresas do painel, a partir dos balanços abertos da CVM.

Para cada empresa e cada trimestre (desde ANO_INICIAL):
  receita, EBIT e lucro do trimestre; patrimônio líquido, ativo total, dívida bruta,
  caixa e número de ações em circulação no fim do trimestre.

Fontes (dados.cvm.gov.br):
  ITR  = 1º, 2º e 3º trimestres (resultado do trimestre vem pronto)
  DFP  = ano fechado (o 4º trimestre = ano − 9 meses)
  FCA  = códigos de negociação, para ligar o ticker (PETR4) ao CNPJ

Para não olhar o futuro no backtest, cada trimestre só fica "disponível" 90 dias depois
do fim (o prazo da CVM é 45 dias para ITR e 3 meses para DFP).

Saída: b3/dados/fundamentos_trimestrais.csv
"""
import io
import json
import math
import re
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import requests

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / "b3" / "dados" / "fundamentos_trimestrais.csv"
PAINEL = RAIZ / "docs" / "b3" / "data.json"
BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"
ANO_INICIAL = 2016
DIAS_DISPONIVEL = 90
UA = {"User-Agent": "Mozilla/5.0 rastreador-b3/1.0"}

CONTAS = {
    "DRE": {"3.01": "receita", "3.05": "ebit", "3.11": "lucro", "3.09": "lucro_alt"},
    "BPA": {"1": "ativo", "1.01.01": "caixa", "1.01.02": "aplicacoes"},
    "BPP": {"2.03": "pl", "2.01.04": "emprest_cp", "2.02.01": "emprest_lp"},
}


# ---------------------------------------------------------------- download
def baixar_zip(url):
    for tentativa in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=180)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return zipfile.ZipFile(io.BytesIO(r.content))
        except requests.RequestException as e:
            print(f"  {url.rsplit('/', 1)[-1]}: tentativa {tentativa + 1} falhou ({type(e).__name__})")
            time.sleep(15 * (tentativa + 1))
    raise RuntimeError(f"CVM não respondeu: {url}")


def ler(z, padrao, colunas=None):
    """Lê o CSV do zip cujo nome casa com o padrão (regex, sem diferenciar maiúsculas)."""
    nomes = [n for n in z.namelist() if re.search(padrao, n, re.I)]
    if not nomes:
        return None
    with z.open(nomes[0]) as f:
        df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)
    if colunas:
        df = df[[c for c in colunas if c in df.columns]]
    return df


# ---------------------------------------------------------------- tickers
def mapa_cnpj(prefixos):
    """{prefixo de 4 letras do ticker: CNPJ}, pelo FCA (cadastro de valores mobiliários)."""
    ano = date.today().year
    for a in (ano, ano - 1, ano - 2):
        z = baixar_zip(f"{BASE}/FCA/DADOS/fca_cia_aberta_{a}.zip")
        if z is None:
            continue
        df = ler(z, r"valor_mobiliario")
        if df is None:
            continue
        c_cnpj = next(c for c in df.columns if "cnpj" in c.lower())
        c_cod = next(c for c in df.columns if "codigo_negociacao" in c.lower())
        mapa = {}
        for cnpj, cod in zip(df[c_cnpj], df[c_cod]):
            if isinstance(cod, str) and len(cod) >= 4 and cod[:4] in prefixos:
                mapa.setdefault(cod[:4], cnpj)
        print(f"FCA {a}: {len(mapa)} de {len(prefixos)} empresas encontradas")
        return mapa
    raise RuntimeError("Não consegui ler o FCA da CVM.")


# ---------------------------------------------------------------- balanços
def contas(z, tipo, cnpjs):
    """Valores das contas de interesse, consolidado (ou individual se a empresa não publica consolidado)."""
    cols = ["CNPJ_CIA", "DT_REFER", "DT_INI_EXERC", "DT_FIM_EXERC", "ORDEM_EXERC",
            "CD_CONTA", "ESCALA_MOEDA", "VL_CONTA"]
    partes = []
    for grupo in ("con", "ind"):
        df = ler(z, rf"_{tipo}_{grupo}_", cols)
        if df is None:
            continue
        df = df[df["CNPJ_CIA"].isin(cnpjs) & df["CD_CONTA"].isin(CONTAS[tipo])]
        df = df[df["ORDEM_EXERC"].str.upper().str.startswith("ÚLT") | df["ORDEM_EXERC"].str.upper().str.startswith("ULT")]
        df = df.assign(grupo=grupo)
        partes.append(df)
    if not partes:
        return pd.DataFrame()
    df = pd.concat(partes)
    # prefere consolidado; usa individual só para quem não tem consolidado naquela data
    tem_con = set(map(tuple, df[df.grupo == "con"][["CNPJ_CIA", "DT_REFER"]].values))
    df = df[(df.grupo == "con") | ~df[["CNPJ_CIA", "DT_REFER"]].apply(tuple, axis=1).isin(tem_con)]
    escala = df["ESCALA_MOEDA"].str.upper().map({"MIL": 1000.0, "UNIDADE": 1.0}).fillna(1.0)
    df = df.assign(valor=pd.to_numeric(df["VL_CONTA"], errors="coerce") * escala,
                   conta=df["CD_CONTA"].map(CONTAS[tipo]))
    return df


def acoes(z, cnpjs):
    df = ler(z, r"composicao_capital")
    if df is None:
        return pd.DataFrame(columns=["CNPJ_CIA", "DT_REFER", "acoes"])
    c_total = next((c for c in df.columns if re.search(r"TOTAL_CAP_INTEGR", c, re.I)), None)
    c_tes = next((c for c in df.columns if re.search(r"TOTAL_TESOURO", c, re.I)), None)
    c_cnpj = next(c for c in df.columns if c.upper().startswith("CNPJ"))
    df = df[df[c_cnpj].isin(cnpjs)]
    tot = pd.to_numeric(df[c_total], errors="coerce") if c_total else 0
    tes = pd.to_numeric(df[c_tes], errors="coerce").fillna(0) if c_tes else 0
    return pd.DataFrame({"CNPJ_CIA": df[c_cnpj], "DT_REFER": df["DT_REFER"], "acoes": tot - tes})


def resultado_trimestral(dre, anual):
    """DRE do trimestre. ITR: linha cujo início é o começo do trimestre. DFP: ano inteiro."""
    if dre.empty:
        return dre
    dre = dre.copy()
    ini = pd.to_datetime(dre["DT_INI_EXERC"], errors="coerce")
    fim = pd.to_datetime(dre["DT_FIM_EXERC"], errors="coerce")
    meses = (fim.dt.year - ini.dt.year) * 12 + fim.dt.month - ini.dt.month + 1
    dre["meses"] = meses
    if anual:
        return dre[meses >= 12]
    return dre[meses <= 3]


def normalizar_acoes(serie):
    """Número de ações na mesma base do preço do Yahoo (ajustado por desdobramentos).

    A CVM mistura escalas (milhares num balanço, unidades no outro) e a série muda em
    desdobramentos/grupamentos, que o Yahoo já desconta no preço. Andando do mais recente
    para trás: corrige saltos de ×1000; uma variação acima de 30% num trimestre é tratada
    como desdobramento/grupamento (fica igual à do trimestre seguinte).
    """
    v = serie.replace(0, float("nan")).tolist()
    out = [float("nan")] * len(v)
    ref = None
    for i in range(len(v) - 1, -1, -1):
        x = v[i]
        if x is None or x != x:
            continue
        if ref is None:
            out[i] = ref = x
            continue
        x = min((x * 1000.0 ** k for k in range(-3, 4)), key=lambda y: abs(math.log(y / ref)))
        if not 0.77 <= x / ref <= 1.3:
            x = ref
        out[i] = ref = x
    return pd.Series(out, index=serie.index).ffill().bfill()


def main():
    painel = json.loads(PAINEL.read_text(encoding="utf-8"))
    tickers = [a["papel"] for a in painel["acoes"]]
    prefixos = {t[:4] for t in tickers}
    mapa = mapa_cnpj(prefixos)
    cnpjs = set(mapa.values())

    trimestres, anuais, saldos, n_acoes = [], [], [], []
    for ano in range(ANO_INICIAL, date.today().year + 1):
        for doc in ("ITR", "DFP"):
            z = baixar_zip(f"{BASE}/{doc}/DADOS/{doc.lower()}_cia_aberta_{ano}.zip")
            if z is None:
                print(f"{doc} {ano}: ainda não publicado")
                continue
            dre = resultado_trimestral(contas(z, "DRE", cnpjs), anual=(doc == "DFP"))
            (anuais if doc == "DFP" else trimestres).append(dre)
            saldos += [contas(z, "BPA", cnpjs), contas(z, "BPP", cnpjs)]
            n_acoes.append(acoes(z, cnpjs))
            print(f"{doc} {ano}: {dre['CNPJ_CIA'].nunique() if not dre.empty else 0} empresas")

    def pivot(df):
        if df.empty:
            return pd.DataFrame()
        return df.pivot_table(index=["CNPJ_CIA", "DT_REFER"], columns="conta", values="valor", aggfunc="first")

    q = pivot(pd.concat(trimestres)) if trimestres else pd.DataFrame()
    a = pivot(pd.concat(anuais)) if anuais else pd.DataFrame()
    s = pivot(pd.concat([x for x in saldos if not x.empty]))
    n = pd.concat(n_acoes).dropna().drop_duplicates(["CNPJ_CIA", "DT_REFER"], keep="last").set_index(["CNPJ_CIA", "DT_REFER"])

    for df in (q, a):
        if not df.empty:
            if "lucro" not in df.columns:
                df["lucro"] = float("nan")
            if "lucro_alt" in df.columns:
                df["lucro"] = df["lucro"].fillna(df["lucro_alt"])

    # 4º trimestre = ano − (1º + 2º + 3º)
    linhas = []
    if not a.empty:
        for (cnpj, ref), r in a.iterrows():
            ano = ref[:4]
            tri = q.loc[cnpj] if cnpj in q.index.get_level_values(0) else pd.DataFrame()
            tri = tri[[str(i).startswith(ano) for i in tri.index]] if len(tri) else tri
            if len(tri) != 3:
                continue
            d = {"CNPJ_CIA": cnpj, "DT_REFER": ref}
            for c in ("receita", "ebit", "lucro"):
                if c in a.columns:
                    d[c] = r[c] - tri[c].sum() if c in tri.columns and tri[c].notna().all() else float("nan")
            linhas.append(d)
    q4 = pd.DataFrame(linhas).set_index(["CNPJ_CIA", "DT_REFER"]) if linhas else pd.DataFrame()
    fluxo = pd.concat([q[[c for c in ("receita", "ebit", "lucro") if c in q.columns]], q4])
    fluxo = fluxo[~fluxo.index.duplicated(keep="last")]

    tudo = fluxo.join(s, how="outer").join(n, how="left").reset_index()
    tudo["divida"] = tudo.get("emprest_cp", 0).fillna(0) + tudo.get("emprest_lp", 0).fillna(0)
    tudo["caixa_total"] = tudo.get("caixa", 0).fillna(0) + tudo.get("aplicacoes", 0).fillna(0)
    inv = {v: k for k, v in mapa.items()}
    tudo["empresa"] = tudo["CNPJ_CIA"].map(inv)
    tudo["data"] = pd.to_datetime(tudo["DT_REFER"])
    tudo["disponivel_em"] = tudo["data"] + pd.Timedelta(days=DIAS_DISPONIVEL)
    cols = ["empresa", "CNPJ_CIA", "data", "disponivel_em", "receita", "ebit", "lucro", "pl", "ativo",
            "divida", "caixa_total", "acoes"]
    for c in cols:
        if c not in tudo.columns:
            tudo[c] = float("nan")
    tudo = tudo[cols].sort_values(["empresa", "data"])
    tudo = tudo[tudo["data"].dt.month.isin([3, 6, 9, 12])]
    tudo["acoes"] = tudo.groupby("empresa")["acoes"].transform(normalizar_acoes)
    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    tudo.to_csv(SAIDA, index=False, date_format="%Y-%m-%d")
    ok = tudo.dropna(subset=["lucro", "pl"])
    print(f"OK: {tudo['empresa'].nunique()} empresas, {len(tudo)} trimestres "
          f"({len(ok)} com lucro e PL) em {SAIDA}")
    # amostra para conferência no log
    for emp in ("PETR", "ITUB", "WEGE", "MILS"):
        x = tudo[tudo["empresa"] == emp].tail(4)
        if len(x):
            print(emp, x[["data", "receita", "lucro", "pl", "divida", "acoes"]].to_string(index=False))


if __name__ == "__main__":
    sys.exit(main())
