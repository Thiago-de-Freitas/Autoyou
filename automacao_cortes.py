import os
import json
import time
import re
import random
import subprocess

# =================================================================
# FURA-BLOQUEIO DO PYTHON PARA ENXERGAR O NODE.JS
# =================================================================
try:
    caminho_node = subprocess.check_output("where node", shell=True, text=True).strip().split('\n')[0]
    os.environ["PATH"] = os.path.dirname(caminho_node) + os.pathsep + os.environ["PATH"]
except:
    pass

from moviepy import VideoFileClip, concatenate_videoclips, CompositeVideoClip
from moviepy.video.fx import FadeOut, Resize, Crop
from moviepy.audio.fx import AudioFadeOut
from yt_dlp import YoutubeDL
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CONFIGURAÇÕES GERAIS E CHAVES
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") 
SCOPES = ['https://www.googleapis.com/auth/youtube'] 
ARQUIVO_SECRETS = 'client_secrets.json'
ARQUIVO_TOKEN = 'token.json'
ARQUIVO_HISTORICO = 'historico_cortes.json'
# yt-dlp espera arquivo de cookies no formato Netscape:
# http://curl.haxx.se/rfc/cookie_spec.html
ARQUIVO_COOKIES_YT = 'cookies.txt'
ARQUIVO_COOKIES_YT_FALLBACK = 'cookies_estaticos.txt'

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

genai.configure(api_key=GEMINI_API_KEY)

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
        "captcha",
        "sign in",
    ]
    return any(c in m for c in chaves)

# ==========================================
# SISTEMA DE HISTÓRICO
# ==========================================
def extrair_id_youtube(url):
    match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", url)
    return match.group(1) if match else "ID_DESCONHECIDO"

def salvar_historico(video_id, inicio, fim):
    dados = {}
    if os.path.exists(ARQUIVO_HISTORICO):
        with open(ARQUIVO_HISTORICO, 'r') as f:
            dados = json.load(f)
    if video_id not in dados:
        dados[video_id] = []
    dados[video_id].append({"inicio": inicio, "fim": fim})
    with open(ARQUIVO_HISTORICO, 'w') as f:
        json.dump(dados, f, indent=4)

# ==========================================
# FUNÇÕES DE CAPTAÇÃO E DOWNLOAD
# ==========================================
def obter_live_recente_mbl():
    canais = ["https://www.youtube.com/@MBLiveTV"]
    canal_alvo = random.choice(canais)
    url_streams = f"{canal_alvo}/streams"
    print(f"[*] Varrendo a aba Ao Vivo do canal: {canal_alvo}...")
    
    cookiefile = None
    if os.path.exists(ARQUIVO_COOKIES_YT) and arquivo_cookies_netscape_valido(ARQUIVO_COOKIES_YT):
        cookiefile = ARQUIVO_COOKIES_YT
    elif os.path.exists(ARQUIVO_COOKIES_YT_FALLBACK) and arquivo_cookies_netscape_valido(ARQUIVO_COOKIES_YT_FALLBACK):
        cookiefile = ARQUIVO_COOKIES_YT_FALLBACK
    elif os.path.exists(ARQUIVO_COOKIES_YT_FALLBACK):
        print(f"[!] Cookies inválidos (formato) em '{ARQUIVO_COOKIES_YT_FALLBACK}'. Ignorando cookies para o yt-dlp.")

    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        # Ajuda em casos de anti-bot que checa headers básicos
        'http_headers': HTTP_HEADERS,
        # O solver EJS usa um runtime JS (na API do yt-dlp precisa ser js_runtimes).
        'js_runtimes': {'node': {}},
    }
    if cookiefile:
        ydl_opts['cookiefile'] = cookiefile

    def tentar_extracao(ydl_local_opts):
        with YoutubeDL(ydl_local_opts) as ydl:
            info = ydl.extract_info(url_streams, download=False)
            if 'entries' in info:
                for entry in info['entries']:
                    if entry.get('live_status') == 'is_upcoming':
                        print(f"[-] Pulando live agendada: {entry.get('title')}")
                        continue
                    video_id = entry['id']
                    url_final = f"https://www.youtube.com/watch?v={video_id}"
                    print(f"[+] Live Válida Encontrada: '{entry.get('title')}'")
                    return url_final
        return None

    try:
        url = tentar_extracao(ydl_opts)
        if url:
            return url
    except Exception as e:
        print(f"[-] Erro ao buscar live: {e}")

    raise ValueError("Nenhuma live recente encontrada.")

