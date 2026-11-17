"""Compatibilidade com o fluxo antigo por Selenium.

O preenchimento do portal foi substituído pelo Web Service oficial em
`gnre_api.emitir_guia`, que autentica com o e-CNPJ e devolve o PDF da guia.
"""

from __future__ import annotations

import gnre_api


def executar_robo_gnre(*_args, **_kwargs):
    raise RuntimeError(
        "O robô Selenium foi removido. Use gnre_api.emitir_guia(caminho_cte) "
        "ou a interface do app.py para emitir a GNRE pelo Web Service."
    )


__all__ = ["executar_robo_gnre", "gnre_api"]
