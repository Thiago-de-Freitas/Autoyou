import os
import json
import time
import re
import random
import socket
import subprocess
import threading
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from contextlib import contextmanager

# Usa o repositório de certificados do Windows (evita CERTIFICATE_VERIFY_FAILED).
try:
    from pip_system_certs.wrapt_requests import inject_truststore
    inject_truststore()
except ImportError:
    pass

# =================================================================
# FURA-BLOQUEIO DO PYTHON PARA ENXERGAR O NODE.JS
# =================================================================
try:
    caminho_node = subprocess.check_output("where node", shell=True, text=True).strip().split('\n')[0]
    os.environ["PATH"] = os.path.dirname(caminho_node) + os.pathsep + os.environ["PATH"]
except:
    pass

from yt_dlp import YoutubeDL
from yt_dlp.utils import download_range_func
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CONFIGURAÇÕES GERAIS E CHAVES
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
_modelos_env = os.getenv("GEMINI_MODELOS", "").strip()
# Ordem: maior cota diaria (free tier) primeiro. Evite gemini-3.5-flash (20 RPD, esgota rapido).
GEMINI_MODELOS = (
    [m.strip() for m in _modelos_env.split(",") if m.strip()]
    if _modelos_env
    else [
        "gemini-3.1-flash-lite-preview",
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ]
)
GEMINI_TENTATIVAS_POR_MODELO = 3
SCOPES = ['https://www.googleapis.com/auth/youtube'] 
ARQUIVO_SECRETS = 'client_secrets.json'
ARQUIVO_TOKEN = 'token.json'
ARQUIVO_HISTORICO = 'historico_cortes.json'
ARQUIVO_PIPELINE_STATE = 'pipeline_state.json'
ARQUIVO_PIPELINE_DECISAO = 'pipeline_decisao.json'
# yt-dlp espera arquivo de cookies no formato Netscape:
# http://curl.haxx.se/rfc/cookie_spec.html
ARQUIVO_COOKIES_YT = 'cookies.txt'
ARQUIVO_COOKIES_YT_FALLBACK = 'cookies_estaticos.txt'
ARQUIVO_COOKIES_TIKTOK = 'tiktok_cookies.txt'
# Limite de gravacao ao baixar live ativa (evita download infinito)
LIVE_GRAVACAO_MAX_MINUTOS = int(os.getenv("LIVE_GRAVACAO_MAX_MINUTOS", "90"))
# Duracao minima do corte longo (YouTube) e margem entre cortes ja publicados
DURACAO_MIN_CORTE_LONGO = 240.0
MARGEM_HISTORICO_SEG = 30.0
MAX_CORTES_POR_VIDEO = int(os.getenv("MAX_CORTES_POR_VIDEO", "3"))
MAX_TENTATIVAS_NOVO_VIDEO = 5
# Crop 9:16 sem letterbox: 0.5=centro, <0.5=puxa esquerda (MBL costuma ter apresentador a esquerda)
FOCO_VERTICAL_X_FRAC = float(os.getenv("FOCO_VERTICAL_X", "0.42"))
VERTICAL_LARGURA = 1080
VERTICAL_ALTURA = 1920
INTRO_DURACAO_SEG = float(os.getenv("INTRO_DURACAO_SEG", "3"))
THUMB_HORIZONTAL = "thumb_horizontal.jpg"
THUMB_VERTICAL = "thumb_vertical.jpg"
ARQUIVO_CACHE_ASSUNTOS = "cache_assuntos_politica.json"
ASSUNTOS_CACHE_HORAS = int(os.getenv("ASSUNTOS_CACHE_HORAS", "6"))
ASSUNTOS_DIAS_BUSCA = int(os.getenv("ASSUNTOS_DIAS_BUSCA", "7"))
RSS_FEEDS_POLITICA = [
    ("G1 Politica", "https://g1.globo.com/rss/g1/politica"),
    ("G1 Economia", "https://g1.globo.com/rss/g1/economia"),
    ("InfoMoney", "https://www.infomoney.com.br/feed/"),
]
ASSUNTOS_POLITICA_FALLBACK = [
    "suspensao Renan Santos Toffoli TSE",
    "eleicoes 2026 Partido Missao",
    "MBL Movimento Brasil Livre",
    "politica brasileira", "congresso nacional", "stf",
]

# Pesquisa web (set/2026): assuntos em alta — suspensao de Renan Santos (Toffoli/TSE),
# eleicoes 2026, Partido Missao homologado pelo TSE (nov/2025).
# Canais MBL com evidencia publica: @MBLiveTV (lives oficiais), @PartidoMissao (partido),
# @mblivre (org MBL), @kimkataguiri (deputado MBL). @cortesdombl e canal de cortes
# terceirizado (nao fonte de live). strike_risk e HEURISTICO — nao garantia legal.
YOUTUBE_CANAIS_MBL_DEFAULT = [
    {
        "url": "https://www.youtube.com/@MBLiveTV",
        "strike_risk": "low",
        "label": "MBLiveTV",
        "motivo": "Canal oficial de lives do MBL; ecossistema incentiva clipagem (Cortes do MBL)",
    },
    {
        "url": "https://www.youtube.com/@PartidoMissao",
        "strike_risk": "low",
        "label": "Partido Missao",
        "motivo": "Canal oficial do Partido Missao (legenda 14, MBL)",
    },
    {
        "url": "https://www.youtube.com/@mblivre",
        "strike_risk": "medium",
        "label": "MBL oficial",
        "motivo": "Canal institucional do Movimento Brasil Livre",
    },
    {
        "url": "https://www.youtube.com/@kimkataguiri",
        "strike_risk": "medium",
        "label": "Kim Kataguiri",
        "motivo": "Deputado e lider MBL; lives e cortes frequentes",
    },
]
_STRIKE_RISK_ORDEM = {"low": 0, "medium": 1, "high": 2}
_STRIKE_RISK_BONUS = {"low": 12, "medium": 6, "high": 0}
MBL_MISSAO_KEYWORDS = [
    "mbl", "missao", "missão", "renan santos", "kim kataguiri", "mamae falei",
    "mamãe falei", "movimento brasil livre", "mblivetv", "mblive", "partido missao",
    "partido missão", "arthur do val", "amanda vettorazzo",
]

# User-Agent realista para reduzir fingerprinting
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

gemini_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(
            attempts=3,
            initial_delay=2.0,
            max_delay=45.0,
            # 429 tratado manualmente (troca de modelo / espera RPM)
            http_status_codes=[408, 500, 502, 503, 504],
        ),
    ),
)

# ==========================================
# BARRA DE PROGRESSO DO PIPELINE
# ==========================================
class BarraProgresso:
    LARGURA = 30
    INTERVALO = 2.0

    def __init__(self, etapas):
        self.etapas = etapas
        self.total = len(etapas)
        self.indice = -1
        self.inicio = time.time()
        self.etapa_inicio = self.inicio
        self.etapa_nome = "Iniciando"
        self.sub_pct = 0.0
        self.detalhe = ""
        self._ultima_impressao = 0.0
        self._ultimo_pct = -1.0
        self._ultimo_detalhe = ""
        self._lock = threading.Lock()
        self._timer_ativo = False
        self._timer_thread = None
        self._timer_mensagem = ""

    @staticmethod
    def _formatar_tempo(segundos):
        segundos = max(0, int(segundos))
        h, resto = divmod(segundos, 3600)
        m, s = divmod(resto, 60)
        if h:
            return f"{h}h{m:02d}m"
        if m:
            return f"{m}m{s:02d}s"
        return f"{s}s"

    def _pct_global(self):
        if self.total == 0:
            return 100.0
        concluidas = max(0, self.indice)
        atual = self.sub_pct / 100.0 if self.indice >= 0 else 0.0
        return min(100.0, ((concluidas + atual) / self.total) * 100.0)

    def _imprimir(self, forcar=False):
        now = time.time()
        pct = self._pct_global()

        with self._lock:
            if not forcar:
                if now - self._ultima_impressao < self.INTERVALO:
                    return
                if abs(pct - self._ultimo_pct) < 0.4 and self.detalhe == self._ultimo_detalhe:
                    return

            elapsed = now - self.inicio
            etapa_elapsed = now - self.etapa_inicio
            preenchido = int(self.LARGURA * pct / 100)
            barra = "#" * preenchido + "-" * (self.LARGURA - preenchido)

            eta_txt = ""
            if pct >= 8 and self.sub_pct > 0:
                eta_seg = (elapsed / (pct / 100.0)) - elapsed
                if eta_seg > 0:
                    eta_txt = f" | ETA ~{self._formatar_tempo(eta_seg)}"

            linha = (
                f"  [{barra}] {pct:5.1f}% | {self.etapa_nome} "
                f"({self._formatar_tempo(etapa_elapsed)}){eta_txt}"
            )
            if self.detalhe:
                linha += f" | {self.detalhe}"

            print(linha, flush=True)
            self._ultima_impressao = now
            self._ultimo_pct = pct
            self._ultimo_detalhe = self.detalhe

    def iniciar_etapa(self, nome):
        self.parar_timer()
        self.indice += 1
        self.etapa_nome = nome
        self.etapa_inicio = time.time()
        self.sub_pct = 0.0
        self.detalhe = ""
        print(f"\n[{self.indice + 1}/{self.total}] {nome}...", flush=True)

    def atualizar_sub(self, pct, detalhe="", forcar=False):
        pct = min(100.0, max(0.0, pct))
        if not forcar:
            pct = max(pct, self.sub_pct)
        self.sub_pct = pct
        if detalhe:
            self.detalhe = detalhe
        self._imprimir(forcar=forcar)

    def concluir_etapa(self, detalhe=""):
        self.parar_timer()
        self.sub_pct = 100.0
        if detalhe:
            self.detalhe = detalhe
        duracao = self._formatar_tempo(time.time() - self.etapa_inicio)
        pct = self._pct_global()
        print(
            f"  -> concluido em {duracao} ({pct:.0f}% do pipeline)",
            flush=True,
        )

    def iniciar_timer(self, mensagem="aguardando..."):
        self.parar_timer()
        self._timer_ativo = True
        self._timer_mensagem = mensagem

        def _loop():
            while self._timer_ativo:
                decorrido = self._formatar_tempo(time.time() - self.etapa_inicio)
                self.detalhe = f"{self._timer_mensagem} ({decorrido})"
                self._imprimir()
                time.sleep(self.INTERVALO)

        self._timer_thread = threading.Thread(target=_loop, daemon=True)
        self._timer_thread.start()

    def parar_timer(self):
        self._timer_ativo = False
        if self._timer_thread and self._timer_thread.is_alive():
            self._timer_thread.join(timeout=0.5)
        self._timer_thread = None

    def executar_com_timer(self, mensagem, func, *args, **kwargs):
        resultado = [None]
        erro = [None]

        def _run():
            try:
                resultado[0] = func(*args, **kwargs)
            except Exception as exc:
                erro[0] = exc

        self.iniciar_timer(mensagem)
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        while thread.is_alive():
            time.sleep(0.5)
        thread.join()
        self.parar_timer()
        if erro[0]:
            raise erro[0]
        return resultado[0]

    def resumo_final(self):
        total = self._formatar_tempo(time.time() - self.inicio)
        print(f"\n[OK] Tempo total do pipeline: {total}", flush=True)

# ==========================================
# VALIDAÇÃO DE COOKIES (NETSCAPE)
# ==========================================
def arquivo_cookies_netscape_valido(path: str) -> bool:
    """
    Valida de forma leve se o arquivo parece um cookie jar Netscape,
    evitando erro do yt-dlp ao receber arquivos tipo robots.txt.
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for _ in range(30):
                line = f.readline()
                if not line:
                    break
                if 'Netscape HTTP Cookie File' in line:
                    return True
                s = line.strip()
                if not s or s.startswith('#'):
                    continue
                # Tenta inferir a estrutura: domain<TAB>TRUE|FALSE<TAB>/...
                parts = re.split(r'\s+', s)
                if len(parts) >= 7 and parts[1] in ('TRUE', 'FALSE'):
                    return True
    except Exception:
        return False
    return False

def tiktok_cookies_validos():
    if not os.path.exists(ARQUIVO_COOKIES_TIKTOK):
        return False
    if not arquivo_cookies_netscape_valido(ARQUIVO_COOKIES_TIKTOK):
        return False
    try:
        with open(ARQUIVO_COOKIES_TIKTOK, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                partes = line.split("\t")
                if len(partes) < 7:
                    continue
                dominio, nome, valor = partes[0], partes[5], partes[6].strip()
                if "tiktok.com" in dominio and nome == "sessionid" and valor:
                    return True
    except OSError:
        return False
    return False

def eh_erro_anti_bot_youtube(msg: str) -> bool:
    """
    Heurística simples pra detectar quando o YouTube bloqueia/bota desafio,
    para disparar retry com cookies do navegador.
    """
    m = msg.lower()
    chaves = [
        "not a bot",
        "confirm you\u2019re not a bot",
        "confirm you're not a bot",
        "page needs to be reloaded",
        "needs to be reloaded",
        "reloaded",
        "challenge",
        "challenge solving failed",
        "captcha",
        "sign in",
        "http error 403",
        "403: forbidden",
        "forbidden",
        "requested format is not available",
    ]
    return any(c in m for c in chaves)

def eh_erro_download_403(msg: str) -> bool:
    m = msg.lower()
    return "403" in m or "forbidden" in m

def eh_erro_cookies_youtube_invalidos(msg: str) -> bool:
    m = msg.lower()
    return any(
        chave in m
        for chave in (
            "cookies are no longer valid",
            "provided youtube account cookies",
            "cookie file",
            "sign in to confirm",
        )
    )

def _fontes_cookies_youtube():
    fontes = []
    if os.path.exists(ARQUIVO_COOKIES_YT) and arquivo_cookies_netscape_valido(ARQUIVO_COOKIES_YT):
        fontes.append(("arquivo", ARQUIVO_COOKIES_YT))
    if os.path.exists(ARQUIVO_COOKIES_YT_FALLBACK) and arquivo_cookies_netscape_valido(ARQUIVO_COOKIES_YT_FALLBACK):
        fontes.append(("arquivo", ARQUIVO_COOKIES_YT_FALLBACK))
    browser_env = os.getenv("YOUTUBE_COOKIE_BROWSER", "").strip().lower()
    if browser_env:
        fontes.append(("browser", browser_env))
    elif sys.platform == "win32":
        for navegador in ("chrome", "edge"):
            fontes.append(("browser", navegador))
    return fontes

def _rotulo_fonte_cookies(fonte):
    if not fonte:
        return "sem cookies"
    tipo, valor = fonte
    if tipo == "arquivo":
        return f"arquivo {valor}"
    return f"navegador {valor}"

def _aplicar_fonte_cookies_youtube(opts, fonte):
    opts = dict(opts)
    opts.pop("cookiefile", None)
    opts.pop("cookiesfrombrowser", None)
    if not fonte:
        return opts
    tipo, valor = fonte
    if tipo == "arquivo":
        opts["cookiefile"] = valor
    else:
        opts["cookiesfrombrowser"] = (valor,)
    return opts

def _cookiefile_youtube():
    fontes = _fontes_cookies_youtube()
    for tipo, valor in fontes:
        if tipo == "arquivo":
            return valor
    return None

def _mensagem_renovar_cookies_youtube():
    linhas = [
        "YouTube bloqueou o download (cookies expirados ou anti-bot).",
        f"1) Exporte cookies novos para '{ARQUIVO_COOKIES_YT}' (Chrome logado no YouTube).",
        "   Extensao: Get cookies.txt LOCALLY — exporte youtube.com.",
    ]
    if sys.platform == "win32":
        linhas.append(
            "2) Ou feche o Chrome e rode de novo (o script tenta cookies do navegador)."
        )
    linhas.append('3) Atualize o yt-dlp: pip install -U "yt-dlp[default]"')
    return "\n".join(linhas)

def eh_live_agendada(msg: str) -> bool:
    m = msg.lower()
    chaves = [
        "will begin in",
        "live event will begin",
        "has not started",
        "is upcoming",
        "premiere in",
        "begins in",
    ]
    return any(c in m for c in chaves)

class YtDlpLoggerSilencioso:
    """Evita poluir o terminal com erros esperados de lives agendadas."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        if not eh_live_agendada(msg):
            print(f"[!] yt-dlp: {msg}")

    def error(self, msg):
        if eh_live_agendada(msg):
            return
        print(f"[-] yt-dlp: {msg}")

