# CTE-GNRE — Emissor de Guias (GNRE e portais estaduais)

Programa desktop que lê o XML do CT-e autorizado, monta e emite a guia
GNRE 2.00 pelo webservice oficial (mTLS com certificado A1), entrega o PDF
de pagamento e, quando a UF não usa o GNRE, aciona os portais estaduais
(SP, ES e PR).

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

No menu (ícone no canto superior esquerdo) use **Verificar atualização**.
O app consulta a última release deste repositório e, se houver versão nova,
baixa, instala silenciosamente e reabre. A pasta `ConfGNRE` é preservada.

## Uso responsável

- O ambiente de homologação é o padrão do programa.
- A emissão em produção usa o certificado do contribuinte e gera guias com
  valor a pagar: confira o CT-e e a UF antes de emitir.
- O programa bloqueia guia divergente do CT-e e protege contra duplicidade
  pela chave do CT-e.

## Estrutura

- `app.py` — interface e fluxo de emissão
- `gnre_api.py` — cliente SOAP/mTLS do GNRE 2.00, regras por UF e PDF oficial
- `leitor_cte.py` — leitura e validação do XML do CT-e
- `validacao_pdf.py` — conferência do PDF contra o retorno oficial
- `atualizador.py` — verificação e instalação de novas versões pelo GitHub
- `bypassguia/` — robô dos portais estaduais (SP, ES, PR)
- `schemas_gnre/` — XSD do lote GNRE 2.00

## Aviso

Este repositório não contém certificado digital, senha, configuração de
contribuinte nem guias emitidas. O uso do programa é de responsabilidade do
contribuinte e deve seguir as regras da SEFAZ da UF favorecida.
