"""Proteção contra requisições vindas de outros sites (CSRF) e contra DNS rebinding.

O app não tem login: quem alcança a porta usa o app. Isso não pode incluir uma página
maliciosa aberta no navegador de quem usa o app. Duas defesas, num middleware ASGI:

1. Allowlist de Host (todas as requisições, menos /healthz). Contra DNS rebinding: o atacante
   faz o domínio dele resolver para 127.0.0.1, e o navegador passa a tratar o app como "mesma
   origem" do site do atacante. O cabeçalho Host, porém, continua sendo o domínio do atacante
   (o navegador não deixa o JavaScript alterá-lo). Por isso só aceitamos os hosts de
   ALLOWED_HOSTS. Resposta: 400.

2. Origem em métodos que alteram estado (POST/PUT/PATCH/DELETE). Resposta: 403 quando
   - Sec-Fetch-Site é "cross-site"; ou
   - Origin (ou, na falta dele, Referer) existe e o host[:porta] não é o mesmo da requisição.
     "Origin: null" (iframe sandbox, file://) conta como outro site.
   Sem Origin nem Referer, deixamos passar: navegadores modernos sempre mandam Origin em POST,
   então isso só acontece com clientes que não são navegador (curl, scripts, TestClient), que
   não servem de vetor de CSRF.

Proxy reverso / Tailscale: o host público (ex.: vm.tailnet.ts.net) vem no Origin. Se o proxy
preserva o Host, ele chega igual no Host; se o proxy reescreve o Host (ex.: para
127.0.0.1:8000) e manda o original em X-Forwarded-Host, aceitamos o Origin quando ele bate com
o X-Forwarded-Host, desde que esse host também esteja em ALLOWED_HOSTS. Confiar no
X-Forwarded-Host aqui é seguro contra CSRF porque um site de terceiros não consegue mandar esse
cabeçalho (exige preflight CORS, que o app não autoriza), e o host dele não está na allowlist
de qualquer forma. Em todos os casos, basta colocar o hostname público em ALLOWED_HOSTS.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

log = logging.getLogger("nfse.seguranca")

METODOS_SEGUROS = {"GET", "HEAD", "OPTIONS", "TRACE"}
ISENTOS_DE_HOST = {"/healthz"}
HOSTS_PADRAO = "localhost,127.0.0.1,[::1]"
PORTAS_PADRAO = {"http": "80", "https": "443"}


def hosts_permitidos(valor: str | None = None) -> list[str]:
    """ALLOWED_HOSTS: lista separada por vírgula. Entradas que começam com "." aceitam
    subdomínios (".ts.net" aceita "vm.tailnet.ts.net"); "*" desliga a verificação."""
    if valor is None:
        valor = os.environ.get("ALLOWED_HOSTS") or HOSTS_PADRAO
    return [h.strip().lower() for h in valor.split(",") if h.strip()]


def nome_do_host(host: str) -> str:
    """"Exemplo.com:8000" → "exemplo.com"; "[::1]:8000" → "[::1]"."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1] if "]" in host else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def host_permitido(host: str, permitidos: list[str]) -> bool:
    nome = nome_do_host(host)
    if not nome:
        return False
    for p in permitidos:
        if p == "*" or nome == p:
            return True
        if p.startswith(".") and (nome.endswith(p) or nome == p[1:]):
            return True
    return False


def _normaliza_host(host: str) -> str:
    """Host[:porta] em minúsculas, sem porta padrão (o Host não diz o esquema)."""
    host = host.strip().lower()
    for porta in PORTAS_PADRAO.values():
        if host.endswith(":" + porta):
            return host[: -len(porta) - 1]
    return host


def _host_da_url(url: str) -> str | None:
    """Host[:porta] normalizado de um Origin/Referer, ou None se não der para extrair."""
    try:
        partes = urlsplit(url.strip())
    except ValueError:
        return None
    if partes.scheme not in PORTAS_PADRAO or not partes.netloc or "@" in partes.netloc:
        return None
    netloc = partes.netloc.lower()
    padrao = ":" + PORTAS_PADRAO[partes.scheme]
    return netloc[: -len(padrao)] if netloc.endswith(padrao) else netloc


class ProtecaoOrigem:
    def __init__(self, app, allowed_hosts: list[str] | None = None):
        self.app = app
        self.permitidos = allowed_hosts if allowed_hosts is not None else hosts_permitidos()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        h = Headers(scope=scope)
        host = h.get("host", "")
        if scope["path"] not in ISENTOS_DE_HOST and not host_permitido(host, self.permitidos):
            self._registra("host fora de ALLOWED_HOSTS", scope, h)
            nome = nome_do_host(host) or "(vazio)"
            resp = PlainTextResponse(
                f"Host não permitido: {nome}\n\nSe você acessa o app por este endereço "
                f"(Tailscale, proxy reverso…), inclua \"{nome}\" na variável ALLOWED_HOSTS "
                f"e reinicie o app.\n", status_code=400)
            return await resp(scope, receive, send)
        if scope["method"] not in METODOS_SEGUROS:
            motivo = self._motivo_cross_site(h)
            if motivo:
                self._registra(motivo, scope, h)
                resp = PlainTextResponse(
                    "Requisição recusada: veio de outro site.\n\nSe você enviou este formulário "
                    "pelo próprio app, recarregue a página e tente de novo.\n", status_code=403)
                return await resp(scope, receive, send)
        await self.app(scope, receive, send)

    def _motivo_cross_site(self, h: Headers) -> str | None:
        if h.get("sec-fetch-site", "").lower() == "cross-site":
            return "Sec-Fetch-Site: cross-site"
        origem = h.get("origin")
        cabecalho = "Origin"
        if origem is None:
            origem = h.get("referer")
            cabecalho = "Referer"
        if origem is None:
            return None           # cliente que não é navegador
        alvo = _host_da_url(origem)
        if alvo is None:
            return f"{cabecalho} inválido ou null"
        if alvo == _normaliza_host(h.get("host", "")):
            return None
        fwd = h.get("x-forwarded-host", "").split(",")[0].strip()
        if fwd and alvo == _normaliza_host(fwd) and host_permitido(fwd, self.permitidos):
            return None
        return f"{cabecalho} de outro host"

    @staticmethod
    def _registra(motivo: str, scope, h: Headers):
        log.warning("recusado (%s): %s %s host=%r origin=%r referer=%r x-forwarded-host=%r "
                    "sec-fetch-site=%r", motivo, scope["method"], scope["path"], h.get("host"),
                    h.get("origin"), h.get("referer"), h.get("x-forwarded-host"),
                    h.get("sec-fetch-site"))