@contextmanager
def _suprimir_stderr_ytdlp():
    """yt-dlp ainda escreve ERROR no stderr mesmo com logger customizado."""
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        stderr_antigo = sys.stderr
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stderr = stderr_antigo

def _ydl_opts_youtube(cookiefile=None, extract_flat=False, ignoreerrors=False, fonte_cookies=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": YtDlpLoggerSilencioso(),
        "http_headers": HTTP_HEADERS,
        "js_runtimes": {"node": {}},
        "remote_components": {"ejs:github"},
        "extractor_args": {
            "youtube": {"player_client": ["default", "-android_sdkless"]},
            "youtubetab": {"skip": ["authcheck"]},
        },
    }
    if extract_flat is not None:
        opts["extract_flat"] = extract_flat
    if fonte_cookies:
        opts = _aplicar_fonte_cookies_youtube(opts, fonte_cookies)
    elif cookiefile:
        opts["cookiefile"] = cookiefile
    if ignoreerrors:
        opts["ignoreerrors"] = True
    return opts

YTDLP_FORMATOS_DOWNLOAD = [
    (
        "bv[protocol=m3u8_native]+ba[protocol=m3u8_native]/b[protocol=m3u8_native]/18/best",
        "m3u8 (HLS)",
    ),
    ("bv*+ba/best", "dash"),
]

def _info_eh_live_ativa(info):
    if not info or not info.get("id"):
        return False
    if info.get("live_status") == "is_upcoming":
        return False
    if info.get("live_status") == "is_live" or info.get("is_live"):
        return True
    return False

def _info_eh_replay_recente(info):
    if not info or not info.get("id"):
        return False
    status = info.get("live_status")
    if status in ("is_live", "is_upcoming"):
        return False
    if status in ("was_live", "post_live") or info.get("was_live"):
        return True
    return status is None and bool(info.get("duration"))

def salvar_estado_pipeline(video_id, url):
    with open(ARQUIVO_PIPELINE_STATE, "w", encoding="utf-8") as f:
        json.dump({"video_id": video_id, "url": url}, f, indent=2)

def carregar_estado_pipeline():
    if not os.path.exists(ARQUIVO_PIPELINE_STATE):
        return {}
    with open(ARQUIVO_PIPELINE_STATE, encoding="utf-8") as f:
        return json.load(f)

def _extrair_info_video_youtube(fonte_cookies, video_id):
    try:
        with _suprimir_stderr_ytdlp():
            with YoutubeDL(_ydl_opts_youtube(extract_flat=False, ignoreerrors=True, fonte_cookies=fonte_cookies)) as ydl:
                return ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}",
                    download=False,
                )
    except Exception as e:
        if not eh_live_agendada(str(e)):
            print(f"[-] Erro ao validar {video_id}: {e}")
        return None

# ==========================================
# SISTEMA DE HISTÓRICO
# ==========================================
class VideoEsgotadoError(Exception):
    """Nao ha mais trechos de 4+ min disponiveis neste video."""

def extrair_id_youtube(url):
    match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", url)
    return match.group(1) if match else "ID_DESCONHECIDO"

def carregar_dados_historico():
    if not os.path.exists(ARQUIVO_HISTORICO):
        return {}
    with open(ARQUIVO_HISTORICO, encoding="utf-8") as f:
        return json.load(f)

def salvar_dados_historico(dados):
    with open(ARQUIVO_HISTORICO, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4, ensure_ascii=False)

def carregar_historico_video(video_id):
    dados = carregar_dados_historico()
    intervalos = dados.get(video_id, [])
    if not isinstance(intervalos, list):
        return []
    return [iv for iv in intervalos if isinstance(iv, dict) and "inicio" in iv and "fim" in iv]

def carregar_videos_esgotados():
    dados = carregar_dados_historico()
    return set(dados.get("_esgotados", []))

def marcar_video_esgotado(video_id):
    dados = carregar_dados_historico()
    esgotados = set(dados.get("_esgotados", []))
    esgotados.add(video_id)
    dados["_esgotados"] = sorted(esgotados)
    salvar_dados_historico(dados)
    print(f"[*] Video {video_id} marcado como esgotado (sem mais cortes).", flush=True)

def salvar_historico(video_id, inicio, fim):
    dados = carregar_dados_historico()
    if video_id not in dados or not isinstance(dados[video_id], list):
        dados[video_id] = []
    dados[video_id].append({"inicio": float(inicio), "fim": float(fim)})
    salvar_dados_historico(dados)

def mesclar_intervalos(intervalos):
    if not intervalos:
        return []
    ordenados = sorted(intervalos, key=lambda x: float(x["inicio"]))
    mesclado = [{"inicio": float(ordenados[0]["inicio"]), "fim": float(ordenados[0]["fim"])}]
    for iv in ordenados[1:]:
        ini, fim = float(iv["inicio"]), float(iv["fim"])
        ult = mesclado[-1]
        if ini <= ult["fim"]:
            ult["fim"] = max(ult["fim"], fim)
        else:
            mesclado.append({"inicio": ini, "fim": fim})
    return mesclado

def calcular_lacunas_disponiveis(duracao_video, historico, min_duracao=DURACAO_MIN_CORTE_LONGO, margem=MARGEM_HISTORICO_SEG):
    bloqueados = mesclar_intervalos(historico)
    lacunas = []
    cursor = 0.0
    for bl in bloqueados:
        ini_b = max(0.0, bl["inicio"] - margem)
        if ini_b - cursor >= min_duracao:
            lacunas.append({"inicio": round(cursor, 1), "fim": round(ini_b, 1)})
        cursor = max(cursor, bl["fim"] + margem)
    if duracao_video - cursor >= min_duracao:
        lacunas.append({"inicio": round(cursor, 1), "fim": round(duracao_video, 1)})
    return lacunas

def video_esgotado(duracao_video, historico, min_duracao=DURACAO_MIN_CORTE_LONGO):
    return len(calcular_lacunas_disponiveis(duracao_video, historico, min_duracao)) == 0

def contar_cortes_video(historico):
    return len(historico or [])

def video_atingiu_limite_cortes(historico, limite=MAX_CORTES_POR_VIDEO):
    return contar_cortes_video(historico) >= limite

def motivo_trocar_video(duracao_video, historico):
    if video_atingiu_limite_cortes(historico):
        n = contar_cortes_video(historico)
        return f"limite de {MAX_CORTES_POR_VIDEO} cortes atingido ({n} feitos)"
    if video_esgotado(duracao_video, historico):
        return f"sem lacunas de 4+ min apos {contar_cortes_video(historico)} corte(s)"
    return None

def corte_conflita_historico(inicio, fim, historico, margem=MARGEM_HISTORICO_SEG):
    ini, fim = float(inicio), float(fim)
    for bl in historico:
        if ini < float(bl["fim"]) + margem and fim > float(bl["inicio"]) - margem:
            return True
    return False

def corte_dentro_de_lacuna(inicio, fim, lacunas):
    ini, fim = float(inicio), float(fim)
    for lac in lacunas:
        if ini >= float(lac["inicio"]) - 1 and fim <= float(lac["fim"]) + 1:
            return True
    return False

def liberar_video_atual(arquivo="video_original.mp4"):
    for nome in (arquivo, "temp_audio.mp3"):
        if os.path.exists(nome):
            os.remove(nome)

def baixar_proximo_video_disponivel(progresso, ignorar, arquivo_original, youtube_service=None):
    for tentativa in range(1, MAX_TENTATIVAS_NOVO_VIDEO + 1):
        progresso.iniciar_etapa("Buscar live")
        url_alvo = obter_live_recente_canais(progresso=progresso, video_ids_ignorar=ignorar)
        progresso.concluir_etapa()
        video_id = extrair_id_youtube(url_alvo)

        if youtube_service is None:
            progresso.iniciar_etapa("Autenticar YouTube")
            youtube_service = progresso.executar_com_timer(
                "autenticando...",
                autenticar_youtube,
            )
            progresso.concluir_etapa()

        progresso.iniciar_etapa("Download do vídeo")
        baixar_video(url_alvo, arquivo_original, progresso=progresso)
        salvar_estado_pipeline(video_id, url_alvo)
        progresso.concluir_etapa()

        historico = carregar_historico_video(video_id)
        motivo = motivo_trocar_video(ffprobe_duracao(arquivo_original), historico)
        if not motivo:
            return video_id, youtube_service

        print(
            f"[!] Video {video_id} indisponivel ({motivo}). "
            f"Tentando outro ({tentativa}/{MAX_TENTATIVAS_NOVO_VIDEO})...",
            flush=True,
        )
        marcar_video_esgotado(video_id)
        ignorar.add(video_id)
        liberar_video_atual(arquivo_original)
    return None, youtube_service

def garantir_video_disponivel(progresso, arquivo_original, ignorar, youtube_service=None):
    if os.path.exists(arquivo_original):
        estado = carregar_estado_pipeline()
        video_id = estado.get("video_id", "ID_EXISTENTE")
        historico = carregar_historico_video(video_id)
        motivo = motivo_trocar_video(ffprobe_duracao(arquivo_original), historico)
        if not motivo:
            return video_id, youtube_service
        print(
            f"[*] Video {video_id} encerrado ({motivo}). Buscando proxima live...",
            flush=True,
        )
        marcar_video_esgotado(video_id)
        liberar_video_atual(arquivo_original)

    video_id, youtube_service = baixar_proximo_video_disponivel(
        progresso, ignorar, arquivo_original, youtube_service=youtube_service,
    )
    if not video_id:
        raise RuntimeError(
            "Nao encontrei video disponivel para cortar. "
            "Aguarde nova live ou limpe _esgotados em historico_cortes.json."
        )
    return video_id, youtube_service

def preparar_proxima_live(progresso, arquivo_original, ignorar, youtube_service=None):
    print(
        f"[*] Baixando proxima live (video atual atingiu {MAX_CORTES_POR_VIDEO} cortes)...",
        flush=True,
    )
    try:
        video_id, _ = baixar_proximo_video_disponivel(
            progresso, ignorar, arquivo_original, youtube_service=youtube_service,
        )
        if video_id:
            print(f"[+] Proxima live pronta: {video_id} ({arquivo_original})", flush=True)
        return video_id
    except Exception as exc:
        print(f"[!] Nao foi possivel baixar proxima live agora: {exc}", flush=True)
        return None

def formatar_lacunas_prompt(lacunas):
    if not lacunas:
        return "NENHUMA (video esgotado)"
    linhas = []
    for i, lac in enumerate(lacunas, 1):
        dur = lac["fim"] - lac["inicio"]
        linhas.append(f"  {i}. {lac['inicio']:.1f}s - {lac['fim']:.1f}s ({dur:.0f}s disponiveis)")
    return "\n".join(linhas)

def formatar_historico_prompt(historico):
    if not historico:
        return "Nenhum corte anterior neste video."
    linhas = []
    for i, bl in enumerate(historico, 1):
        linhas.append(f"  {i}. {bl['inicio']:.1f}s - {bl['fim']:.1f}s")
    return "\n".join(linhas)

def _normalizar_texto_score(texto):
    texto = str(texto or "").lower()
    for orig, dest in (
        ("á", "a"), ("à", "a"), ("â", "a"), ("ã", "a"),
        ("é", "e"), ("ê", "e"), ("í", "i"), ("ó", "o"),
        ("ô", "o"), ("õ", "o"), ("ú", "u"), ("ç", "c"),
    ):
        texto = texto.replace(orig, dest)
    return texto

