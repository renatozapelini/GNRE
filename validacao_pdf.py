"""Conferência automática da guia local contra o PDF oficial do portal.

A comparação é feita sobre os dados fiscais impressos na guia — valores,
datas, CNPJ/CPF, Nº de Controle, código da receita, linha digitável, UF,
código de barras ITF e QR PIX. Fonte, posição e carimbos do portal
("Guia Inválida", "UF não emite") são apresentação e não bloqueiam a guia:
o PDF oficial é o entregue sempre que o portal o devolve.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable

LIMITE_PRODUCAO = 5
_ESTADO = Path(__file__).resolve().parent / "cache" / "validacao_pdf.json"

_UFS = {
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
    "SE", "SP", "TO",
}

_RE_MOEDA = re.compile(r"R\$\s?\d{1,3}(?:\.\d{3})*,\d{2}")
_RE_DATA = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
_RE_DOC = re.compile(
    r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b|\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"
)
_RE_CONTROLE = re.compile(r"\b\d{16}\b")
_RE_RECEITA = re.compile(r"\b\d{6}\b")
_RE_LINHA = re.compile(
    r"\d{11}\s+\d\s+\d{11}\s+\d\s+\d{11}\s+\d\s+\d{11}\s+\d"
)
_RE_UF = re.compile(r"\b[A-Z]{2}\b")

_CATEGORIAS = (
    ("valores", "valores (R$)"),
    ("datas", "datas"),
    ("documentos", "CNPJ/CPF"),
    ("controle", "Nº de Controle"),
    ("receita", "código da receita"),
    ("linha", "linha digitável"),
    ("uf", "UF"),
)


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


def _campos(pagina) -> dict:
    texto = _sem_acento(pagina.get_text("text"))
    linha = [re.sub(r"\D", "", achado) for achado in _RE_LINHA.findall(texto)]
    return {
        "valores": Counter(_RE_MOEDA.findall(texto)),
        "datas": Counter(_RE_DATA.findall(texto)),
        "documentos": Counter(_RE_DOC.findall(texto)),
        "controle": Counter(_RE_CONTROLE.findall(texto)),
        "receita": Counter(_RE_RECEITA.findall(texto)),
        "linha": Counter(linha),
        "uf": Counter(
            achado for achado in _RE_UF.findall(texto) if achado in _UFS
        ),
        "_texto": texto,
    }


def _somar(destino: dict, origem: dict) -> None:
    for chave, valores in origem.items():
        if chave == "_texto":
            destino["_texto"] = destino.get("_texto", "") + valores
        else:
            destino.setdefault(chave, Counter()).update(valores)


def _comparar_campos(local: dict, oficial: dict) -> tuple[list[str], list[str]]:
    linhas: list[str] = []
    divergencias: list[str] = []
    for chave, rotulo in _CATEGORIAS:
        esperados = oficial.get(chave) or Counter()
        obtidos = local.get(chave) or Counter()
        if not esperados:
            continue
        conferidos = sum(
            min(quantidade, obtidos.get(valor, 0))
            for valor, quantidade in esperados.items()
        )
        if conferidos == sum(esperados.values()):
            linhas.append(f"{rotulo[0].upper() + rotulo[1:]}: idêntico(s) ao oficial.")
            continue
        for valor, quantidade in sorted(esperados.items()):
            presentes = obtidos.get(valor, 0)
            if presentes < quantidade:
                divergencias.append(
                    f"{rotulo}: '{valor}' aparece {presentes}x na guia local e "
                    f"{quantidade}x na oficial"
                )
    return linhas, divergencias


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
    if oficial["ITF"] and local["ITF"] != oficial["ITF"]:
        divergencias.append(
            f"código de barras difere: local {sorted(local['ITF'])} x "
            f"oficial {sorted(oficial['ITF'])}"
        )
    if oficial["QR"] and local["QR"] != oficial["QR"]:
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
    """Compara os dados fiscais da guia local com o PDF oficial."""
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
    avisos: list[str] = []
    try:
        doc_local = pymupdf.open(local)
        doc_oficial = pymupdf.open(oficial)
        try:
            paginas_local = [doc_local[i] for i in range(doc_local.page_count)]
            paginas_oficial = [doc_oficial[i] for i in range(doc_oficial.page_count)]

            if doc_local.page_count != doc_oficial.page_count:
                avisos.append(
                    f"páginas: local {doc_local.page_count} x oficial "
                    f"{doc_oficial.page_count}"
                )
            else:
                linhas.append(
                    f"Páginas: {doc_local.page_count} — mesma quantidade."
                )

            campos_local: dict = {}
            campos_oficial: dict = {}
            for pagina in paginas_local:
                _somar(campos_local, _campos(pagina))
            for pagina in paginas_oficial:
                _somar(campos_oficial, _campos(pagina))

            conferidas, divs = _comparar_campos(campos_local, campos_oficial)
            linhas.extend(conferidas)
            detalhes.extend(divs)

            texto_oficial = campos_oficial.get("_texto", "").lower()
            if "guia invalida" in texto_oficial or "uf nao emite" in texto_oficial:
                avisos.append(
                    "o portal marcou este PDF como 'Guia Inválida' (UF não emite "
                    "neste ambiente); os dados conferem e a guia não é pagável."
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
    if avisos:
        linhas.append("Observações do PDF oficial:")
        linhas.extend(f"- {aviso}" for aviso in avisos)
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
