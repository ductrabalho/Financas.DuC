"""
gerar_resumos.py
-----------------
Roda automaticamente (via GitHub Action) toda vez que um novo
index-V*.html é adicionado ao repositório.

O que faz:
  1. Lista todos os index-V*.html do repositório e ordena por versão.
  2. Pra cada versão que ainda não tem resumo em resumos.json,
     pega o texto dela e o da versão anterior, manda os dois pro
     Claude e pede um resumo curto (1-2 frases) da mudança.
  3. Salva tudo em resumos.json na raiz do repo.

O index.html só lê o resumos.json pronto — não chama IA nenhuma
em produção, então não tem custo nem risco de expor chave de API
no navegador de quem visita o site.

Usa a Groq (gratuito, sem cartão) pra gerar o texto. Precisa
criar uma chave em console.groq.com e colar como secret
GROQ_API_KEY no GitHub Actions.
"""

import json
import os
import re
import sys
import time
import urllib.request

from PIL import ImageFont

PASTA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARQUIVO_RESUMOS = os.path.join(PASTA, "resumos.json")
API_KEY = os.environ.get("GROQ_API_KEY")
MODELO = "llama-3.3-70b-versatile"

# Precisam bater com o CSS do index.html (.resumo). Se mudar o card lá,
# ajustar aqui também.
LARGURA_CARD_PX = 200
PADDING_LATERAL_PX = 12 * 2       # padding: 8px 12px -> 12px de cada lado
LARGURA_UTIL_PX = LARGURA_CARD_PX - PADDING_LATERAL_PX  # espaço real do texto
TAMANHO_FONTE_PX = 12
LIMITE_LINHAS = 3
MAX_TENTATIVAS = 4

# Fonte embutida no próprio Pillow (não depende de fonte instalada no
# servidor/runner do GitHub Actions — funciona igual em qualquer máquina).
_FONTE = ImageFont.load_default(size=TAMANHO_FONTE_PX)


def largura_px(texto):
    caixa = _FONTE.getbbox(texto)
    return caixa[2] - caixa[0]


def quebrar_em_linhas(texto, largura_max_px=LARGURA_UTIL_PX):
    """Simula o word-wrap do navegador: quebra o texto em linhas medindo
    a largura real (em pixels) de cada palavra, não a quantidade de
    caracteres — uma palavra cheia de 'i' cabe mais que uma cheia de 'W'."""
    palavras = texto.split()
    linhas = []
    linha_atual = ""
    for palavra in palavras:
        candidata = f"{linha_atual} {palavra}".strip()
        if largura_px(candidata) <= largura_max_px:
            linha_atual = candidata
        else:
            if linha_atual:
                linhas.append(linha_atual)
            linha_atual = palavra
    if linha_atual:
        linhas.append(linha_atual)
    return linhas


def cabe_no_card(texto):
    return len(quebrar_em_linhas(texto)) <= LIMITE_LINHAS

PADRAO_NOME = re.compile(r"^index-V([\d.]+)\.html$", re.IGNORECASE)


def listar_versoes():
    """Retorna [(numero_str, chave_ordenacao, caminho_arquivo), ...] ordenado."""
    versoes = []
    for nome in os.listdir(PASTA):
        m = PADRAO_NOME.match(nome)
        if not m:
            continue
        numero = m.group(1)
        chave = tuple(int(p) for p in numero.split("."))
        versoes.append((numero, chave, os.path.join(PASTA, nome)))
    versoes.sort(key=lambda v: v[1])
    return versoes


