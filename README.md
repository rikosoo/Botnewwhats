# Bot de WhatsApp — Dra. Ana Paula (Nefrologia, São Carlos)

Assistente virtual de WhatsApp para o consultório: tira dúvidas administrativas, agenda
consultas e retornos no Google Agenda, envia lembrete 3 dias antes (D-3) e mensagem de
acompanhamento 3 dias depois (D+3). A equipe pode assumir qualquer conversa pelo Chatwoot,
o que pausa o bot. **Não dá orientação clínica.**

```
Paciente (WhatsApp) ─► WhatsApp Cloud API ─► Chatwoot (inbox)  ◄── equipe atende aqui
                                                 │ webhook "Agent Bot"
                                                 ▼
                                           Bot (FastAPI)
                                            ├─ LLM (Gemini / Groq) com tool calling
                                            ├─ Google Agenda (service account)
                                            ├─ Postgres
                                            └─ Rotina diária 09:00 (D-3 / D+3 / limpeza LGPD)
```

Deploy: **AWS**, uma instância EC2 em São Paulo (`sa-east-1`) rodando tudo em Docker
(bot, Chatwoot, Postgres, Redis e Caddy com HTTPS automático).

## Estrutura

```
app/
  main.py              rotas: /webhooks/chatwoot, /jobs/daily, /health
  config.py            variáveis de ambiente + config/clinic.yaml
  conversation.py      filtro do webhook, debounce, botões do lembrete, orquestração
  llm.py               cliente OpenAI-compatível + loop de tool calling (máx. 5)
  persona.py           system prompt
  tools.py             ferramentas do LLM (agenda, retorno, handoff, alerta)
  calendar_service.py  Google Agenda + cálculo de horários livres
  chatwoot.py          cliente da API do Chatwoot
  humanize.py          quebra em até 3 mensagens + delay de digitação
  scheduler.py         rotina diária (D-3, D+3, consultas realizadas, retenção)
  security.py          token do webhook/scheduler, rate limit, máscara de telefones
  db/                  modelos SQLAlchemy e sessão
alembic/               migrations
config/clinic.yaml     endereço, horários, valores, textos  ← [A DEFINIR]
deploy/aws/            docker-compose, Caddyfile, bootstrap, backup
tests/                 testes (LLM mockado)
```

## Como funciona

**Handoff.** O bot só responde conversas com status **Pendente** no Chatwoot e sem a
etiqueta `atendimento-humano`. Quando a equipe assume (status **Aberta**), o bot para.
As ferramentas `chamar_humano`/`alerta_urgente` mudam a conversa para Aberta, adicionam a
etiqueta e deixam uma nota privada com o resumo. **Para devolver ao bot:** mude o status
para **Pendente** (o bot remove a etiqueta sozinho ao receber o evento; se não remover,
tire a etiqueta manualmente).

**Agenda.** Tudo que existe na agenda é considerado ocupado — a equipe pode bloquear
horários direto no Google Agenda. Horários oferecidos vêm das janelas do `clinic.yaml`,
respeitando antecedência mínima (24h) e janela máxima (60 dias). Todo agendamento
revalida o horário no momento de gravar. Retornos só até 45 dias da última consulta.

**Lembretes (09:00).** D-3 → template `lembrete_consulta` com botões *Confirmar* /
*Remarcar*. "Confirmar" marca como confirmada e põe ✅ no título do evento; "Remarcar" faz
o bot oferecer novos horários. D+3 → template `pos_consulta`. A tabela
`lembretes_enviados` (única por consulta+tipo) garante envio único. Consultas marcadas
direto na agenda recebem lembrete se o título começar com "Consulta"/"Retorno" e houver
telefone na descrição (ex.: `Tel: (16) 99999-8888`); sem telefone, fica só um aviso no log.

**Humanização.** Aguarda ~4s de silêncio para juntar mensagens seguidas, responde em até 3
mensagens com delay `min(1.5 + len/40, 6)`s, e o primeiro contato recebe apresentação e
aviso curto de privacidade.

---

## Setup passo a passo

### 1. Meta / WhatsApp Cloud API
1. Crie conta em <https://developers.facebook.com>, app do tipo **Business**, adicione o
   produto **WhatsApp**.
2. Registre o número do consultório (não pode estar ativo no app WhatsApp comum — se
   estiver, precisa ser removido antes).
