"""Interface desktop do emissor de GNRE a partir do CT-e."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import ttkbootstrap as ttkb
from ttkbootstrap.style import ThemeDefinition

from calendario import vencimento_padrao
from certificado import carregar_certificado
from consulta_cte import DistribuicaoCTe
from gnre_api import (
    ClienteGNRE,
    ConfigGNRE,
    ResultadoEmissao,
    baixar_pdf_recibo,
    carregar_config,
    detalhes_do_recibo,
    emitir_guia,
    numero_do_documento,
    resolver_contribuinte,
    salvar_certificado_config,
)
from leitor_cte import DadosCTe, ler_cte_xml, validar_chave
from versao import NOME, VERSAO

try:
    import portal_guia
except Exception:  # o app GNRE continua funcionando sem o módulo de portais
    portal_guia = None

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    RECURSOS = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent
    RECURSOS = BASE_DIR


def _pasta_documentos() -> Path:
    """Documentos do usuário, respeitando a pasta redirecionada (OneDrive etc.)."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\User Shell Folders",
            ) as chave:
                valor, _ = winreg.QueryValueEx(chave, "Personal")
            caminho = Path(os.path.expandvars(str(valor)))
            if caminho.is_dir():
                return caminho
        except OSError:
            pass
    return Path.home() / "Documents"


def _pasta_utilizavel(pasta: Path) -> bool:
    """Confirma que a pasta aceita arquivos.

    Um junction/reparse point quebrado (OneDrive) passa em is_dir() e no
    mkdir(exist_ok=True), mas toda gravação falha com Errno 2. Só a escrita
    de uma sonda prova que a pasta serve.
    """
    sonda = pasta / ".escrita.tmp"
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        sonda.write_text("ok", encoding="utf-8")
        return True
    except OSError:
        return False
    finally:
        try:
            sonda.unlink()
        except OSError:
            pass


def _pasta_dados() -> Path:
    """ConfGNRE utilizável: Documentos quando possível, senão LOCALAPPDATA.

    Em algumas máquinas a pasta Documentos é um atalho do OneDrive quebrado;
    criar pastas dentro dela falha com WinError 2. Nesse caso usamos a pasta
    local do usuário, que existe em qualquer Windows.
    """
    preferida = os.environ.get("GNRE_CONF_DIR")
    candidatas: list[Path] = []
    if preferida:
        candidatas.append(Path(preferida))
    candidatas.append(_pasta_documentos() / "ConfGNRE")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidatas.append(Path(local) / "ConfGNRE")
    candidatas.append(Path.home() / "ConfGNRE")

    for pasta in candidatas:
        if _pasta_utilizavel(pasta):
            return pasta
    return candidatas[-1]


# Configuração, certificado, cache e guias ficam todos fora da área de trabalho.
DADOS_DIR = _pasta_dados().resolve()
# O robô dos portais usa a mesma pasta do app, em um subdiretório próprio.
os.environ.setdefault("BYPASSGUIA_DIR", str(DADOS_DIR / "portal"))
CONFIG_PATH = DADOS_DIR / "config.json"
LOG_PATH = DADOS_DIR / "emissor.log"
UI_STATE_PATH = DADOS_DIR / "ui_state.json"
ICONE_PATH = RECURSOS / "assets" / "gnre.ico"
ICONE_PNG = RECURSOS / "assets" / "gnre.png"
LOGO_PATH = RECURSOS / "assets" / "transreis_claro.png"

# Paleta clara seguindo o ERP Senior: branco, cinzas e o teal da marca.
FUNDO = "#EBECED"
CARTAO = "#F5F6F6"
LATERAL = "#F3F4F4"
ALCA = "#D9E8E8"
BORDA = "#DEDFE0"
TEXTO = "#1C1C1C"
TEXTO_SUAVE = "#6E6E6E"
ACENTO = "#009999"
ACENTO_CLARO = "#00B0B0"
ACENTO_ESCURO = "#007878"
SUCESSO = "#107C41"
ALERTA = "#9A6700"
PERIGO = "#B3261E"

# Campos de caminho do config.json; todos ficam relativos à pasta ConfGNRE.
CAMPOS_ARQUIVO = {
    "pfx_path": "ESKIMO.pfx",
    "pasta_saida": "guias",
    "arquivo_regras": "regras_uf.json",
    "cache_config": "cache",
    "schemas_dir": "schemas_gnre",
    "indice_guias": "guias_emitidas.json",
}


def _config_inicial() -> dict:
    return {
        "ambiente": "2",
        "timeout": 180,
        "tentativas": 24,
        "intervalo": 2,
        "verificar_ssl": True,
        "cache_horas": 168,
        "convenios": {},
        "contribuinte": {"papel": "emit"},
        **CAMPOS_ARQUIVO,
    }


def _trazer_para_dados(bruto: dict, config_antigo: Path) -> dict:
    for chave, padrao in CAMPOS_ARQUIVO.items():
        valor = str(bruto.get(chave) or "").strip()
        if not valor:
            bruto[chave] = padrao
            continue
        origem = Path(valor)
        if not origem.is_absolute():
            origem = config_antigo.parent / origem
        destino = DADOS_DIR / origem.name
        try:
            if origem.exists() and origem.resolve() != destino.resolve():
                if origem.is_dir():
                    shutil.copytree(origem, destino, dirs_exist_ok=True)
                else:
                    destino.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(origem, destino)
        except OSError:
            pass
        bruto[chave] = destino.name
    return bruto


