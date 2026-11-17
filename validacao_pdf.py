"""Conferência automática da guia local contra o PDF oficial do portal."""

from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable

LIMITE_PRODUCAO = 5
_TOLERANCIA = 0.25
_ESTADO = Path(__file__).resolve().parent / "cache" / "validacao_pdf.json"


def _ler_estado() -> dict:
    try:
        dados = json.loads(_ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dados = {}
    if not isinstance(dados, dict):
        dados = {}
    dados.setdefault("usadas", 0)
    dados.setdefault("historico", [])
    return dados


def _gravar_estado(dados: dict) -> None:
    _ESTADO.parent.mkdir(parents=True, exist_ok=True)
    historico = dados.get("historico") or []
    dados["historico"] = historico[-30:]
    _ESTADO.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def usadas() -> int:
    try:
        return int(_ler_estado().get("usadas") or 0)
    except (TypeError, ValueError):
        return 0


def ativo(_ambiente: str = "") -> bool:
    """True enquanto as 5 emissões de produção não foram conferidas."""
    return usadas() < LIMITE_PRODUCAO


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()


def _spans(pagina) -> list[tuple]:
    achados = []
    for bloco in pagina.get_text("rawdict")["blocks"]:
        for linha in bloco.get("lines", []):
            for span in linha["spans"]:
                texto = "".join(ch["c"] for ch in span["chars"])
                if not texto.strip():
                    continue
                achados.append(
                    (
                        span["font"],
                        round(float(span["size"]), 1),
                        _sem_acento(texto),
                        float(span["origin"][0]),
                        float(span["origin"][1]),
                    )
                )
    return achados


def _comparar_texto(local, oficial) -> tuple[int, int, list[str]]:
    """(conferidos, total, divergências) entre duas páginas, por posição."""
    posicoes_local = defaultdict(list)
    posicoes_oficial = defaultdict(list)
    for fonte, tamanho, texto, x, y in _spans(local):
        posicoes_local[(fonte, tamanho, texto)].append((y, x))
    for fonte, tamanho, texto, x, y in _spans(oficial):
        posicoes_oficial[(fonte, tamanho, texto)].append((y, x))

    conferidos = 0
    total = sum(len(v) for v in posicoes_oficial.values())
    divergencias = []
    for chave, esperadas in posicoes_oficial.items():
        obtidas = posicoes_local.get(chave, [])
        if len(obtidas) != len(esperadas):
            divergencias.append(
                f"'{chave[2]}' aparece {len(obtidas)}x no local e "
                f"{len(esperadas)}x no oficial"
            )
            continue
        for (y_local, x_local), (y_oficial, x_oficial) in zip(
            sorted(obtidas), sorted(esperadas)
        ):
            if (
                abs(y_local - y_oficial) > _TOLERANCIA
                or abs(x_local - x_oficial) > _TOLERANCIA
            ):
                divergencias.append(
                    f"'{chave[2]}' fora de lugar: local ({x_local:.2f}, {y_local:.2f}) "
                    f"x oficial ({x_oficial:.2f}, {y_oficial:.2f})"
                )
            else:
                conferidos += 1
    return conferidos, total, divergencias


def _decodificar(pagina) -> dict[str, set[str]]:
    import numpy
    import zxingcpp

    imagem = pagina.get_pixmap(dpi=200)
    matriz = numpy.frombuffer(
        imagem.samples, dtype=numpy.uint8
    ).reshape(imagem.height, imagem.width, imagem.n)
    achados: dict[str, set[str]] = {"ITF": set(), "QR": set()}
    for codigo in zxingcpp.read_barcodes(matriz):
        formato = str(codigo.format).upper()
        if "ITF" in formato or "INTERLEAVED" in formato:
            achados["ITF"].add(codigo.text)
        elif "QR" in formato:
            achados["QR"].add(codigo.text)
    return achados


def _comparar_codigos(paginas_local, paginas_oficial) -> tuple[dict, list[str]]:
    local: dict[str, set[str]] = {"ITF": set(), "QR": set()}
    oficial: dict[str, set[str]] = {"ITF": set(), "QR": set()}
    for pagina in paginas_local:
        for tipo, valores in _decodificar(pagina).items():
            local[tipo].update(valores)
    for pagina in paginas_oficial:
        for tipo, valores in _decodificar(pagina).items():
            oficial[tipo].update(valores)

    divergencias = []
    if local["ITF"] != oficial["ITF"]:
        divergencias.append(
            f"código de barras difere: local {sorted(local['ITF'])} x "
            f"oficial {sorted(oficial['ITF'])}"
        )
    if local["QR"] != oficial["QR"]:
        divergencias.append("payload do QR PIX difere do oficial")
    return oficial, divergencias


def validar(
    ambiente: str,
    recibo: str,
    local: Path,
    oficial: Path,
    relatorio: Path,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """Compara a guia local com o PDF oficial e grava o relatório."""
    local = Path(local)
    oficial = Path(oficial)
    if not local.is_file() or not oficial.is_file():
        return None

    def registrar(mensagem: str) -> None:
        if log:
            log(mensagem)

    try:
        import numpy  # noqa: F401
        import pymupdf
        import zxingcpp  # noqa: F401
    except ImportError:
        registrar(
            "Modo validação indisponível: instale as dependências "
            "'pymupdf' e 'zxing-cpp' para a conferência automática."
        )
        return None

    numero = usadas() + 1
    rotulo = f"{numero}/{LIMITE_PRODUCAO}" if ambiente == "1" else "teste"
    linhas: list[str] = []
    detalhes: list[str] = []
    try:
        doc_local = pymupdf.open(local)
        doc_oficial = pymupdf.open(oficial)
        try:
            paginas_local = [doc_local[i] for i in range(doc_local.page_count)]
            paginas_oficial = [doc_oficial[i] for i in range(doc_oficial.page_count)]

            if doc_local.page_count != doc_oficial.page_count:
                detalhes.append(
                    f"páginas: local {doc_local.page_count} x oficial "
                    f"{doc_oficial.page_count}"
                )
            else:
                linhas.append(
                    f"Páginas: {doc_local.page_count} — mesma quantidade."
                )
                for indice, (pl, po) in enumerate(
                    zip(paginas_local, paginas_oficial), start=1
                ):
                    if abs(pl.rect.width - po.rect.width) > 0.5 or abs(
                        pl.rect.height - po.rect.height
                    ) > 0.5:
                        detalhes.append(
                            f"p{indice}: tamanho local "
                            f"{pl.rect.width:.1f}x{pl.rect.height:.1f} x oficial "
                            f"{po.rect.width:.1f}x{po.rect.height:.1f}"
                        )

            conferidos = total = 0
            for pl, po in zip(paginas_local, paginas_oficial):
                c, t, divs = _comparar_texto(pl, po)
                conferidos += c
                total += t
                detalhes.extend(divs)
            if total:
                linhas.append(
                    f"Texto: {conferidos}/{total} trechos idênticos "
                    "(conteúdo, fonte, tamanho e posição)."
                )

            codigos, divs = _comparar_codigos(paginas_local, paginas_oficial)
            detalhes.extend(divs)
            if not divs:
                barras = ", ".join(sorted(codigos["ITF"])) or "nenhum"
                linhas.append(f"Código de barras ITF: idêntico ({barras}).")
                linhas.append("QR PIX: mesmo payload do oficial.")
        finally:
            doc_local.close()
            doc_oficial.close()
    except Exception as erro:  # noqa: BLE001 - a conferência não pode derrubar a emissão
        registrar(f"Modo validação falhou ao ler os PDFs: {erro}")
        return None

    ok = not detalhes
    linhas.insert(0, f"Conferência do PDF — {'OK' if ok else 'DIVERGÊNCIA'} ({rotulo}).")
    for item in detalhes[:40]:
        linhas.append(f"- {item}")

    relatorio = Path(relatorio)
    relatorio.parent.mkdir(parents=True, exist_ok=True)
    cabecalho = [
        f"Data: {datetime.now():%d/%m/%Y %H:%M:%S}",
        f"Ambiente: {'produção' if ambiente == '1' else 'homologação'}",
        f"Recibo: {recibo or '-'}",
        f"Guia local: {local.name}",
        f"Guia oficial: {oficial.name}",
        "",
    ]
    relatorio.write_text(
        "\n".join(cabecalho + linhas) + "\n", encoding="utf-8"
    )

    estado = _ler_estado()
    if ambiente == "1":
        estado["usadas"] = numero
    estado["historico"].append(
        {
            "data": datetime.now().isoformat(timespec="seconds"),
            "ambiente": ambiente,
            "recibo": recibo,
            "ok": ok,
            "local": local.name,
            "oficial": oficial.name,
            "relatorio": relatorio.name,
        }
    )
    _gravar_estado(estado)

    resumo = linhas[0]
    if detalhes:
        resumo += f" {len(detalhes)} divergência(s)."
    resumo += f" Relatório: {relatorio}"
    registrar(linhas[0])
    for linha in linhas[1:]:
        registrar(linha)
    return {
        "ok": ok,
        "linhas": linhas,
        "detalhes": detalhes,
        "resumo": resumo,
        "relatorio": str(relatorio),
        "rotulo": rotulo,
    }