3. No **Business Manager → Usuários do sistema**, crie um usuário do sistema com acesso ao
   app e gere um **token permanente** com as permissões `whatsapp_business_messaging` e
   `whatsapp_business_management`.
4. Anote: **Phone Number ID**, **WhatsApp Business Account ID** e o **token**.
5. Submeta os templates (categoria **Utilidade**, idioma **pt_BR**):
   - `lembrete_consulta` — *Olá, {{1}}. Lembramos da sua consulta com a Dra. Ana Paula no
     dia {{2}}, às {{3}}. Por gentileza, confirme sua presença.* — botões de resposta rápida
     **Confirmar** e **Remarcar**.
   - `pos_consulta` — *Olá, {{1}}. A Dra. Ana Paula gostaria de saber se está tudo bem após
     a sua consulta. Caso tenha alguma dúvida, estamos à disposição.*
   - (opcional) `alerta_urgente` — *Alerta: o(a) paciente {{1}} ({{2}}) relatou uma
     situação grave pelo WhatsApp. Verifique o Chatwoot.* — enviado ao celular da Dra.

### 2. AWS — criar a máquina
1. Console AWS → região **América do Sul (São Paulo) sa-east-1**.
2. **EC2 → Launch instance**: Ubuntu Server 24.04 LTS, tipo **t3.medium** (4 GB RAM —
   o Chatwoot precisa), disco **40 GB gp3**, crie um key pair para SSH.
3. **Security group**: liberar 80 e 443 para todos; 22 só para o seu IP.
4. **Elastic IP**: aloque e associe à instância.
5. **DNS** (no seu provedor de domínio): dois registros **A** apontando para o Elastic IP:
   `bot.seudominio.com.br` e `chat.seudominio.com.br`.
6. SSH na máquina e rode:
   ```bash
   git clone https://github.com/rikosoo/Botnewwhats.git /opt/botnefro   # repo privado: use um deploy key ou token
   sudo bash /opt/botnefro/deploy/aws/bootstrap.sh
   ```
   Saia e entre de novo no SSH (para o grupo `docker` valer).

Custo aproximado: t3.medium + 40 GB + IP ≈ **US$ 60/mês** em sa-east-1 (confira na
calculadora da AWS; Savings Plan de 1 ano reduz ~30%).

### 3. Configurar e subir
```bash
cd /opt/botnefro/deploy/aws
cp .env.example .env
# gere as senhas: openssl rand -hex 32  (POSTGRES_PASSWORD, BOT_DB_PASSWORD, REDIS_PASSWORD,
#   CHATWOOT_SECRET_KEY_BASE, WEBHOOK_SECRET, SCHEDULER_TOKEN)
nano .env
docker compose run --rm rails bundle exec rails db:chatwoot_prepare   # só na 1ª vez
docker compose up -d --build
docker compose ps
```
Acesse `https://chat.seudominio.com.br` e crie a conta de administrador.
(O bot vai reiniciar até o Chatwoot/Google estarem configurados — normal nesta etapa.)

### 4. Chatwoot
1. **Configurações → Caixas de entrada → Adicionar → WhatsApp → WhatsApp Cloud**: informe
   Phone Number ID, Business Account ID e o token permanente. Copie a **URL do webhook** e
   o **token de verificação** que o Chatwoot mostrar e cadastre-os no app da Meta
   (WhatsApp → Configuration → Webhook, assinando o campo `messages`).
2. Anote o **ID da inbox** (aparece na URL ao abrir a inbox) → `CHATWOOT_INBOX_ID`.
3. **Perfil → Token de acesso** do administrador → `CHATWOOT_API_TOKEN`.
4. Crie as etiquetas `atendimento-humano` e `urgente` (Configurações → Etiquetas).
5. **Configurações → Bots → Adicionar bot**: nome "Assistente", URL do webhook
   `https://bot.seudominio.com.br/webhooks/chatwoot?token=<WEBHOOK_SECRET>`.
   Copie o **token de acesso do bot** → `CHATWOOT_BOT_TOKEN`.
   (Se o seu Chatwoot não tiver a tela de bots, crie via console:
   `docker compose exec rails bundle exec rails runner "b=AgentBot.create!(name:'Assistente', outgoing_url:'https://bot.seudominio.com.br/webhooks/chatwoot?token=SEGREDO'); puts b.access_token.token"`.)
