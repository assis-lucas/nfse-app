"""Certificado A1: resolução dinâmica (variáveis de ambiente x enviado pela interface)."""
import base64
import glob
import os
import shutil
import stat

import pytest
from fastapi.testclient import TestClient

from app import sefin
from app.main import app
from conftest import CERT, TMP


def test_certificado_das_variaveis_de_ambiente():
    info = sefin.cert_info()
    assert info["ok"] and info["origem"] == "ambiente"
    assert info["origem_txt"] == "variáveis de ambiente"
    assert "12345678000195" in info["titular"]
    assert sefin.certificado() == (CERT, "1234", "ambiente")


def test_variaveis_lidas_a_cada_uso_sem_reiniciar(monkeypatch, gerar_pfx):
    outro = gerar_pfx(os.path.join(TMP, "outro-env.pfx"), "abcd", cn="OUTRA EMPRESA:11222333000181")
    monkeypatch.setenv("NFSE_CERT_PATH", outro)
    monkeypatch.setenv("NFSE_CERT_SENHA", "abcd")
    assert "OUTRA EMPRESA" in sefin.cert_info()["titular"]
    monkeypatch.setenv("NFSE_CERT_SENHA", "errada")
    assert not sefin.cert_info()["ok"]
    os.remove(outro)


def test_sem_certificado_no_mock_nao_assina(monkeypatch):
    monkeypatch.delenv("NFSE_CERT_PATH")
    monkeypatch.delenv("NFSE_CERT_SENHA")
    info = sefin.cert_info()
    assert not info["ok"] and info["origem"] == "" and "Config" in info["erro"]
    assert sefin.assinar("<DPS/>") == "<DPS/>"     # modo mock sem certificado: devolve sem assinar


# ---------------------------------------------------------------- upload pela interface
SENHA = "Senha-Distinta-9f3K"     # fictícia; nunca pode aparecer em resposta alguma


@pytest.fixture
def c(monkeypatch):
    """Cliente com o certificado "do ambiente" copiado para dentro do DATA_DIR (o upload apaga o
    antigo) e limpeza do certificado enviado no fim, para não afetar os outros testes."""
    antigo = os.path.join(TMP, "antigo-env.pfx")
    shutil.copy(CERT, antigo)
    monkeypatch.setenv("NFSE_CERT_PATH", antigo)
    with TestClient(app) as cliente:
        yield cliente
    for f in (sefin.CERT_ENVIADO, sefin.SENHA_ENVIADA, antigo):
        if os.path.exists(f):
            os.remove(f)


def _enviar(c, dados: bytes, senha=SENHA, nome="meu certificado.pfx"):
    return c.post("/config/certificado", files={"arquivo": (nome, dados, "application/x-pkcs12")},
                  data={"senha": senha})


def _sem_vazamento(r, senha=SENHA):
    """A senha não aparece no corpo, nos cabeçalhos nem no cookie de sessão (que é só assinado)."""
    for resp in [*r.history, r]:
        assert senha not in resp.text
        assert senha not in str(resp.headers)
        for valor in resp.cookies.values():
            payload = valor.split(".")[0]
            assert senha not in base64.b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8", "replace")


def _temporarios():
    return glob.glob(os.path.join(TMP, ".certificado-*"))


def _bytes(caminho):
    with open(caminho, "rb") as f:
        return f.read()


def test_upload_valido_troca_o_certificado(c, gerar_pfx):
    novo = gerar_pfx(os.path.join(TMP, "novo.pfx"), SENHA, cn="NOVA EMPRESA:12345678000195")
    r = _enviar(c, _bytes(novo), nome="../../fora-do-data.pfx")
    _sem_vazamento(r)
    assert r.status_code == 200 and "Certificado salvo" in r.text and "NOVA EMPRESA" in r.text
    info = sefin.cert_info()
    assert info["ok"] and "NOVA EMPRESA" in info["titular"] and info["origem"] == "interface"
    assert sefin.certificado() == (sefin.CERT_ENVIADO, SENHA, "interface")   # vale sem reiniciar
    assert _bytes(sefin.CERT_ENVIADO) == _bytes(novo)
    # nome enviado é ignorado: nada gravado fora do DATA_DIR
    assert not os.path.exists(os.path.join(TMP, "..", "..", "fora-do-data.pfx"))
    for f in (sefin.CERT_ENVIADO, sefin.SENHA_ENVIADA):
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    # o certificado antigo (NFSE_CERT_PATH dentro do DATA_DIR) foi apagado
    assert not os.path.exists(os.environ["NFSE_CERT_PATH"]) and "foi apagado" in r.text
    assert _temporarios() == []
    tela = c.get("/config")
    assert "enviado pela interface" in tela.text and "NOVA EMPRESA" in tela.text
    _sem_vazamento(tela)
    os.remove(novo)


