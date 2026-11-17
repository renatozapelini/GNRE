"""Atualização interna do aplicativo pelo GitHub.

Confere a última release do repositório público, compara a versão com a do
programa, baixa o GNRE.exe e troca o executável em uso. A configuração em
Documentos\\ConfGNRE fica intacta: a troca atinge somente o .exe.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPOSITORIO = "renatozapelini/CTE-GNRE"
API_ULTIMA = f"https://api.github.com/repos/{REPOSITORIO}/releases/latest"
NOME_ASSET = "GNRE.exe"
NOME_INSTALADOR = "Instalar GNRE.exe"
CABECALHOS = {
    "User-Agent": "GNRE-Updater",
    "Accept": "application/vnd.github+json",
}
TEMPO_LIMITE = 30.0
BLOCO = 256 * 1024


class ErroAtualizacao(RuntimeError):
    """Falha prevista: rede, release ausente ou arquivo inválido."""


@dataclass
class Atualizacao:
    versao: str
    tag: str
    notas: str
    url: str
    tamanho: int
    nome: str
    tipo: str = "exe"  # "instalador" (pacote completo) ou "exe" (só o programa)


def _numeros(versao: str) -> tuple[int, ...]:
    partes = re.findall(r"\d+", versao or "")
    return tuple(int(parte) for parte in partes[:4]) or (0,)


def comparar_versoes(esquerda: str, direita: str) -> int:
    """1 se esquerda é mais nova, -1 se mais antiga, 0 se iguais."""
    a, b = _numeros(esquerda), _numeros(direita)
    tamanho = max(len(a), len(b))
    a += (0,) * (tamanho - len(a))
    b += (0,) * (tamanho - len(b))
    if a > b:
        return 1
    if a < b:
        return -1
    return 0


def _requisitar(url: str):
    pedido = urllib.request.Request(url, headers=CABECALHOS)
    return urllib.request.urlopen(pedido, timeout=TEMPO_LIMITE)


def consultar_ultima() -> Atualizacao:
    """Lê a última release publicada. Levanta ErroAtualizacao se não houver."""
    try:
        with _requisitar(API_ULTIMA) as resposta:
            dados = json.loads(resposta.read().decode("utf-8"))
    except urllib.error.HTTPError as erro:
        if erro.code == 404:
            raise ErroAtualizacao(
                f"O repositório {REPOSITORIO} ainda não tem nenhuma release publicada."
            ) from erro
        raise ErroAtualizacao(f"GitHub respondeu {erro.code} ao consultar a versão.") from erro
    except (urllib.error.URLError, TimeoutError, OSError) as erro:
        raise ErroAtualizacao(f"Sem conexão com o GitHub: {erro}") from erro
    except json.JSONDecodeError as erro:
        raise ErroAtualizacao("Resposta do GitHub em formato inesperado.") from erro

    if dados.get("draft") or dados.get("prerelease"):
        raise ErroAtualizacao("A última publicação do GitHub não é uma versão estável.")

    tag = str(dados.get("tag_name") or "").strip()
    versao = tag[1:] if tag.lower().startswith("v") else tag
    if not versao:
        raise ErroAtualizacao("A release do GitHub está sem número de versão.")

    assets = dados.get("assets") or []
    instalador = None
    programa = None
    for asset in assets:
        nome = str(asset.get("name") or "").strip().lower()
        if nome == NOME_INSTALADOR.lower():
            instalador = asset
        elif nome == NOME_ASSET.lower():
            programa = asset
    # O instalador traz o programa inteiro (exe + bibliotecas + dados), por isso
    # é preferido: a troca só do .exe funciona, mas não leva arquivos novos.
    escolhido = instalador or programa
    if escolhido is None:
        for asset in assets:
            nome = str(asset.get("name") or "")
            if nome.lower().endswith(".exe") and "gnre" in nome.lower():
                escolhido = asset
                break
    if escolhido is None:
        raise ErroAtualizacao(
            f"A release {tag} não traz {NOME_INSTALADOR} nem {NOME_ASSET} para instalação."
        )
    tipo = "instalador" if escolhido is instalador else "exe"

    tamanho = int(escolhido.get("size") or 0)
    if tamanho and tamanho < 20 * 1024 * 1024:
        raise ErroAtualizacao(
            f"O arquivo {escolhido.get('name')} da release {tag} parece incompleto."
        )

    return Atualizacao(
        versao=versao,
        tag=tag,
        notas=str(dados.get("body") or "").strip(),
        url=str(escolhido.get("browser_download_url") or ""),
        tamanho=tamanho,
        nome=str(escolhido.get("name") or NOME_ASSET),
        tipo=tipo,
    )


def baixar(
    atualizacao: Atualizacao,
    destino: Path | None = None,
    progresso=None,
) -> Path:
    """Baixa o executável da release para uma pasta temporária do usuário."""
    if not atualizacao.url:
        raise ErroAtualizacao("A release não informou o endereço do executável.")
    pasta = Path(tempfile.gettempdir()) / "GNRE-atualizacao"
    pasta.mkdir(parents=True, exist_ok=True)
    destino = destino or pasta / f"GNRE-{atualizacao.versao}.exe"
    parcial = destino.with_name(destino.name + ".parcial")

    recebido = 0
    try:
        with _requisitar(atualizacao.url) as resposta, parcial.open("wb") as arquivo:
            total = int(resposta.headers.get("Content-Length") or atualizacao.tamanho or 0)
            while True:
                bloco = resposta.read(BLOCO)
                if not bloco:
                    break
                arquivo.write(bloco)
                recebido += len(bloco)
                if progresso is not None and total:
                    progresso(recebido, total)
        if recebido < 20 * 1024 * 1024:
            raise ErroAtualizacao(
                f"O download terminou com {recebido} bytes, menor que um executável válido."
            )
        if parcial.read_bytes()[:2] != b"MZ":
            raise ErroAtualizacao("O arquivo baixado não é um executável do Windows.")
        os.replace(parcial, destino)
    except (urllib.error.URLError, TimeoutError, OSError) as erro:
        parcial.unlink(missing_ok=True)
        raise ErroAtualizacao(f"Falha ao baixar a atualização: {erro}") from erro
    except ErroAtualizacao:
        parcial.unlink(missing_ok=True)
        raise
    return destino


def _pasta_programa() -> Path:
    """Mesmo destino usado pelo instalador (instalação por usuário)."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "Programs" / "GNRE"


