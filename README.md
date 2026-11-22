# 🎮 Monitor Gratuito de Preço do PS5 (GitHub Actions + Gmail)

Robô **100% gratuito** (zero dependências externas) que roda na nuvem pelo **GitHub Actions**, monitora preços do **PlayStation 5 (Com Leitor de Disco e Edição Digital)** e envia alertas por e-mail via **Gmail** com dois níveis de prioridade:

* **🔥 ALERTA:** ofertas até **R$ 3.500,00** (para você ir acompanhando boas oportunidades como R$ 3.300–3.500).
* **🚨 SUPER ALERTA:** ofertas até **R$ 3.000,00** (destaque urgente no assunto e no corpo do e-mail — e dispara na hora mesmo que o produto já tenha gerado um Alerta comum antes).

## O que ele monitora

* **Zoom** e **Buscapé** (reúnem automaticamente ofertas de Amazon, Magalu, Casas Bahia, Mercado Livre, Carrefour, Fast Shop, KaBuM!, etc.).
* **KaBuM!** (captura o preço real à vista/PIX direto do catálogo).
* **URLs extras** (opcional): adicione links diretos de outras lojas em `config.json`.

## Privacidade e Segurança

* **Nenhum dado pessoal fica no código ou nos logs:** seu e-mail e sua Senha de App ficam apenas nos **Secrets criptografados** do GitHub Actions e nunca são impressos no log.
* **Anti-falso-positivo:** ignora jogos, controles DualSense avulsos, leitores de disco avulsos, tampas, acessórios, usados e PS5 Pro (preço mínimo de R$ 1.800).
* **Anti-spam:** salva o histórico de ofertas avisadas via cache do GitHub Actions (`state.json`) e só reenvia se virar **Super Alerta**, cair pelo menos **R$ 50**, ou após **24 horas**.

## Frequência (5 minutos vs. Repositório Privado)

O workflow está configurado com o menor intervalo suportado pelo GitHub Actions: **`*/5 * * * *` (a cada 5 minutos)**.

* **Repositório Público:** minutos **ilimitados e 100% grátis** rodando a cada 5 minutos (com seus dados pessoais protegidos nos Secrets).
* **Repositório Privado:** cota gratuita de **2.000 min/mês**. Para ficar 100% grátis o mês todo em repo privado, altere o cron em `.github/workflows/monitor.yml` para `"*/30 * * * *"` (a cada 30 min = ~1.440 min/mês).

---

## Como ativar

### 1. Gerar uma Senha de App no Gmail
1. Ative a **Verificação em duas etapas** em https://myaccount.google.com/security.
2. Acesse https://myaccount.google.com/apppasswords, crie uma senha `Monitor PS5` e copie o código de **16 letras**.

### 2. Configurar os Secrets no GitHub
No seu repositório no GitHub, vá em **Settings → Secrets and variables → Actions → New repository secret** e adicione:

| Secret | Valor |
|---|---|
| `GMAIL_USER` | Seu e-mail do Gmail que vai enviar o alerta |
| `GMAIL_APP_PASSWORD` | A senha de app de 16 letras do passo 1 |
| `EMAIL_TO` | E-mail de destino (pode ser o mesmo de `GMAIL_USER`) |

### 3. Testar o recebimento do e-mail
1. Abra a aba **Actions** no GitHub → **Monitor de Preço PS5** → **Run workflow**.
2. Marque **`test_email = true`** e clique em **Run workflow**.

---

## Personalizando (`config.json`)

* `alert_price`: teto do **🔥 Alerta** (padrão: `3500.0`).
* `super_alert_price`: teto do **🚨 Super Alerta** (padrão: `3000.0`).
* `allowed_editions`: `["disc", "digital"]`.
* `custom_urls`: links extras de lojas para acompanhar em paralelo.

## Rodando localmente (opcional — zero dependências)

```bash
python3 monitor.py --dry-run
```
