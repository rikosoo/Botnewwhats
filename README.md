# Bot de WhatsApp — Dra. Ana Paula (Nefrologia, São Carlos)

Assistente virtual de WhatsApp para o consultório: tira dúvidas administrativas, agenda
consultas e retornos no Google Agenda, envia lembrete 3 dias antes (D-3) e mensagem de
acompanhamento 3 dias depois (D+3). A equipe pode assumir qualquer conversa pelo Chatwoot,
o que pausa o bot. **A IA nunca responde assuntos de saúde.**

👉 **Configuração completa: [PASSO_A_PASSO.md](PASSO_A_PASSO.md)**

```
Paciente (WhatsApp) ─► WhatsApp Cloud API ─► Chatwoot (inbox)  ◄── equipe atende aqui
                                                 │ webhook "Agent Bot"
                                                 ▼
                                           Bot (FastAPI)
                                            ├─ Router: administrativo | ação | clínico | urgente
                                            ├─ LLM (Gemini / Groq) com tool calling
                                            ├─ Google Agenda (service account)
                                            ├─ Postgres
                                            └─ Rotina diária 09:00 (D-3 / D+3 / limpeza LGPD)
```

## Router: três caminhos

Toda mensagem do paciente passa primeiro pelo router (`app/router.py`):

| Categoria | Exemplos | O que acontece |
|---|---|---|
| **Administrativo** | "Qual o valor?", "Atende Unimed?", "Onde fica?", "Tem horário sexta?" | IA responde. Só tem ferramentas de **leitura** (consultar horários, verificar retorno). |
| **Ação** | "Quero marcar sexta", "Preciso remarcar" | IA + ferramentas: `consultar_horarios` → paciente escolhe → confirma → `agendar_consulta` (revalida o horário antes de gravar). |
| **Clínico** | "Minha creatinina deu 3,2, é grave?", "Posso parar esse medicamento?" | **Sem IA.** Envia uma mensagem fixa, transfere para a equipe (conversa Aberta + etiqueta `atendimento-humano` + nota privada) e o bot para de responder. |
| **Urgente** | "Estou com muita dor", "Falta de ar" | Igual ao clínico, com etiqueta `urgente`, prioridade alta e (opcional) aviso no celular da Dra. **Quem responde é a equipe.** |

Como classifica:
1. **Regras de palavras-chave** (sintomas, exames, medicamentos, sinais de urgência).
   Rápidas, sem custo e determinísticas. Se detectarem algo clínico, a IA nem é consultada.
2. Se nenhuma regra clínica bater, um **LLM classificador** (sem ferramentas) decide a
   categoria. Na dúvida, a instrução é escolher "clínico". Se o classificador falhar,
   valem as regras.
3. Rede de segurança: mesmo no caminho administrativo, a persona proíbe falar de saúde e
   manda usar `chamar_humano` se algo clínico escapar.

As mensagens fixas ficam em `config/clinic.yaml` → `mensagens.clinico` / `mensagens.urgente`.

## Estrutura

```
app/
  main.py              rotas: /webhooks/chatwoot, /jobs/daily, /health
  config.py            variáveis de ambiente + config/clinic.yaml
  router.py            classificação administrativo / ação / clínico / urgente
  conversation.py      filtro do webhook, debounce, botões do lembrete, orquestração
  llm.py               cliente OpenAI-compatível + loop de tool calling (máx. 5)
  persona.py           system prompt
  tools.py             ferramentas (agenda, retorno, handoff, alerta)
  calendar_service.py  Google Agenda + cálculo de horários livres
  chatwoot.py          cliente da API do Chatwoot
  humanize.py          quebra em até 3 mensagens + delay de digitação
  scheduler.py         rotina diária (D-3, D+3, consultas realizadas, retenção)
  security.py          token do webhook/scheduler, rate limit, máscara de telefones
  db/                  modelos SQLAlchemy e sessão
alembic/               migrations
config/clinic.yaml     endereço, horários, valores, textos  ← [A DEFINIR]
deploy/aws/            docker-compose, Caddyfile, bootstrap, update
tests/                 testes (LLM mockado)
```