def calcular_score_relevancia_video(titulo, assuntos_em_alta=None):
    titulo_norm = _normalizar_texto_score(titulo)
    if not titulo_norm:
        return 0
    score = 0
    for kw in MBL_MISSAO_KEYWORDS:
        if _normalizar_texto_score(kw) in titulo_norm:
            score += 3
    for indice, assunto in enumerate(assuntos_em_alta or []):
        assunto_norm = _normalizar_texto_score(assunto)
        peso = max(1, 6 - indice)
        if assunto_norm and assunto_norm in titulo_norm:
            score += peso * 3
        for palavra in assunto_norm.split():
            if len(palavra) >= 4 and palavra in titulo_norm:
                score += peso
    return score

def _carregar_youtube_canais_mbl():
    raw = os.getenv("YOUTUBE_CANAIS_MBL", "").strip()
    if not raw:
        return list(YOUTUBE_CANAIS_MBL_DEFAULT)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and parsed:
            canais = []
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    canais.append({"url": item.strip(), "strike_risk": "medium", "label": item.strip()})
                elif isinstance(item, dict) and item.get("url"):
                    canais.append({
                        "url": str(item["url"]).strip(),
                        "strike_risk": str(item.get("strike_risk", "medium")).lower(),
                        "label": str(item.get("label") or item["url"]).strip(),
                    })
            if canais:
                return canais
    except json.JSONDecodeError:
        pass
    canais = []
    for parte in raw.split(","):
        url = parte.strip()
        if url:
            canais.append({"url": url, "strike_risk": "medium", "label": url})
    return canais or list(YOUTUBE_CANAIS_MBL_DEFAULT)

def _ordenar_canais_mbl(canais):
    return sorted(
        canais,
        key=lambda c: (
            _STRIKE_RISK_ORDEM.get(str(c.get("strike_risk", "medium")).lower(), 1),
            str(c.get("label", c.get("url", ""))).lower(),
        ),
    )

def _score_candidato_video(info, canal_cfg, assuntos_em_alta):
    titulo = info.get("title", "")
    relevancia = calcular_score_relevancia_video(titulo, assuntos_em_alta)
    bonus_risco = _STRIKE_RISK_BONUS.get(str(canal_cfg.get("strike_risk", "medium")).lower(), 0)
    return relevancia + bonus_risco

def _varrer_canal_youtube(canal_url, fonte_atual, video_ids_ignorar, max_validacoes=8):
    live_info = None
    replays = []
    video_ids_ignorar = set(video_ids_ignorar or [])

    try:
        with _suprimir_stderr_ytdlp():
            with YoutubeDL(_ydl_opts_youtube(extract_flat=False, fonte_cookies=fonte_atual)) as ydl:
                info = ydl.extract_info(f"{canal_url}/live", download=False)
                if _info_eh_live_ativa(info):
                    live_info = info
    except Exception as e:
        msg = str(e)
        if eh_erro_cookies_youtube_invalidos(msg):
            raise
        if not eh_live_agendada(msg):
            print(f"[-] Aviso ao consultar /live ({canal_url}): {e}")

    if live_info:
        return live_info, replays

    try:
        opts_streams = _ydl_opts_youtube(extract_flat=True, fonte_cookies=fonte_atual)
        opts_streams["playlistend"] = 15
        with _suprimir_stderr_ytdlp():
            with YoutubeDL(opts_streams) as ydl:
                info = ydl.extract_info(f"{canal_url}/streams", download=False)
        validados = 0
        for entry in info.get("entries") or []:
            if validados >= max_validacoes:
                break
            video_id = entry.get("id")
            if not video_id or entry.get("live_status") == "is_upcoming":
                continue
            validados += 1
            det = _extrair_info_video_youtube(fonte_atual, video_id)
            if not det:
                continue
            if _info_eh_live_ativa(det):
                live_info = det
                break
            if _info_eh_replay_recente(det) and video_id not in video_ids_ignorar:
                replays.append(det)
    except Exception as e:
        print(f"[-] Erro ao varrer canal {canal_url}: {e}")

    return live_info, replays

# ==========================================
# FUNÇÕES DE CAPTAÇÃO E DOWNLOAD
# ==========================================
def obter_live_recente_canais(progresso=None, video_ids_ignorar=None, assuntos_em_alta=None):
    video_ids_ignorar = set(video_ids_ignorar or [])
    canais = _ordenar_canais_mbl(_carregar_youtube_canais_mbl())

    if assuntos_em_alta is None:
        assuntos_em_alta, _ = obter_assuntos_politica_em_alta(progresso=progresso)

    if progresso:
        progresso.atualizar_sub(5, f"{len(canais)} canais MBL/Missao")
    else:
        print(f"[*] Varrendo {len(canais)} canais MBL/Missao (prioridade strike_risk=low)...")

    cookiefile = _cookiefile_youtube()
    fonte_atual = (_fontes_cookies_youtube() or [None])[0]

    def retornar_video(info, rotulo="Live ativa", canal_cfg=None):
        titulo = info.get("title", "")
        url_final = f"https://www.youtube.com/watch?v={info['id']}"
        canal_txt = f" ({canal_cfg['label']})" if canal_cfg else ""
        score = _score_candidato_video(info, canal_cfg or {}, assuntos_em_alta)
        if progresso:
            progresso.atualizar_sub(100, f"'{titulo}'{canal_txt}")
        else:
            print(f"[+] {rotulo} encontrada{canal_txt} [score={score}]: '{titulo}'")
        return url_final

    melhor_live = None
    melhor_live_score = -1
    melhor_live_cfg = None
    melhor_replay = None
    melhor_replay_score = -1
    melhor_replay_cfg = None

    for indice, canal_cfg in enumerate(canais):
        canal_url = canal_cfg["url"]
        pct = 10 + int((indice / max(len(canais), 1)) * 70)
        if progresso:
            progresso.atualizar_sub(pct, f"canal: {canal_cfg.get('label', canal_url)}")
        else:
            print(f"[*] Canal [{canal_cfg.get('strike_risk', 'medium')}]: {canal_url}")

        try:
            live_info, replays = _varrer_canal_youtube(
                canal_url, fonte_atual, video_ids_ignorar,
            )
        except Exception as e:
            if eh_erro_cookies_youtube_invalidos(str(e)):
                print(f"[!] Cookies do YouTube expirados em '{cookiefile or 'navegador'}'. Renove cookies.txt.", flush=True)
            continue

        if live_info:
            score = _score_candidato_video(live_info, canal_cfg, assuntos_em_alta)
            if score > melhor_live_score:
                melhor_live = live_info
                melhor_live_score = score
                melhor_live_cfg = canal_cfg

        for replay in replays:
            score = _score_candidato_video(replay, canal_cfg, assuntos_em_alta)
            if score > melhor_replay_score:
                melhor_replay = replay
                melhor_replay_score = score
                melhor_replay_cfg = canal_cfg

    if melhor_live:
        return retornar_video(melhor_live, canal_cfg=melhor_live_cfg)

    if melhor_replay:
        if progresso:
            progresso.atualizar_sub(85, "replay recente (sem live no ar)")
        else:
            print("[*] Nenhuma live no ar. Usando replay com maior relevancia...")
        return retornar_video(melhor_replay, rotulo="Replay recente", canal_cfg=melhor_replay_cfg)

    raise ValueError(
        "Nenhuma live ativa nem gravacao recente encontrada nos canais configurados. "
        "Verifique YOUTUBE_CANAIS_MBL ou aguarde nova transmissao."
    )

def obter_live_recente_mbl(progresso=None, video_ids_ignorar=None):
    return obter_live_recente_canais(progresso=progresso, video_ids_ignorar=video_ids_ignorar)

def _arquivos_parciais_download(destino):
    pasta = os.path.dirname(os.path.abspath(destino)) or "."
    base = os.path.basename(destino)
    raiz, _ = os.path.splitext(base)
    encontrados = []
    if os.path.isfile(destino):
        encontrados.append(destino)
    try:
        for nome in os.listdir(pasta):
            if nome == base or nome.startswith(raiz) or nome.endswith(".part"):
                caminho = os.path.join(pasta, nome)
                if os.path.isfile(caminho):
                    encontrados.append(caminho)
    except OSError:
        pass
    return encontrados

def _maior_tamanho_parcial(destino):
    tamanhos = [os.path.getsize(p) for p in _arquivos_parciais_download(destino)]
    return max(tamanhos) if tamanhos else 0

def _iniciar_monitor_download(destino, progresso, timer_parado, eh_live, inicio_dl, max_segundos_live):
    parar = threading.Event()

    def _loop():
        ultimo_mb = 0.0
        while not parar.wait(2.0):
            bytes_total = _maior_tamanho_parcial(destino)
            if bytes_total <= 0:
                continue
            if not timer_parado[0]:
                progresso.parar_timer()
                timer_parado[0] = True
            mb = bytes_total / (1024 * 1024)
            if eh_live:
                decorrido = time.time() - inicio_dl
                pct = min(99.0, (decorrido / max_segundos_live) * 100)
                detalhe = f"live {int(decorrido)}s/{max_segundos_live}s | {mb:.0f} MB"
            else:
                pct = min(95.0, mb / 8.0)
                delta = mb - ultimo_mb
                velocidade = f" | +{delta:.0f} MB/2s" if delta > 0.01 else ""
                detalhe = f"{mb:.0f} MB baixados{velocidade}"
            ultimo_mb = mb
            progresso.atualizar_sub(pct, detalhe)

    threading.Thread(target=_loop, daemon=True).start()
    return parar

def baixar_video(url_video, nome_arquivo_saida, progresso=None):
    if not progresso:
        print(f"[*] Iniciando download do vídeo original...")

    fontes = _fontes_cookies_youtube() or [None]
    inicio_dl = time.time()
    max_segundos_live = LIVE_GRAVACAO_MAX_MINUTOS * 60
    eh_live = False

    for indice_fonte, fonte in enumerate(fontes):
        if indice_fonte > 0:
            print(f"[!] Tentando cookies via {_rotulo_fonte_cookies(fonte)}...", flush=True)
        try:
            with YoutubeDL(_ydl_opts_youtube(extract_flat=False, fonte_cookies=fonte)) as ydl:
                info = ydl.extract_info(url_video, download=False)
            eh_live = _info_eh_live_ativa(info)
            break
        except Exception as exc:
            msg = str(exc)
            if indice_fonte < len(fontes) - 1 and (
                eh_erro_cookies_youtube_invalidos(msg)
                or eh_erro_download_403(msg)
                or eh_erro_anti_bot_youtube(msg)
            ):
                print(f"[!] Falha com {_rotulo_fonte_cookies(fonte)}: {exc}", flush=True)
                continue
            raise

    if eh_live:
        msg_live = f"gravando live (max {LIVE_GRAVACAO_MAX_MINUTOS} min)..."
        if progresso:
            progresso.atualizar_sub(2, msg_live, forcar=True)
        else:
            print(f"[*] {msg_live}")

    def hook_download(d):
        if not progresso:
            return
        status = d.get("status")
        if status == "downloading":
            if not timer_parado[0]:
                progresso.parar_timer()
                timer_parado[0] = True
            baixado = d.get("downloaded_bytes", 0) or 0
            mb = baixado / (1024 * 1024)
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                pct = (baixado / total) * 100
                detalhe = f"{d.get('_percent_str', '').strip()} {d.get('_speed_str', '').strip()}".strip()
            elif eh_live:
                decorrido = time.time() - inicio_dl
                pct = min(99.0, (decorrido / max_segundos_live) * 100)
                detalhe = f"live {int(decorrido)}s/{max_segundos_live}s | {mb:.0f} MB"
            else:
                pct = min(95.0, mb / 5.0)
                detalhe = f"{mb:.0f} MB baixados"
            progresso.atualizar_sub(pct, detalhe)
        elif status in ("preparing", "processing"):
            if not timer_parado[0]:
                progresso.parar_timer()
                timer_parado[0] = True
            progresso.atualizar_sub(max(progresso.sub_pct, 3), "preparando download...")
        elif status == "finished":
            if not timer_parado[0]:
                progresso.parar_timer()
                timer_parado[0] = True
            progresso.atualizar_sub(99, "mesclando audio/video...")

    timer_parado = [False]
    monitor_parar = None

    base_opts = {
        "outtmpl": nome_arquivo_saida,
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "http_headers": HTTP_HEADERS,
        "js_runtimes": {"node": {}},
        "remote_components": {"ejs:github"},
        "extractor_args": {
            "youtube": {"player_client": ["default", "-android_sdkless"]},
            "youtubetab": {"skip": ["authcheck"]},
        },
        "progress_hooks": [hook_download],
    }
    if eh_live:
        base_opts["download_ranges"] = download_range_func(None, [(0, max_segundos_live)])
        base_opts["force_keyframes_at_cuts"] = True

    if progresso:
        progresso.iniciar_timer("conectando ao YouTube...")
        monitor_parar = _iniciar_monitor_download(
            nome_arquivo_saida, progresso, timer_parado, eh_live, inicio_dl, max_segundos_live,
        )

        def _timeout_conectando():
            time.sleep(12)
            if not timer_parado[0]:
                progresso.parar_timer()
                timer_parado[0] = True
                progresso.atualizar_sub(max(progresso.sub_pct, 1), "baixando video...", forcar=True)

        threading.Thread(target=_timeout_conectando, daemon=True).start()

    ultimo_erro = None
    download_ok = False
    try:
        for indice_fonte, fonte in enumerate(fontes):
            if indice_fonte > 0:
                print(f"[!] Retry download com {_rotulo_fonte_cookies(fonte)}...", flush=True)
            for indice_fmt, (formato, rotulo_fmt) in enumerate(YTDLP_FORMATOS_DOWNLOAD):
                ydl_opts = _aplicar_fonte_cookies_youtube({**base_opts, "format": formato}, fonte)
                if indice_fmt > 0 or indice_fonte > 0:
                    print(f"[!] Tentando download via {rotulo_fmt}...", flush=True)
                    if progresso:
                        progresso.atualizar_sub(max(progresso.sub_pct, 2), f"retry {rotulo_fmt}...")
                try:
                    with YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url_video])
                    download_ok = True
                    break
                except Exception as e:
                    ultimo_erro = e
                    msg = str(e)
                    if eh_erro_download_403(msg) and indice_fmt < len(YTDLP_FORMATOS_DOWNLOAD) - 1:
                        print(f"[!] YouTube bloqueou formato {rotulo_fmt} (403).", flush=True)
                        continue
                    print(f"[-] Erro ao baixar video ({_rotulo_fonte_cookies(fonte)} / {rotulo_fmt}): {e}")
                    if eh_live_agendada(msg):
                        raise RuntimeError(
                            "A URL encontrada e uma live agendada, nao uma transmissao ativa. "
                            "Aguarde o inicio da live e execute novamente."
                        ) from e
                    if indice_fonte < len(fontes) - 1 and (
                        eh_erro_cookies_youtube_invalidos(msg)
                        or eh_erro_download_403(msg)
                        or eh_erro_anti_bot_youtube(msg)
                    ):
                        break
                    if indice_fmt >= len(YTDLP_FORMATOS_DOWNLOAD) - 1:
                        if indice_fonte >= len(fontes) - 1:
                            raise RuntimeError(_mensagem_renovar_cookies_youtube()) from e
                        break
            if download_ok:
                break
        if not download_ok and ultimo_erro:
            raise ultimo_erro
    finally:
        if monitor_parar:
            monitor_parar.set()
        if progresso:
            progresso.parar_timer()

    # Se falhou sem levantar exceção (raramente), evita pipeline continuar
    if not os.path.exists(nome_arquivo_saida) or os.path.getsize(nome_arquivo_saida) == 0:
        raise RuntimeError(f"Falha no download: arquivo '{nome_arquivo_saida}' não foi gerado.")
    return nome_arquivo_saida

