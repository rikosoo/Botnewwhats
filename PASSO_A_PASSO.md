# Passo a passo: tudo o que precisa ser configurado

Siga na ordem. Cada etapa diz **o que anotar** e em qual variável do arquivo
`deploy/aws/.env` isso entra. Tempo total estimado: 2 a 3 horas, mais a espera da
aprovação dos templates da Meta (minutos a 1 dia).

> Senhas, tokens e chaves vão **somente** no `.env` do servidor. Não mande por chat nem
> coloque no GitHub.

## Resumo: todas as variáveis

| Variável | De onde vem | Etapa |
|---|---|---|
| `BOT_DOMAIN`, `CHATWOOT_DOMAIN` | Seu domínio (ex.: `bot.anapaulagiraldes.com.br`, `chat.anapaulagiraldes.com.br`) | 1 |
| `ACME_EMAIL` | Seu e-mail (avisos do certificado HTTPS) | 1 |
| `POSTGRES_PASSWORD`, `BOT_DB_PASSWORD`, `REDIS_PASSWORD`, `CHATWOOT_SECRET_KEY_BASE`, `WEBHOOK_SECRET`, `SCHEDULER_TOKEN` | Gerados por você no servidor (comando abaixo) | 3 |
| `LLM_API_KEY` | Google AI Studio | 4 |
| `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_CALENDAR_ID` | Google Cloud + Google Agenda | 5 |
| `CHATWOOT_API_TOKEN`, `CHATWOOT_INBOX_ID`, `CHATWOOT_BOT_TOKEN` | Painel do Chatwoot | 8 |
| `DOCTOR_ALERT_PHONE` | Celular da Dra. (opcional; deixe vazio para desligar) | 9 |

O resto já vem preenchido no `.env.example`.

---

## Etapa 1 — Domínio
Vamos usar o domínio do site, **anapaulagiraldes.com.br**, com dois subdomínios novos. O
site continua funcionando como está.
- `bot.anapaulagiraldes.com.br` → o bot
- `chat.anapaulagiraldes.com.br` → o painel do Chatwoot onde a equipe atende

Você vai precisar do acesso ao painel onde o DNS do domínio é gerenciado: Registro.br,
ou a empresa que hospeda o site (Hostinger, Locaweb, Wix etc.). Se foi uma agência que
fez o site, peça a ela para criar os dois registros da etapa 2.

## Etapa 2 — Servidor na AWS (Lightsail)
1. Acesse <https://lightsail.aws.amazon.com> (mesma conta da AWS).
2. **Create instance**:
   - Região: **São Paulo (sa-east-1)**
   - Plataforma: **Linux/Unix** → Blueprint: **OS Only → Ubuntu 24.04 LTS**
   - Plano: **US$ 24/mês — 4 GB RAM, 2 vCPUs, 80 GB SSD**
     (menos que 4 GB não aguenta o Chatwoot)
   - Nome: `botnefro` → **Create instance**
3. Aba **Networking** da instância:
   - **Attach static IP** → crie e associe um IP fixo (grátis enquanto estiver associado).
     **Anote o IP.**
   - Em **IPv4 Firewall**, adicione a regra **HTTPS (443)**. As portas 22 e 80 já vêm abertas.
4. No painel DNS do seu domínio, crie dois registros **A** apontando para o IP fixo:
   - `bot` → IP
   - `chat` → IP
5. Na instância, clique em **Connect using SSH** (abre um terminal no navegador) e rode:
   ```bash
   sudo git clone https://github.com/rikosoo/Botnewwhats.git /opt/botnefro
   sudo bash /opt/botnefro/deploy/aws/bootstrap.sh
   ```
   Como o repositório é privado, o `git clone` pede usuário e senha. Use seu usuário do
   GitHub e, como senha, um **token** criado em GitHub → Settings → Developer settings →
   Fine-grained tokens (acesso de leitura só a este repositório).
6. Feche e abra o terminal SSH de novo.