def instalar(
    novo_exe: Path,
    destino: Path | None = None,
    reiniciar: bool = True,
    tipo: str = "exe",
) -> Path:
    """Agenda a troca do programa em uso e devolve o script auxiliar criado.

    O Windows não deixa sobrescrever um .exe em execução, então um script
    destacado espera este processo terminar, aplica a atualização e reabre o
    programa. A pasta ConfGNRE não é tocada.

    tipo="instalador": roda o instalador completo em modo silencioso (leva
    também bibliotecas e arquivos de dados novos) e abre a cópia instalada.
    tipo="exe": troca somente o executável atual.
    """
    if not getattr(sys, "frozen", False):
        raise ErroAtualizacao(
            "A troca automática só funciona no GNRE.exe; no código-fonte baixe a release manualmente."
        )
    novo_exe = Path(novo_exe).resolve()
    if not novo_exe.is_file():
        raise ErroAtualizacao("O arquivo baixado não foi encontrado.")

    if tipo == "instalador":
        alvo = (_pasta_programa() / "GNRE.exe").resolve()
        abertura = (
            f"Start-Process -FilePath '{_ps(alvo)}' -WorkingDirectory '{_ps(alvo.parent)}'"
            if reiniciar
            else "Write-Output 'Atualizado.'"
        )
        aplicacao = (
            f"Start-Process -FilePath '{_ps(novo_exe)}' "
            f"-ArgumentList '--silencioso' -Wait\n"
            f"if (-not (Test-Path -LiteralPath '{_ps(alvo)}')) "
            "{ throw 'O instalador não gravou o programa.' }"
        )
    else:
        alvo = Path(destino or sys.executable).resolve()
        if novo_exe == alvo:
            raise ErroAtualizacao("O arquivo baixado já é o programa em execução.")
        abertura = (
            f"Start-Process -FilePath '{_ps(alvo)}' "
            f"-WorkingDirectory '{_ps(alvo.parent)}'"
            if reiniciar
            else "Write-Output 'Atualizado.'"
        )
        aplicacao = f"Copy-Item -LiteralPath '{_ps(novo_exe)}' -Destination '{_ps(alvo)}' -Force"

    script = Path(tempfile.gettempdir()) / "atualizar_gnre.ps1"
    conteudo = f"""$ErrorActionPreference = 'Stop'
for ($i = 0; $i -lt 480; $i++) {{
    if (-not (Get-Process -Id {os.getpid()} -ErrorAction SilentlyContinue)) {{ break }}
    Start-Sleep -Milliseconds 500
}}
Start-Sleep -Milliseconds 700
{aplicacao}
{abertura}
Remove-Item -LiteralPath '{_ps(novo_exe)}' -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"""
    script.write_text(conteudo, encoding="utf-8-sig")
    comando = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(script),
    ]
    criacao = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        comando,
        creationflags=criacao,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return script


def _ps(caminho: Path) -> str:
    """Escapa um caminho para uso entre aspas simples no PowerShell."""
    return str(caminho).replace("'", "''")