# ==========================================
# EDIÇÃO DE VÍDEO (FFMPEG + NVENC)
# ==========================================
def ffprobe_valor(arquivo, query):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", query,
        "-of", "default=noprint_wrappers=1:nokey=1",
        arquivo,
    ]
    return subprocess.check_output(cmd, text=True).strip()

def _primeiro_valor_ffprobe(valor_bruto):
    for linha in valor_bruto.splitlines():
        linha = linha.strip()
        if linha:
            return linha
    return valor_bruto.strip()

def _parsear_fps_ffprobe(valor_bruto):
    for linha in valor_bruto.splitlines():
        linha = linha.strip()
        if not linha or linha in ("0/0", "0/1"):
            continue
        if "/" in linha:
            num, den = linha.split("/", 1)
            den_f = float(den)
            if den_f == 0:
                continue
            fps = float(num) / den_f
            if fps > 0:
                return round(fps, 3)
        else:
            fps = float(linha)
            if fps > 0:
                return round(fps, 3)
    return 30.0

def ffprobe_duracao(arquivo):
    return float(_primeiro_valor_ffprobe(ffprobe_valor(arquivo, "format=duration")))

def ffprobe_fps(arquivo):
    for campo in ("stream=avg_frame_rate", "stream=r_frame_rate"):
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", campo,
            "-of", "default=noprint_wrappers=1:nokey=1",
            arquivo,
        ]
        taxa = subprocess.check_output(cmd, text=True)
        fps = _parsear_fps_ffprobe(taxa)
        if fps > 0:
            return fps
    return 30.0

