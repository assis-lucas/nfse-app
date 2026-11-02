# NFS-e — emissor próprio (Sistema Nacional)

App web simples para emitir NFS-e pela **API oficial do SEFIN Nacional** com o seu certificado A1:
cadastro de tomadores com "molde" fiscal, emissão em 2 cliques, histórico, totais por mês,
RBT12, CSV, XML e PDF (DANFSe).

Stack: FastAPI + SQLite + Jinja + Pico.css. A DPS é montada por `app/dps.py` (cobre tomador
estrangeiro, exportação e comércio exterior, que as libs prontas não cobrem) e validada contra o
XSD oficial 1.01. A assinatura XMLDSIG e o transporte mTLS vêm da `PyNFSeNacionalGT`.

## Primeiro uso (ordem importa)

1. **Importe o XML de uma nota que você já emitiu** (Emissor Nacional → Notas Emitidas → baixar XML)
   em *Tomadores → Importar de XML*. Isso cria o tomador com exatamente os códigos, a tributação e o
   grupo de comércio exterior da nota que já foi aceita, e preenche sua empresa.
2. Em *Config*, confira CNPJ, município e regime. O ambiente começa em **Homologação**.
3. Emita uma nota de teste. Ela vai para a *produção restrita* (sem validade jurídica).
   Se vier rejeição, a mensagem do SEFIN (código E0xxx) aparece na tela; ajuste o molde do tomador.
4. Quando o teste passar, mude para **Produção** em *Config* (exige marcar a confirmação).

A partir daí, cada mês é: botão "+ Nota para X" → valor → revisar → emitir.

## Segurança

- A emissão tem uma tela de revisão, com token de uso único: dar F5 ou clicar duas vezes não gera duas notas.
- Antes de enviar, o app consulta se aquela DPS já virou nota (`HEAD /dps/{id}`). Se um envio
  anterior caiu no meio, ele recupera a nota em vez de duplicar.
- O certificado e a senha ficam em variáveis de ambiente, não no banco.
- **O app não tem login.** Quem alcançar a porta consegue emitir notas com o seu certificado.
  Por isso o `docker-compose.yml` publica só em `127.0.0.1`. **Nunca exponha a porta na internet.**
  Para acessar de outro aparelho, use uma rede privada (o jeito mais simples é o Tailscale, abaixo)
  ou coloque um proxy reverso com autenticação na frente.

## Rodar local

```bash
cp .env.example .env    # preencha
mkdir -p data && cp ~/certificado.pfx data/
docker compose up -d --build     # http://127.0.0.1:8000
```

Sem Docker: `pip install -r requirements.txt && uvicorn app.main:app` (use as mesmas variáveis).
Testes: `pip install pytest httpx && pytest -q` (rodam em modo mock, sem tocar no governo).

## Deploy numa VM Linux

```bash
# na VM (Debian/Ubuntu)
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
git clone <seu-repo> nfse && cd nfse      # ou scp do zip
cp .env.example .env && nano .env
mkdir -p data && chmod 700 data           # copie o .pfx para data/ via scp
sudo docker compose up -d --build

# acesso privado pelo celular/notebook, sem abrir porta:
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
sudo tailscale serve --bg 8000            # https://<nome-da-vm>.<tailnet>.ts.net
```

Uma VM pequena (1 GB de RAM) dá conta com folga. Faça backup de `data/nfse.db` de vez em quando.
Se o SEFIN recusar conexão vinda de IP fora do Brasil, use uma VM hospedada no Brasil
com os mesmos comandos.

## Estrutura

```
app/dps.py        montagem da DPS (leiautes 20260209 e NT 009) + validação XSD
app/importer.py   lê XML de NFS-e/DPS → tomador, molde e nota
app/sefin.py      assinatura, envio, idempotência, modo mock, leitura do certificado
app/main.py       rotas/telas
app/db.py         modelos (valores em centavos)
```

## Limitações conhecidas

- Cancelamento ainda é feito no Emissor Nacional; depois é só marcar como cancelada no app.
- Só um prestador (a sua empresa). Para vários CNPJs, dá para rodar uma instância por empresa.
- A validação XSD local só cobre o leiaute de produção. No NT 009 (homologação), quem valida é o SEFIN.