## Etapa 3 — Criar o `.env` e gerar as senhas
```bash
cd /opt/botnefro/deploy/aws
cp .env.example .env
for v in POSTGRES_PASSWORD BOT_DB_PASSWORD REDIS_PASSWORD CHATWOOT_SECRET_KEY_BASE WEBHOOK_SECRET SCHEDULER_TOKEN; do
  sed -i "s|^$v=.*|$v=$(openssl rand -hex 32)|" .env
done
nano .env
```
No `nano`, preencha `BOT_DOMAIN`, `CHATWOOT_DOMAIN` e `ACME_EMAIL`.
Salve com `Ctrl+O`, `Enter` e saia com `Ctrl+X`.

## Etapa 4 — Chave do Gemini (gratuita)
1. Acesse <https://aistudio.google.com/apikey> com uma conta Google.
2. **Create API key** → copie.
3. No `.env`: `LLM_API_KEY=<chave>`. Mantenha `LLM_PROVIDER=gemini` e
   `LLM_MODEL=gemini-2.5-flash-lite`.

## Etapa 5 — Google Agenda
**5.1 Service account (a "conta robô" que mexe na agenda)**
1. <https://console.cloud.google.com> → crie um projeto (ex.: `bot-consultorio`).
2. Menu **APIs e serviços → Biblioteca** → busque **Google Calendar API** → **Ativar**.
3. Menu **IAM e administrador → Contas de serviço → Criar conta de serviço**
   (nome: `bot-agenda`). Pule as permissões opcionais e conclua.
4. Abra a conta criada → aba **Chaves → Adicionar chave → Criar nova chave → JSON**.
   Um arquivo `.json` é baixado.
5. **Anote o e-mail da service account** (algo como
   `bot-agenda@bot-consultorio.iam.gserviceaccount.com`).

**5.2 Agenda do consultório**
1. No Google Agenda (conta da clínica): **Outras agendas → + → Criar nova agenda** →
   nome **"Consultório Dra. Ana Paula"**, fuso **(GMT-03:00) São Paulo**.
2. Configurações dessa agenda → **Compartilhar com pessoas específicas** → adicione o
   e-mail da service account com a permissão **"Fazer alterações nos eventos"**.
3. Mais abaixo, em **Integrar agenda**, copie o **ID da agenda**
   → `GOOGLE_CALENDAR_ID=`.

**5.3 Colocar a chave no servidor**
No seu computador, abra o `.json` baixado num editor de texto e copie todo o conteúdo.
No servidor:
```bash
nano /tmp/chave.json        # cole o conteúdo, salve
sed -i "s|^GOOGLE_SERVICE_ACCOUNT_JSON=.*|GOOGLE_SERVICE_ACCOUNT_JSON=$(base64 -w0 /tmp/chave.json)|" /opt/botnefro/deploy/aws/.env
rm /tmp/chave.json
```

A equipe continua usando essa agenda normalmente: tudo que estiver nela conta como
ocupado. Para consultas marcadas à mão receberem lembrete, use o título
`Consulta — Nome` (ou `Retorno — Nome`) e escreva o telefone na descrição.

## Etapa 6 — WhatsApp (Meta)

> **Nada da Meta precisa ser enviado para mim nem vai no `.env` do bot.** Os três dados
> abaixo (Phone Number ID, Business Account ID e token) são digitados **só no Chatwoot**
> (etapa 8). Quem conversa com o WhatsApp é o Chatwoot, não o bot.

**O número do consultório hoje está no app WhatsApp Business.** A API oficial não
funciona com o número ativo no app ao mesmo tempo. A exceção seria a "coexistência", mas
ela exige ser *Tech Provider* da Meta e não vale a pena aqui. Por isso o caminho é:

- **6A.** Montar e testar tudo com o **número de teste gratuito da Meta**. Nesse tempo o
  consultório segue usando o app normalmente.
- **6B.** Com tudo funcionando, **migrar o número real** para a API. A partir daí a equipe
  atende pelo Chatwoot (tem app para celular), não mais pelo WhatsApp Business.

### 6A — App da Meta e número de teste
1. Acesse <https://business.facebook.com> e confira se a clínica tem um **Business
   Manager** (Portfólio empresarial). Se não tiver, crie.
2. Acesse <https://developers.facebook.com> → **Meus apps → Criar app** → caso de uso
   **Outro** → tipo **Empresa (Business)** → vincule ao Business Manager da clínica.
