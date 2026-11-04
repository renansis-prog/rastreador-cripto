# Rastreador de Criptos

Triagem diária de criptomoedas por **tendência de preço**, com dados públicos da Binance.
Roda sozinho no GitHub Actions e publica um painel no GitHub Pages.

## Como funciona

1. Pega todos os pares em USDT da Binance com volume de 24h acima de US$ 10 milhões (sem stablecoins nem tokens alavancados) e fica com as 120 mais negociadas.
2. Baixa 220 dias de candles diários de cada uma.
3. Dá uma nota de 0 a 100:

| Critério | Pontos |
|---|---|
| Preço acima da MM20 | 15 |
| Preço acima da MM50 | 20 |
| Preço acima da MM200 | 20 |
| MM50 acima da MM200 | 15 |
| MM50 subindo (10 dias) | 10 |
| Retorno 30 dias positivo | 10 |
| Retorno 90 dias positivo | 10 |

80+ alta forte · 60–79 alta · 40–59 lateral · <40 baixa. O RSI(14) aparece como alerta quando passa de 70.

## Instalação (uma vez)

1. Crie o repositório `rastreador-cripto` no GitHub e suba estes arquivos.
2. **Settings → Pages**: Source = *Deploy from a branch*, branch `main`, pasta `/docs`.
3. **Actions → Atualizar rastreador → Run workflow** para gerar os primeiros dados.
4. Abra `https://renansis-prog.github.io/rastreador-cripto/`.

Depois disso atualiza sozinho todo dia às 00:17 (Brasília).

## Ajustes

No topo de `rastreador.py`: `VOLUME_MINIMO_USDT`, `MAX_MOEDAS` e a lista `STABLES`.
Os pesos da nota ficam na função `analisar`.

Rodar localmente: `python rastreador.py` (só biblioteca padrão).

> Triagem técnica, não recomendação de investimento.
