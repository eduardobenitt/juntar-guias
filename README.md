# Juntar Guias e Comprovantes

Aplicação web interna do **financeiro** que automatiza a montagem dos PDFs de guia + comprovante de pagamento.

Antes, cada comprovante precisava ser renomeado, localizado a guia correspondente e os dois PDFs unidos manualmente. Agora o usuário envia tudo de uma vez pelo navegador e recebe um ZIP pronto, com um relatório do que não foi possível casar.

---

## Como usar (financeiro)

1. Abra no navegador: **`http://<IP-do-servidor>:8000`**
2. No campo **1. Guias de pagamento**, selecione os PDFs das guias.
   - O nome do arquivo **precisa conter o número da SPF**, ex.: `SPF 280738.pdf`.
3. No campo **2. Comprovantes de pagamento**, selecione os PDFs dos comprovantes **com o nome original do banco** (não precisa renomear).
4. Clique em **Processar** e aguarde (lotes grandes podem levar alguns minutos).
5. A tela de resultado mostra:
   - quantos arquivos foram **juntados** e quantos ficaram **pendentes**;
   - o **motivo** de cada pendência;
   - como cada comprovante foi renomeado.
6. Clique em **Baixar ZIP**. Dentro dele:

   | Pasta / arquivo | Conteúdo |
   |---|---|
   | `juntados/` | Um PDF por guia: a guia seguida do(s) seu(s) comprovante(s) |
   | `pendentes/` | Guias sem comprovante e comprovantes sem guia, para tratar manualmente |
   | `relatorio.csv` | Lista de tudo o que aconteceu (abre direto no Excel) |

> O ZIP fica disponível para download por **24 horas**. Depois disso é apagado do servidor.

### Como o sistema casa guia e comprovante

| O comprovante tem... | Ele é renomeado para... | E casado com a guia pelo... |
|---|---|---|
| Linha digitável (47 ou 48 dígitos) | `<linha digitável>.pdf` | **Código de barras** da guia (lido do texto ou da imagem do PDF) |
| Número no campo "Descrição" | `SPF <número>-1.pdf` | **Número da SPF** no nome da guia |

### Pendências comuns e o que fazer

| Motivo | O que fazer |
|---|---|
| *Nenhum comprovante correspondente* | O comprovante dessa guia não foi enviado, ou está ilegível. Confira e reenvie. |
| *Nenhuma guia correspondente* | A guia não foi enviada, ou o código de barras/SPF não bate. |
| *Mais de uma guia possível* | Há guias duplicadas no lote. Remova a duplicada e reenvie. |
| *Sem linha digitável e sem número na 'Descrição'* | Comprovante em formato não reconhecido. Junte manualmente. |
| *Nome sem número de SPF* | Renomeie a guia para o padrão `SPF <número>.pdf`. |

---

## O que é necessário para rodar (servidor)

| Item | Para quê |
|---|---|
| **Windows** (Server ou 10/11) sempre ligado | Hospeda a aplicação |
| **[Python 3.11+](https://www.python.org/downloads/)** (marque *"Add python.exe to PATH"*) | Executa o código |
| **[Git](https://git-scm.com/download/win)** | Baixa e atualiza o código |
| **Porta 8000 liberada** no firewall | Permite o acesso pela rede interna |

Bibliotecas Python (instaladas automaticamente pelo `requirements.txt`):

| Biblioteca | Função |
|---|---|
| `flask` | Páginas web e upload dos arquivos |
| `waitress` | Servidor de produção (funciona bem no Windows) |
| `pypdfium2` | Leitura do texto dos PDFs (motor do Chrome) |
| `zxing-cpp` | Leitura do código de barras em guias escaneadas |
| `pypdf` | Junção dos PDFs |

### 1. Instalar

```powershell
git clone https://github.com/eduardobenitt/juntar-guias.git C:\apps\juntar_guias
cd C:\apps\juntar_guias
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 2. Liberar a porta (PowerShell como administrador)

```powershell
New-NetFirewallRule -DisplayName "Juntar Guias" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

### 3. Executar

```powershell
.\iniciar.bat
```

Teste no próprio servidor em `http://localhost:8000` e, de outro computador, em `http://<IP-do-servidor>:8000` (descubra o IP com `ipconfig`).

### 4. Iniciar automaticamente com o Windows

Agendador de Tarefas → **Criar Tarefa**:
- **Geral:** "Executar estando o usuário conectado ou não"
- **Disparadores:** "Ao inicializar o sistema"
- **Ações:** Iniciar programa → `C:\apps\juntar_guias\iniciar.bat`
- **Configurações:** desmarque "Interromper a tarefa se ela for executada por mais de..."

### Atualizar para uma nova versão

```powershell
cd C:\apps\juntar_guias
git pull
.venv\Scripts\pip install -r requirements.txt
```
Depois reinicie a tarefa no Agendador (ou o servidor).

---

## Estrutura do projeto

```
app.py          -> parte web (rotas, upload, download do ZIP)
nucleo.py       -> regras: identificar, renomear, casar e juntar os PDFs (não depende da web)
templates/      -> telas HTML
iniciar.bat     -> atalho para subir a aplicação
saida/          -> ZIPs gerados (criada automaticamente, apagados após 24h)
```

---

## Próximos passos

- [ ] **Integração com o ERP Jurídico CPJ para baixa das guias.** Após juntar guia + comprovante, registrar automaticamente o pagamento no CPJ (baixa da guia/despesa) e anexar o PDF juntado ao processo, eliminando a etapa manual de lançamento.
  - Levantar com o fornecedor do CPJ a forma de integração disponível (API, webservice, importação de arquivo ou acesso ao banco de dados).
  - Definir a chave de ligação entre a guia e o registro no CPJ (ex.: número da SPF).
  - Incluir no relatório o status da baixa (baixada / erro / pendente).
- [ ] Login de usuários (hoje qualquer pessoa na rede interna acessa).
- [ ] Histórico das execuções (quem processou, quando e o resultado).

---

## Observações de segurança

- Use **apenas na rede interna** (não há autenticação).
- **Nunca** adicione PDFs reais ao repositório: ele é público. O `.gitignore` já bloqueia `*.pdf`, `*.zip`, `arquivos_exemplo/` e `saida/`.