def carregar_resumos():
    if os.path.exists(ARQUIVO_RESUMOS):
        with open(ARQUIVO_RESUMOS, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_resumos(resumos):
    with open(ARQUIVO_RESUMOS, "w", encoding="utf-8") as f:
        json.dump(resumos, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")


def extrair_diff_relevante(texto_antigo, texto_novo, max_linhas=400):
    """Diff linha a linha, só o que mudou (contexto mínimo), pra não estourar
    o prompt em arquivos grandes."""
    import difflib

    linhas_antigas = texto_antigo.splitlines()
    linhas_novas = texto_novo.splitlines()
    diff = list(
        difflib.unified_diff(linhas_antigas, linhas_novas, lineterm="", n=1)
    )
    if len(diff) > max_linhas:
        diff = diff[:max_linhas] + ["... (diff truncado)"]
    return "\n".join(diff)


def _chamar_ia(prompt):
    corpo = json.dumps(
        {
            "model": MODELO,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=corpo,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "FinancasDuC-Action/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        dados = json.loads(resp.read().decode("utf-8"))

    return dados["choices"][0]["message"]["content"].strip()


def pedir_resumo_ia(numero, diff_texto, eh_primeira_versao):
    if not API_KEY:
        raise RuntimeError("GROQ_API_KEY não definida no ambiente.")

    if eh_primeira_versao:
        contexto = (
            "Este é o código-fonte (HTML/CSS/JS) da primeira versão de um "
            "app web chamado Finanças.DuC, um app de controle financeiro "
            "pessoal. Descreva em português informal o que esse app faz.\n\n"
            f"CÓDIGO:\n{diff_texto[:6000]}"
        )
    else:
        contexto = (
            "Aqui está o diff (formato unificado) entre duas versões de um "
            "app web chamado Finanças.DuC (controle financeiro pessoal). "
            "Resuma em português informal a mudança do PONTO DE VISTA DO "
            "USUÁRIO — o que ele vai perceber de diferente ao usar o app. "
            "Se a mudança for só técnica/interna sem efeito visível, diga "
            "isso bem curto (ex.: 'Ajustes internos, sem mudança "
            "perceptível').\n\n"
            f"DIFF:\n{diff_texto}"
        )

    instrucao_base = (
        f"{contexto}\n\n"
        "LINGUAGEM: escreva do jeito mais simples e cotidiano possível — "
        "como se estivesse explicando pra alguém leigo em informática, sem "
        "nenhum termo técnico. NUNCA use palavras como: refatorou, "
        "otimizou, backend, frontend, endpoint, renderização, z-index, "
        "CSS, JS, API, cache, deploy, bug (use 'erro' ou 'problema'), "
        "framework, componente, layout, ou qualquer jargão de "
        "programação. Fale do que a PESSOA vê ou sente ao usar o app "
        "(ex.: 'a tela abre mais rápido', 'o botão mudou de lugar', "
        "'agora ele lembra o que você digitou'), não do que o código faz "
        "por dentro.\n\n"
        "Responda só a frase, sem aspas, sem preâmbulo, sem ponto final "
        "no fim."
    )

    melhor_texto = None
    melhor_linhas = None
    prompt = instrucao_base

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        texto = _chamar_ia(prompt).strip().strip('"')
        linhas = quebrar_em_linhas(texto)

        # guarda o melhor resultado até agora (o que ocupa menos linhas)
        if melhor_linhas is None or len(linhas) < melhor_linhas:
            melhor_texto, melhor_linhas = texto, len(linhas)

        if len(linhas) <= LIMITE_LINHAS:
            return texto

        # não coube: manda de novo, agora dizendo exatamente quanto sobrou
        prompt = (
            f"{instrucao_base}\n\nVocê já respondeu isto, mas ficou grande "
            f"demais: \"{texto}\" — isso ocupa {len(linhas)} linhas no card "
            f"da tela, e o limite é {LIMITE_LINHAS} linhas (o card tem só "
            f"{LARGURA_UTIL_PX}px de largura útil, fonte {TAMANHO_FONTE_PX}px). "
            "Reescreva bem mais curto, cortando informação (não abreviação "
            "forçada), até caber."
        )

    # depois de todas as tentativas, usa o mais curto que conseguiu —
    # nunca corta o texto manualmente, só avisa no log se ainda não coube.
    if melhor_linhas > LIMITE_LINHAS:
        print(
            f"  [aviso] V{numero}: melhor tentativa ainda ocupa "
            f"{melhor_linhas} linhas (limite {LIMITE_LINHAS})."
        )
    return melhor_texto


PAUSA_ENTRE_CHAMADAS_SEG = 3


def main():
    versoes = listar_versoes()
    if not versoes:
        print("Nenhum index-V*.html encontrado.")
        return

    resumos = carregar_resumos()
    houve_mudanca = False

    for i, (numero, _chave, caminho) in enumerate(versoes):
        if numero in resumos:
            continue  # já tem resumo, pula

        with open(caminho, "r", encoding="utf-8", errors="replace") as f:
            texto_atual = f.read()

        if i == 0:
            resumo = pedir_resumo_ia(numero, texto_atual, eh_primeira_versao=True)
        else:
            caminho_anterior = versoes[i - 1][2]
            with open(caminho_anterior, "r", encoding="utf-8", errors="replace") as f:
                texto_anterior = f.read()
            diff_texto = extrair_diff_relevante(texto_anterior, texto_atual)
            if not diff_texto.strip():
                resumo = "Sem mudança perceptível."
            else:
                resumo = pedir_resumo_ia(numero, diff_texto, eh_primeira_versao=False)

        resumos[numero] = resumo
        houve_mudanca = True
        print(f"V{numero}: {resumo}")

        # salva a cada versão — se der 429 (limite da Groq) no meio,
        # o que já foi gerado não se perde, e a próxima execução do
        # Action continua de onde parou.
        salvar_resumos(resumos)
        time.sleep(PAUSA_ENTRE_CHAMADAS_SEG)

    if houve_mudanca:
        print(f"\nresumos.json atualizado ({len(resumos)} versões).")
    else:
        print("Nada novo pra resumir.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        sys.exit(1)
