# Rastreador de Criptos

Triagem diária de criptomoedas por **tendência de preço**.
Dados do **CoinGecko** (preço médio de todas as corretoras, market cap e ranking), filtrados para
as moedas que têm par em USDT na **Binance**, que é onde a compra é feita.
Roda sozinho no GitHub Actions e publica um painel no GitHub Pages.

## Como funciona

1. Pega as 300 maiores moedas por market cap e fica só com as que são negociadas em USDT na Binance,
   com volume de 24h acima de US$ 10 milhões, sem stablecoins nem tokens embrulhados (WBTC, stETH…).
2. Baixa 220 dias de preço diário das 120 maiores que sobraram.
3. Dá uma nota de 0 a 100:

| Critério | Pontos |
|---|---|
| Preço acima da MM20 | 10 |
| Preço acima da MM50 | 15 |
| Preço acima da MM200 | 20 |
| MM50 acima da MM200 | 15 |
| MM50 subindo (10 dias) | 10 |
| Subiu mais que o BTC em 30 dias | 15 |
| Retorno 30 dias positivo | 10 |
| Retorno 90 dias positivo | 5 |

80+ alta forte · 60–79 alta · 40–59 lateral · <40 baixa. O RSI(14) aparece como alerta quando passa de 70.

O topo do painel mostra o **clima do mercado** pelo BTC (acima ou abaixo da MM200). Em mercado de baixa,
até moedas de nota alta costumam cair junto.

## Instalação (uma vez)

1. Repositório **público** (o GitHub Pages gratuito só funciona assim).
2. **Chave do CoinGecko** (gratuita): crie conta em coingecko.com/en/api/pricing → plano *Demo* →
   *Developer Dashboard* → *Add New Key*. Copie a chave (começa com `CG-`).
3. No repositório: **Settings → Secrets and variables → Actions → New repository secret**.
   Nome: `COINGECKO_API_KEY` · Valor: a chave.
4. **Settings → Pages**: *Deploy from a branch*, branch `main`, pasta `/docs`.
5. **Actions → Atualizar rastreador → Run workflow** (leva uns 6 minutos).

Painel: `https://renansis-prog.github.io/rastreador-cripto/` — atualiza sozinho todo dia às 00:17 (Brasília).

Consumo: cerca de 150 chamadas por dia (~4.500/mês), dentro das 10.000 do plano gratuito.

## Ajustes

No topo de `rastreador.py`: `TOP_MARKET_CAP`, `VOLUME_MINIMO_USD`, `MAX_MOEDAS` e a lista `STABLES`.
Os pesos da nota ficam em `PESOS`.

> Triagem técnica, não recomendação de investimento.