def baixar_video(url_video, nome_arquivo_saida):
    print(f"[*] Iniciando download do vídeo original...")
    ydl_opts = {
        "outtmpl": nome_arquivo_saida,
        "format": "bv*+ba/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "http_headers": HTTP_HEADERS,
        # O solver EJS usa um runtime JS (na API do yt-dlp precisa ser js_runtimes).
        "js_runtimes": {"node": {}},
    }
    if os.path.exists(ARQUIVO_COOKIES_YT) and arquivo_cookies_netscape_valido(ARQUIVO_COOKIES_YT):
        ydl_opts["cookiefile"] = ARQUIVO_COOKIES_YT
    elif os.path.exists(ARQUIVO_COOKIES_YT_FALLBACK) and arquivo_cookies_netscape_valido(ARQUIVO_COOKIES_YT_FALLBACK):
        ydl_opts["cookiefile"] = ARQUIVO_COOKIES_YT_FALLBACK

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url_video])
    except Exception as e:
        msg = str(e)
        print(f"[-] Erro ao baixar vídeo: {e}")
        if eh_erro_anti_bot_youtube(msg):
            raise RuntimeError(
                "YouTube bloqueou (anti-bot). Renovar cookies em 'cookies.txt' "
                "e tente novamente. "
                "Se continuar, pode ser necessário instalar/ativar suporte a JS runtime "
                "e solver de desafio do yt-dlp (EJS) no seu ambiente."
            )
        raise

    # Se falhou sem levantar exceção (raramente), evita pipeline continuar
    if not os.path.exists(nome_arquivo_saida) or os.path.getsize(nome_arquivo_saida) == 0:
        raise RuntimeError(f"Falha no download: arquivo '{nome_arquivo_saida}' não foi gerado.")
    return nome_arquivo_saida

# ==========================================
# EDIÇÃO DE VÍDEO (TURBO GPU + ZOOM)
# ==========================================
def fazer_corte_video(arquivo_entrada, arquivo_horizontal, arquivo_vertical, t_longo, t_curto):
    print(f"[*] Iniciando produção TURBO (GPU NVIDIA + Enquadramento Correto)...")
    video = VideoFileClip(arquivo_entrada)
    params_gpu = ["-preset", "p1"]

    # 1. FORMATO HORIZONTAL (YouTube Longo)
    corte_h = video.subclipped(t_longo['inicio'], t_longo['fim'])
    intro = VideoFileClip("intro_onca.mp4")
    intro_res = intro.with_effects([Resize(new_size=corte_h.size)])
    video_h = concatenate_videoclips([intro_res, corte_h], method="compose")
    video_h = video_h.with_effects([FadeOut(2.5), AudioFadeOut(2.5)])
    
    video_h.write_videofile(arquivo_horizontal, codec='h264_nvenc', audio_codec='aac', logger=None, threads=8, ffmpeg_params=params_gpu)

    # 2. FORMATO VERTICAL (TikTok/Shorts) - ENQUADRAMENTO CORRETO
    print(f"    -> Renderizando TikTok com Redimensionamento e Letterbox...")
    corte_v = video.subclipped(t_curto['inicio'], t_curto['fim'])
    
    # Definindo o tamanho padrão Shorts/TikTok (1080x1920)
    LARGURA_ALVO = 1080
    ALTURA_ALVO = 1920
    
    # Redimensiona o vídeo original para caber na largura de 1080px mantendo a proporção
    video_redimensionado = corte_v.with_effects([Resize(width=LARGURA_ALVO)])
    
    # Cria um fundo preto no tamanho 1080x1920 e centraliza o vídeo redimensionado nele
    video_final_v = CompositeVideoClip(
        [video_redimensionado.with_position("center")], 
        size=(LARGURA_ALVO, ALTURA_ALVO)
    )
    
    video_final_v = video_final_v.with_effects([FadeOut(1.5), AudioFadeOut(1.5)])
    
    video_final_v.write_videofile(
        arquivo_vertical, 
        codec='h264_nvenc', 
        audio_codec='aac', 
        logger=None, 
        fps=video.fps, 
        threads=8, 
        ffmpeg_params=params_gpu
    )

    video.close(); intro.close(); video_h.close(); video_final_v.close()