def test_novo_upload_substitui_o_anterior(c, gerar_pfx):
    a = gerar_pfx(os.path.join(TMP, "a.pfx"), SENHA, cn="EMPRESA A:12345678000195")
    b = gerar_pfx(os.path.join(TMP, "b.pfx"), "outra-senha", cn="EMPRESA B:12345678000195")
    _enviar(c, _bytes(a))
    r = _enviar(c, _bytes(b), senha="outra-senha")
    _sem_vazamento(r, "outra-senha")
    assert "EMPRESA B" in sefin.cert_info()["titular"]
    assert _bytes(sefin.CERT_ENVIADO) == _bytes(b) and sefin.certificado()[1] == "outra-senha"
    assert sorted(os.path.basename(f) for f in glob.glob(os.path.join(TMP, "certificado*"))) == \
        ["certificado.pfx", "certificado.senha"]
    assert _temporarios() == []
    os.remove(a)
    os.remove(b)


def test_upload_tem_prioridade_sobre_variaveis(c, gerar_pfx, monkeypatch):
    novo = gerar_pfx(os.path.join(TMP, "prio.pfx"), SENHA, cn="PELA INTERFACE:12345678000195")
    _enviar(c, _bytes(novo))
    monkeypatch.setenv("NFSE_CERT_PATH", CERT)
    monkeypatch.setenv("NFSE_CERT_SENHA", "1234")
    assert sefin.cert_info()["origem"] == "interface"
    assert "PELA INTERFACE" in sefin.cert_info()["titular"]
    os.remove(novo)


def test_senha_errada_nao_grava_nada(c, gerar_pfx):
    bom = gerar_pfx(os.path.join(TMP, "bom.pfx"), SENHA)
    r = _enviar(c, _bytes(bom), senha="senha-errada-XYZ")
    _sem_vazamento(r, "senha-errada-XYZ")
    assert "senha errada" in r.text
    assert not os.path.exists(sefin.CERT_ENVIADO) and not os.path.exists(sefin.SENHA_ENVIADA)
    assert os.path.exists(os.environ["NFSE_CERT_PATH"])          # o antigo continua lá
    assert sefin.cert_info()["origem"] == "ambiente"
    # com um certificado já enviado: erro não mexe no que está gravado
    _enviar(c, _bytes(bom))
    antes = _bytes(sefin.CERT_ENVIADO), _bytes(sefin.SENHA_ENVIADA)
    outro = gerar_pfx(os.path.join(TMP, "outro.pfx"), "xyz", cn="OUTRA:11222333000181")
    r = _enviar(c, _bytes(outro), senha=SENHA)
    _sem_vazamento(r)
    assert (_bytes(sefin.CERT_ENVIADO), _bytes(sefin.SENHA_ENVIADA)) == antes
    assert _temporarios() == []
    os.remove(bom)
    os.remove(outro)


@pytest.mark.parametrize("dados", [b"isto nao e um certificado", b"\x30\x82\x01\x00" + b"\x00" * 300, b""])
def test_arquivo_lixo_e_rejeitado(c, dados):
    r = _enviar(c, dados, nome="lixo.pfx")
    _sem_vazamento(r)
    assert "flash-erro" in r.text and "Certificado salvo" not in r.text
    assert not os.path.exists(sefin.CERT_ENVIADO) and not os.path.exists(sefin.SENHA_ENVIADA)
    assert _temporarios() == []


def test_arquivo_enorme_e_rejeitado(c):
    r = _enviar(c, b"0" * (2 * 1024 * 1024))
    _sem_vazamento(r)
    assert "grande demais" in r.text
    assert not os.path.exists(sefin.CERT_ENVIADO)
    # a checagem interna também barra, mesmo sem Content-Length confiável
    with pytest.raises(sefin.CertificadoInvalido, match="grande demais"):
        sefin.salvar_certificado(b"0" * (sefin.TAMANHO_MAX_CERT + 1), SENHA)
    assert not os.path.exists(sefin.CERT_ENVIADO) and _temporarios() == []


@pytest.mark.parametrize("kwargs", [
    {"data": {"senha": SENHA}},                                         # sem arquivo
    {"data": {"senha": SENHA, "arquivo": "texto no lugar do arquivo"}},
    {"files": {"senha": (None, SENHA)}},                                # multipart sem arquivo
])
def test_formulario_malformado_nao_vaza_senha(c, kwargs):
    r = c.post("/config/certificado", **kwargs)
    _sem_vazamento(r)
    assert r.status_code < 500 and "Certificado salvo" not in r.text
    assert not os.path.exists(sefin.CERT_ENVIADO)


def test_upload_de_outro_site_e_recusado(c, gerar_pfx):
    novo = gerar_pfx(os.path.join(TMP, "csrf.pfx"), SENHA)
    r = c.post("/config/certificado", files={"arquivo": ("c.pfx", _bytes(novo))}, data={"senha": SENHA},
               headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and SENHA not in r.text
    assert not os.path.exists(sefin.CERT_ENVIADO)
    os.remove(novo)


def test_nao_apaga_certificado_fora_do_data_dir(c, gerar_pfx, monkeypatch, tmp_path):
    fora = gerar_pfx(str(tmp_path / "fora.pfx"), "1234")
    monkeypatch.setenv("NFSE_CERT_PATH", fora)
    novo = gerar_pfx(os.path.join(TMP, "dentro.pfx"), SENHA)
    r = _enviar(c, _bytes(novo))
    assert "Certificado salvo" in r.text and "foi apagado" not in r.text
    assert os.path.exists(fora)
    os.remove(novo)