## Como funciona

**Handoff.** O bot só responde conversas com status **Pendente** no Chatwoot e sem a
etiqueta `atendimento-humano`. Quando a equipe assume (status **Aberta**), o bot para.
**Para devolver ao bot:** mude o status para **Pendente**. O bot remove a etiqueta sozinho
ao receber o evento; se não remover, tire a etiqueta manualmente.

**Agenda.** Tudo que existe na agenda é considerado ocupado, então a equipe pode bloquear
horários direto no Google Agenda. Os horários oferecidos vêm das janelas do `clinic.yaml`,
respeitando antecedência mínima (24h) e janela máxima (60 dias). Todo agendamento
revalida o horário no momento de gravar. Retornos só até 45 dias da última consulta.

**Lembretes (09:00).** D-3 → template `lembrete_consulta` com botões *Confirmar* /
*Remarcar*. "Confirmar" marca como confirmada e põe ✅ no título do evento; "Remarcar" faz
o bot oferecer novos horários. D+3 → template `pos_consulta`. A tabela
`lembretes_enviados` (única por consulta+tipo) garante envio único.

**Humanização.** Aguarda ~4s de silêncio para juntar mensagens seguidas, responde em até 3
mensagens com delay `min(1.5 + len/40, 6)`s, e o primeiro contato recebe apresentação e
aviso curto de privacidade.

## Infraestrutura (AWS)

Uma máquina **Lightsail de 4 GB em São Paulo (US$ 24/mês)** rodando tudo em Docker:
Caddy (HTTPS automático), bot, Chatwoot (rails + sidekiq), Postgres e Redis. O plano
inclui disco de 80 GB, IP fixo e tráfego.

Por que 4 GB: o Chatwoot sozinho usa ~2–2,5 GB. O bot usa ~150 MB.

Não há backup automático configurado (decisão do projeto). Se mudar de ideia, o Lightsail
tem **snapshots automáticos diários** na aba *Snapshots* da instância (cobrados à parte).

## LLM — decisão registrada

**Gemini, plano gratuito** (`gemini-2.5-flash-lite`).

- ⚠️ No plano gratuito, o Google pode usar o conteúdo das mensagens para melhorar os
  modelos. Mitigações: o bot não pede dados clínicos, e mensagens clínicas **não passam
  pela IA de resposta** (só pelo classificador, quando as regras não detectam antes).
- ⚠️ O plano gratuito tem limite diário de requisições (na casa de ~1.000/dia para o
  Flash-Lite em 2026; confira em <https://ai.google.dev/gemini-api/docs/rate-limits>).
  Cada mensagem do paciente usa de 1 a 4 requisições (router + resposta + ferramentas).
  Se estourar, o bot transfere a conversa para a equipe. Para sair do limite, basta ativar
  o faturamento no Google AI Studio (mesma chave) ou mudar para Groq
  (`LLM_PROVIDER=groq`, `LLM_MODEL=llama-3.3-70b-versatile`).

## Desenvolvimento local
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                      # LLM, Chatwoot e Google mockados; banco SQLite em memória
cp .env.example .env        # para rodar de verdade, precisa de Postgres
alembic upgrade head
uvicorn app.main:app --reload
```

## LGPD e segurança
- Coleta apenas nome e telefone; o bot não pede dados clínicos, CPF ou documentos.
- Histórico de mensagens apagado após `privacidade.retencao_mensagens_dias` (padrão 365).
- Telefones mascarados nos logs.
- Webhook validado por segredo na URL; `/jobs/daily` exige Bearer token.
- Rate limit: 20 mensagens/min por telefone.
- Segredos só no `.env` do servidor (fora do git).
- Servidor e banco no Brasil (São Paulo).

## Limitações conhecidas
- Rodar **uma** instância do bot (debounce, rate limit e trava de agendamento ficam em
  memória).
- Mensagens de áudio/imagem não são interpretadas; o bot pede para escrever.
- As regras de palavras-chave podem mandar para a equipe algumas mensagens que não são
  clínicas (ex.: "preciso levar exames?"). É o lado seguro do erro.
