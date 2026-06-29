#!/usr/bin/env python3
"""
Guardiao de limites Oracle Cloud Always Free para automacao_cortes.py.

Uso no cron (3x/dia):
  python3 oracle_free_guard.py run -- python3 automacao_cortes.py

Comandos:
  check    Verifica se pode rodar (exit 0=ok, 1=bloqueado)
  cleanup  Remove temporarios e libera disco
  report   Mostra uso atual vs limites
  run      Executa comando apos validar limites e registrar uso
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DIRETORIO_BASE = Path(__file__).resolve().parent
ARQUIVO_ESTADO = DIRETORIO_BASE / "oracle_free_state.json"

# Limites conservadores para Always Free (ajuste via .env se necessario).
LIMITES = {
    "disco_livre_min_gb": float(os.getenv("ORACLE_FREE_DISK_MIN_GB", "8")),
    "disco_projeto_max_gb": float(os.getenv("ORACLE_FREE_PROJECT_MAX_GB", "12")),
    "disco_uso_max_pct": float(os.getenv("ORACLE_FREE_DISK_MAX_PCT", "85")),
    "ram_livre_min_mb": int(os.getenv("ORACLE_FREE_RAM_MIN_MB", "1500")),
    "runs_dia_max": int(os.getenv("ORACLE_FREE_RUNS_PER_DAY", "3")),
    "egress_mes_max_gb": float(os.getenv("ORACLE_FREE_EGRESS_MAX_GB", "800")),
    "egress_estimado_por_run_gb": float(os.getenv("ORACLE_FREE_EGRESS_PER_RUN_GB", "2.5")),
    "live_gravacao_max_minutos": int(os.getenv("ORACLE_FREE_LIVE_MAX_MIN", "60")),
}

ARQUIVOS_PRESERVAR = {
    "automacao_cortes.py",
    "oracle_free_guard.py",
    "intro_onca.mp4",
    "video_original.mp4",
    "client_secrets.json",
    "token.json",
    "historico_cortes.json",
    "pipeline_state.json",
    "pipeline_decisao.json",
    "cache_assuntos_politica.json",
    "oracle_free_state.json",
    "cookies.txt",
    "cookies_estaticos.txt",
    "tiktok_cookies.txt",
    ".env",
}

GLOBS_LIMPEZA = [
    "*.part",
    "*.part-*",
    "*.tmp.mp4",
    "temp_*.mp4",
    "temp_*.mp3",
    "test_*.mp4",
    "test_*.mp3",
    "corte_horizontal.mp4",
    "corte_vertical.mp4",
    "thumb_horizontal.jpg",
    "thumb_vertical.jpg",
    "temp_audio.mp3",
]


def agora_utc():
    return datetime.now(timezone.utc)


def chave_mes(dt=None):
    dt = dt or agora_utc()
    return dt.strftime("%Y-%m")


def chave_dia(dt=None):
    dt = dt or agora_utc()
    return dt.strftime("%Y-%m-%d")


def carregar_estado():
    if not ARQUIVO_ESTADO.exists():
        return {"mes": chave_mes(), "egress_bytes": 0, "runs_por_dia": {}, "historico_runs": []}
    try:
        with open(ARQUIVO_ESTADO, encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, json.JSONDecodeError):
        dados = {}
    dados.setdefault("runs_por_dia", {})
    dados.setdefault("historico_runs", [])
    if dados.get("mes") != chave_mes():
        dados["mes"] = chave_mes()
        dados["egress_bytes"] = 0
    return dados


def salvar_estado(dados):
    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)


def disco_livre_gb():
    uso = shutil.disk_usage(DIRETORIO_BASE)
    return uso.free / (1024 ** 3)


def disco_uso_pct():
    uso = shutil.disk_usage(DIRETORIO_BASE)
    return (uso.used / uso.total) * 100 if uso.total else 100.0


def tamanho_projeto_gb():
    total = 0
    for raiz, dirs, arquivos in os.walk(DIRETORIO_BASE):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules", ".venv", "venv"}]
        for nome in arquivos:
            caminho = Path(raiz) / nome
            try:
                total += caminho.stat().st_size
            except OSError:
                continue
    return total / (1024 ** 3)


def memoria_livre_mb():
    try:
        import psutil
        return int(psutil.virtual_memory().available / (1024 * 1024))
    except ImportError:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for linha in f:
                if linha.startswith("MemAvailable:"):
                    return int(linha.split()[1]) // 1024
    except OSError:
        return None
    return None


def runs_hoje(estado):
    return int(estado.get("runs_por_dia", {}).get(chave_dia(), 0))


def egress_mes_gb(estado):
    return int(estado.get("egress_bytes", 0)) / (1024 ** 3)


def limpar_temporarios():
    removidos = []
    for padrao in GLOBS_LIMPEZA:
        for caminho in DIRETORIO_BASE.glob(padrao):
            if not caminho.is_file():
                continue
            if caminho.name in ARQUIVOS_PRESERVAR:
                continue
            try:
                tamanho = caminho.stat().st_size
                caminho.unlink()
                removidos.append((caminho.name, tamanho))
            except OSError:
                continue
    return removidos


def aplicar_env_oracle_free():
    os.environ.setdefault("FORCE_CPU_ENCODE", "1")
    os.environ.setdefault(
        "LIVE_GRAVACAO_MAX_MINUTOS",
        str(LIMITES["live_gravacao_max_minutos"]),
    )
    os.environ.setdefault("MAX_CORTES_POR_VIDEO", "3")
    os.environ.setdefault("ASSUNTOS_CACHE_HORAS", "12")


def avaliar_limites(estado, simular_egress_gb=0.0):
    bloqueios = []
    avisos = []

    livre_gb = disco_livre_gb()
    if livre_gb < LIMITES["disco_livre_min_gb"]:
        bloqueios.append(
            f"disco livre {livre_gb:.1f} GB < minimo {LIMITES['disco_livre_min_gb']:.1f} GB"
        )

    uso_pct = disco_uso_pct()
    if sys.platform.startswith("linux") and uso_pct > LIMITES["disco_uso_max_pct"]:
        bloqueios.append(
            f"disco em {uso_pct:.0f}% > maximo {LIMITES['disco_uso_max_pct']:.0f}%"
        )

    projeto_gb = tamanho_projeto_gb()
    if projeto_gb > LIMITES["disco_projeto_max_gb"]:
        bloqueios.append(
            f"projeto {projeto_gb:.1f} GB > maximo {LIMITES['disco_projeto_max_gb']:.1f} GB"
        )

    ram_mb = memoria_livre_mb()
    if ram_mb is not None and ram_mb < LIMITES["ram_livre_min_mb"]:
        bloqueios.append(
            f"RAM livre {ram_mb} MB < minimo {LIMITES['ram_livre_min_mb']} MB"
        )
    elif ram_mb is None:
        avisos.append("RAM nao detectada (instale psutil para checagem)")

    if runs_hoje(estado) >= LIMITES["runs_dia_max"]:
        bloqueios.append(
            f"limite diario atingido ({runs_hoje(estado)}/{LIMITES['runs_dia_max']} runs)"
        )

    egress_total = egress_mes_gb(estado) + simular_egress_gb
    if egress_total > LIMITES["egress_mes_max_gb"]:
        bloqueios.append(
            f"egress estimado {egress_total:.1f} GB > maximo {LIMITES['egress_mes_max_gb']:.1f} GB/mes"
        )

    return bloqueios, avisos, {
        "disco_livre_gb": round(livre_gb, 2),
        "disco_uso_pct": round(uso_pct, 1),
        "projeto_gb": round(projeto_gb, 2),
        "ram_livre_mb": ram_mb,
        "runs_hoje": runs_hoje(estado),
        "egress_mes_gb": round(egress_mes_gb(estado), 2),
    }


def imprimir_relatorio(estado):
    bloqueios, avisos, metricas = avaliar_limites(estado)
    print("=== Oracle Always Free — relatorio ===")
    for chave, valor in metricas.items():
        print(f"  {chave}: {valor}")
    print("  limites:")
    for chave, valor in LIMITES.items():
        print(f"    {chave}: {valor}")
    if avisos:
        print("  avisos:")
        for aviso in avisos:
            print(f"    - {aviso}")
    if bloqueios:
        print("  bloqueios:")
        for bloqueio in bloqueios:
            print(f"    - {bloqueio}")
        return False
    print("  status: OK para executar")
    return True


def registrar_run(estado, sucesso, egress_gb=None):
    hoje = chave_dia()
    estado.setdefault("runs_por_dia", {})
    estado["runs_por_dia"][hoje] = int(estado["runs_por_dia"].get(hoje, 0)) + 1
    if egress_gb is None:
        egress_gb = LIMITES["egress_estimado_por_run_gb"] if sucesso else 0.5
    estado["egress_bytes"] = int(estado.get("egress_bytes", 0) + egress_gb * (1024 ** 3))
    estado.setdefault("historico_runs", []).append({
        "em": agora_utc().isoformat(),
        "sucesso": sucesso,
        "egress_gb": round(float(egress_gb), 2),
    })
    estado["historico_runs"] = estado["historico_runs"][-120:]
    salvar_estado(estado)


def cmd_cleanup():
    removidos = limpar_temporarios()
    if not removidos:
        print("[*] Nenhum temporario para remover.")
        return 0
    total_mb = sum(t for _, t in removidos) / (1024 * 1024)
    print(f"[*] Removidos {len(removidos)} arquivo(s) ({total_mb:.1f} MB).")
    for nome, _ in removidos[:10]:
        print(f"    - {nome}")
    if len(removidos) > 10:
        print(f"    ... e mais {len(removidos) - 10}")
    return 0


def cmd_check():
    estado = carregar_estado()
    ok = imprimir_relatorio(estado)
    return 0 if ok else 1


def cmd_report():
    return cmd_check()


def cmd_run(comando):
    if not comando:
        print("[-] Informe o comando apos '--', ex.: run -- python3 automacao_cortes.py")
        return 2

    aplicar_env_oracle_free()
    cmd_cleanup()

    estado = carregar_estado()
    bloqueios, _, _ = avaliar_limites(
        estado,
        simular_egress_gb=LIMITES["egress_estimado_por_run_gb"],
    )
    if bloqueios:
        print("[-] Pipeline bloqueado pelo guardiao Oracle Free:")
        for bloqueio in bloqueios:
            print(f"    - {bloqueio}")
        print("[!] Rode: python3 oracle_free_guard.py cleanup")
        return 1

    print("[*] Limites OK. Iniciando pipeline...", flush=True)
    print(f"[*] CPU encode: {os.environ.get('FORCE_CPU_ENCODE')}", flush=True)
    print(f"[*] Live max: {os.environ.get('LIVE_GRAVACAO_MAX_MINUTOS')} min", flush=True)

    inicio = agora_utc()
    proc = subprocess.run(comando, cwd=DIRETORIO_BASE)
    sucesso = proc.returncode == 0

    cmd_cleanup()
    registrar_run(estado, sucesso=sucesso)
    duracao = (agora_utc() - inicio).total_seconds()
    print(f"[*] Run {'OK' if sucesso else 'FALHOU'} em {int(duracao)}s", flush=True)
    return proc.returncode


def main():
    parser = argparse.ArgumentParser(description="Guardiao Oracle Always Free")
    parser.add_argument(
        "acao",
        nargs="?",
        default="report",
        choices=("check", "cleanup", "report", "run"),
    )
    parser.add_argument("resto", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.acao == "run":
        if args.resto[:1] == ["--"]:
            comando = args.resto[1:]
        else:
            comando = args.resto
        raise SystemExit(cmd_run(comando))
    if args.acao == "cleanup":
        raise SystemExit(cmd_cleanup())
    raise SystemExit(cmd_check())


if __name__ == "__main__":
    main()
