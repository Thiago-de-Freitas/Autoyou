import os
import json
from openai import OpenAI
from moviepy.editor import VideoFileClip

# Coloque sua chave de API aqui ou configure nas variáveis de ambiente
OPENAI_API_KEY = "sk-sua-chave-api-aqui"
cliente_ia = OpenAI(api_key=OPENAI_API_KEY)

def extrair_audio_para_ia(arquivo_video, arquivo_audio="temp_audio.mp3"):
    print("[*] Extraindo áudio para análise da IA (Modo Turbo ativado)...")
    
    # Em vez de usar o MoviePy que é lento, usamos o motor nativo do FFmpeg
    # Ele extrai o áudio de um vídeo de 2 horas em questão de segundos
    comando = f'ffmpeg -y -i "{arquivo_video}" -vn -acodec libmp3lame -ab 64k "{arquivo_audio}"'
    
    # Executa o comando em segundo plano de forma silenciosa
    subprocess.run(comando, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    return arquivo_audio

def encontrar_melhor_corte(arquivo_video):
    # 1. Prepara o áudio
    arquivo_audio = extrair_audio_para_ia(arquivo_video)
    
    # 2. Transcrição com Timestamps usando Whisper
    print("[*] Enviando áudio para o Whisper (Transcrição)...")
    with open(arquivo_audio, "rb") as arquivo:
        transcricao = cliente_ia.audio.transcriptions.create(
            file=arquivo,
            model="whisper-1",
            response_format="verbose_json",
            timestamp_granularities=["segment"]
        )
        
    # Formata a transcrição para o GPT entender quem falou o que e quando
    texto_mapeado = ""
    for segmento in transcricao.segments:
        inicio = round(segmento['start'], 1)
        fim = round(segmento['end'], 1)
        texto = segmento['text'].strip()
        texto_mapeado += f"[{inicio}s - {fim}s] {texto}\n"
        
    # 3. Análise Semântica com LLM
    print("[*] Solicitando análise de contexto ao GPT...")
    
    # O Prompt é o coração do seu negócio. Ele dita o que a IA vai procurar.
    prompt_sistema = """
    Você é um editor de vídeos viral para YouTube Shorts e TikTok.
    Sua tarefa é ler a transcrição de um vídeo (que possui marcações de tempo em segundos) 
    e identificar o bloco contínuo mais engajante, de alta tensão ou engraçado. 
    
    Por exemplo: Se for uma partida de Counter-Strike 2, procure por momentos de 'clutch', 
    comemorações explosivas, falhas engraçadas ou reações intensas. Se for um podcast, 
    procure por uma revelação impactante ou um debate acalorado.
    
    O trecho deve ter entre 30 e 60 segundos de duração e ter começo, meio e fim lógicos.
    
    Retorne APENAS um objeto JSON válido, sem markdown ou formatação extra, com esta estrutura:
    {
        "inicio_segundos": 12.5,
        "fim_segundos": 55.0,
        "titulo_sugerido": "Título chamativo e viral com emojis",
        "justificativa": "Explicação breve do motivo da escolha"
    }
    """
    
    resposta = cliente_ia.chat.completions.create(
        model="gpt-4o-mini", # Rápido, barato e inteligente o suficiente para esta tarefa
        messages=[
            {"role": "system", "content": prompt_sistema},
            {"role": "user", "content": f"Analise esta transcrição e encontre o melhor corte:\n\n{texto_mapeado}"}
        ],
        temperature=0.7
    )
    
    # 4. Processa o resultado
    conteudo_resposta = resposta.choices[0].message.content.strip()
    
    # Limpeza caso o GPT retorne blocos de código markdown indesejados
    if conteudo_resposta.startswith("```json"):
        conteudo_resposta = conteudo_resposta[7:-3]
        
    decisao_ia = json.loads(conteudo_resposta)
    
    # Limpa o arquivo de áudio temporário
    if os.path.exists(arquivo_audio):
        os.remove(arquivo_audio)
        
    print(f"[+] IA decidiu cortar de {decisao_ia['inicio_segundos']}s até {decisao_ia['fim_segundos']}s")
    print(f"[+] Título sugerido: {decisao_ia['titulo_sugerido']}")
    print(f"[+] Motivo: {decisao_ia['justificativa']}")
    
    return decisao_ia