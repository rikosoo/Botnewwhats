"""Router: classifica cada mensagem ANTES de qualquer resposta.

- administrativo  -> IA responde (valor, convênio, endereço, horários disponíveis)
- acao            -> IA + ferramentas (agendar, remarcar, cancelar)
- clinico         -> nenhuma resposta da IA; mensagem fixa + transferência para a equipe
- urgente         -> idem, com etiqueta "urgente" e prioridade alta

Primeiro passam regras de palavras-chave (determinísticas, sem custo). Se nenhuma regra
clínica bater, um LLM classifica (sem ferramentas, só devolve a categoria). Em caso de
dúvida o router escolhe o caminho mais seguro (humano).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

log = logging.getLogger(__name__)

ADMINISTRATIVO = "administrativo"
ACAO = "acao"
CLINICO = "clinico"
URGENTE = "urgente"
CATEGORIAS = (ADMINISTRATIVO, ACAO, CLINICO, URGENTE)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(text.lower().split())


def _rx(words: list[str]) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(words) + r")\b")


# Sinais de possível urgência (sintoma intenso / piora / situação grave).
URGENTE_RX = _rx([
    r"muita dor", r"dor (muito )?(forte|intensa|insuportavel)", r"falta de ar", r"nao consigo respirar",
    r"desmai\w*", r"convuls\w*", r"sangr\w*", r"sangue na urina", r"urina com sangue",
    r"nao (estou )?urin\w*", r"parou de urinar", r"inchad[oa] demais", r"muito inchad[oa]",
    r"dor no peito", r"confus[ao]\w*", r"piorou muito", r"emergencia", r"urgente", r"socorro",
    r"passando (muito )?mal", r"muito mal", r"febre alta",
])

# Assuntos clínicos: sintomas, exames, resultados, medicamentos, diagnósticos, dieta.
CLINICO_RX = _rx([
    r"dor(es)?", r"sintoma\w*", r"inchaco", r"inchad[oa]", r"febre", r"enjoo", r"vomit\w*", r"tontura",
    r"pressao (alta|baixa)", r"creatinina", r"ureia", r"potassio", r"sodio", r"hemoglobina", r"proteinuria",
    r"taxa de filtracao", r"tfg", r"resultado\w*", r"exame\w*", r"laudo\w*",
    r"remedio\w*", r"medicament\w*", r"medicacao", r"dose", r"comprimido\w*", r"parar de tomar",
    r"posso (parar|tomar|comer|beber)", r"diagnostic\w*", r"e grave", r"doenca", r"dialise", r"hemodialise",
    r"calculo renal", r"pedra no rim", r"infeccao", r"dieta", r"receita",
])

# Pedidos de ação na agenda.
ACAO_RX = _rx([
    r"marcar", r"agendar", r"agenda(mento)?", r"remarcar", r"desmarcar", r"cancelar", r"mudar (o )?horario",
    r"trocar (o )?horario", r"quero (uma )?consulta", r"quero (um )?retorno", r"confirmar",
])


def classify_by_rules(text: str) -> str | None:
    t = _norm(text)
    if URGENTE_RX.search(t):
        return URGENTE
    if CLINICO_RX.search(t):
        return CLINICO
    if ACAO_RX.search(t):
        return ACAO
    return None


ROUTER_PROMPT = """Você classifica mensagens de pacientes enviadas ao WhatsApp de um consultório de nefrologia.
Responda SOMENTE com JSON no formato {"categoria": "<valor>"}, onde <valor> é:
- "urgente": relato de sintoma intenso, piora súbita ou situação que pode ser grave.
- "clinico": qualquer pergunta ou relato sobre saúde, sintomas, exames, resultados, medicamentos,
  diagnóstico, dieta ou tratamento (mesmo que junto de outro assunto).
- "acao": pedido para marcar, remarcar, cancelar ou confirmar consulta/retorno.
- "administrativo": valores, convênio, endereço, formas de pagamento, horários disponíveis,
  saudações, agradecimentos e demais assuntos não clínicos.
Na dúvida entre clínico e outro, escolha "clinico"."""


class Router:
    def __init__(self, client: Any, model: str) -> None:
        self.client = client
        self.model = model

    async def classify(self, text: str, contexto: list[dict[str, str]] | None = None) -> str:
        by_rules = classify_by_rules(text)
        if by_rules in (URGENTE, CLINICO):
            return by_rules
        try:
            messages = [{"role": "system", "content": ROUTER_PROMPT}]
            if contexto:
                resumo = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in contexto[-4:])
                messages.append({"role": "user", "content": f"Contexto recente da conversa:\n{resumo}"})
            messages.append({"role": "user", "content": f"Mensagem a classificar:\n{text}"})
            resp = await self.client.chat.completions.create(model=self.model, messages=messages, temperature=0)
            raw = (resp.choices[0].message.content or "").strip()
            match = re.search(r"\{.*\}", raw, re.S)
            categoria = json.loads(match.group(0))["categoria"] if match else raw.strip('"').lower()
            if categoria in CATEGORIAS:
                return categoria
            log.warning("Router devolveu categoria desconhecida: %r", raw[:100])
        except Exception:
            log.exception("Falha no router LLM; usando apenas as regras")
        return by_rules or ADMINISTRATIVO
