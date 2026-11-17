"""Guias estaduais emitidas pelo portal (SP, ES e PR) para o app unificado.

O BypassGuia roda em segundo plano, sem janela, reaproveitando o mesmo XML do CT-e.
"""

from __future__ import annotations

import os
from pathlib import Path

from gnre_api import ResultadoEmissao
from leitor_cte import DadosCTe, ler_cte_xml

UFS_PORTAL = ("SP", "ES", "PR")
UFS_IMPLANTACAO = ("GO",)


def usa_portal(uf: str | None) -> bool:
    return (uf or "").strip().upper() in UFS_PORTAL


def em_implantacao(uf: str | None) -> bool:
    return (uf or "").strip().upper() in UFS_IMPLANTACAO


def _carregar_modulo():
    """Importa o BypassGuia só quando necessário (e depois do env estar definido)."""
    # O app define BYPASSGUIA_DIR com a pasta ConfGNRE realmente utilizável;
    # este padrão só vale para uso isolado do módulo.
    os.environ.setdefault(
        "BYPASSGUIA_DIR", str(Path.home() / "ConfGNRE" / "portal")
    )
    from bypassguia.config import carregar
    from bypassguia.runner import Runner

    return Runner, carregar


def _dados_cte(caminho: str) -> DadosCTe | None:
    try:
        return ler_cte_xml(caminho)
    except Exception:
        return None


def emitir(
    caminho: str | Path,
    dados: DadosCTe | None = None,
    log=None,
    visivel: bool | None = None,
    saida_dir: Path | None = None,
) -> ResultadoEmissao:
    """Emite a guia pelo portal e devolve o resultado no formato do app GNRE."""
    caminho = str(caminho)
    dados = dados or _dados_cte(caminho)
    Runner, carregar = _carregar_modulo()
    config = carregar()
    if visivel is not None:
        config["modo_visivel"] = bool(visivel)

    def _log_portal(mensagem: str, nivel: str = "info") -> None:
        if log is None:
            return
        prefixo = {"aviso": "AVISO: ", "erro": "ERRO: "}.get(nivel, "")
        log(prefixo + mensagem)

    runner = Runner(config=config, log=_log_portal, saida_dir=saida_dir)
    entrega = runner.processar(caminho)

    resultado = ResultadoEmissao(
        sucesso=entrega.estado == "ok",
        ambiente="portal",
        dados=dados,
        avisos=list(entrega.avisos),
        pdf_path=entrega.pdf,
    )
    if entrega.estado == "ok":
        resultado.pdf_local = True
        resultado.mensagens = []
        return resultado
    if entrega.estado == "divergente":
        resultado.guia_divergente = True
        resultado.divergencias = [entrega.mensagem]
        resultado.pdf_path = None
        return resultado
    resultado.pdf_path = None
    if entrega.estado == "captcha":
        resultado.mensagens = [
            "O portal pediu captcha. Use Opções → Emitir com navegador visível "
            "para concluir esta UF.",
            entrega.mensagem,
        ]
    else:
        resultado.mensagens = [entrega.mensagem or "Falha ao emitir pelo portal."]
    return resultado
