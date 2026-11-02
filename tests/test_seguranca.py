"""CSRF (Origin/Referer/Sec-Fetch-Site) e DNS rebinding (allowlist de Host)."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seguranca import host_permitido, hosts_permitidos, nome_do_host

ATAQUE = {"ambiente": "producao", "confirma_prod": "1"}   # o que um site malicioso tentaria
INOFENSIVO = {"serie": "1"}


@pytest.fixture
def c():
    with TestClient(app) as cliente:
        yield cliente


def _em_producao(c) -> bool:
    return 'value="producao" selected' in c.get("/config").text


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Origin": "http://testserver.evil.example"},
    {"Origin": "http://testserver:8001"},            # outra porta = outra origem
    {"Origin": "null"},                               # iframe sandbox / file://
    {"Referer": "http://evil.example/pagina"},        # sem Origin: Referer decide
    {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
    {"Host": "127.0.0.1:8000", "Origin": "http://testserver",
     "X-Forwarded-Host": "evil.example"},             # X-Forwarded-Host fora da allowlist
])
def test_post_de_outro_site_e_recusado(c, headers):
    r = c.post("/config", data=ATAQUE, headers=headers, follow_redirects=False)
    assert r.status_code == 403
    assert not _em_producao(c)


def test_outros_posts_tambem_sao_protegidos(c):
    for url in ("/notas/1/excluir", "/notas/1/cancelada", "/tomadores/salvar", "/importar",
                "/emitir/preview", "/emitir/confirmar"):
        assert c.post(url, headers={"Origin": "http://evil.example"}).status_code == 403


@pytest.mark.parametrize("headers", [
    {},                                                    # curl, scripts, TestClient
    {"Origin": "http://testserver"},
    {"Origin": "http://TestServer:80"},                    # porta padrão e maiúsculas
    {"Referer": "http://testserver/config"},
    {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    # proxy que reescreve o Host e manda o original em X-Forwarded-Host
    {"Host": "127.0.0.1:8000", "Origin": "http://testserver", "X-Forwarded-Host": "testserver"},
])
def test_post_do_proprio_app_passa(c, headers):
    r = c.post("/config", data=INOFENSIVO, headers=headers, follow_redirects=False)
    assert r.status_code == 303


def test_host_fora_da_allowlist_e_recusado(c):
    for metodo in ("get", "post"):
        r = getattr(c, metodo)("/", headers={"Host": "rebind.evil.example:8000"})
        assert r.status_code == 400
        assert "ALLOWED_HOSTS" in r.text and "rebind.evil.example" in r.text
    assert c.get("/notas.csv", headers={"Host": "rebind.evil.example"}).status_code == 400


def test_host_fora_da_allowlist_via_base_url():
    with TestClient(app, base_url="http://evil.example") as c:
        assert c.get("/").status_code == 400
        assert c.post("/config", data=ATAQUE, headers={"Origin": "http://evil.example"}).status_code == 400
        assert c.get("/healthz").status_code == 200      # healthcheck não depende do Host


def test_get_normal_continua_funcionando(c):
    for url in ("/", "/notas", "/tomadores", "/config", "/importar", "/healthz"):
        assert c.get(url, headers={"Host": "localhost:8000"}).status_code == 200


def test_regras_de_host():
    padrao = hosts_permitidos("localhost,127.0.0.1,[::1]")
    assert host_permitido("localhost:8000", padrao)
    assert host_permitido("127.0.0.1", padrao)
    assert host_permitido("[::1]:8000", padrao)
    assert not host_permitido("", padrao)
    assert not host_permitido("localhost.evil.example", padrao)
    assert not host_permitido("evil.example", padrao)
    ts = hosts_permitidos("localhost, VM.tailnet.ts.net ")
    assert host_permitido("vm.tailnet.ts.net", ts) and not host_permitido("outra.tailnet.ts.net", ts)
    sufixo = hosts_permitidos(".tailnet.ts.net")
    assert host_permitido("vm.tailnet.ts.net:443", sufixo) and not host_permitido("tailnet.ts.net.evil", sufixo)
    assert host_permitido("qualquer", hosts_permitidos("*"))
    assert nome_do_host("[::1]:8000") == "[::1]" and nome_do_host("Exemplo.COM:80") == "exemplo.com"
