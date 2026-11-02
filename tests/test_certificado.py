"""Certificado A1: resolução dinâmica (variáveis de ambiente x enviado pela interface)."""
import os

from app import sefin
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