def _preparar_dados() -> None:
    """Garante a pasta ConfGNRE com config, regras e schemas, migrando o layout antigo."""
    DADOS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        bruto = None
        for legado in (BASE_DIR / "config.json", RECURSOS / "config.json"):
            if legado.exists() and legado.resolve() != CONFIG_PATH:
                try:
                    bruto = _trazer_para_dados(
                        json.loads(legado.read_text(encoding="utf-8")), legado
                    )
                except (OSError, ValueError):
                    bruto = None
                break
        CONFIG_PATH.write_text(
            json.dumps(bruto or _config_inicial(), ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
    for nome in ("regras_uf.json", "schemas_gnre"):
        origem = RECURSOS / nome
        destino = DADOS_DIR / nome
        if destino.exists() or not origem.exists() or origem.resolve() == destino.resolve():
            continue
        try:
            if origem.is_dir():
                shutil.copytree(origem, destino)
            else:
                shutil.copy2(origem, destino)
        except OSError:
            pass


class _SemEstilo:
    """Evita que o ttkbootstrap sobrescreva as cores próprias dos widgets tk."""

    def __init__(self, *args, **kwargs):
        kwargs["autostyle"] = False
        super().__init__(*args, **kwargs)


class Quadro(_SemEstilo, tk.Frame):
    pass


class Rotulo(_SemEstilo, tk.Label):
    pass


class CaixaTexto(_SemEstilo, tk.Text):
    pass


class AreaDesenho(_SemEstilo, tk.Canvas):
    pass


class Janela(_SemEstilo, tk.Toplevel):
    pass


def _fonte_preferida(*nomes: str) -> str:
    disponiveis = set(tkfont.families())
    for nome in nomes:
        if nome in disponiveis:
            return nome
    return "TkDefaultFont"


def _tema_erp(estilo: "ttkb.Style") -> None:
    """Tema ttkbootstrap com a paleta do ERP (teal, branco e cinza)."""
    estilo.register_theme(
        ThemeDefinition(
            "erp",
            {
                "primary": ACENTO,
                "secondary": TEXTO_SUAVE,
                "success": SUCESSO,
                "info": ACENTO_CLARO,
                "warning": ALERTA,
                "danger": PERIGO,
                "light": "#F5F5F5",
                "dark": TEXTO,
                "bg": CARTAO,
                "fg": TEXTO,
                "selectbg": ACENTO,
                "selectfg": "#FFFFFF",
                "border": BORDA,
                "inputfg": TEXTO,
                "inputbg": CARTAO,
                "active": "#E1E1E1",
            },
        )
    )
    estilo.theme_use("erp")


class AppGNRE:
    def __init__(self, root: tk.Tk):
        self.root = root

        self.config: ConfigGNRE | None = None
        self.certificado = None
        self.dados: DadosCTe | None = None
        self.arquivo_lido: str | None = None
        self.arquivos: list[str] = []
        self.arquivos_lidos: dict[str, DadosCTe] = {}
        self.resultado: ResultadoEmissao | None = None
        self.pdf_path: Path | None = None
        self.em_execucao = False
        self.fila: queue.Queue = queue.Queue()
        self._pdf_retry_id = 0
        self._lote_info: tuple[int, int] | None = None
        self._lateral_aberta = False
        self._portal_visivel = False
        self.modo_entrada = "xml"
        self.chaves: list[dict] = []
        self.xml_dir = DADOS_DIR / "xmls"
        self._ultima_distribuicao = 0.0
        self._fila_chaves: queue.Queue = queue.Queue()
        self._worker_chaves = threading.Thread(
            target=self._baixar_chaves, daemon=True
        )
        self._worker_chaves.start()

        self.var_arquivo = tk.StringVar()
        self.var_chave = tk.StringVar()
        self.var_modo = tk.StringVar(value="unica")
        self.var_uf = tk.StringVar()
        self.var_valor = tk.StringVar()
        self.var_vencimento = tk.StringVar(value=vencimento_padrao().strftime("%d/%m/%Y"))
        self.var_pagamento = tk.StringVar(value=date.today().strftime("%d/%m/%Y"))
        self.var_ambiente = tk.StringVar(value="2")
        self.var_status = tk.StringVar(value="Pronto.")

        self._montar_tela()
        self._carregar_contexto()
        self.root.after(120, self._processar_fila)
        self.root.after(5000, self._checagem_inicial)
        self.root.protocol("WM_DELETE_WINDOW", self._fechar)

    # ------------------------------------------------------------------
    # tela
    # ------------------------------------------------------------------

    def _montar_tela(self) -> None:
        self.fonte = _fonte_preferida("Segoe UI Variable Text", "Segoe UI", "Tahoma")
        fonte_titulo = _fonte_preferida("Segoe UI Variable Display", "Segoe UI", "Tahoma")
        fonte_mono = _fonte_preferida("Cascadia Mono", "Consolas", "Courier New")

        self._ajustar_janela()
        estilo = ttkb.Style()
        _tema_erp(estilo)
        self._configurar_estilos(estilo)
        self.root.configure(bg=FUNDO)

        Quadro(self.root, bg=ACENTO, height=2).pack(fill="x", side="top")
        self._barra_topo(fonte_titulo)
        Quadro(self.root, bg=BORDA, height=1).pack(fill="x", side="top")
        self._barra_status()

        corpo = Quadro(self.root, bg=FUNDO)
        corpo.pack(fill="both", expand=True)
        self._barra_lateral(corpo)
        moldura = Quadro(corpo, bg=FUNDO)
        moldura.pack(side="left", fill="both", expand=True, padx=16, pady=(8, 6))

        titulo_area = Quadro(moldura, bg=FUNDO)
        titulo_area.pack(fill="x", pady=(0, 5))
        Rotulo(
            titulo_area,
            text="Nova guia",
            bg=FUNDO,
            fg=TEXTO,
            font=(fonte_titulo, 15, "bold"),
        ).pack(anchor="w")
        Rotulo(
            titulo_area,
            text="Emita a GNRE a partir do CT-e autorizado.",
            bg=FUNDO,
            fg=TEXTO_SUAVE,
            font=(self.fonte, 8),
        ).pack(anchor="w", pady=(1, 0))

        cartao_cte = self._cartao(
            moldura, "CT-e autorizado", "selecione o XML ou informe a chave"
        )
        linha_abas = Quadro(cartao_cte, bg=CARTAO)
        linha_abas.pack(fill="x", pady=(0, 5))
        self.btn_aba_xml = ttk.Button(
            linha_abas,
            text="XML",
            style="AbaAtiva.TButton",
            command=lambda: self._trocar_aba("xml"),
        )
        self.btn_aba_xml.pack(side="left")
        self.btn_aba_chave = ttk.Button(
            linha_abas,
            text="Chave do CT-e",
            style="Aba.TButton",
            command=lambda: self._trocar_aba("chave"),
        )
        self.btn_aba_chave.pack(side="left", padx=(4, 0))
        self.legenda_aba = Rotulo(
            linha_abas,
            text="Selecione o XML autorizado.",
            bg=CARTAO,
            fg=TEXTO_SUAVE,
            font=(self.fonte, 8),
        )
        self.legenda_aba.pack(side="right")

        linha_modo = Quadro(cartao_cte, bg=CARTAO)
        linha_modo.pack(fill="x", pady=(0, 5))
        ttk.Label(linha_modo, text="Modo:", style="Legenda.TLabel").pack(
            side="left", padx=(0, 8)
        )
        ttkb.Radiobutton(
            linha_modo,
            text="Gerar GNRE única",
            value="unica",
            variable=self.var_modo,
            bootstyle="primary",
        ).pack(side="left")
        ttkb.Radiobutton(
            linha_modo,
            text="Gerar várias GNRE",
            value="varias",
            variable=self.var_modo,
            bootstyle="primary",
        ).pack(side="left", padx=(14, 0))

        self.painel_xml = Quadro(cartao_cte, bg=CARTAO)
        self.painel_xml.pack(fill="x")
        linha_cte = Quadro(self.painel_xml, bg=CARTAO)
        linha_cte.pack(fill="x")
        self.entrada_arquivo = ttkb.Entry(
            linha_cte, textvariable=self.var_arquivo, style="Campo.TEntry"
        )
        self.entrada_arquivo.pack(side="left", fill="x", expand=True)
        self.entrada_arquivo.bind("<Return>", lambda _evento: self._ler_cte())
        ttkb.Button(
            linha_cte,
            text="Procurar",
            bootstyle="secondary",
            command=self._procurar,
        ).pack(side="left", padx=(8, 0))
        ttkb.Button(
            linha_cte,
            text="Ler e conferir",
            bootstyle="secondary",
            command=self._ler_cte,
        ).pack(side="left", padx=(8, 0))

        self.painel_chave = Quadro(cartao_cte, bg=CARTAO)
        linha_chave = Quadro(self.painel_chave, bg=CARTAO)
        linha_chave.pack(fill="x")
        self.entrada_chave = ttkb.Entry(
            linha_chave, textvariable=self.var_chave, style="Campo.TEntry"
        )
        self.entrada_chave.pack(side="left", fill="x", expand=True)
        self.entrada_chave.bind("<Return>", lambda _evento: self._adicionar_chave())
        ttkb.Button(
            linha_chave,
            text="Adicionar",
            bootstyle="secondary",
            command=self._adicionar_chave,
        ).pack(side="left", padx=(8, 0))
        ttkb.Button(
            linha_chave,
            text="Remover",
            bootstyle="secondary",
            command=self._remover_chave,
        ).pack(side="left", padx=(4, 0))
        ttkb.Button(
            linha_chave,
            text="Limpar",
            bootstyle="secondary",
            command=self._limpar_chaves,
        ).pack(side="left", padx=(4, 0))

        caixa_lista = Quadro(self.painel_chave, bg=CARTAO)
        caixa_lista.pack(fill="x", pady=(5, 0))
        self.lista_chaves = ttk.Treeview(
            caixa_lista,
            columns=("chave", "situacao"),
            show="",
            height=2,
            selectmode="browse",
            style="Chaves.Treeview",
        )
        self.lista_chaves.column("chave", width=350, anchor="w")
        self.lista_chaves.column("situacao", width=190, anchor="w", stretch=True)
        barra_lista = ttk.Scrollbar(
            caixa_lista, orient="vertical", command=self.lista_chaves.yview
        )
        self.lista_chaves.configure(yscrollcommand=barra_lista.set)
        self.lista_chaves.pack(side="left", fill="x", expand=True)
        barra_lista.pack(side="left", fill="y")
        self.lista_chaves.tag_configure("pendente", foreground=ALERTA)
        self.lista_chaves.tag_configure("pronto", foreground=SUCESSO)
        self.lista_chaves.tag_configure("erro", foreground=PERIGO)

        cartao_dados = self._cartao(
            moldura, "Dados da guia", "confira antes de emitir"
        )
        grade = Quadro(cartao_dados, bg=CARTAO)
        grade.pack(fill="x")
        self.entrada_uf = self._campo(grade, 0, "UF favorecida", self.var_uf, 8)
        self._campo(grade, 1, "Valor da guia", self.var_valor, 16)
        self._campo(grade, 2, "Vencimento", self.var_vencimento, 13)
        self._campo(grade, 3, "Pagamento", self.var_pagamento, 13)

        linha_acao = Quadro(cartao_dados, bg=CARTAO)
        linha_acao.pack(fill="x", pady=(9, 0))
        self.btn_emitir = ttk.Button(
            linha_acao,
            text="Emitir guia",
            style="Acao.TButton",
            command=self._emitir,
        )
        self.btn_emitir.pack(side="right")
        ttk.Label(
            linha_acao, text="Ambiente:", style="Valor.TLabel"
        ).pack(side="left", padx=(0, 10))
        ttkb.Radiobutton(
            linha_acao,
            text="Homologação (teste)",
            value="2",
            variable=self.var_ambiente,
            bootstyle="primary",
        ).pack(side="left")
        ttkb.Radiobutton(
            linha_acao,
            text="Produção (guia real)",
            value="1",
            variable=self.var_ambiente,
            bootstyle="primary",
        ).pack(side="left", padx=(14, 0))

        cartao_log = self._cartao(
            moldura, "Registro", "cada etapa registrada com horário", expande=True
        )
        caixa_log = Quadro(cartao_log, bg=CARTAO)
        caixa_log.pack(fill="both", expand=True)
        self.txt_log = CaixaTexto(
            caixa_log,
            height=3,
            font=(fonte_mono, 9),
            wrap="word",
            state="disabled",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDA,
            highlightcolor=BORDA,
            bg="#F2F3F3",
            fg="#2B2B2B",
            padx=9,
            pady=5,
            spacing1=0,
            spacing3=2,
            cursor="arrow",
            insertbackground=TEXTO,
            selectbackground="#CCEBEB",
            selectforeground=TEXTO,
        )
        recuo = tkfont.Font(root=self.root, family=fonte_mono, size=9).measure(
            "00:00   "
        )
        self.txt_log.tag_configure("hora", foreground="#8C8C8C")
        self.txt_log.tag_configure(
            "titulo",
            foreground=ACENTO_ESCURO,
            font=(fonte_mono, 9, "bold"),
            lmargin2=recuo,
        )
        self.txt_log.tag_configure(
            "recibo",
            foreground=ACENTO_ESCURO,
            font=(fonte_mono, 9, "bold"),
            lmargin2=recuo,
        )
        self.txt_log.tag_configure("aviso", foreground=ALERTA, lmargin2=recuo)
        self.txt_log.tag_configure(
            "erro", foreground=PERIGO, font=(fonte_mono, 9, "bold"), lmargin2=recuo
        )
        self.txt_log.tag_configure("ok", foreground=SUCESSO, lmargin2=recuo)
        self.txt_log.tag_configure("info", foreground="#2B2B2B", lmargin2=recuo)
        self.txt_log.tag_configure(
            "espaco", font=(fonte_mono, 3), spacing1=0, spacing3=3
        )
        barra = ttkb.Scrollbar(
            caixa_log, command=self.txt_log.yview, bootstyle="secondary-round"
        )
        self.txt_log.configure(yscrollcommand=barra.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y", padx=(2, 0))

        self.var_modo.trace_add("write", lambda *_args: self._mudar_modo())
        self.root.bind("<Control-o>", lambda _evento: self._procurar())

    def _barra_status(self) -> None:
        Quadro(self.root, bg=BORDA, height=1).pack(fill="x", side="bottom")
        rodape = Quadro(self.root, bg=FUNDO)
        rodape.pack(fill="x", side="bottom")
        self.lbl_status = Rotulo(
            rodape,
            text="●  Pronto.",
            bg=FUNDO,
            fg=TEXTO_SUAVE,
            font=(self.fonte, 8),
            anchor="w",
            justify="left",
        )
        self.lbl_status.pack(side="left", fill="x", expand=True, padx=(14, 6), pady=3)
        Rotulo(
            rodape,
            text=f"v{VERSAO}",
            bg=FUNDO,
            fg="#9A9A9A",
            font=(self.fonte, 8),
        ).pack(side="right", padx=(0, 14), pady=3)
        self.var_status.trace_add("write", lambda *_args: self._pintar_status())
        self._pintar_status()

    def _area_util(self) -> tuple[int, int, int, int]:
        try:
            import ctypes
            from ctypes import wintypes

            retangulo = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(retangulo), 0
            )
            if retangulo.right > retangulo.left and retangulo.bottom > retangulo.top:
                return (
                    retangulo.left,
                    retangulo.top,
                    retangulo.right - retangulo.left,
                    retangulo.bottom - retangulo.top,
                )
        except Exception:
            pass
        return (
            0,
            0,
            self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(),
        )

    def _ajustar_janela(self) -> None:
        self.root.configure(bg=FUNDO)
        self.root.title(f"{NOME} v{VERSAO}")
        try:
            self.root.iconbitmap(str(ICONE_PATH))
            self.root.iconbitmap(default=str(ICONE_PATH))
        except tk.TclError:
            try:
                self._icone_png = tk.PhotoImage(file=str(ICONE_PNG))
                self.root.iconphoto(True, self._icone_png)
            except tk.TclError:
                pass
        escala = float(self.root.tk.call("tk", "scaling")) / 1.3349
        area_x, area_y, area_largura, area_altura = self._area_util()
        largura = min(int(800 * escala), area_largura - 40)
        altura = min(int(690 * escala), area_altura - 40)
        largura_minima = min(int(748 * escala), largura)
        altura_minima = min(int(650 * escala), altura)
        self.root.minsize(largura_minima, altura_minima)
        x = area_x + max(0, (area_largura - largura) // 2)
        y = area_y + max(0, (area_altura - altura) // 2)

        salva = self._estado_janela()
        if salva is not None:
            largura_salva, altura_salva, x_salvo, y_salvo, zoom = salva
            largura = max(largura_minima, min(largura_salva, area_largura))
            altura = max(altura_minima, min(altura_salva, area_altura))
            x = max(area_x, min(x_salvo, area_x + area_largura - largura))
            y = max(area_y, min(y_salvo, area_y + area_altura - altura))
        else:
            zoom = False

        self.root.geometry(f"{largura}x{altura}+{x}+{y}")
        if zoom:
            self.root.state("zoomed")

    def _estado_janela(self) -> tuple[int, int, int, int, bool] | None:
        try:
            dados = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        combinacao = re.fullmatch(
            r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", str(dados.get("geometry", ""))
        )
        if not combinacao:
            return None
        largura, altura, x, y = (int(valor) for valor in combinacao.groups())
        return largura, altura, x, y, bool(dados.get("zoom"))

    def _salvar_estado_janela(self) -> None:
        try:
            dados = {
                "geometry": self.root.geometry(),
                "zoom": self.root.state() == "zoomed",
            }
            UI_STATE_PATH.write_text(json.dumps(dados), encoding="utf-8")
        except (OSError, tk.TclError):
            pass

    def _configurar_estilos(self, estilo: "ttkb.Style") -> None:
        fonte = self.fonte
        estilo.configure(
            ".", font=(fonte, 10), background=CARTAO, foreground=TEXTO, focuscolor=CARTAO
        )
        estilo.configure(
            "Secao.TLabel", background=CARTAO, foreground=TEXTO, font=(fonte, 10, "bold")
        )
        estilo.configure(
            "Legenda.TLabel", background=CARTAO, foreground=TEXTO_SUAVE, font=(fonte, 8)
        )
        estilo.configure(
            "Valor.TLabel", background=CARTAO, foreground=TEXTO, font=(fonte, 9, "bold")
        )

        # Botões e seletores usam os estilos do tema ttkbootstrap; aqui só
        # ajustamos tipografia e espaçamento para o layout compacto do ERP.
        estilo.configure(
            "primary.TButton", font=(fonte, 9, "bold"), padding=(16, 6)
        )
        estilo.configure(
            "secondary.TButton",
            background="#E1E1E1",
            foreground=TEXTO,
            bordercolor="#ADADAD",
            lightcolor="#FBFBFB",
            darkcolor="#C4C4C4",
            relief="flat",
            font=(fonte, 9),
            padding=(10, 4),
        )
        estilo.map(
            "secondary.TButton",
            background=[
                ("disabled", "#F0F0F0"),
                ("pressed", "#D2D2D2"),
                ("active", "#E9E9E9"),
            ],
            foreground=[("disabled", "#A6A6A6")],
            bordercolor=[("disabled", "#C8C8C8"), ("active", "#8C8C8C")],
        )
        estilo.configure(
            "Acao.TButton",
            background="#E1E1E1",
            foreground=TEXTO,
            bordercolor="#A9A9A9",
            lightcolor="#FBFBFB",
            darkcolor="#C4C4C4",
            relief="flat",
            font=(fonte, 9, "bold"),
            padding=(14, 6),
        )
        estilo.map(
            "Acao.TButton",
            background=[
                ("disabled", "#F0F0F0"),
                ("pressed", "#CFCFCF"),
                ("active", "#D8D8D8"),
            ],
            foreground=[("disabled", "#A6A6A6")],
            bordercolor=[("disabled", "#C8C8C8"), ("active", "#8C8C8C")],
        )
        estilo.configure(
            "Campo.TEntry",
            fieldbackground="#F3F4F4",
            foreground=TEXTO,
            bordercolor="#D8D9DA",
            lightcolor="#F3F4F4",
            darkcolor="#F3F4F4",
            padding=(7, 6),
        )
        estilo.map(
            "Campo.TEntry",
            bordercolor=[("focus", ACENTO)],
            lightcolor=[("focus", ACENTO)],
        )
        estilo.configure(
            "Leitura.TEntry",
            fieldbackground="#E9EAEA",
            foreground="#2B2B2B",
            bordercolor="#DBDCDD",
            lightcolor="#E9EAEA",
            darkcolor="#E9EAEA",
            padding=(7, 6),
            relief="flat",
        )
        estilo.configure("primary.TRadiobutton", font=(fonte, 9), padding=(0, 2))
        estilo.configure("primary.TCheckbutton", font=(fonte, 9))

        # Abas segmentadas do cartão do CT-e (XML / Chave).
        estilo.configure(
            "Aba.TButton",
            background="#E7E8E9",
            foreground=TEXTO_SUAVE,
            bordercolor="#CFD0D1",
            lightcolor="#F0F1F1",
            darkcolor="#D6D7D8",
            relief="flat",
            font=(fonte, 9),
            padding=(16, 5),
        )
        estilo.map(
            "Aba.TButton",
            background=[("active", "#EDEEEE"), ("pressed", "#DEDFDF")],
        )
        estilo.configure(
            "AbaAtiva.TButton",
            background=CARTAO,
            foreground=ACENTO,
            bordercolor=ACENTO,
            lightcolor=CARTAO,
            darkcolor=CARTAO,
            relief="flat",
            font=(fonte, 9, "bold"),
            padding=(16, 5),
        )
        estilo.map(
            "AbaAtiva.TButton",
            background=[("active", CARTAO), ("pressed", CARTAO)],
            foreground=[("active", ACENTO_ESCURO)],
        )
        estilo.configure(
            "Chaves.Treeview",
            background=CARTAO,
            fieldbackground=CARTAO,
            foreground=TEXTO,
            bordercolor=BORDA,
            rowheight=19,
            font=(fonte, 8),
        )
        estilo.map(
            "Chaves.Treeview",
            background=[("selected", ACENTO_CLARO)],
            foreground=[("selected", "#FFFFFF")],
        )

    def _barra_topo(self, fonte_titulo: str) -> None:
        topo = Quadro(self.root, bg=FUNDO)
        topo.pack(fill="x", side="top")
        linha = Quadro(topo, bg=FUNDO)
        linha.pack(fill="x", padx=12, pady=7)

        self.btn_menu = AreaDesenho(
            linha,
            width=28,
            height=24,
            bg=FUNDO,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.btn_menu.pack(side="left")
        self._pintar_menu()
        self.btn_menu.bind("<Button-1>", lambda _evento: self._alternar_lateral())
        self.btn_menu.bind("<Enter>", lambda _evento: self._pintar_menu(hover=True))
        self.btn_menu.bind("<Leave>", lambda _evento: self._pintar_menu(hover=False))

        marca = Quadro(linha, bg=FUNDO)
        marca.pack(side="left", padx=(12, 0))
        self._logo_topo = self._carregar_logo(marca, 27)
        if self._logo_topo is not None:
            Rotulo(marca, image=self._logo_topo, bg=FUNDO).pack(anchor="w")
        else:
            Rotulo(
                marca,
                text=NOME,
                bg=FUNDO,
                fg=TEXTO,
                font=(fonte_titulo, 11, "bold"),
            ).pack(anchor="w")

        Rotulo(
            linha,
            text=f"GNRE 2.00 · v{VERSAO}",
            bg=FUNDO,
            fg=TEXTO_SUAVE,
            font=(self.fonte, 7),
        ).pack(side="right")

    def _carregar_logo(self, pai: tk.Widget, altura: int):
        try:
            from PIL import Image, ImageTk

            imagem = Image.open(LOGO_PATH)
            proporcao = altura / imagem.height
            imagem = imagem.resize(
                (max(1, int(imagem.width * proporcao)), altura), Image.LANCZOS
            )
            return ImageTk.PhotoImage(imagem, master=pai)
        except Exception:
            return None

    def _pintar_menu(self, hover: bool = False) -> None:
        fundo = "#E2E3E4" if hover else FUNDO
        if self._lateral_aberta:
            cor = ACENTO_ESCURO
        else:
            cor = TEXTO if hover else TEXTO_SUAVE
        self.btn_menu.configure(bg=fundo)
        self.btn_menu.delete("all")
        for y in (7, 12, 17):
            self.btn_menu.create_line(8, y, 22, y, fill=cor, width=2)

    def _alternar_lateral(self) -> None:
        if self._lateral_aberta:
            self._recolher_lateral()
        else:
            self._abrir_lateral()

    def _barra_lateral(self, pai: tk.Widget) -> None:
        self.alca = Quadro(pai, bg=ALCA, width=4)
        self.alca.pack(side="left", fill="y")
        self.alca.pack_propagate(False)

        self.lateral = Quadro(pai, bg=LATERAL, width=1)
        self.lateral.pack(side="left", fill="y")
        self.lateral.pack_propagate(False)
        self._lateral_aberta = False
        self._lateral_alvo = 1
        self._lateral_largura = 1
        self._lateral_animando = False

        conteudo = Quadro(self.lateral, bg=LATERAL)
        conteudo.pack(side="left", fill="both", expand=True)
        Quadro(self.lateral, bg="#C9CACB", width=1).pack(side="right", fill="y")

        self._grupo_lateral(conteudo, "EMISSÃO")
        self._item_lateral(conteudo, "Nova guia", self._focar_cte, ativo=True)
        self._item_lateral(conteudo, "Lote de XMLs", self._abrir_lote)
        self._item_lateral(
            conteudo, "Chave do CT-e", lambda: self._trocar_aba("chave")
        )

        self._grupo_lateral(conteudo, "SISTEMA")
        self._item_lateral(
            conteudo, "Certificado digital", self._atualizar_certificado
        )
        self._item_lateral(conteudo, "Pasta das guias", self._abrir_pasta_guias)
        self._item_lateral(conteudo, "Pasta de configuração", self._abrir_pasta_config)
        self._item_lateral(conteudo, "Verificar atualização", self._verificar_atualizacao)
        self._item_lateral(
            conteudo, "Portal: mostrar navegador", self._alternar_portal_visivel
        )
        self._item_lateral(
            conteudo, "Portal: instalar navegador", self._instalar_navegador_portal
        )

        rodape = Quadro(conteudo, bg=LATERAL)
        rodape.pack(side="bottom", fill="x", padx=10, pady=10)
        cartao = Quadro(
            rodape,
            bg=CARTAO,
            highlightthickness=1,
            highlightbackground=BORDA,
            cursor="hand2",
        )
        cartao.pack(fill="x")
        dentro = Quadro(cartao, bg=CARTAO)
        dentro.pack(fill="x", padx=9, pady=7)
        cabecalho_cert = Quadro(dentro, bg=CARTAO)
        cabecalho_cert.pack(fill="x")
        Rotulo(
            cabecalho_cert,
            text="CERTIFICADO A1",
            bg=CARTAO,
            fg="#9A9A9A",
            font=(self.fonte, 7, "bold"),
        ).pack(side="left")
        self.lbl_cert_ponto = Rotulo(
            cabecalho_cert, text="●", bg=CARTAO, fg=TEXTO_SUAVE, font=(self.fonte, 7)
        )
        self.lbl_cert_ponto.pack(side="right")
        self.lbl_certificado = Rotulo(
            dentro,
            text="Carregando certificado...",
            bg=CARTAO,
            fg=TEXTO_SUAVE,
            font=(self.fonte, 8),
            anchor="w",
            justify="left",
            wraplength=140,
        )
        self.lbl_certificado.pack(fill="x", pady=(3, 0))
        for alvo in (cartao, dentro, cabecalho_cert, self.lbl_certificado, self.lbl_cert_ponto):
            alvo.bind("<Button-1>", lambda _evento: self._atualizar_certificado())

    # A lateral é uma gaveta: abre e fecha apenas pelo clique no menu sanduíche.

    def _abrir_lateral(self) -> None:
        if self._lateral_alvo == 182:
            return
        self._lateral_alvo = 182
        self._lateral_aberta = True
        self._animar_lateral()
        self._pintar_menu()

    def _recolher_lateral(self) -> None:
        if self._lateral_alvo == 1:
            return
        self._lateral_alvo = 1
        self._lateral_aberta = False
        self._animar_lateral()
        self._pintar_menu()

    def _animar_lateral(self) -> None:
        if self._lateral_animando:
            return
        self._lateral_animando = True
        self._passo_lateral()

    def _passo_lateral(self) -> None:
        atual = self._lateral_largura
        alvo = self._lateral_alvo
        if atual == alvo:
            self._lateral_animando = False
            return
        passo = max(18, abs(alvo - atual) // 2)
        novo = min(atual + passo, alvo) if alvo > atual else max(atual - passo, alvo)
        self._lateral_largura = novo
        self.lateral.configure(width=max(novo, 1))
        self.root.after(10, self._passo_lateral)

    def _grupo_lateral(self, pai: tk.Widget, titulo: str) -> None:
        Rotulo(
            pai,
            text=titulo,
            bg=LATERAL,
            fg="#9A9A9A",
            font=(self.fonte, 7, "bold"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(14, 4))

    def _item_lateral(
        self, pai: tk.Widget, texto: str, comando, ativo: bool = False
    ) -> tk.Frame:
        fundo = "#EAF5F5" if ativo else LATERAL
        item = Quadro(pai, bg=fundo, cursor="hand2")
        item.pack(fill="x", padx=8, pady=1)
        marca = Quadro(item, bg=ACENTO if ativo else fundo, width=3)
        marca.pack(side="left", fill="y")
        rotulo = Rotulo(
            item,
            text=texto,
            bg=fundo,
            fg=ACENTO_ESCURO if ativo else "#333333",
            font=(self.fonte, 9, "bold" if ativo else "normal"),
            anchor="w",
            padx=7,
            pady=4,
        )
        rotulo.pack(side="left", fill="x", expand=True)

        def pintar(entrando: bool) -> None:
            if ativo:
                return
            cor = "#E7E8E9" if entrando else LATERAL
            item.configure(bg=cor)
            rotulo.configure(bg=cor)
            marca.configure(bg=ACENTO_CLARO if entrando else cor)

        for alvo in (item, rotulo):
            alvo.bind("<Button-1>", lambda _evento: comando())
            alvo.bind("<Enter>", lambda _evento: pintar(True))
            alvo.bind("<Leave>", lambda _evento: pintar(False))
        return item

    def _focar_cte(self) -> None:
        if self.modo_entrada == "chave":
            self.entrada_chave.focus_set()
        else:
            self.entrada_arquivo.focus_set()

    def _abrir_pasta_guias(self) -> None:
        pasta = Path(self.config.pasta_saida) if self.config else DADOS_DIR / "guias"
        if not pasta.is_absolute():
            pasta = DADOS_DIR / pasta
        try:
            pasta.mkdir(parents=True, exist_ok=True)
            os.startfile(str(pasta))
        except OSError as erro:
            messagebox.showerror("Erro", f"Não foi possível abrir a pasta: {erro}")

    def _abrir_pasta_config(self) -> None:
        try:
            DADOS_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(DADOS_DIR))
        except OSError as erro:
            messagebox.showerror("Erro", f"Não foi possível abrir a pasta: {erro}")

    # ------------------------------------------------------------------
    # atualização interna pelo GitHub
    # ------------------------------------------------------------------

    def _checagem_inicial(self) -> None:
        # No código-fonte não há troca automática; a checagem é só do .exe.
        if not getattr(sys, "frozen", False):
            return
        from atualizador import ErroAtualizacao, comparar_versoes, consultar_ultima

        def tarefa() -> None:
            try:
                info = consultar_ultima()
            except ErroAtualizacao:
                return
            except Exception:
                return
            if comparar_versoes(info.versao, VERSAO) > 0:
                self.fila.put(("atualizacao_disponivel", info))

        threading.Thread(target=tarefa, daemon=True).start()

    def _verificar_atualizacao(self) -> None:
        from atualizador import ErroAtualizacao, consultar_ultima

        self.var_status.set("Consultando a última versão no GitHub...")
        self._log("Atualização: consultando a última versão publicada no GitHub.")

        def tarefa() -> None:
            try:
                info = consultar_ultima()
            except ErroAtualizacao as erro:
                self.fila.put(("atualizacao_erro", str(erro)))
                return
            except Exception as erro:  # rede/proxy inesperados
                self.fila.put(("atualizacao_erro", f"Falha inesperada: {erro}"))
                return
            self.fila.put(("atualizacao_consulta", info))

        threading.Thread(target=tarefa, daemon=True).start()

    def _avaliar_atualizacao(self, info) -> None:
        from atualizador import ErroAtualizacao, baixar, comparar_versoes

        if comparar_versoes(info.versao, VERSAO) <= 0:
            self.var_status.set(f"Atualização: v{VERSAO} é a versão mais recente.")
            self._log(f"Atualização: v{VERSAO} é a versão mais recente publicada.")
            messagebox.showinfo(
                "Atualização", f"Você já está na versão mais recente (v{VERSAO})."
            )
            return

        notas = (info.notas or "").strip()
        if len(notas) > 900:
            notas = notas[:900].rstrip() + "..."
        texto = f"Nova versão disponível: v{info.versao} (instalada: v{VERSAO})."
        if notas:
            texto += f"\n\n{notas}"
        texto += (
            "\n\nBaixar e instalar agora?\n"
            "O programa fecha, aplica a atualização e reabre sozinho. "
            "A configuração em Documentos\\ConfGNRE é mantida."
        )
        self._log(f"Atualização: v{info.versao} disponível (instalada v{VERSAO}).")
        if not messagebox.askyesno("Atualização", texto):
            self.var_status.set("Atualização adiada.")
            self._log(f"Atualização v{info.versao} adiada pelo usuário.")
            return

        self.var_status.set(f"Baixando a versão {info.versao}...")
        self._log(
            f"Atualização: baixando v{info.versao} "
            f"({info.tamanho / 1048576:.0f} MB) do GitHub."
        )
        ultimo = {"porcento": -1}

        def progresso(recebido: int, total: int) -> None:
            porcento = int(recebido * 100 / total) if total else 0
            if porcento != ultimo["porcento"]:
                ultimo["porcento"] = porcento
                self.fila.put(("atualizacao_progresso", porcento))

        def tarefa() -> None:
            try:
                caminho = baixar(info, progresso=progresso)
            except ErroAtualizacao as erro:
                self.fila.put(("atualizacao_erro", str(erro)))
                return
            except Exception as erro:
                self.fila.put(("atualizacao_erro", f"Falha inesperada no download: {erro}"))
                return
            self.fila.put(("atualizacao_baixada", (info, caminho)))

        threading.Thread(target=tarefa, daemon=True).start()

    def _instalar_atualizacao(self, info, caminho) -> None:
        from atualizador import ErroAtualizacao, instalar

        if not getattr(sys, "frozen", False):
            self.var_status.set("Atualização baixada; instalação manual no código-fonte.")
            self._log(f"Atualização v{info.versao} baixada em {caminho}.")
            messagebox.showinfo(
                "Atualização",
                f"A versão {info.versao} foi baixada em:\n{caminho}\n\n"
                "Como o app está rodando do código-fonte, a troca automática não se aplica.",
            )
            return
        if not messagebox.askyesno(
            "Atualização",
            f"A versão {info.versao} foi baixada e conferida.\n\n"
            "Fechar o programa agora para instalar? Ele reabre sozinho.",
        ):
            self.var_status.set("Atualização baixada; aplicada ao fechar.")
            self._log(f"Atualização v{info.versao} baixada; aplicação adiada.")
            return
        try:
            script = instalar(caminho, tipo=info.tipo)
        except ErroAtualizacao as erro:
            self.var_status.set("Falha ao preparar a atualização.")
            self._log(f"ERRO na atualização: {erro}")
            messagebox.showerror("Atualização", str(erro))
            return
        self.var_status.set(f"Instalando a versão {info.versao}...")
        self._log(
            f"Atualização: aplicando v{info.versao} ({script.name}). "
            "O programa vai fechar e reabrir já atualizado."
        )
        self.root.after(700, self.root.destroy)

    def _alternar_portal_visivel(self) -> None:
        self._portal_visivel = not self._portal_visivel
        estado = "visível" if self._portal_visivel else "oculto"
        self._log(
            f"Portal estadual: navegador {estado}. "
            "Use visível somente quando o portal pedir captcha."
        )
        self.var_status.set(f"Navegador do portal: {estado}.")

    def _instalar_navegador_portal(self) -> None:
        if portal_guia is None:
            messagebox.showerror("Portal", "O módulo dos portais estaduais não está disponível.")
            return
        from bypassguia import navegador

        if navegador.instalado():
            messagebox.showinfo(
                "Navegador do portal",
                "O navegador usado por SP, ES e PR já está instalado nesta máquina.",
            )
            return
        if not messagebox.askyesno(
            "Navegador do portal",
            "Baixar agora o navegador usado pelas guias de SP, ES e PR?\n\n"
            "É feito uma única vez por máquina (cerca de 170 MB).",
        ):
            return
        self._log("Portal estadual: baixando o navegador...")

        def _baixar() -> None:
            try:
                navegador.instalar(lambda mensagem: self.fila.put(("log", mensagem)))
                self.fila.put(("log", "Navegador do portal pronto para uso."))
            except Exception as erro:
                self.fila.put(("log", f"ERRO: {erro}"))

        threading.Thread(target=_baixar, daemon=True).start()

    def _cartao(
        self,
        pai: tk.Widget,
        titulo: str = "",
        legenda: str = "",
        expande: bool = False,
    ) -> tk.Frame:
        borda = Quadro(pai, bg=BORDA)
        borda.pack(fill="both" if expande else "x", expand=expande, pady=(0, 6))
        miolo = Quadro(borda, bg=CARTAO)
        miolo.pack(fill="both", expand=True, padx=1, pady=1)
        if titulo:
            cabecalho = Quadro(miolo, bg=CARTAO)
            cabecalho.pack(fill="x", padx=12, pady=(7, 5))
            Rotulo(
                cabecalho,
                text=titulo,
                bg=CARTAO,
                fg=TEXTO,
                font=(self.fonte, 9, "bold"),
            ).pack(side="left")
            if legenda:
                Rotulo(
                    cabecalho,
                    text=legenda,
                    bg=CARTAO,
                    fg=TEXTO_SUAVE,
                    font=(self.fonte, 8),
                ).pack(side="right")
            Quadro(miolo, bg="#E7E8E9", height=1).pack(fill="x")
        conteudo = Quadro(miolo, bg=CARTAO)
        conteudo.pack(fill="both", expand=True, padx=12, pady=(6, 7))
        return conteudo

    def _campo(
        self,
        pai: tk.Widget,
        coluna: int,
        legenda: str,
        variavel: tk.StringVar,
        largura: int,
    ) -> ttk.Entry:
        caixa = Quadro(pai, bg=CARTAO)
        caixa.grid(row=0, column=coluna, sticky="w", padx=(0, 22))
        ttk.Label(caixa, text=legenda, style="Legenda.TLabel").pack(anchor="w")
        entrada = ttk.Entry(
            caixa,
            textvariable=variavel,
            width=largura,
            style="Leitura.TEntry",
            state="readonly",
        )
        entrada.pack(anchor="w", pady=(3, 0))
        return entrada

    def _pintar_status(self) -> None:
        texto = self.var_status.get()
        baixo = texto.lower()
        if any(
            palavra in baixo
            for palavra in (
                "divergen",
                "não pague",
                "reprovad",
                "falha",
                "erro",
                "rejeit",
                "invalid",
            )
        ):
            cor = PERIGO
        elif any(
            palavra in baixo
            for palavra in (
                "aguard",
                "indispon",
                "recebido",
                "processando",
                "consultando",
                "pendênc",
                "pendenc",
            )
        ):
            cor = ALERTA
        elif "conclu" in baixo:
            cor = SUCESSO
        else:
            cor = TEXTO_SUAVE
        self.lbl_status.configure(text=f"●  {texto}", fg=cor)

    # ------------------------------------------------------------------
    # contexto (configuração e certificado)
    # ------------------------------------------------------------------

    def _carregar_contexto(self) -> None:
        try:
            self.config = carregar_config(CONFIG_PATH, tolerar_sem_senha=True)
        except Exception as erro:
            self.config = None
            self._log(f"ERRO na configuração: {erro}")
            self.lbl_certificado.configure(
                text="Configuração inválida (config.json)", fg=PERIGO
            )
            self.lbl_cert_ponto.configure(fg=PERIGO)
            self._bloquear("Corrija o config.json ou atualize o certificado.")
            return
        if self.config.schemas_dir is not None and not Path(
            self.config.schemas_dir
        ).is_dir():
            # No EXE os schemas viajam dentro do pacote (sys._MEIPASS).
            embutidos = RECURSOS / "schemas_gnre"
            if embutidos.is_dir():
                self.config.schemas_dir = embutidos

        self.var_ambiente.set(self.config.ambiente)
        try:
            self.certificado = carregar_certificado(self.config.caminho_pfx, self.config.senha)
        except Exception as erro:
            self.certificado = None
            self._log(f"ERRO no certificado: {erro}")
            self.lbl_certificado.configure(
                text="Não foi possível carregar o certificado "
                "(use 'Certificado digital')",
                fg=PERIGO,
            )
            self.lbl_cert_ponto.configure(fg=PERIGO)
            self._bloquear("Atualize o certificado para começar.")
            return

        cert = self.certificado
        texto = (
            f"{cert.razao_social}\n"
            f"CNPJ {cert.cnpj} · até {cert.validade:%d/%m/%Y}"
        )
        if cert.avisos:
            texto += "\nATENÇÃO: " + " ".join(cert.avisos)
            self.lbl_certificado.configure(text=texto, fg=ALERTA)
            self.lbl_cert_ponto.configure(fg=ALERTA)
        else:
            self.lbl_certificado.configure(text=texto, fg=TEXTO_SUAVE)
            self.lbl_cert_ponto.configure(fg=SUCESSO)
        for aviso in cert.avisos:
            self._log(f"AVISO: {aviso}")
        if not self.em_execucao:
            self.btn_emitir.configure(state="normal")
        self.var_status.set("Pronto. Selecione o XML do CT-e.")

    def _atualizar_certificado(self) -> None:
        DialogoCertificado(self.root, self)

    def _bloquear(self, motivo: str) -> None:
        self.btn_emitir.configure(state="disabled")
        self.var_status.set(motivo)

    # ------------------------------------------------------------------
    # ações
    # ------------------------------------------------------------------

    def _caminhos_selecionados(self) -> list[str]:
        if self.modo_entrada == "chave":
            return [
                item["caminho"]
                for item in self.chaves
                if item.get("caminho") and not item.get("erro")
            ]
        if len(self.arquivos) > 1:
            return list(self.arquivos)
        caminho = self.var_arquivo.get().strip().strip('"')
        return [caminho] if caminho else []

    def _limpar_leitura(self) -> None:
        self.arquivos_lidos = {}
        self.dados = None
        self.arquivo_lido = None
        self.resultado = None
        self.pdf_path = None
        self.var_uf.set("")
        self.var_valor.set("")
        self.var_vencimento.set(vencimento_padrao().strftime("%d/%m/%Y"))
        self.var_pagamento.set(date.today().strftime("%d/%m/%Y"))
        self.entrada_uf.configure(state="readonly")

    def _trocar_aba(self, modo: str) -> None:
        if modo == self.modo_entrada:
            return
        self.modo_entrada = modo
        if modo == "chave":
            self.painel_xml.pack_forget()
            self.painel_chave.pack(fill="x")
            self.btn_aba_chave.configure(style="AbaAtiva.TButton")
            self.btn_aba_xml.configure(style="Aba.TButton")
            self.legenda_aba.configure(text="O XML é baixado pela chave na SEFAZ.")
            self.entrada_chave.focus_set()
            self.var_status.set(
                "Informe a chave do CT-e. O XML é baixado pela distribuição da SEFAZ."
            )
        else:
            self.painel_chave.pack_forget()
            self.painel_xml.pack(fill="x")
            self.btn_aba_xml.configure(style="AbaAtiva.TButton")
            self.btn_aba_chave.configure(style="Aba.TButton")
            self.legenda_aba.configure(text="Selecione o XML autorizado.")
            self.entrada_arquivo.focus_set()
            self.var_status.set("Selecione o XML do CT-e.")
        self._limpar_leitura()

    @staticmethod
    def _formatar_chave(chave: str) -> str:
        return " ".join(chave[i : i + 4] for i in range(0, len(chave), 4))

    @staticmethod
    def _moeda(valor: float) -> str:
        texto = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {texto}"

    def _rotulo_valor(self, lidos: dict[str, DadosCTe]) -> str:
        valores = [dados.valor_icms for dados in lidos.values() if dados.valor_icms > 0]
        if not valores:
            return ""
        distintos = {round(valor, 2) for valor in valores}
        if len(distintos) == 1:
            texto = self._moeda(valores[0])
            return f"{texto} cada" if len(valores) > 1 else texto
        return f"{self._moeda(sum(valores))} total"

    def _resetar_selecao(self) -> None:
        self.entrada_arquivo.configure(state="normal")
        self.arquivos = []
        self.arquivos_lidos = {}
        self.dados = None
        self.arquivo_lido = None
        self.resultado = None
        self.pdf_path = None
        self.var_arquivo.set("")
        self.var_uf.set("")
        self.var_valor.set("")
        self.var_vencimento.set(vencimento_padrao().strftime("%d/%m/%Y"))
        self.var_pagamento.set(date.today().strftime("%d/%m/%Y"))
        self.entrada_uf.configure(state="readonly")
        self._atualizar_selecao()

    def _modo_varias(self) -> bool:
        return self.var_modo.get() == "varias"

    def _mudar_modo(self) -> None:
        self._limpar_leitura()
        if self.modo_entrada == "chave":
            if self._modo_varias():
                self.var_status.set(
                    "Modo várias GNRE: adicione as chaves do mesmo estado."
                )
            else:
                self.var_status.set("Modo GNRE única: adicione a chave do CT-e.")
        elif self._modo_varias():
            self.var_status.set("Modo várias GNRE: selecione XMLs do mesmo estado.")
        else:
            self.var_status.set("Modo GNRE única: selecione um XML do CT-e.")

    def _atualizar_selecao(self) -> None:
        self.btn_emitir.configure(
            text="Emitir guias" if self._modo_varias() else "Emitir guia"
        )

    def _abrir_lote(self) -> None:
        self._trocar_aba("xml")
        self.var_modo.set("varias")
        self._procurar()

    # ------------------------------------------------------------------
    # entrada por chave do CT-e
    # ------------------------------------------------------------------

    def _item_chave(self, chave: str) -> dict | None:
        return next((item for item in self.chaves if item["chave"] == chave), None)

    def _linha_chave(self, item: dict) -> None:
        if not self.lista_chaves.exists(item["chave"]):
            return
        self.lista_chaves.item(
            item["chave"],
            values=(self._formatar_chave(item["chave"]), item["situacao"]),
            tags=(item["tag"],),
        )

    def _definir_situacao(self, chave: str, situacao: str, tag: str) -> None:
        item = self._item_chave(chave)
        if item is None:
            return
        item["situacao"] = situacao
        item["tag"] = tag
        self._linha_chave(item)

    def _adicionar_chave(self) -> None:
        chave = re.sub(r"\D", "", self.var_chave.get())
        if not chave:
            return
        self.var_chave.set("")
        try:
            validar_chave(chave)
        except Exception as erro:
            messagebox.showwarning("Chave inválida", str(erro))
            self.var_status.set("Chave inválida. Confira os 44 dígitos.")
            return
        if self._item_chave(chave) is not None:
            messagebox.showinfo("Chave repetida", "Esta chave já está na lista.")
            return
        if self.modo_entrada != "chave":
            self._trocar_aba("chave")
        item = {
            "chave": chave,
            "situacao": "Aguardando o XML...",
            "tag": "pendente",
            "caminho": None,
            "erro": None,
            "ambiente": self.var_ambiente.get(),
        }
        self.chaves.append(item)
        self.lista_chaves.insert(
            "", "end", iid=chave,
            values=(self._formatar_chave(chave), item["situacao"]),
            tags=("pendente",),
        )
        self.lista_chaves.see(chave)
        self._log(f"Chave adicionada: {self._formatar_chave(chave)}")
        self.var_status.set("Baixando o XML do CT-e pela chave...")
        self._fila_chaves.put(chave)

    def _baixar_chaves(self) -> None:
        while True:
            chave = self._fila_chaves.get()
            if chave is None:
                return
            item = self._item_chave(chave)
            if item is None:
                continue
            cert = self.certificado
            if cert is None:
                self.fila.put(
                    ("chave_erro", (chave, "Certificado digital não carregado."))
                )
                continue
            cache = self.xml_dir / f"{chave}.xml"
            if cache.exists() and time.monotonic() - self._ultima_distribuicao < 300:
                self.fila.put(("chave_pronta", (chave, str(cache))))
                continue
            self.fila.put(("chave_situacao", (chave, "Consultando a SEFAZ...")))
            config = self.config
            try:
                with DistribuicaoCTe(
                    cert,
                    self.xml_dir,
                    ambiente=item["ambiente"],
                    uf_autor=config.uf_autor if config is not None else "42",
                    timeout=config.timeout if config is not None else 60,
                    verificar_ssl=config.verificar_ssl if config is not None else True,
                    log=lambda mensagem: self.fila.put(("log", mensagem)),
                ) as cliente:
                    caminho = cliente.obter(chave)
                self._ultima_distribuicao = time.monotonic()
                self.fila.put(("chave_pronta", (chave, str(caminho))))
            except Exception as erro:
                self._ultima_distribuicao = time.monotonic()
                self.fila.put(("chave_erro", (chave, f"{erro}")))

    def _remover_chave(self) -> None:
        selecao = self.lista_chaves.selection()
        if not selecao:
            messagebox.showwarning("Aviso", "Selecione a chave que deseja remover.")
            return
        chave = selecao[0]
        self.lista_chaves.delete(chave)
        self.chaves = [item for item in self.chaves if item["chave"] != chave]
        if self._caminhos_selecionados():
            self._ler_cte(avisar=False)
        else:
            self._limpar_leitura()
            self.var_status.set("Nenhuma chave selecionada.")

    def _limpar_chaves(self) -> None:
        for chave in list(self.lista_chaves.get_children()):
            self.lista_chaves.delete(chave)
        self.chaves = []
        self._limpar_leitura()
        self.var_status.set("Lista de chaves limpa.")

    def _procurar(self) -> None:
        if self._modo_varias():
            arquivos = filedialog.askopenfilenames(
                title="Selecionar XMLs do CT-e (mesmo estado)",
                filetypes=[("Arquivos XML", "*.xml"), ("Todos os arquivos", "*.*")],
            )
        else:
            escolhido = filedialog.askopenfilename(
                title="Selecionar XML do CT-e",
                filetypes=[("Arquivos XML", "*.xml"), ("Todos os arquivos", "*.*")],
            )
            arquivos = (escolhido,) if escolhido else ()
        if not arquivos:
            return
        self._resetar_selecao()
        caminhos = [str(Path(arquivo).resolve()) for arquivo in arquivos]
        if len(caminhos) == 1:
            self.var_arquivo.set(caminhos[0])
            self._log(f"Arquivo selecionado: {caminhos[0]}")
            if self._modo_varias():
                self.var_status.set("Lendo o CT-e...")
                self._ler_cte()
            else:
                self.var_status.set("Clique em 'Ler e conferir'.")
            return

        self.arquivos = caminhos
        self.var_arquivo.set(f"{len(caminhos)} XMLs selecionados")
        self.entrada_arquivo.configure(state="readonly")
        self._log(f"{len(caminhos)} arquivos selecionados:")
        for caminho in caminhos:
            self._log(f"- {caminho}")
        self.var_status.set("Lendo os CT-es...")
        self._ler_cte()

    def _ler_cte(self, avisar: bool = True) -> None:
        caminhos = self._caminhos_selecionados()
        if not caminhos:
            if avisar:
                messagebox.showwarning(
                    "Aviso", "Selecione um arquivo XML de CT-e válido."
                )
            else:
                self.var_status.set("Aguardando o XML do CT-e.")
            return
        if self.config is None:
            if avisar:
                messagebox.showerror(
                    "Erro", "Configuração inválida. Corrija o config.json."
                )
            else:
                self.var_status.set("Configuração inválida. Corrija o config.json.")
            return

        for caminho in caminhos:
            if not Path(caminho).exists():
                self.var_status.set("Arquivo não encontrado.")
                if avisar:
                    messagebox.showwarning(
                        "Aviso", f"Arquivo não encontrado:\n{caminho}"
                    )
                else:
                    self._log(f"ERRO: arquivo não encontrado: {caminho}")
                return

        self.arquivos_lidos = {}
        self.dados = None
        self.arquivo_lido = None
        self.var_uf.set("")
        self.var_valor.set("")
        self._atualizar_selecao()

        lidos: dict[str, DadosCTe] = {}
        for caminho in caminhos:
            try:
                lidos[str(Path(caminho).resolve())] = ler_cte_xml(caminho)
            except Exception as erro:
                self.var_status.set("Erro ao ler o CT-e.")
                self._log(f"ERRO ao ler o CT-e {caminho}: {erro}")
                if avisar:
                    messagebox.showerror(
                        "Erro ao ler o CT-e",
                        f"{Path(caminho).name}\n\n{erro}",
                    )
                return

        lote = self._modo_varias() or len(lidos) > 1
        ufs = {dados.uf_favorecida for dados in lidos.values() if dados.uf_favorecida}
        sem_uf = [caminho for caminho, dados in lidos.items() if not dados.uf_favorecida]

        if len(ufs) > 1:
            lista = ", ".join(sorted(ufs))
            self.var_status.set(f"XMLs de UFs diferentes ({lista}) — lote não carregado.")
            self._log(f"ERRO: os XMLs do lote têm UFs diferentes: {lista}.")
            if avisar:
                messagebox.showerror(
                    "UFs diferentes",
                    f"Os XMLs selecionados são de UFs diferentes ({lista}).\n"
                    "Selecione apenas arquivos do mesmo estado para gerar o lote.",
                )
            return
        if lote and sem_uf:
            nomes = "\n".join(f"- {Path(caminho).name}" for caminho in sem_uf)
            self.var_status.set("XML sem UF favorecida — lote não carregado.")
            self._log("ERRO: o lote contém XML sem UF favorecida:")
            for caminho in sem_uf:
                self._log(f"- {Path(caminho).name}")
            if avisar:
                messagebox.showerror(
                    "UF não informada",
                    "Estes XMLs não trazem a UF favorecida (ICMSOutraUF):\n"
                    f"{nomes}\n\nO lote exige a UF definida em todos os arquivos. "
                    "Emita esses arquivos individualmente e informe a UF.",
                )
            return

        self.arquivos_lidos = lidos
        self.arquivo_lido, self.dados = next(iter(lidos.items()))
        uf_comum = next(iter(ufs), "")
        self.var_uf.set(uf_comum)
        self.var_valor.set(self._rotulo_valor(lidos))
        venc = vencimento_padrao(uf_comum or None)
        self.var_vencimento.set(venc.strftime("%d/%m/%Y"))
        self.var_pagamento.set(date.today().strftime("%d/%m/%Y"))
        if uf_comum:
            self.entrada_uf.configure(state="readonly")
        else:
            self.entrada_uf.configure(state="normal")
            self.entrada_uf.focus_set()

        self._log("")
        if lote:
            if len(lidos) > 1:
                self._log(f"=== Lote de {len(lidos)} CT-es lidos (UF {uf_comum}) ===")
            else:
                self._log(f"=== CT-e lido (UF {uf_comum}) — modo lote ===")
            for caminho, dados in lidos.items():
                valor = f"{dados.valor_icms:,.2f}".replace(",", "X")
                valor = valor.replace(".", ",").replace("X", ".")
                self._log(
                    f"- CT-e {dados.numero} (série {dados.serie}) | "
                    f"{Path(caminho).name} | R$ {valor} | "
                    f"{dados.uf_ini} -> {dados.uf_fim}"
                )
                for aviso in dados.avisos:
                    self._log(f"  AVISO: {aviso}")
            self._log("Cada XML gera uma guia separada; a conferência é individual.")
        else:
            self._log("=== CT-e lido ===")
            for linha in self.dados.resumo().splitlines():
                self._log(linha)
            for aviso in self.dados.avisos:
                self._log(f"AVISO: {aviso}")

        if (
            not lote
            and self.certificado is not None
            and self.config is not None
            and not (
                portal_guia is not None
                and portal_guia.usa_portal(self.dados.uf_favorecida)
            )
        ):
            try:
                contribuinte = resolver_contribuinte(self.dados, self.certificado, self.config)
                self._log(
                    f"Contribuinte: {contribuinte.nome} | CNPJ {contribuinte.cnpj} | "
                    "CNPJ + endereço (sem IE na UF favorecida)"
                )
            except Exception as erro:
                self._log(f"AVISO: {erro}")

        if not lote and not self.dados.uf_favorecida:
            self._log(
                "AVISO: este CT-e não trouxe ICMSOutraUF. "
                "O campo UF favorecida foi liberado para preenchimento manual."
            )

        self._atualizar_selecao()
        if lote:
            if len(lidos) > 1:
                self.var_status.set(
                    f"{len(lidos)} CT-es lidos. Uma guia será gerada para cada XML."
                )
            else:
                self.var_status.set("CT-e lido. O modo lote gera uma guia para o XML.")
        else:
            self.var_status.set("CT-e lido. Confira os dados e escolha a ação.")

    def _emitir(self) -> None:
        if self.em_execucao:
            return
        if self.modo_entrada == "chave":
            pendentes = [item for item in self.chaves if not item.get("caminho")]
            if pendentes:
                messagebox.showwarning(
                    "Aviso",
                    f"{len(pendentes)} chave(s) ainda sem XML. Aguarde o download "
                    "ou remova as chaves com erro antes de emitir.",
                )
                return
        caminhos = self._caminhos_selecionados()
        if not caminhos:
            if self.modo_entrada == "chave":
                messagebox.showwarning("Aviso", "Adicione ao menos uma chave do CT-e.")
            else:
                messagebox.showwarning("Aviso", "Selecione o XML do CT-e.")
            return
        if self.config is None:
            messagebox.showerror("Erro", "Configuração inválida. Corrija o config.json.")
            return
        if self._modo_varias() or len(caminhos) > 1:
            self._emitir_lote(caminhos)
            return

        caminho = caminhos[0]
        if not Path(caminho).exists():
            messagebox.showwarning("Aviso", "Selecione o XML do CT-e.")
            return
        lido = (
            self.dados is not None
            and self.arquivo_lido == str(Path(caminho).resolve())
        )
        if not lido:
            # A rota (portal x GNRE) sai da UF do XML, nunca do que está na tela.
            try:
                self.dados = ler_cte_xml(caminho)
                self.arquivo_lido = str(Path(caminho).resolve())
                lido = True
            except Exception as erro:
                self._log(
                    f"AVISO: o CT-e não pôde ser lido antes da emissão ({erro})."
                )
        uf: str | None = None
        if lido and not self.dados.uf_favorecida:
            uf = self.var_uf.get().strip().upper() or None
            if not uf:
                messagebox.showwarning(
                    "Aviso",
                    "Este CT-e não informa a UF favorecida (ICMSOutraUF).\n"
                    "Informe a UF no campo 'UF favorecida'.",
                )
                return
        elif lido:
            self.var_uf.set(self.dados.uf_favorecida)

        uf_efetiva = (self.dados.uf_favorecida if lido else None) or uf
        via_portal = bool(portal_guia and portal_guia.usa_portal(uf_efetiva))
        if self.certificado is None and not via_portal:
            messagebox.showerror(
                "Erro",
                "Certificado digital inválido. Carregue o A1 para emitir pela GNRE.",
            )
            return

        vencimento: str | None = None
        if lido or uf:
            uf_calendario = (self.dados.uf_favorecida if lido else None) or uf
            venc = vencimento_padrao(uf_calendario)
            vencimento = venc.isoformat()
            self.var_vencimento.set(venc.strftime("%d/%m/%Y"))
        hoje = date.today()
        pagamento = hoje.isoformat()
        self.var_pagamento.set(hoje.strftime("%d/%m/%Y"))
        ambiente = self.var_ambiente.get()

        if ambiente == "1":
            if not messagebox.askyesno(
                "Emitir em produção",
                "A guia será emitida de verdade em PRODUÇÃO e gerará um débito fiscal.\n\n"
                "Confirma a emissão?",
            ):
                return

        self.em_execucao = True
        self.resultado = None
        self.pdf_path = None
        self._pdf_retry_id += 1
        self.btn_emitir.configure(state="disabled")
        self._limpar_log()
        self.var_status.set("Emitindo guia...")

        self._log(
            f"CT-e: {caminho}\n"
            f"UF favorecida: {uf_efetiva or 'automática'} | "
            f"Via: {'portal estadual (SP/ES/PR)' if via_portal else 'GNRE'} | Ambiente: "
            f"{'produção' if ambiente == '1' else 'homologação'}"
        )

        thread = threading.Thread(
            target=self._executar,
            args=(
                caminho,
                True,
                uf_efetiva or None,
                vencimento or None,
                pagamento or None,
                ambiente,
            ),
            daemon=True,
        )
        thread.start()

    def _executar(
        self,
        caminho: str,
        emitir: bool,
        uf: str | None,
        vencimento: str | None,
        pagamento: str | None,
        ambiente: str,
    ) -> None:
        try:
            dados = self.dados
            if dados is None or self.arquivo_lido != str(Path(caminho).resolve()):
                try:
                    dados = ler_cte_xml(caminho)
                except Exception:
                    dados = self.dados
            uf_efetiva = (dados.uf_favorecida if dados is not None else None) or uf
            if portal_guia is not None and portal_guia.usa_portal(uf_efetiva):
                resultado = portal_guia.emitir(
                    caminho,
                    dados=dados,
                    log=lambda mensagem: self.fila.put(("log", mensagem)),
                    visivel=self._portal_visivel,
                )
            elif (uf_efetiva or "").strip().upper() in ("SP", "ES", "PR"):
                raise RuntimeError(
                    f"A UF {uf_efetiva} só pode ser emitida pelo portal e o robô "
                    "do portal não carregou nesta execução. Nada foi enviado à GNRE."
                )
            else:
                resultado = emitir_guia(
                    caminho,
                    config=self.config,
                    ambiente=ambiente,
                    emitir=emitir,
                    gerar_pdf=True,
                    consultar_regras=True,
                    uf_favorecida=uf,
                    data_vencimento=vencimento,
                    data_pagamento=pagamento,
                    log=lambda mensagem: self.fila.put(("log", mensagem)),
                    ao_receber=lambda recibo: self.fila.put(("recibo", recibo)),
                )
        except Exception as erro:
            self.fila.put(("erro", f"{type(erro).__name__}: {erro}"))
        else:
            self.fila.put(("fim", resultado))

    def _emitir_lote(self, caminhos: list[str]) -> None:
        faltando = [caminho for caminho in caminhos if caminho not in self.arquivos_lidos]
        if faltando:
            dica = (
                "Aguarde o download dos XMLs das chaves antes de emitir."
                if self.modo_entrada == "chave"
                else "Clique em 'Ler e conferir' para validar o lote antes de emitir."
            )
            messagebox.showinfo("Leia os CT-es", dica)
            return
        dados = [self.arquivos_lidos[caminho] for caminho in caminhos]
        ufs = {item.uf_favorecida for item in dados}
        if len(ufs) != 1 or not next(iter(ufs)):
            messagebox.showerror(
                "UFs diferentes",
                "O lote precisa de XMLs do mesmo estado, todos com UF definida.",
            )
            return
        uf = next(iter(ufs))
        venc = vencimento_padrao(uf)
        hoje = date.today()
        self.var_uf.set(uf)
        self.var_vencimento.set(venc.strftime("%d/%m/%Y"))
        self.var_pagamento.set(hoje.strftime("%d/%m/%Y"))
        ambiente = self.var_ambiente.get()
        total_guias = len(caminhos)
        plural = "s" if total_guias > 1 else ""

        if ambiente == "1":
            if not messagebox.askyesno(
                "Emitir lote em produção",
                f"{total_guias} guia{plural} {'serão emitidas' if plural else 'será emitida'} "
                "de verdade em PRODUÇÃO e gerará débitos fiscais.\n\nConfirma a emissão?",
            ):
                return

        self.em_execucao = True
        self.resultado = None
        self.pdf_path = None
        self._pdf_retry_id += 1
        self.btn_emitir.configure(state="disabled")
        self._limpar_log()
        self.var_status.set(
            f"Emitindo {total_guias} guia{plural}..."
        )
        self._log(
            f"Lote: {total_guias} CT-e{plural} | UF {uf} | Ambiente "
            f"{'produção' if ambiente == '1' else 'homologação'}\n"
            "Ao final da emissão você escolhe onde salvar os PDFs."
        )

        thread = threading.Thread(
            target=self._executar_lote,
            args=(
                caminhos,
                dict(self.arquivos_lidos),
                uf,
                venc.isoformat(),
                hoje.isoformat(),
                ambiente,
            ),
            daemon=True,
        )
        thread.start()

    def _executar_lote(
        self,
        caminhos: list[str],
        lidos: dict[str, DadosCTe],
        uf: str,
        vencimento: str,
        pagamento: str,
        ambiente: str,
    ) -> None:
        total = len(caminhos)
        resumo: list[dict] = []
        via_portal = portal_guia is not None and portal_guia.usa_portal(uf)
        try:
            if via_portal:
                for indice, caminho in enumerate(caminhos, 1):
                    item = self._item_do_lote(indice, caminho, lidos)
                    self.fila.put(("log", ""))
                    self.fila.put(
                        ("log", f"=== CT-e {indice}/{total}: {Path(caminho).name} ===")
                    )
                    self.fila.put(("lote_etapa", (indice, total)))
                    try:
                        resultado = portal_guia.emitir(
                            caminho,
                            dados=lidos.get(caminho),
                            log=lambda mensagem: self.fila.put(("log", mensagem)),
                            visivel=self._portal_visivel,
                        )
                        item["resultado"] = resultado
                        self._classificar_item(item, resultado)
                    except Exception as erro:
                        item["tipo"] = "falha"
                        item["mensagem"] = f"{type(erro).__name__}: {erro}"
                        self.fila.put(("log", f"ERRO: {item['mensagem']}"))
                    resumo.append(item)
                return
            try:
                certificado = carregar_certificado(
                    self.config.caminho_pfx, self.config.senha
                )
            except Exception as erro:
                mensagem = f"{type(erro).__name__}: {erro}"
                self.fila.put(("log", f"ERRO: {mensagem}"))
                for indice, caminho in enumerate(caminhos, 1):
                    item = self._item_do_lote(indice, caminho, lidos)
                    item["mensagem"] = mensagem
                    resumo.append(item)
                return
            for aviso in certificado.avisos:
                self.fila.put(("log", f"AVISO: {aviso}"))
            # Um único cliente para o lote inteiro: o certificado é carregado
            # uma vez e o TLS fica aquecido entre um CT-e e outro.
            with ClienteGNRE(
                certificado,
                ambiente=ambiente,
                timeout=self.config.timeout,
                verificar_ssl=self.config.verificar_ssl,
                log=lambda mensagem: self.fila.put(("log", mensagem)),
            ) as cliente:
                for indice, caminho in enumerate(caminhos, 1):
                    item = self._item_do_lote(indice, caminho, lidos)
                    self.fila.put(("log", ""))
                    self.fila.put(
                        ("log", f"=== CT-e {indice}/{total}: {Path(caminho).name} ===")
                    )
                    self.fila.put(("lote_etapa", (indice, total)))
                    try:
                        resultado = emitir_guia(
                            caminho,
                            config=self.config,
                            ambiente=ambiente,
                            emitir=True,
                            gerar_pdf=True,
                            consultar_regras=True,
                            uf_favorecida=uf,
                            data_vencimento=vencimento,
                            data_pagamento=pagamento,
                            log=lambda mensagem: self.fila.put(("log", mensagem)),
                            ao_receber=lambda recibo, i=indice: self.fila.put(
                                ("recibo_lote", (i, total, recibo))
                            ),
                            cert=certificado,
                            cliente=cliente,
                        )
                        item["resultado"] = resultado
                        self._classificar_item(item, resultado)
                    except Exception as erro:
                        item["tipo"] = "falha"
                        item["mensagem"] = f"{type(erro).__name__}: {erro}"
                        self.fila.put(("log", f"ERRO: {item['mensagem']}"))
                    resumo.append(item)
        finally:
            self.fila.put(("lote_fim", resumo))

    def _item_do_lote(
        self, indice: int, caminho: str, lidos: dict[str, DadosCTe]
    ) -> dict:
        dados = lidos.get(caminho)
        return {
            "indice": indice,
            "caminho": caminho,
            "numero": (dados.numero if dados else "") or Path(caminho).stem,
            "tipo": "falha",
            "resultado": None,
            "destino": None,
            "mensagem": "",
        }

    def _classificar_item(self, item: dict, resultado: ResultadoEmissao) -> None:
        if resultado.guia_divergente:
            item["tipo"] = "divergente"
            item["mensagem"] = "; ".join(resultado.divergencias)
            self.fila.put(
                ("log", "ERRO: guia divergente — NÃO PAGUE. Nenhum PDF foi gerado.")
            )
            return
        if resultado.pdf_path:
            item["tipo"] = "ok"
            item["pdf_origem"] = str(resultado.pdf_path)
            self.fila.put(
                ("log", f"Guia {item['numero']} emitida; PDF pronto para salvar.")
            )
            return
        guias = self._guias_processadas(resultado)
        if guias:
            item["tipo"] = "sem_pdf"
            item["mensagem"] = self._detalhe_guias(guias).replace("\n", " | ")
            self.fila.put(
                (
                    "log",
                    f"AVISO: guia {item['numero']} emitida sem PDF do portal; "
                    "pague pela linha digitável.",
                )
            )
            return
        if resultado.uf_indisponivel:
            item["tipo"] = "uf"
            item["mensagem"] = " ".join(resultado.mensagens) or "UF indisponível."
            self.fila.put(("log", f"ERRO: guia {item['numero']} — UF indisponível."))
            return
        if resultado.numero_recibo:
            item["tipo"] = "pendente"
            item["mensagem"] = f"recibo {resultado.numero_recibo}"
            self.fila.put(
                (
                    "log",
                    f"AVISO: guia {item['numero']} ainda em processamento "
                    f"(recibo {resultado.numero_recibo}).",
                )
            )
            return
        item["tipo"] = "falha"
        item["mensagem"] = " ".join(resultado.mensagens) or "Falha não detalhada."
        self.fila.put(("log", f"ERRO: {item['mensagem']}"))

    def _salvar_pdf_no_lote(
        self, resultado: ResultadoEmissao, pasta: Path
    ) -> tuple[Path | None, str]:
        origem = Path(resultado.pdf_path) if resultado.pdf_path else None
        if origem is None or not origem.exists():
            return None, "PDF não encontrado"
        destino = self._destino_livre(pasta, self._nome_pdf(resultado))
        try:
            shutil.copy2(origem, destino)
        except OSError as erro:
            return None, str(erro)
        return destino, ""

    def _destino_livre(self, pasta: Path, nome: str) -> Path:
        destino = pasta / nome
        contador = 2
        while destino.exists():
            destino = pasta / f"{Path(nome).stem} ({contador}){Path(nome).suffix}"
            contador += 1
        return destino

    def _salvar_pdfs_do_lote(self, itens: list[dict]) -> None:
        total = len(itens)
        plural = "s" if total > 1 else ""
        inicial = Path(self.config.pasta_saida) if self.config else DADOS_DIR / "guias"
        if not inicial.is_absolute():
            inicial = DADOS_DIR / inicial
        inicial.mkdir(parents=True, exist_ok=True)
        pasta = filedialog.askdirectory(
            parent=self.root,
            title=f"Salvar {'as' if plural else 'a'} guia{plural} em PDF",
            initialdir=str(inicial),
            mustexist=True,
        )
        if not pasta:
            for item in itens:
                item["destino"] = item.get("pdf_origem") or ""
                self._log(
                    f"Salvamento cancelado; o PDF de CT-e {item['numero']} continua em "
                    f"{item['destino']}."
                )
            return
        for item in itens:
            resultado = item.get("resultado")
            if resultado is None:
                item["destino"] = item.get("pdf_origem") or ""
                continue
            destino, erro = self._salvar_pdf_no_lote(resultado, Path(pasta))
            item["destino"] = str(destino) if destino else (item.get("pdf_origem") or "")
            if destino:
                self._log(f"Guia {item['numero']}: PDF salvo em {destino}.")
            else:
                item["mensagem"] = erro
                self._log(
                    f"AVISO: guia {item['numero']} emitida, mas o PDF não foi copiado "
                    f"({erro}). Original em {item['destino']}."
                )

    def _oficializar_lote(self, itens: list[dict]) -> None:
        """Busca o PDF oficial de cada guia e substitui o PDF entregue."""
        alvos = []
        for item in itens:
            resultado = item.get("resultado")
            if resultado is None or not item.get("destino"):
                continue
            if not resultado.numero_recibo or not resultado.pdf_local:
                continue
            if resultado.ambiente not in ("1", "2"):
                continue
            alvos.append(item)
        if alvos:
            threading.Thread(
                target=self._baixar_oficiais, args=(alvos,), daemon=True
            ).start()

    def _baixar_oficiais(self, itens: list[dict]) -> None:
        # Uma guia por vez: o portal limita consultas e cada download já
        # faz tentativas próprias.
        for item in itens:
            self._baixar_oficial_item(item)

    def _baixar_oficial_item(self, item: dict) -> None:
        resultado: ResultadoEmissao = item["resultado"]
        destino = Path(item["destino"])
        municipio = (
            resultado.dados.emitente.municipio_nome
            if resultado.dados and resultado.dados.emitente
            else ""
        )
        for tentativa in range(1, 4):
            if tentativa > 1:
                time.sleep(30)
            self.fila.put(
                (
                    "log",
                    f"Buscando o PDF oficial do CT-e {item['numero']} "
                    f"(tentativa {tentativa}/3)...",
                )
            )
            try:
                recuperacao = baixar_pdf_recibo(
                    resultado.numero_recibo,
                    config=self.config,
                    ambiente=resultado.ambiente,
                    log=lambda mensagem: self.fila.put(("log", mensagem)),
                    municipio_nome=municipio,
                    gerar_local=False,
                    conferir=False,
                )
            except Exception as erro:  # noqa: BLE001 - a guia local já está entregue
                self.fila.put(
                    (
                        "log",
                        f"AVISO: PDF oficial do CT-e {item['numero']} falhou "
                        f"({type(erro).__name__}: {erro}).",
                    )
                )
                continue
            oficial = recuperacao.pdf_path
            if oficial is None:
                continue
            aplicado, mensagem = self._aplicar_pdf_oficial(
                Path(oficial), destino
            )
            if aplicado:
                item["destino"] = str(aplicado)
                self.fila.put(
                    (
                        "log",
                        f"PDF oficial aplicado na guia do CT-e "
                        f"{item['numero']}: {aplicado}.",
                    )
                )
            self.fila.put(("log", mensagem))
            return
        self.fila.put(
            (
                "log",
                f"AVISO: o portal não entregou o PDF oficial do CT-e "
                f"{item['numero']}; a guia local (mesmo layout) foi mantida.",
            )
        )

    def _aplicar_pdf_oficial(
        self, oficial: Path, destino: Path
    ) -> tuple[Path | None, str]:
        """Copia o PDF oficial sobre o entregue, tolerando arquivo aberto."""
        if oficial.resolve() == destino.resolve():
            return destino, f"PDF oficial confirmado em {destino}."
        for tentativa in range(1, 6):
            try:
                shutil.copy2(oficial, destino)
                return destino, f"PDF oficial aplicado: {destino}."
            except PermissionError:
                if tentativa < 5:
                    time.sleep(1.2)
            except OSError as erro:
                return None, (
                    f"AVISO: PDF oficial disponível em {oficial}; não copiei "
                    f"para {destino} ({erro})."
                )
        alternativo = destino.with_name(
            f"{destino.stem} (oficial){destino.suffix}"
        )
        try:
            shutil.copy2(oficial, alternativo)
            return alternativo, (
                f"A guia estava aberta; o PDF oficial foi salvo em "
                f"{alternativo}."
            )
        except OSError as erro:
            return None, (
                f"AVISO: PDF oficial disponível em {oficial}; não copiei "
                f"({erro})."
            )

    # ------------------------------------------------------------------
    # fila e finalização
    # ------------------------------------------------------------------

    def _processar_fila(self) -> None:
        try:
            while True:
                tipo, dado = self.fila.get_nowait()
                if tipo == "log":
                    self._log(dado)
                elif tipo == "recibo":
                    self.var_status.set(
                        f"Lote recebido — recibo {dado}. Consultando o processamento..."
                    )
                    self._log(
                        f">>> Recibo {dado} — guarde este número. "
                        "Se precisar retomar depois, clique de novo em 'Emitir guia' "
                        "com o mesmo CT-e: o app consulta esse recibo no portal e não reenvia o lote."
                    )
                elif tipo == "lote_etapa":
                    indice, total = dado
                    self._lote_info = (indice, total)
                    self.var_status.set(f"Emitindo guia {indice} de {total}...")
                elif tipo == "recibo_lote":
                    indice, total, recibo = dado
                    self.var_status.set(
                        f"Guia {indice}/{total}: lote recebido (recibo {recibo}). "
                        "Consultando..."
                    )
                    self._log(f">>> Recibo {recibo} — guia {indice} de {total}.")
                elif tipo == "lote_fim":
                    self._finalizar_lote(dado)
                elif tipo == "erro":
                    self._finalizar_erro(dado)
                elif tipo == "fim":
                    self._finalizar(dado)
                elif tipo == "pdf_preparado":
                    self._pdf_preparado(*dado)
                elif tipo == "chave_situacao":
                    chave, situacao = dado
                    self._definir_situacao(chave, situacao, "pendente")
                elif tipo == "chave_pronta":
                    chave, caminho = dado
                    item = self._item_chave(chave)
                    if item is not None:
                        item["caminho"] = caminho
                        item["erro"] = None
                    self._definir_situacao(chave, "XML pronto", "pronto")
                    self._log(
                        f"CT-e {self._formatar_chave(chave)} baixado: {caminho}"
                    )
                    pendentes = [
                        outro
                        for outro in self.chaves
                        if not outro.get("caminho") and not outro.get("erro")
                    ]
                    if pendentes:
                        self.var_status.set(
                            f"Baixando os CT-es... {len(pendentes)} restante(s)."
                        )
                    else:
                        self.var_status.set("CT-es baixados. Lendo os dados da guia...")
                        self._ler_cte(avisar=False)
                elif tipo == "chave_erro":
                    chave, mensagem = dado
                    item = self._item_chave(chave)
                    if item is not None:
                        item["erro"] = mensagem
                    self._definir_situacao(chave, "Erro — veja o Registro", "erro")
                    self._log(
                        f"ERRO na chave {self._formatar_chave(chave)}: {mensagem}"
                    )
                    self.var_status.set(
                        "Não foi possível baixar um dos CT-es. Veja o Registro."
                    )
                elif tipo == "atualizacao_consulta":
                    self._avaliar_atualizacao(dado)
                elif tipo == "atualizacao_disponivel":
                    self.var_status.set(f"Nova versão v{dado.versao} disponível.")
                    self._log(
                        f"Atualização: versão v{dado.versao} publicada no GitHub."
                    )
                    self._avaliar_atualizacao(dado)
                elif tipo == "atualizacao_progresso":
                    self.var_status.set(f"Baixando a atualização: {dado}%")
                elif tipo == "atualizacao_baixada":
                    self._instalar_atualizacao(*dado)
                elif tipo == "atualizacao_erro":
                    self.var_status.set("Atualização: falha na consulta ou no download.")
                    self._log(f"ERRO na atualização: {dado}")
                    messagebox.showerror("Atualização", dado)
        except queue.Empty:
            pass
        self.root.after(120, self._processar_fila)

    def _guias_processadas(self, resultado: ResultadoEmissao) -> list[dict]:
        return [
            guia
            for guia in resultado.guias
            if str(guia.get("codigo") or "") == "0"
        ]

    def _detalhe_guias(self, guias: list[dict]) -> str:
        linhas = []
        for guia in guias:
            numero = guia.get("numero") or ""
            linha = guia.get("linha_digitavel") or ""
            if numero:
                linhas.append(f"Guia: {numero}")
            if linha:
                linhas.append(f"Linha digitável: {linha}")
        return "\n".join(linhas)

    def _finalizar(self, resultado: ResultadoEmissao) -> None:
        self.resultado = resultado
        self._log("")
        self._log("=== Resultado ===")
        for linha in resultado.resumo().splitlines():
            self._log(linha)
        for aviso in resultado.avisos:
            self._log(f"AVISO: {aviso}")
        for mensagem in resultado.mensagens:
            self._log(f"- {mensagem}")
        if resultado.validacao:
            self._log("")
            for linha in resultado.validacao.get("linhas", [])[1:]:
                self._log(linha)

        guias = self._guias_processadas(resultado)
        if resultado.guia_divergente:
            self._quarentenar_pdf(resultado, "a guia divergiu do CT-e;")
            self.var_status.set("CONFERÊNCIA REPROVADA — não pague esta guia.")
            detalhe = [
                "A guia devolvida pelo portal NÃO confere com o CT-e e não deve ser paga.",
                "",
                *[f"- {linha}" for linha in resultado.divergencias],
            ]
            if resultado.numero_recibo:
                detalhe.append(f"\nRecibo do lote: {resultado.numero_recibo}")
            if resultado.retorno_path:
                detalhe.append(f"Retorno oficial: {resultado.retorno_path}")
            detalhe.append(
                "\nConfira o lote no portal GNRE antes de qualquer pagamento. "
                "Nenhum PDF foi gerado."
            )
            messagebox.showerror("Guia divergente — NÃO PAGUE", "\n".join(detalhe))
        elif resultado.sucesso and resultado.pdf_path:
            destino = self._entregar_pdf(resultado)
            if destino is None:
                self.var_status.set("Guia emitida, mas o PDF não foi encontrado.")
                self._log("AVISO: o PDF da guia não foi encontrado para entrega.")
            else:
                self.var_status.set(f"Guia pronta para pagamento: {destino.name}")
                self._abrir_pdf(destino)
                if resultado.pdf_local and resultado.numero_recibo:
                    self._retomar_pdf(resultado, silencioso=True)
        elif guias:
            self.var_status.set(
                "Guia emitida; o portal não entregou o PDF. Use a linha digitável."
            )
            detalhe = [
                "A guia foi processada pelo portal, mas o PDF não foi entregue agora.",
                "",
                self._detalhe_guias(guias),
            ]
            if resultado.numero_recibo:
                detalhe.append(f"\nRecibo do lote: {resultado.numero_recibo}")
            detalhe.append(
                "\nVocê já pode pagar com a linha digitável. O app gera o PDF com esses "
                "dados na hora e continua tentando o PDF oficial em segundo plano; a "
                "guia é entregue e aberta automaticamente."
            )
            faltantes = [m for m in resultado.mensagens if m]
            if faltantes:
                detalhe.append("\n" + "\n".join(faltantes))
            self._retomar_pdf(resultado)
            messagebox.showwarning(
                "Guia emitida — PDF indisponível no portal", "\n".join(detalhe)
            )
        elif resultado.uf_indisponivel:
            self.var_status.set("UF indisponível no portal; o lote pode ser reenviado.")
            detalhe = list(resultado.mensagens) or ["UF indisponível no portal GNRE."]
            if resultado.numero_recibo:
                detalhe.append(f"Recibo do lote: {resultado.numero_recibo}.")
            messagebox.showwarning(
                "GNRE não emitida — UF indisponível", "\n".join(detalhe)
            )
        elif resultado.numero_recibo and resultado.situacao:
            self.var_status.set(
                f"Lote recebido — recibo {resultado.numero_recibo}. Ainda em processamento."
            )
            detalhe = list(resultado.mensagens) or [
                "O portal não confirmou o processamento dentro do tempo."
            ]
            detalhe.append(f"Recibo do lote: {resultado.numero_recibo}.")
            detalhe.append(
                "Clique de novo em 'Emitir guia' com o mesmo CT-e para "
                "consultar o recibo e baixar a guia — o lote não é reenviado."
            )
            messagebox.showwarning("Lote recebido — aguardando o portal", "\n".join(detalhe))
        elif resultado.ambiente == "portal":
            self.var_status.set("O portal estadual não concluiu a emissão.")
            detalhe = list(resultado.mensagens) or ["Falha não detalhada no portal."]
            mensagem = "\n".join(detalhe)
            messagebox.showerror(
                "Emissão pelo portal estadual",
                mensagem
                + "\n\nO navegador do robô é oculto por padrão. Se o portal exigir "
                "captcha, use 'Portal: mostrar navegador' e emita novamente.",
            )
        else:
            self.var_status.set("Falha na emissão. Veja o andamento.")
            detalhe = list(resultado.mensagens) or ["Falha não detalhada."]
            if resultado.numero_recibo:
                detalhe.append(
                    f"O lote já foi recebido pelo portal (recibo {resultado.numero_recibo}). "
                    "Clique de novo em 'Emitir guia' com o mesmo CT-e para "
                    "consultar esse recibo antes de reenviar."
                )
            messagebox.showerror("Falha na GNRE", "\n".join(detalhe))

        self.em_execucao = False
        self.btn_emitir.configure(state="normal")

    def _finalizar_lote(self, resumo: list[dict]) -> None:
        self.em_execucao = False
        self._lote_info = None
        self.btn_emitir.configure(state="normal")

        total = len(resumo)
        emitidas = [item for item in resumo if item["tipo"] == "ok"]
        if emitidas:
            self._salvar_pdfs_do_lote(emitidas)
            self._oficializar_lote(emitidas)
        com_pdf = [item for item in emitidas if item["destino"]]
        sem_pdf = [
            item
            for item in resumo
            if item["tipo"] == "sem_pdf"
            or (item["tipo"] == "ok" and not item["destino"])
        ]
        divergentes = [item for item in resumo if item["tipo"] == "divergente"]
        pendentes = [item for item in resumo if item["tipo"] == "pendente"]
        indisponiveis = [item for item in resumo if item["tipo"] == "uf"]
        falhas = [item for item in resumo if item["tipo"] == "falha"]

        self._log("")
        self._log("=== Resumo do lote ===")
        rotulos = {
            "ok": "guia pronta",
            "sem_pdf": "emitida sem PDF",
            "divergente": "DIVERGENTE — NÃO PAGUE",
            "pendente": "em processamento",
            "uf": "UF indisponível",
            "falha": "falha",
        }
        for item in resumo:
            linha = f"CT-e {item['numero']}: {rotulos.get(item['tipo'], item['tipo'])}"
            if item["destino"]:
                linha += f" — {item['destino']}"
            elif item["mensagem"]:
                linha += f" — {item['mensagem']}"
            self._log(linha)

        plural_lote = "s" if total > 1 else ""
        plural_pdf = "s" if len(com_pdf) != 1 else ""
        linhas = [
            f"Lote de {total} CT-e{plural_lote}: "
            f"{len(com_pdf)} guia{plural_pdf} com PDF salvo."
        ]
        if com_pdf:
            linhas.append("")
            linhas.append("Guias geradas:")
            for item in com_pdf:
                linhas.append(f"- CT-e {item['numero']}: {item['destino']}")
        if sem_pdf:
            linhas.append("")
            linhas.append("Emitidas sem PDF salvo (pague pela linha digitável):")
            for item in sem_pdf:
                detalhe = item["mensagem"] or "PDF não copiado"
                linhas.append(f"- CT-e {item['numero']}: {detalhe}")
        if pendentes:
            linhas.append("")
            linhas.append("Ainda em processamento no portal:")
            for item in pendentes:
                linhas.append(f"- CT-e {item['numero']}: {item['mensagem']}")
            linhas.append(
                "Reenvie o mesmo lote depois: os CT-es já emitidos são apenas "
                "consultados, sem novo envio."
            )
        if indisponiveis:
            linhas.append("")
            linhas.append("UF indisponível no portal:")
            for item in indisponiveis:
                linhas.append(f"- CT-e {item['numero']}: {item['mensagem']}")
        if falhas:
            linhas.append("")
            linhas.append("Falhas:")
            for item in falhas:
                linhas.append(f"- CT-e {item['numero']}: {item['mensagem']}")
        if divergentes:
            linhas.append("")
            linhas.append("DIVERGENTES — NÃO PAGUE:")
            for item in divergentes:
                linhas.append(f"- CT-e {item['numero']}: {item['mensagem']}")
            linhas.append(
                "Confira esses lotes no portal GNRE antes de qualquer pagamento. "
                "Nenhum PDF foi gerado para eles."
            )
        detalhe = "\n".join(linhas)

        if divergentes:
            self.var_status.set(
                f"Lote concluído: {len(divergentes)} guia(s) divergente(s) — NÃO PAGUE."
            )
            messagebox.showerror("Lote — guias divergentes: NÃO PAGUE", detalhe)
        elif falhas or indisponiveis:
            self.var_status.set(
                f"Lote com pendências: {len(com_pdf)} de {total} guia{plural_lote} com PDF."
            )
            messagebox.showerror("Lote concluído com pendências", detalhe)
        elif pendentes or sem_pdf:
            self.var_status.set(
                f"Lote com pendências: {len(com_pdf)} de {total} guia{plural_lote} com PDF."
            )
            messagebox.showwarning("Lote concluído com pendências", detalhe)
        else:
            self.var_status.set(
                f"Lote concluído: {len(com_pdf)} guia{plural_pdf} com PDF."
            )
            messagebox.showinfo("Lote concluído", detalhe)

    def _finalizar_erro(self, mensagem: str) -> None:
        self._log(f"ERRO: {mensagem}")
        self.var_status.set("Erro inesperado. Veja o andamento.")
        self.em_execucao = False
        self.btn_emitir.configure(state="normal")
        messagebox.showerror("Erro", mensagem)

    def _retomar_pdf(
        self, resultado: ResultadoEmissao, silencioso: bool = False
    ) -> None:
        """Tenta o PDF oficial em segundo plano quando o lote já está processado."""
        if not resultado.numero_recibo or self.config is None:
            return
        self._pdf_retry_id += 1
        meu_id = self._pdf_retry_id
        recibo = resultado.numero_recibo
        ambiente = resultado.ambiente
        ja_impressa = resultado.pdf_path is not None

        def trabalhar() -> None:
            for tentativa in range(1, 4):
                espera = 0 if (tentativa == 1 and not ja_impressa) else (
                    20 if silencioso else 45
                )
                if espera:
                    time.sleep(espera)
                if self._pdf_retry_id != meu_id:
                    return
                self.fila.put(
                    (
                        "log",
                        f"Buscando o PDF do recibo {recibo} em segundo plano "
                        f"(tentativa {tentativa}/3)...",
                    )
                )
                recuperacao = baixar_pdf_recibo(
                    recibo,
                    config=self.config,
                    ambiente=ambiente,
                    log=lambda mensagem: self.fila.put(("log", mensagem)),
                    municipio_nome=(
                        resultado.dados.emitente.municipio_nome
                        if resultado.dados and resultado.dados.emitente
                        else ""
                    ),
                    gerar_local=not silencioso,
                )
                if recuperacao.pdf_path and (
                    not recuperacao.pdf_local or not ja_impressa
                ):
                    self.fila.put(
                        (
                            "pdf_preparado",
                            (
                                meu_id,
                                resultado,
                                recuperacao.pdf_path,
                                recuperacao.validacao,
                                silencioso,
                            ),
                        )
                    )
                    return
            self.fila.put(
                (
                    "log",
                    "O portal continua sem entregar o PDF; a guia pode ser paga "
                    "pela linha digitável e o PDF pode ser buscado depois.",
                )
            )

        self._log("Tentando baixar o PDF em segundo plano (3 tentativas).")
        threading.Thread(target=trabalhar, daemon=True).start()

    def _pdf_preparado(
        self,
        meu_id: int,
        resultado: ResultadoEmissao,
        caminho: Path,
        validacao: dict | None = None,
        silencioso: bool = False,
    ) -> None:
        if meu_id != self._pdf_retry_id:
            return
        self._log(f"PDF obtido: {caminho}.")
        if validacao:
            resultado.validacao = validacao
            for linha in validacao.get("linhas", []):
                self._log(linha)
            if not validacao.get("ok", True):
                self._quarentenar_pdf(resultado, "a guia divergiu do CT-e;")
                self.var_status.set("CONFERÊNCIA REPROVADA — não pague esta guia.")
                messagebox.showerror(
                    "Guia divergente — NÃO PAGUE",
                    "\n".join(validacao.get("linhas", []))
                    + "\n\nRelatório: "
                    + str(validacao.get("relatorio") or "")
                    + (
                        f"\n\nO PDF foi retido como: {resultado.pdf_path}"
                        if resultado.pdf_path
                        else ""
                    ),
                )
                return
        if silencioso:
            alvo = Path(resultado.pdf_path) if resultado.pdf_path else None
            if alvo and alvo.exists():
                aplicado, mensagem = self._aplicar_pdf_oficial(
                    Path(caminho), alvo
                )
                if aplicado:
                    resultado.pdf_path = aplicado
                self._log(mensagem)
            else:
                self._log(f"PDF oficial confirmado em {caminho}.")
            return
        resultado.pdf_path = caminho
        destino = self._entregar_pdf(resultado)
        if destino is None:
            return
        self.var_status.set(f"Guia pronta para pagamento: {destino.name}")
        self._abrir_pdf(destino)

    def _pasta_guias(self) -> Path:
        pasta = Path(self.config.pasta_saida) if self.config else DADOS_DIR / "guias"
        if not pasta.is_absolute():
            pasta = DADOS_DIR / pasta
        pasta.mkdir(parents=True, exist_ok=True)
        return pasta

    def _quarentenar_pdf(self, resultado: ResultadoEmissao, motivo: str) -> None:
        """Tira de circulação um PDF que não confere com o CT-e."""
        origem = Path(resultado.pdf_path) if resultado.pdf_path else None
        if origem is None or not origem.exists():
            return
        pasta = origem.parent / "diagnosticos"
        try:
            pasta.mkdir(parents=True, exist_ok=True)
            destino = (
                pasta
                / f"{time.strftime('%Y%m%d_%H%M%S')}_{origem.stem}_NAO_PAGUE.pdf"
            )
            shutil.move(str(origem), destino)
        except OSError as erro:
            self._log(f"AVISO: não consegui reter o PDF divergente ({erro}).")
            return
        resultado.pdf_path = destino
        self.pdf_path = destino
        self._log(f"ERRO: {motivo} PDF retido como NÃO PAGUE em {destino}.")

    def _entregar_pdf(self, resultado: ResultadoEmissao) -> Path | None:
        origem = Path(resultado.pdf_path) if resultado.pdf_path else None
        if origem is None or not origem.exists():
            self._log("AVISO: o PDF da guia não foi encontrado para entrega.")
            return None
        pasta = self._pasta_guias()
        nome = self._nome_pdf(resultado)
        candidato = pasta / nome
        if origem.resolve().parent == pasta.resolve() and origem.name == nome:
            destino = origem
        elif candidato.exists():
            destino = candidato
            try:
                shutil.copy2(origem, candidato)
            except OSError as erro:
                self._log(
                    f"AVISO: não foi possível atualizar {candidato} ({erro}); "
                    f"entregando {origem}."
                )
                destino = origem
        else:
            destino = self._destino_livre(pasta, nome)
            try:
                shutil.copy2(origem, destino)
            except OSError as erro:
                self._log(
                    f"AVISO: não foi possível copiar o PDF ({erro}); entregando {origem}."
                )
                destino = origem
        resultado.pdf_path = destino
        self.pdf_path = destino
        self._log(f"Guia pronta para pagamento: {destino}")
        return destino

    def _abrir_pdf(self, caminho: Path) -> None:
        try:
            os.startfile(str(caminho))
        except OSError as erro:
            self._log(f"AVISO: não foi possível abrir o PDF automaticamente ({erro}).")

    def _nome_pdf(self, resultado: ResultadoEmissao) -> str:
        uf = ""
        numero = ""
        if resultado.resumo_lote:
            uf = str(resultado.resumo_lote.get("uf_favorecida") or "")
        if resultado.dados:
            numero = resultado.dados.numero or ""
            uf = uf or (resultado.dados.uf_favorecida or "")
        if (not uf or not numero) and resultado.numero_recibo and self.config:
            uf_lote, numero_lote = detalhes_do_recibo(
                self.config.pasta_saida, resultado.numero_recibo
            )
            uf = uf or uf_lote
            numero = numero or numero_lote
        numero = numero_do_documento(numero)
        partes = ["Guia"]
        if uf:
            partes.append(uf)
        if numero:
            partes.append(f"CTE {numero}")
        if len(partes) == 1:
            partes.append(resultado.numero_recibo or "GNRE")
        nome = re.sub(r'[\\/:*?"<>|]', "", " - ".join(partes))
        return nome + ".pdf"

    def _fechar(self) -> None:
        if self.em_execucao and not messagebox.askyesno(
            "Sair", "Há uma emissão em andamento. Deseja fechar mesmo assim?"
        ):
            return
        self._salvar_estado_janela()
        self.root.destroy()

    # ------------------------------------------------------------------
    # log
    # ------------------------------------------------------------------

    def _tag_do_log(self, mensagem: str) -> str:
        texto = mensagem.strip()
        baixo = texto.lower()
        if texto.startswith(">>>"):
            return "recibo"
        if texto.startswith("="):
            return "titulo"
        if texto.startswith("ERRO") or "falha" in baixo:
            return "erro"
        if texto.startswith("AVISO"):
            return "aviso"
        if baixo.startswith("pdf:") or any(
            chave in baixo
            for chave in (
                "pdf salvo",
                "pdf obtido",
                "pdf da guia gravado",
                "processamento concluído",
                "certificado atualizado",
            )
        ):
            return "ok"
        return "info"

    def _log(self, mensagem: str) -> None:
        self.txt_log.configure(state="normal")
        if mensagem.strip():
            tag = self._tag_do_log(mensagem)
            for indice, parte in enumerate(mensagem.splitlines()):
                if indice == 0:
                    self.txt_log.insert("end", f"{datetime.now():%H:%M}", "hora")
                    self.txt_log.insert("end", "   ")
                else:
                    self.txt_log.insert("end", " " * 8)
                self.txt_log.insert("end", parte + "\n", tag)
        else:
            self.txt_log.insert("end", "\n", "espaco")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > 5_000_000:
                os.replace(LOG_PATH, LOG_PATH.with_name(LOG_PATH.name + ".1"))
            with LOG_PATH.open("a", encoding="utf-8") as arquivo:
                arquivo.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {mensagem}\n")
        except OSError:
            pass

    def _limpar_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")


class DialogoCertificado(Janela):
    """Atualiza o PFX e grava a senha protegida pelo Windows (DPAPI)."""

    def __init__(self, master: tk.Tk, app: "AppGNRE"):
        super().__init__(master)
        self.app = app
        self.title("Atualizar certificado digital")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.var_pfx = tk.StringVar(
            value=str(app.config.caminho_pfx) if app.config else ""
        )
        self.var_senha = tk.StringVar()
        self.var_mostrar = tk.BooleanVar(value=False)

        self.configure(bg=CARTAO)
        Quadro(self, bg=ACENTO, height=3).pack(fill="x", side="top")
        moldura = ttk.Frame(self, padding=18)
        moldura.pack(fill="both", expand=True)

        ttk.Label(
            moldura,
            text="Atualizar certificado digital",
            style="Secao.TLabel",
            font=(app.fonte, 13, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            moldura,
            text="Selecione o novo e-CNPJ (A1) e informe a senha.",
            style="Legenda.TLabel",
        ).pack(anchor="w", pady=(2, 12))

        ttk.Label(moldura, text="ARQUIVO .PFX / .P12", style="Legenda.TLabel").pack(
            anchor="w"
        )
        frame_pfx = ttk.Frame(moldura)
        frame_pfx.pack(fill="x", pady=(3, 0))
        ttk.Entry(
            frame_pfx, textvariable=self.var_pfx, width=62, style="Campo.TEntry"
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttkb.Button(
            frame_pfx,
            text="Procurar",
            bootstyle="secondary",
            command=self._escolher,
        ).pack(side="left")

        ttk.Label(moldura, text="SENHA DO CERTIFICADO", style="Legenda.TLabel").pack(
            anchor="w", pady=(12, 0)
        )
        frame_senha = ttk.Frame(moldura)
        frame_senha.pack(fill="x", pady=(3, 0))
        self.entrada_senha = ttk.Entry(
            frame_senha,
            textvariable=self.var_senha,
            show="*",
            width=32,
            style="Campo.TEntry",
        )
        self.entrada_senha.pack(side="left")
        ttkb.Checkbutton(
            frame_senha,
            text="Mostrar",
            variable=self.var_mostrar,
            bootstyle="primary",
            command=self._alternar_senha,
        ).pack(side="left", padx=(10, 0))

        ttk.Label(
            moldura,
            text="A senha fica protegida pelo Windows (DPAPI), nunca em texto puro.",
            style="Legenda.TLabel",
        ).pack(anchor="w", pady=(10, 0))

        botoes = ttk.Frame(moldura)
        botoes.pack(fill="x", pady=(16, 0))
        ttkb.Button(
            botoes,
            text="Cancelar",
            bootstyle="secondary",
            command=self.destroy,
        ).pack(side="right")
        ttk.Button(
            botoes,
            text="Validar e salvar",
            style="Acao.TButton",
            command=self._salvar,
        ).pack(side="right", padx=(0, 8))

        self.bind("<Return>", lambda _evento: self._salvar())
        self.bind("<Escape>", lambda _evento: self.destroy())
        self.entrada_senha.focus_set()

        self.update_idletasks()
        largura = self.winfo_width()
        altura = self.winfo_height()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - largura) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - altura) // 3)
        self.geometry(f"+{x}+{y}")

    def _alternar_senha(self) -> None:
        self.entrada_senha.configure(show="" if self.var_mostrar.get() else "*")

    def _escolher(self) -> None:
        arquivo = filedialog.askopenfilename(
            parent=self,
            title="Selecionar certificado e-CNPJ",
            filetypes=[
                ("Certificado digital", "*.pfx *.p12"),
                ("Todos os arquivos", "*.*"),
            ],
        )
        if arquivo:
            self.var_pfx.set(arquivo)

    def _salvar(self) -> None:
        caminho = self.var_pfx.get().strip().strip('"')
        senha = self.var_senha.get()
        if not caminho or not Path(caminho).exists():
            messagebox.showwarning(
                "Aviso", "Selecione um arquivo .pfx/.p12 válido.", parent=self
            )
            return
        if not senha:
            messagebox.showwarning(
                "Aviso", "Informe a senha do certificado.", parent=self
            )
            self.entrada_senha.focus_set()
            return

        try:
            cert = carregar_certificado(caminho, senha)
        except Exception as erro:
            messagebox.showerror("Certificado inválido", str(erro), parent=self)
            return

        origem = Path(caminho).resolve()
        destino = (DADOS_DIR / origem.name).resolve()
        if origem != destino:
            try:
                shutil.copy2(origem, destino)
                caminho = str(destino)
            except OSError as erro:
                messagebox.showerror(
                    "Erro ao copiar",
                    f"Não foi possível guardar o certificado em {DADOS_DIR}: {erro}",
                    parent=self,
                )
                return

        try:
            salvar_certificado_config(CONFIG_PATH, caminho, senha)
        except Exception as erro:
            messagebox.showerror("Erro ao salvar", str(erro), parent=self)
            return

        self.destroy()
        self.app._carregar_contexto()
        self.app._log(
            f"Certificado atualizado: {cert.razao_social} | CNPJ {cert.cnpj} | "
            f"válido até {cert.validade:%d/%m/%Y}"
        )
        detalhe = (
            "Certificado salvo com sucesso.\n\n"
            f"{cert.razao_social}\nCNPJ {cert.cnpj}\n"
            f"Válido até {cert.validade:%d/%m/%Y}"
        )
        if cert.avisos:
            detalhe += "\n\nATENÇÃO:\n" + "\n".join(cert.avisos)
        messagebox.showinfo("Certificado atualizado", detalhe, parent=self.app.root)


def main() -> None:
    # Sem isso o Windows estica a janela em telas com escala > 100% e o texto fica borrado.
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    # Identidade própria para a barra de tarefas usar o ícone do app,
    # e não o logo do pythonw.exe.
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Eskimo.EmissorGNRE.2"
        )
    except Exception:
        pass
    try:
        _preparar_dados()
    except OSError as erro:
        from tkinter import messagebox

        messagebox.showerror(
            "Pasta de dados inacessível",
            "Não foi possível criar nem gravar na pasta de configuração "
            f"do programa.\n\nPasta: {DADOS_DIR}\n\n{erro}",
        )
        return
    root = tk.Tk()
    AppGNRE(root)
    root.mainloop()


if __name__ == "__main__":
    main()