6. Na inbox → aba **Configurações do bot**, selecione o bot "Assistente".
7. Atualize o `.env` e rode `docker compose up -d bot`.

### 5. Google Agenda
1. <https://console.cloud.google.com> → novo projeto → **APIs e serviços → Ativar APIs** →
   **Google Calendar API**.
2. **IAM → Contas de serviço** → criar → aba **Chaves** → adicionar chave **JSON**.
3. No Google Agenda da clínica, crie a agenda **"Consultório Dra. Ana Paula"**, abra
   *Configurações e compartilhamento* → *Compartilhar com pessoas específicas* → e-mail da
   service account com **"Fazer alterações nos eventos"**.
4. Copie o **ID da agenda** (em *Integrar agenda*) → `GOOGLE_CALENDAR_ID`.
5. Converta a chave: `base64 -w0 chave.json` → `GOOGLE_SERVICE_ACCOUNT_JSON`.

### 6. LLM
- **Gemini**: chave em <https://aistudio.google.com/apikey> → `LLM_PROVIDER=gemini`,
  `LLM_API_KEY=...`.
- **Groq**: chave em <https://console.groq.com> → `LLM_PROVIDER=groq`,
  `LLM_MODEL=llama-3.3-70b-versatile`.

Trocar de provedor = mudar essas variáveis e `docker compose up -d bot`.

> **Decisão LGPD (registrar aqui):** no plano **gratuito** do Gemini, o Google pode usar
> os dados para melhorar modelos. Para produção, use o Gemini com **faturamento ativado**
> (plano pago, sem uso para treino) ou Groq. Decisão tomada: **[A DEFINIR]**.

### 7. Dados do consultório
Edite `config/clinic.yaml` (endereço, formas de pagamento, política de cancelamento,
dias/horários, duração do retorno) e rode `deploy/aws/update.sh` (ou
`docker compose up -d --build bot`). Enquanto um item estiver `[A DEFINIR]`, o bot
encaminha a pergunta para a equipe em vez de inventar.

### 8. Backup (recomendado)
1. Crie um bucket S3 (ex.: `botnefro-backups`, região sa-east-1, com regra de ciclo de
   vida apagando após 30 dias).
2. Crie uma IAM role para EC2 com `s3:PutObject` nesse bucket e anexe à instância.
3. `crontab -e`:
   `30 3 * * * BACKUP_BUCKET=botnefro-backups /opt/botnefro/deploy/aws/backup.sh >> /home/ubuntu/backup.log 2>&1`
4. Opcional: **EC2 → Lifecycle Manager** para snapshot diário do disco.

### 9. Teste (critérios de aceite)
Mande mensagens de um número pessoal e valide:
- [ ] Pergunta de valor/convênio → resposta correta e formal
- [ ] Agendamento completo em conversa livre, com evento criado no Google Agenda
- [ ] Horário bloqueado manualmente na agenda nunca é oferecido
- [ ] Retorno só é oferecido dentro de 45 dias da última consulta
- [ ] Pergunta clínica → handoff e o bot para de responder
- [ ] Equipe devolve a conversa (status Pendente) → bot volta a responder
- [ ] Relato de mal-estar grave → orientação + alerta urgente
- [ ] Lembrete D-3 enviado uma única vez; "Confirmar" atualiza status
- [ ] Mensagem D+3 enviada uma única vez
- [ ] Troca Gemini → Groq só mudando variáveis

Para disparar a rotina diária manualmente:
```bash
curl -X POST https://bot.seudominio.com.br/jobs/daily -H "Authorization: Bearer $SCHEDULER_TOKEN"
```

Logs: `docker compose logs -f bot`.

---

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
- Segredos só no `.env` da instância (fora do git).
- Dados ficam no Brasil (sa-east-1). Apenas o LLM processa mensagens fora (Google/Groq).

## Limitações conhecidas
- Rodar **uma** instância do bot (debounce, rate limit e trava de agendamento ficam em
  memória). Para escalar horizontalmente, mover isso para Redis.
- Mensagens de áudio/imagem não são interpretadas; o bot pede para escrever.
- Consultas marcadas direto na agenda só têm direito a retorno reconhecido pelo bot se
  tiverem passado pela rotina de lembrete (telefone na descrição).
