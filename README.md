# GNRE — Emissor de Guias (GNRE e portais estaduais)

Programa desktop que lê o XML do CT-e autorizado, monta e emite a guia
GNRE 2.00 pelo webservice oficial (mTLS com certificado A1), entrega o PDF
de pagamento e, quando a UF não usa o GNRE, aciona os portais estaduais
(SP, ES e PR).

## Entrada do CT-e

O cartão **CT-e autorizado** tem duas abas:

- **XML** — seleciona o arquivo autorizado do computador.
- **Chave do CT-e** — informe a chave de 44 dígitos e clique em **Adicionar**.
  O programa consulta a Distribuição DF-e do CT-e com o certificado A1,
  baixa o XML autorizado, confere cancelamento e já preenche os dados da guia.
  Várias chaves podem ser adicionadas para gerar o lote (mesma UF).

Os XMLs baixados ficam em `Documentos\ConfGNRE\xmls`, junto do ponto de
parada da consulta. A consulta usa a janela mais recente da distribuição
(por causa do limite de requisições da SEFAZ); para CT-es mais antigos,
use a aba **XML**. Se a SEFAZ limitar as consultas (consumo indevido), o
programa avisa e reaproveita os XMLs já baixados.

## Instalação

1. Baixe `Instalar GNRE.exe` na última [release](../../releases/latest).
2. Execute o instalador: ele grava o programa em
   `%LOCALAPPDATA%\Programs\GNRE`, cria a pasta de configuração em
   `Documentos\ConfGNRE` e os atalhos na Área de Trabalho e no Menu Iniciar.
3. Abra o Emissor de Guias, selecione o certificado A1 e o CT-e.

O certificado, a configuração, o cache e as guias emitidas ficam somente em
`Documentos\ConfGNRE` (ou `%LOCALAPPDATA%\ConfGNRE` quando a pasta Documentos
não permite escrita). Nada sensível é gravado na pasta do programa.

## Atualização

Ao abrir, o programa confere em segundo plano a última release deste
repositório e avisa quando há versão nova. Também é possível conferir a
qualquer momento pelo menu (ícone no canto superior esquerdo):
**Verificar atualização**. O download e a instalação são automáticos, e a
pasta `ConfGNRE` é preservada.

Para publicar uma versão nova, crie uma release com tag `vX.Y.Z` e envie os
arquivos `Instalar GNRE.exe` e `GNRE.exe`. O programa compara a tag com a
versão instalada, prefere o instalador completo e, se ele não estiver
presente, atualiza somente o executável.

## Uso responsável

- O ambiente de homologação é o padrão do programa.
- A emissão em produção usa o certificado do contribuinte e gera guias com
  valor a pagar: confira o CT-e e a UF antes de emitir.
- O programa bloqueia guia divergente do CT-e e protege contra duplicidade
  pela chave do CT-e.
- O PDF entregue é o oficial do portal sempre que ele está disponível; quando
  o portal não o entrega, o programa gera a réplica local no mesmo layout.
  A conferência compara os dados fiscais (valores, datas, CNPJ/CPF, Nº de
  Controle, receita, linha digitável, UF, código de barras e QR PIX) — fonte
  ou posição diferentes não bloqueiam a guia.

## Estrutura

- `app.py` — interface e fluxo de emissão
- `gnre_api.py` — cliente SOAP/mTLS do GNRE 2.00, regras por UF e PDF oficial
- `consulta_cte.py` — distribuição DF-e do CT-e (busca o XML pela chave)
- `leitor_cte.py` — leitura e validação do XML do CT-e
- `validacao_pdf.py` — conferência do PDF contra o retorno oficial
- `atualizador.py` — verificação e instalação de novas versões pelo GitHub
- `bypassguia/` — robô dos portais estaduais (SP, ES, PR)
- `schemas_gnre/` — XSD do lote GNRE 2.00

## Aviso

Este repositório não contém certificado digital, senha, configuração de
contribuinte nem guias emitidas. O uso do programa é de responsabilidade do
contribuinte e deve seguir as regras da SEFAZ da UF favorecida.