3. No painel do app, adicione o produto **WhatsApp** → **Configuração da API**.
   A Meta cria automaticamente um **número de teste**. Nessa tela, **anote**:
   - **Identificação do número de telefone** (Phone Number ID)
   - **Identificação da conta do WhatsApp Business** (WhatsApp Business Account ID)
4. Ainda nessa tela, em **Para**, cadastre o seu celular pessoal (até 5 números). Só
   esses números conseguem conversar com o número de teste.
5. **Token permanente** (o token da tela de configuração expira em 24h, não use):
   <https://business.facebook.com> → **Configurações → Usuários → Usuários do sistema →
   Adicionar** → nome `chatwoot`, função **Administrador** → **Atribuir ativos** → **Apps**
   → selecione o app → **Controle total** → **Gerar novo token** → escolha o app, validade
   **Nunca**, permissões `whatsapp_business_messaging` e `whatsapp_business_management`
   → **copie e guarde o token** (ele só aparece uma vez).
6. Siga para as etapas 7 a 11 usando esse número de teste. Os templates (item 7 abaixo)
   podem ser criados já nessa conta.

### 6B — Migrar o número real (depois que o teste estiver ok)
1. No celular do consultório, faça backup das conversas se quiser guardar o histórico
   (WhatsApp Business → Configurações → Conversas → Backup). Ele **não** vai para o
   Chatwoot.
2. Anote a foto, a descrição e o horário do perfil comercial. Eles serão cadastrados de
   novo na Meta.
3. No app WhatsApp Business: **Configurações → Conta → Apagar conta** (excluir minha conta).
   O número fica livre para a API. Esse passo é irreversível para o app.
