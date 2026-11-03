"""Testes ponta a ponta em modo MOCK (nada vai ao governo). Rode: pytest -q
O ambiente (DATA_DIR temporário, certificado de teste, NFSE_MOCK=1) é montado em conftest.py."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from decimal import Decimal

from app import main
from app.db import Prestador, Tomador
from app.dps import montar_dps, render_descricao, validar_xsd
from app.importer import molde_descricao
from app.main import app


def _exportacao():
    p = Prestador(cnpj="12345678000195", cod_municipio="3550308", op_simp_nac=3, ambiente="producao")
    t = Tomador(nome="ACME Inc.", tipo_doc="NIF", documento="12-3456789", end_tipo="exterior",
                end_pais="US", end_cod_postal="94105", end_cidade="SF", end_estado="CA",
                end_lgr="Market St", c_trib_nac="010101", c_nbs="115021000", trib_issqn=3,
                c_pais_result="US", p_tot_trib_sn="6.00", com_ext=True)
    return p, t


def test_dps_exportacao_valida_no_xsd():
    p, t = _exportacao()
    xml = montar_dps(p, t, ndps=1, valor="10.000,00", valor_moeda="2000", competencia=date(2026, 10, 1),
                     descricao="x", dh_emi="2026-11-02T10:00:00-03:00")
    assert validar_xsd(xml) == []
    assert "<tribISSQN>3</tribISSQN><cPaisResult>US</cPaisResult>" in xml
    assert "<vServ>10000.00</vServ>" in xml and "<vServMoeda>2000.00</vServMoeda>" in xml


def test_nt009_move_xdescserv_e_finnfse():
    p, t = _exportacao()
    p.ambiente = "homologacao"
    xml = montar_dps(p, t, ndps=1, valor="10", valor_moeda="2", competencia=date(2026, 10, 1),
                     descricao="x", dh_emi="2026-11-02T10:00:00-03:00")
    assert "<tpEmit>1</tpEmit><finNFSe>0</finNFSe><cLocEmi>" in xml
    assert "<cNBS>115021000</cNBS><xDescServ>x</xDescServ>" in xml


def test_quebra_de_linha_do_navegador_vira_lf():
    # Textarea manda CRLF; o CR sai como &#13; e o SEFIN rejeita a assinatura (E0714).
    p, t = _exportacao()
    t.info_compl = "a\r\nb"
    xml = montar_dps(p, t, ndps=1, valor="10", valor_moeda="2", competencia=date(2026, 10, 1),
                     descricao="linha 1\r\nlinha 2\rlinha 3", dh_emi="2026-11-02T10:00:00-03:00")
    assert "&#13;" not in xml and "\r" not in xml
    assert "<xDescServ>linha 1\nlinha 2\nlinha 3</xDescServ>" in xml
    assert "<xInfComp>a\nb</xInfComp>" in xml


def test_exportacao_sem_pais_falha():
    p, t = _exportacao()
    t.c_pais_result = ""
    with pytest.raises(ValueError):
        montar_dps(p, t, ndps=1, valor="10", valor_moeda="2", competencia=date(2026, 10, 1), descricao="x")


def test_fluxo_completo():
    with TestClient(app) as c:
        assert c.get("/", follow_redirects=False).status_code == 200
        c.post("/config", data={"razao_social": "Minha LTDA", "cnpj": "12345678000195", "im": "",
                                "cod_municipio": "3550308", "op_simp_nac": "3", "reg_ap_trib_sn": "1",
                                "reg_esp_trib": "0", "ambiente": "homologacao", "leiaute": "auto",
                                "serie": "1", "prox_ndps_homologacao": "1", "prox_ndps_producao": "1"})
        form = {"id": "", "apelido": "ACME", "nome": "ACME Inc.", "tipo_doc": "NIF",
                "documento": "12-3456789", "c_nao_nif": "0", "end_tipo": "exterior", "end_pais": "US",
                "end_cod_postal": "94105", "end_cidade": "SF", "end_estado": "CA", "end_lgr": "Market",
                "c_trib_nac": "010101", "c_nbs": "115021000", "trib_issqn": "3", "c_pais_result": "US",
                "tp_ret_issqn": "1", "piscofins_cst": "08", "tot_trib_modo": "pTotTribSN",
                "p_tot_trib_sn": "6.00", "com_ext": "1", "md_prestacao": "1", "vinc_prest": "0",
                "tp_moeda": "220", "mec_af_comex_p": "01", "mec_af_comex_t": "01",
                "mov_temp_bens": "1", "mdic": "0", "ativo": "1",
                "descricao_padrao": "Software development services - {mes_nome}/{ano}"}
        r = c.post("/tomadores/salvar", data=form)
        assert "ACME" in r.text
        r = c.post("/emitir/preview", data={"tomador_id": "1", "competencia": "2026-10-31",
                                            "valor": "10.000,00", "valor_moeda": "2.000,00",
                                            "descricao": "Software development services - {mes_nome}/{ano}"})
        assert "outubro/2026" in r.text and "R$ 10.000,00" in r.text
        token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
        dados = {"token": token, "tomador_id": "1", "competencia": "2026-10-31", "valor": "10.000,00",
                 "valor_moeda": "2.000,00", "descricao": "Software development services - {mes_nome}/{ano}"}
        r = c.post("/emitir/confirmar", data=dados)
        assert "emitida" in r.text and "MOCK" in r.text
        # reenvio do mesmo formulário NÃO emite de novo
        r = c.post("/emitir/confirmar", data=dados)
        assert "enviado duas vezes" in r.text
        # XML assinado guardado; contador avançou
        assert "Signature" in c.get("/notas/1/xml?tipo=dps").text
        assert "<nDPS>2</nDPS>" not in c.get("/notas/1/xml?tipo=dps").text
        r = c.get("/config")
        assert 'name="prox_ndps_homologacao" value="2"' in r.text
        # importar o XML gerado: tomador já existe, nota já existe (mesma chave) → nada novo
        nfse = c.get("/notas/1/xml").content
        r = c.post("/importar", files=[("arquivos", ("n.xml", nfse, "text/xml"))])
        assert "Nada novo" in r.text
        assert "ACME" in c.get("/notas.csv").text
        assert c.get("/").status_code == 200


def test_molde_descricao_troca_mes_e_ano_da_competencia():
    out = date(2026, 10, 30)
    molde = molde_descricao("Software development services - outubro/2026", out)
    assert molde == "Software development services - {mes_nome}/{ano}"
    assert render_descricao(molde, date(2026, 11, 3)) == "Software development services - novembro/2026"
    assert molde_descricao("HORAS DEV | OUTUBRO, 10/2026", out) == "HORAS DEV | {mes_nome}, {competencia}"
    # sem o nome do mês, o ano fica; chaves literais não quebram o format
    assert molde_descricao("Contrato 2026 {fase 2}", out) == "Contrato 2026 {{fase 2}}"
    assert render_descricao(molde_descricao("Contrato 2026 {fase 2}", out), out) == "Contrato 2026 {fase 2}"


def test_cotacao(monkeypatch):
    pedidos = []

    def ptax(sigla, ate):
        pedidos.append((sigla, ate))
        return Decimal("5.42060"), date(2026, 10, 30)

    monkeypatch.setattr(main, "_ptax", ptax)
    with TestClient(app) as c:
        r = c.get("/cotacao", params={"moeda": "220", "data": "2026-11-01"})
        assert r.json() == {"taxa": "5.4206", "data": "2026-10-30"}
        assert pedidos == [("USD", date(2026, 11, 1))]
        assert c.get("/cotacao", params={"moeda": "790", "data": "2026-11-01"}).json() == {"taxa": "1.0000", "data": None}
        assert c.get("/cotacao", params={"moeda": "999", "data": "2026-11-01"}).status_code == 400
        assert c.get("/cotacao", params={"moeda": "220", "data": ""}).status_code == 400

        def falha(sigla, ate):
            raise OSError("sem rede")

        monkeypatch.setattr(main, "_ptax", falha)
        r = c.get("/cotacao", params={"moeda": "978", "data": "2026-11-01"})
        assert r.status_code == 502 and "EUR" in r.json()["erro"]