def ffprobe_tamanho_video(arquivo, inicio=None):
    """Retorna largura e altura do video (constantes em todo o arquivo)."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0",
        arquivo,
    ]
    campos = [
        c.strip() for c in _primeiro_valor_ffprobe(
            subprocess.check_output(cmd, text=True)
        ).split(",")
        if c.strip()
    ]
    if len(campos) < 2:
        raise ValueError(f"ffprobe nao retornou largura/altura para {arquivo}: {campos!r}")
    return int(campos[0]), int(campos[1])

def args_corte_sincronizado(arquivo, inicio, fim):
    """Seek rapido antes de -i; decode em CPU evita frames pretos no NVENC."""
    duracao = max(0.1, float(fim) - float(inicio))
    return [
        "-hwaccel", "none",
        "-ss", str(inicio),
        "-i", arquivo,
        "-t", str(duracao),
    ]

def ffmpeg_tem_nvenc():
    if os.getenv("FORCE_CPU_ENCODE", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    try:
        saida = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
        )
        return "h264_nvenc" in saida
    except Exception:
        return False

def args_encode_video(fps):
    if ffmpeg_tem_nvenc():
        return [
            "-c:v", "h264_nvenc", "-preset", "p1",
            "-fps_mode", "cfr", "-r", str(fps),
            "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        ]
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-fps_mode", "cfr", "-r", str(fps),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
    ]

def args_encode_nvenc(fps):
    return args_encode_video(fps)

def detectar_crop_conteudo(arquivo, timestamp=0.0):
    """Detecta area sem barras pretas via cropdetect (mediana de frames)."""
    cmd = [
        "ffmpeg", "-hide_banner", "-ss", f"{max(0.0, float(timestamp)):.2f}",
        "-i", arquivo,
        "-vf", "cropdetect=limit=24:round=2:reset=0",
        "-frames:v", "45", "-an", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    crops = [tuple(map(int, m)) for m in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", proc.stderr)]
    if not crops:
        return None
    return max(set(crops), key=crops.count)

def bbox_remove_letterbox(bbox, largura, altura):
    if not bbox:
        return None
    w, h, x, y = bbox
    if w >= largura * 0.97 and h >= altura * 0.97:
        return None
    return bbox

def calcular_foco_x_916(largura, altura, bbox=None):
    """Fracao horizontal (0-1) do crop final 9:16 apos scale-increase."""
    bbox_util = bbox_remove_letterbox(bbox, largura, altura)
    if bbox_util:
        ew, eh, ox, oy = bbox_util
    else:
        ew, eh, ox, oy = largura, altura, 0, 0

    crop_w = eh * 9 / 16
    if crop_w >= ew - 2:
        return 0.5

    excess = ew - crop_w
    if bbox_util:
        centro = ox + ew / 2
        ideal = max(ox, min(ox + ew - crop_w, centro - crop_w / 2))
        return (ideal - ox) / excess if excess > 0 else 0.5

    return max(0.0, min(1.0, FOCO_VERTICAL_X_FRAC))

def video_ja_e_vertical(arquivo):
    try:
        largura, altura = ffprobe_tamanho_video(arquivo)
    except (subprocess.CalledProcessError, ValueError):
        return False
    return largura == VERTICAL_LARGURA and altura == VERTICAL_ALTURA

def filtro_vf_vertical_tiktok(fade_inicio=None, fade_duracao=None, bbox=None, foco_x=0.5):
    """
    16:9 -> 9:16 letterbox para TikTok/Shorts.
    Escala o video para caber em 1080x1920 e centraliza com barras pretas
    acima/abaixo (conteudo horizontal intacto, como Shorts tipico de live).
    """
    w_out, h_out = VERTICAL_LARGURA, VERTICAL_ALTURA
    partes = [
        "setsar=1",
        f"scale={w_out}:{h_out}:force_original_aspect_ratio=decrease:flags=lanczos",
        f"pad={w_out}:{h_out}:(ow-iw)/2:(oh-ih)/2:black",
        "setsar=1",
    ]
    if fade_inicio is not None and fade_duracao is not None:
        partes.append(f"fade=t=out:st={fade_inicio}:d={fade_duracao}")
    partes.append("format=yuv420p")
    return ",".join(partes)

def ffprobe_json(arquivo):
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", arquivo]
    return json.loads(subprocess.check_output(cmd, text=True))

def validar_video_para_tiktok(arquivo, duracao_min=3.0, tamanho_min_kb=500):
    if not os.path.isfile(arquivo):
        return False, f"arquivo nao encontrado: {arquivo}"
    tamanho = os.path.getsize(arquivo)
    if tamanho < tamanho_min_kb * 1024:
        return False, (
            f"arquivo invalido ({tamanho / 1024:.0f} KB). "
            "Nao use download do YouTube Shorts — re-renderize o corte vertical."
        )
    try:
        info = ffprobe_json(arquivo)
    except subprocess.CalledProcessError as exc:
        return False, f"ffprobe falhou: {exc}"
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        return False, "sem faixa de video"
    if not audio:
        return False, "sem faixa de audio"
    duracao = float(info.get("format", {}).get("duration") or 0)
    if duracao < duracao_min:
        return False, f"duracao muito curta ({duracao:.1f}s)"
    vcodec = video.get("codec_name", "")
    acodec = audio.get("codec_name", "")
    if vcodec != "h264":
        return False, f"video em '{vcodec}' — TikTok precisa de h264"
    if acodec not in ("aac", "mp3"):
        return False, f"audio em '{acodec}' — reencode para aac necessario"
    vw = int(video.get("width") or 0)
    vh = int(video.get("height") or 0)
    if vw != VERTICAL_LARGURA or vh != VERTICAL_ALTURA:
        return False, f"resolucao {vw}x{vh} — esperado {VERTICAL_LARGURA}x{VERTICAL_ALTURA}"
    return True, ""

def reencodar_video_tiktok(arquivo_entrada, arquivo_saida=None):
    """Forca h264 + aac 9:16 para compatibilidade com TikTok."""
    destino = arquivo_saida or arquivo_entrada
    temporario = f"{destino}.tmp.mp4"
    fps = ffprobe_fps(arquivo_entrada)
    duracao = ffprobe_duracao(arquivo_entrada)
    if video_ja_e_vertical(arquivo_entrada):
        vf = "setsar=1,format=yuv420p"
    else:
        vf = filtro_vf_vertical_tiktok()
    rodar_ffmpeg(
        [
            "-i", arquivo_entrada,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", vf,
            "-af", "aresample=async=1:first_pts=0",
            *args_encode_nvenc(fps),
            temporario,
        ],
        None,
        "Reencode TikTok",
        0,
        100,
        duracao,
    )
    os.replace(temporario, destino)
    return destino

def validar_video_renderizado(arquivo, duracao_esperada, rotulo):
    if not os.path.isfile(arquivo):
        raise RuntimeError(f"{rotulo}: arquivo nao foi gerado")
    tamanho = os.path.getsize(arquivo)
    duracao = ffprobe_duracao(arquivo)
    if duracao < max(1.0, duracao_esperada * 0.85):
        raise RuntimeError(
            f"{rotulo}: duracao {duracao:.1f}s (esperado ~{duracao_esperada:.1f}s). "
            "Timestamps da IA podem estar fora do video."
        )
    kbps = (tamanho * 8) / max(duracao, 0.1) / 1000
    if kbps < 250:
        raise RuntimeError(
            f"{rotulo}: bitrate {kbps:.0f} kbps — video provavelmente preto ou corrompido."
        )
    info = ffprobe_json(arquivo)
    streams = info.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise RuntimeError(f"{rotulo}: sem faixa de video")
    if not any(s.get("codec_type") == "audio" for s in streams):
        raise RuntimeError(f"{rotulo}: sem faixa de audio")

def duracao_intro_efetiva(arquivo_intro="intro_onca.mp4"):
    if not os.path.isfile(arquivo_intro):
        return INTRO_DURACAO_SEG
    return min(ffprobe_duracao(arquivo_intro), INTRO_DURACAO_SEG)

def extrair_thumbnail_video(arquivo_video, arquivo_saida, momento_seg=3.0, largura_alvo=1280):
    if not os.path.isfile(arquivo_video):
        raise FileNotFoundError(f"Video para thumbnail nao encontrado: {arquivo_video}")
    duracao = ffprobe_duracao(arquivo_video)
    momento = max(0.5, min(float(momento_seg), max(0.5, duracao - 0.1)))
    proc = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", f"{momento:.3f}",
            "-i", arquivo_video,
            "-frames:v", "1",
            "-q:v", "2",
            "-vf", f"scale={int(largura_alvo)}:-2",
            arquivo_saida,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not os.path.isfile(arquivo_saida):
        detalhe = next(
            (ln for ln in reversed((proc.stderr or "").splitlines()) if ln.strip()),
            "erro desconhecido",
        )
        raise RuntimeError(f"Falha ao extrair thumbnail: {detalhe}")

def garantir_thumbnails_para_upload(arquivo_h, arquivo_v):
    if not os.path.isfile(THUMB_HORIZONTAL):
        momento_h = 3.0
        if os.path.isfile("intro_onca.mp4"):
            momento_h = duracao_intro_efetiva() + 3.0
        extrair_thumbnail_video(arquivo_h, THUMB_HORIZONTAL, momento_seg=momento_h)
    if not os.path.isfile(THUMB_VERTICAL):
        dur_v = ffprobe_duracao(arquivo_v)
        extrair_thumbnail_video(
            arquivo_v, THUMB_VERTICAL,
            momento_seg=max(0.5, dur_v / 2),
            largura_alvo=1080,
        )

def normalizar_intervalo_corte(corte, duracao_video, duracao_min=5.0, duracao_max=None):
    inicio = max(0.0, min(float(corte["inicio"]), max(0.0, duracao_video - 1)))
    fim = max(inicio + duracao_min, min(float(corte["fim"]), duracao_video))
    if duracao_max:
        fim = min(fim, inicio + duracao_max)
    if fim > duracao_video:
        fim = duracao_video
    if fim <= inicio:
        inicio = max(0.0, duracao_video - duracao_min)
        fim = duracao_video
    corte["inicio"] = inicio
    corte["fim"] = fim
    return corte

def preparar_video_tiktok(arquivo):
    ok, msg = validar_video_para_tiktok(arquivo)
    if ok:
        return True
    if os.path.getsize(arquivo) < 500 * 1024:
        print(f"[-] {msg}")
        return False
    if "h264" in msg or "aac" in msg or "audio em" in msg or "video em" in msg:
        print(f"[!] {msg} — reencodando...", flush=True)
        reencodar_video_tiktok(arquivo)
        ok, msg = validar_video_para_tiktok(arquivo)
    if not ok:
        print(f"[-] Video invalido para TikTok: {msg}")
        return False
    return True

def rodar_ffmpeg(args, progresso, rotulo, pct_inicio, pct_fim, duracao_seg):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1", *args]
    if progresso:
        progresso.atualizar_sub(pct_inicio, rotulo, forcar=True)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    ultimo_out_s = [0.0]
    stderr_linhas = []

    def _ler_stdout():
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    ultimo_out_s[0] = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                if progresso and duracao_seg > 0:
                    frac = min(1.0, ultimo_out_s[0] / duracao_seg)
                    sub = pct_inicio + frac * (pct_fim - pct_inicio)
                    progresso.atualizar_sub(
                        sub,
                        f"{rotulo} {int(ultimo_out_s[0])}s/{int(duracao_seg)}s",
                    )
            elif line == "progress=end" and progresso:
                progresso.atualizar_sub(pct_fim, f"{rotulo} concluido", forcar=True)

    def _ler_stderr():
        for line in proc.stderr:
            stderr_linhas.append(line.rstrip())

    thread_err = threading.Thread(target=_ler_stderr, daemon=True)
    thread_err.start()
    thread = threading.Thread(target=_ler_stdout, daemon=True)
    thread.start()
    inicio = time.time()
    while proc.poll() is None:
        if progresso and duracao_seg > 0:
            decorrido = int(time.time() - inicio)
            if decorrido >= 3:
                frac = min(1.0, ultimo_out_s[0] / duracao_seg) if ultimo_out_s[0] > 0 else 0.0
                sub = pct_inicio + frac * (pct_fim - pct_inicio)
                progresso.atualizar_sub(sub, f"{rotulo} ffmpeg ({decorrido}s)")
        time.sleep(2)

    thread.join(timeout=5)
    thread_err.join(timeout=2)
    if proc.returncode != 0:
        detalhe = next(
            (ln for ln in reversed(stderr_linhas) if ln.strip() and not ln.startswith("frame=")),
            "",
        )
        msg = f"ffmpeg falhou em '{rotulo}' (codigo {proc.returncode})"
        if detalhe:
            msg += f": {detalhe}"
        raise RuntimeError(msg)

def fazer_corte_video(arquivo_entrada, arquivo_horizontal, arquivo_vertical, t_longo, t_curto, progresso=None):
    if not progresso:
        print("[*] Iniciando producao TURBO (FFmpeg + GPU NVIDIA)...")

    temp_corte_h = "temp_corte_h.mp4"
    temp_intro_h = "temp_intro_h.mp4"
    dur_h = t_longo["fim"] - t_longo["inicio"]
    dur_v = t_curto["fim"] - t_curto["inicio"]
    fade_h = min(2.5, max(0.1, dur_h - 0.1))
    fade_h_inicio = max(0.0, dur_h - fade_h)
    fade_v = min(1.5, max(0.1, dur_v - 0.1))
    fade_v_inicio = max(0.0, dur_v - fade_v)
    fps_fonte = ffprobe_fps(arquivo_entrada)

    try:
        rodar_ffmpeg(
            [
                *args_corte_sincronizado(arquivo_entrada, t_longo["inicio"], t_longo["fim"]),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", f"fade=t=out:st={fade_h_inicio}:d={fade_h},format=yuv420p",
                "-af", f"aresample=async=1:first_pts=0,afade=t=out:st={fade_h_inicio}:d={fade_h}",
                *args_encode_nvenc(fps_fonte),
                temp_corte_h,
            ],
            progresso,
            "Corte horizontal",
            0,
            35,
            dur_h,
        )

        largura, altura = ffprobe_tamanho_video(temp_corte_h)
        dur_intro = duracao_intro_efetiva()

        rodar_ffmpeg(
            [
                "-i", "intro_onca.mp4",
                "-t", f"{dur_intro:.3f}",
                "-vf", (
                    f"scale={largura}:{altura}:force_original_aspect_ratio=decrease,"
                    f"pad={largura}:{altura}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
                ),
                "-af", "aresample=async=1:first_pts=0",
                *args_encode_nvenc(fps_fonte),
                temp_intro_h,
            ],
            progresso,
            "Intro YouTube",
            35,
            42,
            dur_intro,
        )

        filtro_concat = (
            f"[0:v]fps={fps_fonte},settb=AVTB[v0];"
            f"[1:v]fps={fps_fonte},settb=AVTB[v1];"
            f"[0:a]aresample=48000:async=1:first_pts=0[a0];"
            f"[1:a]aresample=48000:async=1:first_pts=0[a1];"
            f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
        )
        rodar_ffmpeg(
            [
                "-i", temp_intro_h,
                "-i", temp_corte_h,
                "-filter_complex", filtro_concat,
                "-map", "[v]", "-map", "[a]",
                *args_encode_nvenc(fps_fonte),
                arquivo_horizontal,
            ],
            progresso,
            "Juntando intro + corte",
            42,
            50,
            dur_intro + dur_h,
        )
        validar_video_renderizado(arquivo_horizontal, dur_intro + dur_h, "YouTube longo")

        if progresso:
            print("  -> YouTube longo pronto. Iniciando TikTok/Shorts...", flush=True)

        largura_fonte, altura_fonte = ffprobe_tamanho_video(arquivo_entrada)
        print(
            f"[*] Enquadramento vertical: letterbox 9:16 centralizado "
            f"({largura_fonte}x{altura_fonte} -> {VERTICAL_LARGURA}x{VERTICAL_ALTURA})",
            flush=True,
        )

        rodar_ffmpeg(
            [
                *args_corte_sincronizado(arquivo_entrada, t_curto["inicio"], t_curto["fim"]),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", filtro_vf_vertical_tiktok(fade_v_inicio, fade_v),
                "-af", f"aresample=async=1:first_pts=0,afade=t=out:st={fade_v_inicio}:d={fade_v}",
                *args_encode_nvenc(fps_fonte),
                arquivo_vertical,
            ],
            progresso,
            "Render TikTok/Shorts",
            50,
            100,
            dur_v,
        )
        validar_video_renderizado(arquivo_vertical, dur_v, "TikTok/Shorts")
        ok_vert, msg_vert = validar_video_para_tiktok(arquivo_vertical, duracao_min=1.0)
        if not ok_vert:
            raise RuntimeError(f"Render vertical invalido: {msg_vert}")

        extrair_thumbnail_video(
            temp_corte_h, THUMB_HORIZONTAL,
            momento_seg=min(5.0, max(1.0, dur_h * 0.12)),
        )
        extrair_thumbnail_video(
            arquivo_vertical, THUMB_VERTICAL,
            momento_seg=max(0.5, dur_v / 2),
            largura_alvo=1080,
        )
    finally:
        for temp in (temp_corte_h, temp_intro_h):
            if os.path.exists(temp):
                os.remove(temp)

# ==========================================
# ASSUNTOS DE POLITICA EM ALTA (RSS + IA)
# ==========================================
def _baixar_rss(url, timeout=15):
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def _texto_elemento_xml(el):
    if el is None:
        return ""
    partes = [el.text or ""]
    for sub in el.iter():
        if sub is not el and sub.text:
            partes.append(sub.text)
        if sub is not el and sub.tail:
            partes.append(sub.tail)
    return re.sub(r"\s+", " ", "".join(partes)).strip()

def _parse_data_rss(texto):
    if not texto:
        return None
    texto = texto.strip()
    try:
        dt = parsedate_to_datetime(texto)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(texto.replace("Z", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None

def _data_item_rss(item):
    for tag in ("pubDate", "published", "updated", "{http://www.w3.org/2005/Atom}published",
                "{http://www.w3.org/2005/Atom}updated"):
        el = item.find(tag)
        if el is not None:
            dt = _parse_data_rss(_texto_elemento_xml(el))
            if dt:
                return dt
    return None

def _titulo_item_rss(item):
    for tag in ("title", "{http://www.w3.org/2005/Atom}title"):
        el = item.find(tag)
        if el is not None:
            titulo = _texto_elemento_xml(el)
            if titulo:
                return titulo
    return ""

def _extrair_itens_rss(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    itens = root.findall(".//item")
    if not itens:
        itens = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    return itens

def buscar_titulos_noticias_politica(dias=ASSUNTOS_DIAS_BUSCA):
    limite = datetime.now(timezone.utc) - timedelta(days=dias)
    titulos = []
    vistos = set()
    for nome, url in RSS_FEEDS_POLITICA:
        try:
            xml_bytes = _baixar_rss(url)
            for item in _extrair_itens_rss(xml_bytes):
                dt = _data_item_rss(item)
                if dt and dt < limite:
                    continue
                titulo = _titulo_item_rss(item)
                if not titulo:
                    continue
                chave = titulo.lower()
                if chave in vistos:
                    continue
                vistos.add(chave)
                titulos.append(titulo)
        except Exception as exc:
            print(f"[!] RSS {nome} indisponivel: {exc}", flush=True)
    return titulos

def _carregar_cache_assuntos():
    if not os.path.exists(ARQUIVO_CACHE_ASSUNTOS):
        return None
    try:
        with open(ARQUIVO_CACHE_ASSUNTOS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _salvar_cache_assuntos(assuntos, titulos):
    dados = {
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
        "assuntos": assuntos,
        "titulos": titulos[:40],
    }
    with open(ARQUIVO_CACHE_ASSUNTOS, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)

def _cache_assuntos_valido(cache):
    try:
        atualizado = datetime.fromisoformat(cache["atualizado_em"])
        if atualizado.tzinfo is None:
            atualizado = atualizado.replace(tzinfo=timezone.utc)
        idade_h = (datetime.now(timezone.utc) - atualizado).total_seconds() / 3600
        return idade_h < ASSUNTOS_CACHE_HORAS and cache.get("assuntos")
    except Exception:
        return False

def sintetizar_assuntos_gemini(titulos, progresso=None):
    if not titulos:
        return list(ASSUNTOS_POLITICA_FALLBACK)
    bloco = "\n".join(f"- {t}" for t in titulos[:40])
    prompt = f"""
    Com base nestes titulos de noticias brasileiras dos ultimos {ASSUNTOS_DIAS_BUSCA} dias,
    liste de 8 a 12 assuntos politicos em alta (frases curtas, foco em temas recorrentes).
    Retorne APENAS JSON: {{"assuntos": ["assunto 1", "assunto 2"]}}

    Titulos:
    {bloco}
    """
    config = types.GenerateContentConfig(response_mime_type="application/json")
    res = gemini_executar_com_retry(
        lambda: gemini_client.models.generate_content(
            model=GEMINI_MODELOS[0],
            contents=prompt,
            config=config,
        ),
        progresso=progresso,
        rotulo="sintese assuntos",
    )
    dados = extrair_json_gemini(res.text)
    assuntos = [str(a).strip() for a in dados.get("assuntos", []) if str(a).strip()]
    return assuntos[:12] or list(ASSUNTOS_POLITICA_FALLBACK)

def obter_assuntos_politica_em_alta(progresso=None):
    cache = _carregar_cache_assuntos()
    if cache and _cache_assuntos_valido(cache):
        assuntos = cache.get("assuntos", [])
        titulos = cache.get("titulos", [])
        print(f"[*] Assuntos em alta (cache): {', '.join(assuntos[:5])}...", flush=True)
        return assuntos, titulos

    if progresso:
        progresso.atualizar_sub(32, "assuntos politica em alta...")
    else:
        print(f"[*] Buscando assuntos de politica em alta (ultimos {ASSUNTOS_DIAS_BUSCA} dias)...", flush=True)

    titulos = buscar_titulos_noticias_politica()
    if titulos:
        print(f"[*] {len(titulos)} manchetes coletadas de feeds RSS.", flush=True)
        try:
            assuntos = sintetizar_assuntos_gemini(titulos, progresso=progresso)
        except Exception as exc:
            print(f"[!] Sintese de assuntos falhou ({exc}). Usando manchetes direto.", flush=True)
            assuntos = titulos[:12]
    else:
        print("[!] Feeds RSS indisponiveis. Usando assuntos padrao.", flush=True)
        assuntos = list(ASSUNTOS_POLITICA_FALLBACK)
        titulos = []

    _salvar_cache_assuntos(assuntos, titulos)
    print(f"[*] Assuntos em alta: {', '.join(assuntos[:6])}...", flush=True)
    return assuntos, titulos

PADROES_LINGUAGEM_DEPRECIATIVA = [
    r"\bque vergonha\b",
    r"\bvergonha alheia\b",
    r"\bque nojo\b",
    r"\bnojo de\b",
    r"\b(lixo|lixos)\b",
    r"\bpalha[cç][oõ]\b",
    r"\brid[ií]cul[oa]\b",
    r"\bpat[eé]tic[oa]\b",
    r"\bimbecil\b",
    r"\bidiot[ao]\b",
    r"\bburr[oa]\b",
    r"\best[uú]pid[oa]\b",
    r"\besc[oó]ri[ao]\b",
    r"\bbabac[ao]\b",
    r"\bot[aá]ri[ao]\b",
    r"\bcanalha\b",
    r"\bvagabund[oa]\b",
    r"\bdesgra[cç]ad[oa]\b",
    r"\bin[eé]pt[oa]\b",
    r"\bverme\b",
    r"\bretardad[oa]\b",
    r"\babsurdo demais\b",
    r"\bhumilha(?:do|da|cao)\b",
    r"\bexp[oõ]e(?: a)? vergonha\b",
]

REGRA_TITULOS_NEUTROS = """
    REGRA DE TITULOS (OBRIGATORIA):
    - Titulos informativos e jornalisticos: foco no tema, pessoa ou fato discutido no audio.
    - PROIBIDO: insultos, zombaria, humilhacao, apelidos ofensivos e frases depreciativas.
    - Nao use expressoes como "que vergonha", "lixo", "palhaco", "ridiculo", "patetico",
      ataques pessoais ou tom de deboche no titulo.
    - Pode ser critico e direto, mas sempre profissional e respeitoso.
    - Exemplo bom: "Renan Santos analisa estrategia do MBL para as eleicoes"
    - Exemplo ruim: "Que vergonha! Renan Santos destrói o MBL"