# ==========================================
# INTELIGÊNCIA ARTIFICIAL (GEMINI)
# ==========================================
def extrair_audio_para_ia(arquivo_video, arquivo_audio="temp_audio.mp3"):
    print("[*] Extraindo áudio (Modo Turbo FFmpeg)...")
    comando = f'ffmpeg -y -i "{arquivo_video}" -vn -acodec libmp3lame -ab 64k "{arquivo_audio}"'
    subprocess.run(comando, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return arquivo_audio

def encontrar_melhor_corte(arquivo_video, video_id, temas_sucesso):
    arquivo_audio = extrair_audio_para_ia(arquivo_video)
    print("[*] Enviando áudio ao Gemini...")
    arquivo_gemini = genai.upload_file(path=arquivo_audio)
    while arquivo_gemini.state.name == "PROCESSING":
        time.sleep(2)
        arquivo_gemini = genai.get_file(arquivo_gemini.name)
        
    prompt = f"""
    Você é o estrategista do canal "Cortadas da Onça". Analise o áudio e escolha DOIS trechos baseados em: {temas_sucesso}
    
    REGRA CRÍTICA DE DURAÇÃO:
    CORTE 1 (YOUTUBE LONGO): Mínimo 240s (4 min), Máximo 900s (15 min).
    CORTE 2 (TIKTOK/SHORTS): Momento mais explosivo DENTRO do Corte 1 (Máximo 58 seg).
    
    Retorne APENAS JSON:
    {{
        "corte_longo": {{"inicio": 300.0, "fim": 720.0, "titulo": "Titulo YouTube", "descricao": "Descricao SEO"}},
        "corte_curto": {{"inicio": 400.0, "fim": 458.0, "titulo": "Titulo TikTok", "tags": ["mbl", "politica"]}}
    }}
    """
    modelo = genai.GenerativeModel('gemini-2.5-flash')
    res = modelo.generate_content([arquivo_gemini, prompt], generation_config={"response_mime_type": "application/json"})
    decisao = json.loads(res.text)
    
    # Validação forçada de 4 minutos
    if (decisao['corte_longo']['fim'] - decisao['corte_longo']['inicio']) < 240:
        print("[!] IA falhou no tempo. Forçando 4 minutos no script...")
        decisao['corte_longo']['fim'] = decisao['corte_longo']['inicio'] + 245

    genai.delete_file(arquivo_gemini.name)
    if os.path.exists(arquivo_audio): os.remove(arquivo_audio)
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

def fazer_upload_youtube(youtube, arquivo, titulo, desc, tags, is_shorts=False):
    print(f"[*] Upload YouTube: {titulo}")
    corpo = {'snippet': {'title': f"{titulo}{' #Shorts' if is_shorts else ''}", 'description': desc, 'tags': tags, 'categoryId': "24"},
             'status': {'privacyStatus': "public", 'selfDeclaredMadeForKids': False}}
    midia = MediaFileUpload(arquivo, chunksize=-1, resumable=True)
    youtube.videos().insert(part=','.join(corpo.keys()), body=corpo, media_body=midia).execute()

def fazer_upload_tiktok(arquivo, titulo):
    print(f"[*] Iniciando upload TikTok...")
    try:
        from tiktok_uploader.upload import upload_video
        upload_video(arquivo, description=titulo, cookies='tiktok_cookies.txt', browser='chrome')
        return True
    except Exception as e:
        print(f"[-] Erro TikTok: {e}")
        return False

def executar_pipeline_completo():
    ORIGINAL, HORIZ, VERT = "video_original.mp4", "corte_horizontal.mp4", "corte_vertical.mp4"
    try:
        URL_ALVO = obter_live_recente_mbl()
        VIDEO_ID = extrair_id_youtube(URL_ALVO)
        youtube_service = autenticar_youtube()
        
        if not os.path.exists(ORIGINAL):
            baixar_video(URL_ALVO, ORIGINAL)
        if not os.path.exists(ORIGINAL):
            raise RuntimeError(f"Arquivo original não existe após download: {ORIGINAL}")
        
        decisao = encontrar_melhor_corte(ORIGINAL, VIDEO_ID, "Política, MBL, Arthur do Val, Renan Santos")
        
        fazer_corte_video(ORIGINAL, HORIZ, VERT, decisao['corte_longo'], decisao['corte_curto'])
        
        print("\n--- DISTRIBUIÇÃO ---")
        fazer_upload_youtube(youtube_service, HORIZ, decisao['corte_longo']['titulo'], decisao['corte_longo']['descricao'], ["mbl"])
        fazer_upload_youtube(youtube_service, VERT, decisao['corte_curto']['titulo'], "#Shorts #MBL", decisao['corte_curto']['tags'], is_shorts=True)
        
        tiktok_ok = fazer_upload_tiktok(VERT, f"{decisao['corte_curto']['titulo']} #MBL")
        
        salvar_historico(VIDEO_ID, decisao['corte_longo']['inicio'], decisao['corte_longo']['fim'])

        # Limpeza
        for f in [ORIGINAL, HORIZ]: 
            if os.path.exists(f): os.remove(f)
        if tiktok_ok and os.path.exists(VERT): os.remove(VERT)
        
        print("\n[!] PROCESSO CONCLUÍDO COM ACELERAÇÃO DE GPU!")
    except Exception as e:
        print(f"[-] Erro: {e}")

if __name__ == '__main__':
    executar_pipeline_completo()