4. Em <https://business.facebook.com> → **WhatsApp Manager → Números de telefone →
   Adicionar número de telefone**: informe o nome de exibição (ex.: "Dra. Ana Paula
   Giraldes"), a categoria (Saúde) e o número. Valide pelo código recebido por **SMS ou
   ligação**. O chip precisa estar no celular.
5. **Anote o novo Phone Number ID** (em Configuração da API, selecione o número real).
6. Em **WhatsApp Manager → Visão geral**, adicione uma **forma de pagamento** (cartão). A
   Meta cobra por template enviado (lembretes). Respostas dentro de 24h da mensagem do
   paciente são gratuitas.
7. No Chatwoot, crie uma **nova caixa de entrada** com o número real (etapa 8, itens 1 a 3
   e 7), troque `CHATWOOT_INBOX_ID` no `.env` e rode `docker compose up -d bot`.
8. Recomendado: **verificar a empresa** no Business Manager (Central de Segurança →
   Verificação da empresa, com CNPJ). Aumenta o limite de conversas iniciadas pela clínica
   e ajuda a aprovar o nome de exibição.

### Templates
Em **WhatsApp Manager → Modelos de mensagem → Criar modelo**, crie estes,
   com categoria **Utilidade** e idioma **Português (BR)**:

   **`lembrete_consulta`**
   > Olá, {{1}}. Lembramos da sua consulta com a Dra. Ana Paula no dia {{2}}, às {{3}}. Por gentileza, confirme sua presença.

   Botões → **Resposta rápida**: `Confirmar` e `Remarcar` (exatamente assim).

   **`pos_consulta`**
   > Olá, {{1}}. A Dra. Ana Paula gostaria de saber se está tudo bem após a sua consulta. Caso tenha alguma dúvida, estamos à disposição.

   **`alerta_urgente`** (só se for usar o aviso no celular da Dra.)
   > Alerta: o(a) paciente {{1}} ({{2}}) enviou uma mensagem com possível urgência. Verifique o Chatwoot.

   A Meta pede exemplos para as variáveis {{1}}, {{2}} e {{3}}: use `Maria Silva`,
   `10/10/2026` e `14:00`.

## Etapa 7 — Subir tudo
```bash
cd /opt/botnefro/deploy/aws
docker compose run --rm rails bundle exec rails db:chatwoot_prepare
docker compose up -d --build
docker compose ps
```
Abra `https://chat.anapaulagiraldes.com.br` e crie a conta de administrador do Chatwoot.
O container `bot` vai reiniciar algumas vezes até a etapa 8 estar concluída. É normal.

## Etapa 8 — Chatwoot
1. **Configurações → Caixas de entrada → Adicionar caixa de entrada → WhatsApp**,
   provedor **WhatsApp Cloud**. Preencha o número, o **Phone Number ID**, o **Business
   Account ID** e o **token permanente** da etapa 6.
2. Ao terminar, o Chatwoot mostra uma **URL de webhook** e um **token de verificação**.
   No app da Meta: **WhatsApp → Configuração → Webhook → Editar**, cole os dois, salve e
   em **Campos do webhook** assine `messages`.
3. Abra a caixa de entrada criada. O número no fim da URL do navegador
   (`.../inboxes/3/...`) → `CHATWOOT_INBOX_ID=3`.
4. Clique na sua foto → **Configurações do perfil** → **Token de acesso**
   → `CHATWOOT_API_TOKEN=`.
5. **Configurações → Etiquetas**: crie `atendimento-humano` e `urgente`.
6. Crie o bot. Descubra o seu `WEBHOOK_SECRET` com `grep WEBHOOK_SECRET .env` e rode no
   servidor (troque os dois valores):
   ```bash
   docker compose exec rails bundle exec rails runner "b=AgentBot.create!(name:'Assistente', outgoing_url:'https://bot.anapaulagiraldes.com.br/webhooks/chatwoot?token=COLE_O_WEBHOOK_SECRET'); puts b.access_token.token"
   ```
   O comando imprime um token → `CHATWOOT_BOT_TOKEN=`.
   (Se o seu Chatwoot mostrar **Configurações → Bots**, dá para criar por lá também.)
7. Volte à caixa de entrada → aba **Bot** (ou **Configurações do bot**) → selecione
   **Assistente** → salvar.
8. **Configurações → Agentes**: convide a equipe (secretária, a Dra.).

## Etapa 9 — Finalizar
```bash
nano .env      # confira CHATWOOT_API_TOKEN, CHATWOOT_INBOX_ID, CHATWOOT_BOT_TOKEN
               # opcional: DOCTOR_ALERT_PHONE=+5516999999999
docker compose up -d bot
docker compose logs -f bot     # deve aparecer "Application startup complete"
```

## Etapa 10 — Dados do consultório
Já estão no `config/clinic.yaml`: PIX ou dinheiro, cancelamento com 10 dias de
antecedência, atendimento às terças das 8h às 16h, consulta de 1h e retorno de 45 min.
**Falta o endereço.** Para mudar qualquer dado, edite o arquivo (ou me peça) e rode
`bash /opt/botnefro/deploy/aws/update.sh`.

## Etapa 11 — Testar
De um celular pessoal, mande mensagens para o número do consultório:
- [ ] "Qual o valor da consulta?" / "Atende Unimed?" → resposta correta e formal
- [ ] "Quero marcar sexta" → oferece horários → escolher → evento criado na agenda
- [ ] Bloquear um horário na agenda → ele não é mais oferecido
- [ ] "Minha creatinina deu 3,2, é grave?" → mensagem fixa, conversa fica **Aberta** no
      Chatwoot com a etiqueta `atendimento-humano` e o bot para de responder
- [ ] No Chatwoot, mudar a conversa para **Pendente** → o bot volta a responder
- [ ] "Estou com muita dor" → mensagem fixa, etiqueta `urgente` e prioridade alta
- [ ] Rodar a rotina de lembretes na hora, sem esperar as 09:00:
  ```bash
  curl -X POST https://bot.anapaulagiraldes.com.br/jobs/daily -H "Authorization: Bearer $(grep SCHEDULER_TOKEN .env | cut -d= -f2)"
  ```

## Dia a dia
- **Equipe assume uma conversa:** responde normalmente pelo Chatwoot (a conversa fica Aberta).
- **Devolver ao bot:** mude o status para **Pendente**. Se a etiqueta `atendimento-humano`
  não sair sozinha, remova-a.
- **Atualizar o bot:** `bash /opt/botnefro/deploy/aws/update.sh`
- **Ver logs:** `cd /opt/botnefro/deploy/aws && docker compose logs -f bot`