"""

def titulo_contem_linguagem_depreciativa(texto):
    if not texto:
        return False
    texto_norm = texto.lower()
    return any(re.search(p, texto_norm, re.IGNORECASE) for p in PADROES_LINGUAGEM_DEPRECIATIVA)

def sanitizar_titulo_neutro(texto, fallback=""):
    original = str(texto or "").strip()
    if not original:
        return fallback
    tinha_depreciativo = titulo_contem_linguagem_depreciativa(original)
    if tinha_depreciativo and fallback:
        print(
            f"[!] Titulo substituido (linguagem depreciativa): '{fallback[:70]}'",
            flush=True,
        )
        return fallback
    resultado = original
    for padrao in PADROES_LINGUAGEM_DEPRECIATIVA:
        resultado = re.sub(padrao, "", resultado, flags=re.IGNORECASE)
    resultado = re.sub(r"[!?]+", " ", resultado)
    resultado = re.sub(r"\s{2,}", " ", resultado)
    resultado = re.sub(r"^[\s\-|:,;]+", "", resultado)
    resultado = re.sub(r"[\s\-|:,;]+$", "", resultado)
    resultado = re.sub(
        r"^(que|de|do|da|dos|das|o|a|os|as|em|no|na|um|uma)\s+",
        "",
        resultado,
        flags=re.IGNORECASE,
    ).strip()
    if not resultado or len(resultado) < 12:
        return fallback or original
    if resultado != original:
        print(
            f"[!] Titulo ajustado (sem linguagem depreciativa): '{resultado[:70]}'",
            flush=True,
        )
    return resultado or fallback or original

def montar_prompt_escolha_cortes(
    duracao_video, historico, lacunas, assuntos_em_alta, titulos_noticias,
    temas_sucesso, modo_fallback=False,
):
    assuntos_txt = "\n".join(f"  - {a}" for a in assuntos_em_alta[:12]) or "  (indisponivel)"
    noticias_txt = "\n".join(f"  - {t[:140]}" for t in titulos_noticias[:15]) or "  (sem manchetes no momento)"
    assunto_prioritario = (assuntos_em_alta[0] if assuntos_em_alta else "politica brasileira")

    bloco_mbl_missao = f"""
    PRIORIDADE MAXIMA — MBL, PARTIDO MISSAO E ASSUNTO #1 EM ALTA:
    - Assunto politico prioritario: {assunto_prioritario}
    - Busque trechos que tratem de MBL, Movimento Brasil Livre, Partido Missao, Renan Santos,
      Kim Kataguiri ou do assunto prioritario acima.
    - Se o audio abordar o assunto em alta com MBL/Missao, priorize esse trecho sobre outros.
    """

    if modo_fallback:
        bloco_editorial = f"""
    MODO FALLBACK — nao houve match exato na primeira analise.
    Voce DEVE escolher o trecho das lacunas com o assunto MAIS PROXIMO dos temas em alta.
    NAO retorne esgotado: existe lacuna de 4+ min disponivel.
    No JSON inclua "assunto_relacionado" (string) com o assunto em alta mais proximo encontrado.
    """
        regra_esgotado = ""
    else:
        bloco_editorial = f"""
    PRIORIDADE EDITORIAL — ASSUNTOS EM ALTA (ultimos {ASSUNTOS_DIAS_BUSCA} dias):
    Priorize um corte longo que trate DIRETAMENTE de um desses assuntos ou personagens das manchetes.
    O titulo e a descricao devem refletir o assunto em alta encontrado no audio.
    No JSON inclua "assunto_relacionado" (string) com o assunto em alta que o corte aborda.
    Se nao houver match exato, retorne {{"esgotado": true}} para permitir fallback automatico.
    """
        regra_esgotado = """
    Se nao houver nenhum trecho com match direto aos assuntos em alta nas lacunas, retorne APENAS:
    {"esgotado": true}
    """

    return f"""
    Voce e o estrategista do canal "Cortadas da Onça". Analise o audio e escolha DOIS trechos.
    Temas do canal: {temas_sucesso}

    DURACAO TOTAL DO VIDEO: {duracao_video:.1f} segundos.
    Todos os timestamps DEVEM estar entre 0 e {duracao_video:.1f}.

    ASSUNTOS POLITICOS EM ALTA:
{assuntos_txt}

    MANCHETES RECENTES (contexto):
{noticias_txt}
{bloco_mbl_missao}
{bloco_editorial}

    CORTES JA PUBLICADOS NESTE VIDEO (NAO REPETIR, NAO SOBREPOR):
{formatar_historico_prompt(historico)}

    LIMITE DO CANAL: Maximo {MAX_CORTES_POR_VIDEO} cortes longos por video. Ja foram {contar_cortes_video(historico)}.

    LACUNAS ONDE PODE HAVER CORTE NOVO (use APENAS estas faixas para o corte longo):
{formatar_lacunas_prompt(lacunas)}

    REGRA CRITICA DE DURACAO:
    CORTE 1 (YOUTUBE LONGO): Minimo 240s (4 min), Maximo 900s (15 min). DEVE ficar inteiro dentro de UMA lacuna.
    CORTE 2 (TIKTOK/SHORTS): Momento mais explosivo DENTRO do Corte 1 (Maximo 58 seg).
{REGRA_TITULOS_NEUTROS}
{regra_esgotado}
    Caso contrario, retorne APENAS JSON:
    {{
        "assunto_relacionado": "assunto em alta abordado",
        "corte_longo": {{"inicio": 300.0, "fim": 720.0, "titulo": "Titulo YouTube", "descricao": "Descricao SEO"}},
        "corte_curto": {{"inicio": 400.0, "fim": 458.0, "titulo": "Titulo TikTok", "tags": ["mbl", "politica"]}}
    }}
    """

def validar_e_finalizar_decisao_cortes(decisao, video_id, duracao_video, historico, lacunas):
    if decisao.get("esgotado"):
        raise VideoEsgotadoError(f"Video {video_id}: IA nao encontrou cortes para assuntos em alta")

    for chave in ("corte_longo", "corte_curto"):
        if chave not in decisao:
            raise VideoEsgotadoError(f"Video {video_id}: resposta da IA incompleta")

    for chave in ("corte_longo", "corte_curto"):
        normalizar_intervalo_corte(decisao[chave], duracao_video)

    longo = decisao["corte_longo"]
    curto = decisao["corte_curto"]

    if corte_conflita_historico(longo["inicio"], longo["fim"], historico):
        raise VideoEsgotadoError(
            f"Video {video_id}: corte longo ({longo['inicio']:.0f}-{longo['fim']:.0f}s) "
            "sobrepoe trecho ja publicado"
        )
    if not corte_dentro_de_lacuna(longo["inicio"], longo["fim"], lacunas):
        raise VideoEsgotadoError(
            f"Video {video_id}: corte longo fora das lacunas disponiveis"
        )

    if (longo["fim"] - longo["inicio"]) < 240:
        print("[!] IA falhou no tempo. Forcando 4 minutos no script...")
        longo["inicio"] = max(0.0, min(longo["inicio"], duracao_video - 245))
        longo["fim"] = min(duracao_video, longo["inicio"] + 245)

    normalizar_intervalo_corte(curto, duracao_video, duracao_min=5.0, duracao_max=58.0)
    if (curto["fim"] - curto["inicio"]) < 5:
        print("[!] Corte curto invalido. Usando inicio do corte longo...")
        curto["inicio"] = longo["inicio"]
        curto["fim"] = min(longo["fim"], longo["inicio"] + 58)
    elif curto["inicio"] < longo["inicio"] or curto["fim"] > longo["fim"]:
        print("[!] Corte curto fora do longo. Reposicionando dentro do corte longo...")
        curto["inicio"] = longo["inicio"]
        curto["fim"] = min(longo["fim"], longo["inicio"] + 58)

    assunto = str(decisao.get("assunto_relacionado") or "").strip()
    if assunto:
        print(f"[*] Assunto em alta relacionado: {assunto}", flush=True)

    print(
        f"[*] Cortes: longo {longo['inicio']:.1f}-{longo['fim']:.1f}s | "
        f"curto {curto['inicio']:.1f}-{curto['fim']:.1f}s | video {duracao_video:.1f}s",
        flush=True,
    )
    sanitizar_decisao_cortes(decisao)
    return decisao

# ==========================================
# INTELIGÊNCIA ARTIFICIAL (GEMINI)
# ==========================================
def erro_rede_recuperavel(exc):
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror)):
        return True
    errno = getattr(exc, "errno", None)
    winerr = getattr(exc, "winerror", None)
    if errno in (-2, -3, 11001) or winerr in (11001, 11002):
        return True
    causa = exc
    for _ in range(4):
        if causa is None:
            break
        if isinstance(causa, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror)):
            return True
        causa = getattr(causa, "__cause__", None) or getattr(causa, "__context__", None)
    msg = str(exc).upper()
    return any(
        chave in msg
        for chave in (
            "GETADDRINFO", "11001", "11002", "NAME OR SERVICE NOT KNOWN",
            "NETWORK IS UNREACHABLE", "CONNECTION RESET", "CONNECTION REFUSED",
            "CONNECTION ABORTED", "TEMPORARILY UNAVAILABLE", "TIMED OUT",
            "TIMEOUT", "BROKEN PIPE", "EOF OCCURRED", "SSL", "UNAVAILABLE",
            "FAILED TO ESTABLISH", "MAX RETRIES EXCEEDED",
        )
    )

def cota_diaria_modelo_esgotada(exc):
    """Cota diaria (RPD) do modelo — retry no mesmo modelo nao adianta."""
    msg = str(exc).upper()
    if "RESOURCE_EXHAUSTED" not in msg and "429" not in msg:
        return False
    return any(
        chave in msg
        for chave in (
            "GENERATEREQUESTSPERDAY",
            "PERDAYPERPROJECTPERMODEL",
            "FREE_TIER_REQUESTS",
            "QUOTA EXCEEDED FOR METRIC",
        )
    )

def extrair_retry_delay_api(exc, padrao=5.0):
    msg = str(exc)
    for pattern in (r"retry in (\d+(?:\.\d+)?)s", r"'retryDelay': '(\d+)s'"):
        match = re.search(pattern, msg, re.IGNORECASE)
        if match:
            return float(match.group(1)) + 1.0
    return padrao

def erro_gemini_recuperavel(exc):
    if erro_rede_recuperavel(exc):
        return True
    if isinstance(exc, genai_errors.APIError):
        return exc.code in (408, 429, 500, 502, 503, 504)
    msg = str(exc).upper()
    return any(
        chave in msg
        for chave in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "HIGH DEMAND", "OVERLOADED")
    )

def _espera_retry_gemini(tentativa):
    return min(90.0, 5.0 * (2 ** (tentativa - 1))) + random.uniform(0, 2.0)

def gemini_executar_com_retry(operacao, progresso=None, rotulo="Gemini"):
    ultimo_erro = None
    for tentativa in range(1, GEMINI_TENTATIVAS_POR_MODELO + 1):
        try:
            return operacao()
        except Exception as exc:
            ultimo_erro = exc
            if cota_diaria_modelo_esgotada(exc):
                print(f"[!] {rotulo}: cota diaria esgotada, aguarde reset ou troque modelo.", flush=True)
                raise
            if not erro_gemini_recuperavel(exc) or tentativa == GEMINI_TENTATIVAS_POR_MODELO:
                raise
            espera = extrair_retry_delay_api(exc, _espera_retry_gemini(tentativa))
            detalhe = f"{rotulo}: retry {tentativa}/{GEMINI_TENTATIVAS_POR_MODELO} em {int(espera)}s"
            if erro_rede_recuperavel(exc):
                detalhe = f"{rotulo}: rede instavel, retry {tentativa}/{GEMINI_TENTATIVAS_POR_MODELO} em {int(espera)}s"
            if progresso:
                progresso.atualizar_sub(40 + tentativa * 8, detalhe)
            else:
                print(f"[!] {detalhe}")
            time.sleep(espera)
    raise ultimo_erro

def gemini_gerar_cortes(arquivo_gemini, prompt, progresso=None):
    config = types.GenerateContentConfig(response_mime_type='application/json')
    ultimo_erro = None

    for indice_modelo, modelo in enumerate(GEMINI_MODELOS):
        for tentativa in range(1, GEMINI_TENTATIVAS_POR_MODELO + 1):
            try:
                if progresso:
                    progresso.atualizar_sub(
                        70 + tentativa * 4,
                        f"{modelo} ({tentativa}/{GEMINI_TENTATIVAS_POR_MODELO})",
                    )
                return gemini_client.models.generate_content(
                    model=modelo,
                    contents=[arquivo_gemini, prompt],
                    config=config,
                )
            except Exception as exc:
                ultimo_erro = exc
                if cota_diaria_modelo_esgotada(exc):
                    print(f"[!] Cota diaria esgotada em {modelo}. Tentando proximo modelo...", flush=True)
                    break
                if not erro_gemini_recuperavel(exc):
                    raise
                if tentativa < GEMINI_TENTATIVAS_POR_MODELO:
                    espera = extrair_retry_delay_api(exc, _espera_retry_gemini(tentativa))
                    detalhe = f"limite RPM em {modelo}, retry em {int(espera)}s"
                    if progresso:
                        progresso.atualizar_sub(65 + tentativa * 4, detalhe)
                    else:
                        print(f"[!] {detalhe}")
                    time.sleep(espera)
                    continue
                if indice_modelo < len(GEMINI_MODELOS) - 1:
                    if progresso:
                        progresso.atualizar_sub(68, f"trocando de {modelo}...")
                    else:
                        print(f"[!] Modelo {modelo} indisponivel. Tentando alternativo...")
                    break
                raise

    raise RuntimeError(
        f"Cota da API Gemini esgotada em todos os modelos ({', '.join(GEMINI_MODELOS)}). "
        "Aguarde reset diario ou use outra chave/plano."
    ) from ultimo_erro

def extrair_json_gemini(texto):
    texto = (texto or "").strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*", "", texto, flags=re.IGNORECASE)
        texto = re.sub(r"\s*```$", "", texto)
    return json.loads(texto)

def sanitizar_decisao_cortes(decisao):
    longo = decisao.setdefault("corte_longo", {})
    curto = decisao.setdefault("corte_curto", {})

    for corte in (longo, curto):
        if corte.get("title") and not corte.get("titulo"):
            corte["titulo"] = corte["title"]
        if corte.get("description") and not corte.get("descricao"):
            corte["descricao"] = corte["description"]

    assunto = str(decisao.get("assunto_relacionado") or "").strip()
    fallback_titulo = assunto or "Cortada da Onça — MBL em destaque"

    titulo_longo = str(longo.get("titulo") or "").strip()
    if not titulo_longo:
        titulo_longo = fallback_titulo
        print("[!] Titulo do corte longo vazio na IA. Usando titulo padrao.", flush=True)
    titulo_longo = sanitizar_titulo_neutro(titulo_longo, fallback=fallback_titulo)
    longo["titulo"] = titulo_longo[:100]

    descricao = str(longo.get("descricao") or "").strip()
    if descricao:
        linhas = descricao.splitlines()
        linhas[0] = sanitizar_titulo_neutro(linhas[0], fallback=titulo_longo)
        descricao = "\n".join(linhas)
    if not descricao:
        descricao = f"{titulo_longo}\n\n#MBL #politica #CortadasDaOnca"
        print("[!] Descricao vazia na IA. Usando descricao padrao.", flush=True)
    longo["descricao"] = descricao[:5000]

    titulo_curto = str(curto.get("titulo") or "").strip()
    if not titulo_curto:
        titulo_curto = titulo_longo[:80]
        print("[!] Titulo do corte curto vazio na IA. Usando titulo do corte longo.", flush=True)
    titulo_curto = sanitizar_titulo_neutro(titulo_curto, fallback=titulo_longo[:80])
    curto["titulo"] = titulo_curto[:100]

    tags = curto.get("tags") or ["mbl", "politica"]
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,#]", tags) if t.strip()]
    curto["tags"] = [str(t).strip("# ")[:30] for t in tags if str(t).strip()][:15] or ["mbl"]

    return decisao

def salvar_decisao_pipeline(decisao):
    with open(ARQUIVO_PIPELINE_DECISAO, "w", encoding="utf-8") as f:
        json.dump(decisao, f, indent=2, ensure_ascii=False)

def carregar_decisao_pipeline():
    if not os.path.exists(ARQUIVO_PIPELINE_DECISAO):
        return None
    with open(ARQUIVO_PIPELINE_DECISAO, encoding="utf-8") as f:
        return sanitizar_decisao_cortes(json.load(f))

def extrair_audio_para_ia(arquivo_video, arquivo_audio="temp_audio.mp3", progresso=None):
    if not progresso:
        print("[*] Extraindo áudio (Modo Turbo FFmpeg)...")
    if progresso:
        progresso.atualizar_sub(25, "FFmpeg extraindo audio...")
    comando = f'ffmpeg -y -i "{arquivo_video}" -vn -acodec libmp3lame -ab 64k "{arquivo_audio}"'
    subprocess.run(comando, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if progresso:
        progresso.atualizar_sub(30, "audio pronto")
    return arquivo_audio

def encontrar_melhor_corte(arquivo_video, video_id, temas_sucesso, progresso=None, historico=None):
    duracao_video = ffprobe_duracao(arquivo_video)
    historico = historico or carregar_historico_video(video_id)
    if video_atingiu_limite_cortes(historico):
        raise VideoEsgotadoError(
            f"Video {video_id}: limite de {MAX_CORTES_POR_VIDEO} cortes atingido"
        )
    lacunas = calcular_lacunas_disponiveis(duracao_video, historico)
    if not lacunas:
        raise VideoEsgotadoError(f"Video {video_id}: sem trechos novos de 4+ min")

    cortes_restantes = max(0, MAX_CORTES_POR_VIDEO - contar_cortes_video(historico))
    print(
        f"[*] Historico: {contar_cortes_video(historico)}/{MAX_CORTES_POR_VIDEO} cortes | "
        f"{len(lacunas)} lacuna(s) | ate {cortes_restantes} corte(s) restante(s) neste video",
        flush=True,
    )

    assuntos_em_alta, titulos_noticias = obter_assuntos_politica_em_alta(progresso=progresso)
    arquivo_audio = extrair_audio_para_ia(arquivo_video, progresso=progresso)
    if not progresso:
        print("[*] Enviando áudio ao Gemini...")
    if progresso:
        progresso.atualizar_sub(35, "upload para Gemini...")

    arquivo_gemini = gemini_executar_com_retry(
        lambda: gemini_client.files.upload(file=arquivo_audio),
        progresso=progresso,
        rotulo="upload Gemini",
    )

    inicio_proc = time.time()
    while arquivo_gemini.state == types.FileState.PROCESSING:
        if progresso:
            progresso.atualizar_sub(
                min(55, 40 + (time.time() - inicio_proc)),
                "Gemini processando audio...",
            )
        time.sleep(2)
        arquivo_gemini = gemini_executar_com_retry(
            lambda: gemini_client.files.get(name=arquivo_gemini.name),
            progresso=progresso,
            rotulo="status Gemini",
        )

    decisao = None
    for modo_fallback in (False, True):
        prompt = montar_prompt_escolha_cortes(
            duracao_video, historico, lacunas, assuntos_em_alta, titulos_noticias,
            temas_sucesso, modo_fallback=modo_fallback,
        )
        if progresso:
            rotulo = "assunto mais proximo..." if modo_fallback else "Gemini analisando cortes..."
            progresso.atualizar_sub(60, rotulo)

        res = gemini_gerar_cortes(arquivo_gemini, prompt, progresso=progresso)
        candidato = extrair_json_gemini(res.text)

        if candidato.get("esgotado"):
            if not modo_fallback:
                print(
                    "[!] Nenhum corte com match direto aos assuntos em alta. "
                    "Buscando assunto mais proximo...",
                    flush=True,
                )
                continue
            raise VideoEsgotadoError(
                f"Video {video_id}: IA nao encontrou cortes mesmo no modo fallback"
            )

        try:
            decisao = validar_e_finalizar_decisao_cortes(
                candidato, video_id, duracao_video, historico, lacunas,
            )
            if modo_fallback:
                print("[*] Corte escolhido por proximidade com assuntos em alta.", flush=True)
            break
        except VideoEsgotadoError as exc:
            if not modo_fallback:
                print(f"[!] {exc}. Tentando assunto mais proximo...", flush=True)
                continue
            raise

    salvar_decisao_pipeline(decisao)
    longo = decisao["corte_longo"]
    curto = decisao["corte_curto"]
    print(f"[*] Titulos: longo='{longo['titulo'][:50]}' | curto='{curto['titulo'][:50]}'", flush=True)

    gemini_client.files.delete(name=arquivo_gemini.name)
    if os.path.exists(arquivo_audio):
        os.remove(arquivo_audio)
    if progresso:
        progresso.atualizar_sub(95, "cortes definidos")
    return decisao

# ==========================================
# DISTRIBUIÇÃO E PIPELINE
# ==========================================
def autenticar_youtube():
    creds = None
    if os.path.exists(ARQUIVO_TOKEN):
        creds = Credentials.from_authorized_user_file(ARQUIVO_TOKEN, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(ARQUIVO_SECRETS, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(ARQUIVO_TOKEN, 'w') as token: token.write(creds.to_json())
    return build('youtube', 'v3', credentials=creds)

def definir_thumbnail_youtube(youtube, video_id, arquivo_thumb, progresso=None):
    if not video_id or not arquivo_thumb or not os.path.isfile(arquivo_thumb):
        return
    if not progresso:
        print(f"[*] Enviando thumbnail para {video_id}...", flush=True)
    midia = MediaFileUpload(arquivo_thumb, mimetype="image/jpeg", resumable=False)
    youtube.thumbnails().set(videoId=video_id, media_body=midia).execute()

def fazer_upload_youtube(
    youtube, arquivo, titulo, desc, tags,
    is_shorts=False, progresso=None, thumbnail=None,
):
    titulo = str(titulo or "").strip()
    if not titulo:
        raise ValueError("Titulo do video vazio — impossivel enviar ao YouTube.")
    desc = str(desc or titulo).strip()
    if not isinstance(tags, list):
        tags = ["mbl"]
    tags = [str(t) for t in tags if str(t).strip()][:15] or ["mbl"]
    titulo_final = f"{titulo}{' #Shorts' if is_shorts else ''}"[:100]
    if not progresso:
        print(f"[*] Upload YouTube: {titulo_final}")
    corpo = {
        'snippet': {
            'title': titulo_final,
            'description': desc[:5000],
            'tags': tags,
            'categoryId': "24",
        },
        'status': {'privacyStatus': "public", 'selfDeclaredMadeForKids': False},
    }
    midia = MediaFileUpload(arquivo, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part=','.join(corpo.keys()), body=corpo, media_body=midia)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status and progresso:
            progresso.atualizar_sub(status.progress() * 100, titulo[:40])
    video_id = response.get("id")
    if video_id and thumbnail:
        try:
            definir_thumbnail_youtube(youtube, video_id, thumbnail, progresso=progresso)
        except Exception as exc:
            print(
                f"[!] Thumbnail nao aplicada em {video_id}: {exc}\n"
                "    Verifique se o canal tem verificacao de telefone no YouTube Studio.",
                flush=True,
            )
    return video_id

def _fechar_modais_tiktok_studio(page):
    """Fecha popups recorrentes do TikTok Studio (ex.: 'New editing features added')."""
    textos_botao = ("Got it", "Entendi", "OK", "Fechar", "Close")
    for _ in range(3):
        fechou = False
        for texto in textos_botao:
            for locator in (
                page.get_by_role("button", name=texto, exact=True),
                page.locator(f"button:has-text('{texto}')"),
                page.locator(f"//button[.//div[normalize-space()='{texto}']]"),
            ):
                try:
                    alvo = locator.first
                    if alvo.is_visible(timeout=600):
                        alvo.click()
                        fechou = True
                        time.sleep(0.7)
                        break
                except Exception:
                    continue
            if fechou:
                break
        if not fechou:
            break

def _aplicar_patch_modais_tiktok():
    if getattr(_aplicar_patch_modais_tiktok, "_aplicado", False):
        return
    import tiktok_uploader.upload as tu_upload

    def complete_upload_form_com_modais(
        page,
        path,
        description,
        schedule,
        skip_split_window,
        cover_path=None,
        product_id=None,
        visibility="everyone",
        num_retries=1,
        headless=False,
        *args,
        **kwargs,
    ):
        tu_upload._go_to_upload(page)
        tu_upload._remove_cookies_window(page)
        _fechar_modais_tiktok_studio(page)

        tu_upload._set_video(page, path=path, num_retries=num_retries, **kwargs)
        _fechar_modais_tiktok_studio(page)

        if cover_path:
            tu_upload._set_cover(page, cover_path)
        if not skip_split_window:
            tu_upload._remove_split_window(page)
        _fechar_modais_tiktok_studio(page)

        tu_upload._set_interactivity(page, **kwargs)
        _fechar_modais_tiktok_studio(page)

        tu_upload._set_description(page, description)
        if visibility != "everyone":
            tu_upload._set_visibility(page, visibility)
        if schedule:
            tu_upload._set_schedule_video(page, schedule)
        if product_id:
            tu_upload._add_product_link(page, product_id)

        _fechar_modais_tiktok_studio(page)
        tu_upload._post_video(page)

    tu_upload.complete_upload_form = complete_upload_form_com_modais
    _aplicar_patch_modais_tiktok._aplicado = True

def fazer_upload_tiktok(arquivo, titulo, progresso=None):
    if not progresso:
        print(f"[*] Iniciando upload TikTok...")
    if not os.path.isfile(arquivo):
        print(f"[-] Arquivo de video nao encontrado: {arquivo}")
        print("[!] Rode o pipeline de render ou copie o .mp4 vertical para esta pasta.")
        return False
    if not tiktok_cookies_validos():
        print(
            "[!] Cookies do TikTok ausentes ou expirados. "
            f"Renove '{ARQUIVO_COOKIES_TIKTOK}' (formato Netscape, com sessionid) e tente de novo."
        )
        return False
    if not preparar_video_tiktok(arquivo):
        return False
    try:
        from tiktok_uploader.upload import upload_video
        _aplicar_patch_modais_tiktok()
        if progresso:
            progresso.iniciar_timer("upload TikTok...")
        falhas = upload_video(
            arquivo,
            description=titulo,
            cookies=ARQUIVO_COOKIES_TIKTOK,
            browser='chrome',
        )
        if progresso:
            progresso.parar_timer()
        if falhas:
            print(f"[-] Falha no upload TikTok: {falhas}")
            return False
        progresso and progresso.atualizar_sub(100, "TikTok enviado")
        return True
    except Exception as e:
        if progresso:
            progresso.parar_timer()
        msg = str(e)
        if "login" in msg.lower() or "#root" in msg:
            print(
                "[!] TikTok redirecionou para login — cookies expirados. "
                f"Exporte cookies novos em '{ARQUIVO_COOKIES_TIKTOK}'."
            )
        else:
            print(f"[-] Erro TikTok: {e}")
        return False

def login_tiktok_interativo():
    """
    Abre o Chrome visivel para login manual (Google, QR, email).
    Salva apenas cookies do tiktok.com em tiktok_cookies.txt.
    """
    from playwright.sync_api import sync_playwright
    from tiktok_uploader.auth import save_cookies

    print("=== LOGIN TIKTOK (manual) ===", flush=True)
    print("O script nao faz login com Google sozinho.", flush=True)
    print("Opcoes na janela que vai abrir:", flush=True)
    print("  - Continuar com Google", flush=True)
    print("  - QR code (logar no app TikTok no celular e escanear)", flush=True)
    print("  - Email/telefone", flush=True)
    print(f"\nQuando o feed carregar, os cookies serao salvos em '{ARQUIVO_COOKIES_TIKTOK}'.\n", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 800}, locale="pt-BR")
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = context.new_page()
        page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")

        print("[*] Aguardando login (ate 5 minutos)...", flush=True)
        for _ in range(300):
            cookies = [
                c for c in context.cookies()
                if "tiktok.com" in c.get("domain", "") and c.get("name") == "sessionid" and c.get("value")
            ]
            if cookies:
                para_salvar = []
                for c in context.cookies():
                    if "tiktok.com" not in c.get("domain", ""):
                        continue
                    item = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c["domain"],
                        "path": c.get("path", "/"),
                    }
                    if c.get("expires"):
                        item["expiry"] = int(c["expires"])
                    para_salvar.append(item)
                save_cookies(ARQUIVO_COOKIES_TIKTOK, para_salvar)
                print(f"[+] Login OK. {len(para_salvar)} cookies salvos em '{ARQUIVO_COOKIES_TIKTOK}'.", flush=True)
                browser.close()
                return True
            time.sleep(1)

        browser.close()
        print("[-] Tempo esgotado. Tente de novo ou use QR code / Chrome normal + extensao de cookies.", flush=True)
        return False

def executar_pipeline_completo():
    ORIGINAL, HORIZ, VERT = "video_original.mp4", "corte_horizontal.mp4", "corte_vertical.mp4"
    precisa_baixar = not os.path.exists(ORIGINAL)

    if not precisa_baixar:
        estado = carregar_estado_pipeline()
        video_id_local = estado.get("video_id", "ID_EXISTENTE")
        historico_local = carregar_historico_video(video_id_local)
        if motivo_trocar_video(ffprobe_duracao(ORIGINAL), historico_local):
            precisa_baixar = True

    etapas = [
        "Buscar live",
        "Autenticar YouTube",
        "Download do vídeo",
        "Análise com IA",
        "Render dos cortes",
        "Upload YouTube longo",
        "Upload YouTube Shorts",
        "Upload TikTok",
    ]
    if not precisa_baixar:
        etapas.remove("Download do vídeo")
        etapas.remove("Buscar live")

    progresso = BarraProgresso(etapas)
    print("=== PIPELINE CORTADAS DA ONCA ===", flush=True)

    try:
        VIDEO_ID = None
        youtube_service = None
        ignorar = carregar_videos_esgotados()

        VIDEO_ID, youtube_service = garantir_video_disponivel(
            progresso, ORIGINAL, ignorar, youtube_service=youtube_service,
        )
        historico = carregar_historico_video(VIDEO_ID)
        lacunas = calcular_lacunas_disponiveis(ffprobe_duracao(ORIGINAL), historico)
        print(
            f"[*] Usando {ORIGINAL} ({VIDEO_ID}) — "
            f"{contar_cortes_video(historico)}/{MAX_CORTES_POR_VIDEO} cortes, "
            f"{len(lacunas)} lacuna(s) restante(s)",
            flush=True,
        )

        if not os.path.exists(ORIGINAL):
            raise RuntimeError(f"Arquivo original nao existe: {ORIGINAL}")

        if youtube_service is None:
            progresso.iniciar_etapa("Autenticar YouTube")
            youtube_service = progresso.executar_com_timer(
                "autenticando...",
                autenticar_youtube,
            )
            progresso.concluir_etapa()

        progresso.iniciar_etapa("Análise com IA")
        decisao = None
        for tentativa_ia in range(1, MAX_TENTATIVAS_NOVO_VIDEO + 1):
            try:
                decisao = encontrar_melhor_corte(
                    ORIGINAL, VIDEO_ID, "Política, MBL, Arthur do Val, Renan Santos",
                    progresso=progresso,
                    historico=carregar_historico_video(VIDEO_ID),
                )
                break
            except VideoEsgotadoError as exc:
                print(f"[!] {exc}", flush=True)
                marcar_video_esgotado(VIDEO_ID)
                liberar_video_atual(ORIGINAL)
                ignorar.add(VIDEO_ID)
                if tentativa_ia >= MAX_TENTATIVAS_NOVO_VIDEO:
                    raise RuntimeError(
                        f"{exc}. Nao encontrei outro video disponivel para cortar."
                    ) from exc
                VIDEO_ID, youtube_service = garantir_video_disponivel(
                    progresso, ORIGINAL, ignorar, youtube_service=youtube_service,
                )
                print(f"[*] Novo video selecionado: {VIDEO_ID}", flush=True)
        progresso.concluir_etapa()

        progresso.iniciar_etapa("Render dos cortes")
        fazer_corte_video(ORIGINAL, HORIZ, VERT, decisao['corte_longo'], decisao['corte_curto'], progresso=progresso)
        progresso.concluir_etapa()

        garantir_thumbnails_para_upload(HORIZ, VERT)

        progresso.iniciar_etapa("Upload YouTube longo")
        fazer_upload_youtube(
            youtube_service, HORIZ, decisao['corte_longo']['titulo'],
            decisao['corte_longo']['descricao'], ["mbl"],
            progresso=progresso, thumbnail=THUMB_HORIZONTAL,
        )
        progresso.concluir_etapa()

        progresso.iniciar_etapa("Upload YouTube Shorts")
        fazer_upload_youtube(
            youtube_service, VERT, decisao['corte_curto']['titulo'],
            "#Shorts #MBL", decisao['corte_curto']['tags'],
            is_shorts=True, progresso=progresso, thumbnail=THUMB_VERTICAL,
        )
        progresso.concluir_etapa()

        progresso.iniciar_etapa("Upload TikTok")
        tiktok_ok = fazer_upload_tiktok(VERT, f"{decisao['corte_curto']['titulo']} #MBL", progresso=progresso)
        progresso.concluir_etapa()

        salvar_historico(VIDEO_ID, decisao['corte_longo']['inicio'], decisao['corte_longo']['fim'])

        historico_atual = carregar_historico_video(VIDEO_ID)
        lacunas_restantes = calcular_lacunas_disponiveis(ffprobe_duracao(ORIGINAL), historico_atual)
        if video_atingiu_limite_cortes(historico_atual):
            print(
                f"[*] Video {VIDEO_ID} concluiu {MAX_CORTES_POR_VIDEO} cortes.",
                flush=True,
            )
            marcar_video_esgotado(VIDEO_ID)
            liberar_video_atual(ORIGINAL)
            ignorar = carregar_videos_esgotados()
            preparar_proxima_live(progresso, ORIGINAL, ignorar, youtube_service=youtube_service)
        elif lacunas_restantes:
            print(
                f"[*] {ORIGINAL} mantido para proximos cortes "
                f"({contar_cortes_video(historico_atual)}/{MAX_CORTES_POR_VIDEO}, "
                f"{len(lacunas_restantes)} lacuna(s) restante(s)).",
                flush=True,
            )
        else:
            print(f"[*] Video {VIDEO_ID} esgotado apos este corte.", flush=True)
            marcar_video_esgotado(VIDEO_ID)
            liberar_video_atual(ORIGINAL)
            ignorar = carregar_videos_esgotados()
            preparar_proxima_live(progresso, ORIGINAL, ignorar, youtube_service=youtube_service)

        if os.path.exists(HORIZ):
            os.remove(HORIZ)
        if os.path.exists(THUMB_HORIZONTAL):
            os.remove(THUMB_HORIZONTAL)
        if tiktok_ok and os.path.exists(VERT):
            os.remove(VERT)
        if tiktok_ok and os.path.exists(THUMB_VERTICAL):
            os.remove(THUMB_VERTICAL)

        progresso.resumo_final()
        print("[!] PROCESSO CONCLUÍDO COM ACELERAÇÃO DE GPU!")
    except Exception as e:
        progresso.parar_timer()
        print(f"\n[-] Erro: {e}")
        if os.path.exists(ORIGINAL) and erro_rede_recuperavel(e):
            print(
                "[!] O video original ja esta salvo. Verifique internet/DNS e rode de novo:\n"
                "    python .\\automacao_cortes.py\n"
                "    (o download sera pulado automaticamente)",
                flush=True,
            )

def executar_apenas_upload():
    HORIZ, VERT = "corte_horizontal.mp4", "corte_vertical.mp4"
    if not os.path.exists(HORIZ):
        raise FileNotFoundError(f"Corte longo nao encontrado: {HORIZ}")
    if not os.path.exists(VERT):
        raise FileNotFoundError(f"Corte curto nao encontrado: {VERT}")

    decisao = carregar_decisao_pipeline()
    if not decisao:
        print("[!] pipeline_decisao.json nao encontrado. Usando metadados padrao.", flush=True)
        decisao = sanitizar_decisao_cortes({
            "corte_longo": {"inicio": 0, "fim": 0, "titulo": "", "descricao": ""},
            "corte_curto": {"inicio": 0, "fim": 0, "titulo": "", "tags": []},
        })

    etapas = ["Autenticar YouTube", "Upload YouTube longo", "Upload YouTube Shorts", "Upload TikTok"]
    progresso = BarraProgresso(etapas)
    print("=== UPLOAD APENAS (cortes ja renderizados) ===", flush=True)

    try:
        progresso.iniciar_etapa("Autenticar YouTube")
        youtube_service = progresso.executar_com_timer("autenticando...", autenticar_youtube)
        progresso.concluir_etapa()

        garantir_thumbnails_para_upload(HORIZ, VERT)

        progresso.iniciar_etapa("Upload YouTube longo")
        fazer_upload_youtube(
            youtube_service, HORIZ, decisao['corte_longo']['titulo'],
            decisao['corte_longo']['descricao'], ["mbl"],
            progresso=progresso, thumbnail=THUMB_HORIZONTAL,
        )
        progresso.concluir_etapa()

        progresso.iniciar_etapa("Upload YouTube Shorts")
        fazer_upload_youtube(
            youtube_service, VERT, decisao['corte_curto']['titulo'],
            "#Shorts #MBL", decisao['corte_curto']['tags'],
            is_shorts=True, progresso=progresso, thumbnail=THUMB_VERTICAL,
        )
        progresso.concluir_etapa()

        progresso.iniciar_etapa("Upload TikTok")
        tiktok_ok = fazer_upload_tiktok(
            VERT, f"{decisao['corte_curto']['titulo']} #MBL", progresso=progresso,
        )
        progresso.concluir_etapa()

        if tiktok_ok:
            for f in (HORIZ, VERT, THUMB_HORIZONTAL, THUMB_VERTICAL):
                if os.path.exists(f):
                    os.remove(f)
        progresso.resumo_final()
        print("[!] UPLOAD CONCLUIDO!")
    except Exception as e:
        progresso.parar_timer()
        print(f"\n[-] Erro: {e}")

def executar_corrigir_thumbnail(video_id, arquivo_video=None, momento_seg=None):
    video_id = str(video_id or "").strip()
    if not video_id:
        raise ValueError("Informe o ID do video do YouTube (ex.: --corrigir-thumbnail dQw4w9WgXcQ)")

    arquivo_video = arquivo_video or "video_original.mp4"
    if not os.path.isfile(arquivo_video):
        raise FileNotFoundError(
            f"Arquivo de video nao encontrado: {arquivo_video}. "
            "Passe o caminho como segundo argumento se necessario."
        )

    if momento_seg is None:
        decisao = carregar_decisao_pipeline()
        if decisao and decisao.get("corte_longo"):
            momento_seg = float(decisao["corte_longo"]["inicio"]) + 5.0
        else:
            momento_seg = 5.0

    print(f"[*] Extraindo thumbnail de {arquivo_video} em {momento_seg:.1f}s...", flush=True)
    extrair_thumbnail_video(arquivo_video, THUMB_HORIZONTAL, momento_seg=momento_seg)

    print(f"[*] Autenticando YouTube...", flush=True)
    youtube = autenticar_youtube()
    definir_thumbnail_youtube(youtube, video_id, THUMB_HORIZONTAL)
    print(f"[!] Thumbnail aplicada em https://youtu.be/{video_id}", flush=True)

if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ("--login-tiktok", "--tiktok-login"):
        ok = login_tiktok_interativo()
        raise SystemExit(0 if ok else 1)
    if len(sys.argv) > 1 and sys.argv[1] in ("--so-upload", "--upload-only"):
        executar_apenas_upload()
        raise SystemExit(0)
    if len(sys.argv) > 1 and sys.argv[1] in ("--corrigir-thumbnail", "--fix-thumbnail"):
        if len(sys.argv) < 3:
            print("Uso: python automacao_cortes.py --corrigir-thumbnail VIDEO_ID [arquivo.mp4] [momento_seg]")
            raise SystemExit(1)
        vid = sys.argv[2]
        arq = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].replace(".", "", 1).isdigit() else None
        momento = None
        for arg in sys.argv[3:]:
            try:
                momento = float(arg)
            except ValueError:
                pass
        try:
            executar_corrigir_thumbnail(vid, arquivo_video=arq, momento_seg=momento)
        except Exception as exc:
            print(f"[-] Erro: {exc}")
            raise SystemExit(1)
        raise SystemExit(0)
    executar_pipeline_completo()