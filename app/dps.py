"""Montagem da DPS (XML não assinado), na ordem exata do XSD v1.01.

Cobre o que as libs prontas não cobrem: tomador estrangeiro (NIF/cNaoNIF + endExt), exportação
de serviço (tribISSQN=3 + cPaisResult) e o grupo de comércio exterior (comExt).

Regras herdadas do PyNFSeNacional (validadas em produção):
  • prestador emitente (tpEmit=1) NÃO leva nome/endereço (E0128/E0121); IM antes do regTrib.
  • regApTribSN só para ME/EPP (opSimpNac=3) — E0162.
  • MEI não envia tribFed — E0676.
  • ME/EPP usa pTotTribSN, nunca indTotTrib — E0712.
  • Leiaute NT 009 (produção restrita desde 20/10/2026): finNFSe vai para o infDPS e o
    xDescServ passa para depois do cNBS.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from lxml import etree

from .db import Prestador, Tomador

NS = "http://www.sped.fazenda.gov.br/nfse"
VERSAO_DPS = "1.01"
VER_APLIC = "nfse-app-0.1"
LEIAUTE_PRODUCAO = "20260209"
LEIAUTE_NT009 = "20261020"

MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
         "setembro", "outubro", "novembro", "dezembro"]


class DpsError(ValueError):
    pass


def digitos(v: str | None) -> str:
    return "".join(c for c in (v or "") if c.isdigit())


def dec(valor) -> Decimal:
    """'1.234,56' | '1234.56' | Decimal | int → Decimal com 2 casas."""
    if isinstance(valor, str):
        t = valor.strip().replace("R$", "").replace(" ", "")
        if "," in t:
            t = t.replace(".", "").replace(",", ".")
        valor = t
    try:
        d = Decimal(str(valor))
    except InvalidOperation:
        raise DpsError(f"valor inválido: {valor!r}") from None
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def render_descricao(modelo: str, competencia: date) -> str:
    return (modelo or "").format(
        mes=f"{competencia.month:02d}", ano=competencia.year,
        mes_nome=MESES[competencia.month - 1], competencia=competencia.strftime("%m/%Y"),
    )


def leiaute_efetivo(p: Prestador) -> str:
    if p.leiaute and p.leiaute != "auto":
        return p.leiaute
    return LEIAUTE_NT009 if p.ambiente == "homologacao" else LEIAUTE_PRODUCAO


def id_dps(p: Prestador, ndps: int, serie: str) -> str:
    cnpj = digitos(p.cnpj)
    return f"DPS{p.cod_municipio[:7]}2{cnpj.zfill(14)}{str(serie).zfill(5)}{str(ndps).zfill(15)}"


def _el(parent, tag, texto=None):
    e = etree.SubElement(parent, f"{{{NS}}}{tag}")
    if texto is not None:
        # Textarea do navegador manda CRLF. O CR só existe no XML como &#13; e o SEFIN
        # rejeita a assinatura (E0714); LF é a quebra de linha normal do XML.
        e.text = str(texto).replace("\r\n", "\n").replace("\r", "\n")
    return e


def _validar(p: Prestador, t: Tomador, valor_moeda) -> None:
    if len(digitos(p.cnpj)) != 14:
        raise DpsError("Configure o CNPJ do prestador (14 dígitos).")
    if len(digitos(p.cod_municipio)) != 7:
        raise DpsError("Código IBGE do município do prestador inválido.")
    if len(digitos(t.c_trib_nac)) != 6:
        raise DpsError(f"Tomador '{t.apelido or t.nome}': cTribNac precisa de 6 dígitos.")
    if not t.nome.strip():
        raise DpsError("Tomador sem nome/razão social.")
    if t.tipo_doc in ("CNPJ", "CPF"):
        n = 14 if t.tipo_doc == "CNPJ" else 11
        if len(digitos(t.documento)) != n:
            raise DpsError(f"{t.tipo_doc} do tomador inválido.")
    if t.tipo_doc == "NIF" and not t.documento.strip():
        raise DpsError("Informe o NIF do tomador estrangeiro.")
    if int(t.trib_issqn) == 3 and len(t.c_pais_result.strip()) != 2:
        raise DpsError("Exportação (tribISSQN=3) exige o país do resultado (cPaisResult, ex.: US).")
    if t.com_ext and (valor_moeda is None or dec(valor_moeda) <= 0):
        raise DpsError("Nota com comércio exterior exige o valor na moeda estrangeira.")


def _endereco(toma, t: Tomador) -> None:
    if t.end_tipo == "nenhum":
        return
    end = _el(toma, "end")
    if t.end_tipo == "nacional":
        n = _el(end, "endNac")
        _el(n, "cMun", digitos(t.end_cmun))
        _el(n, "CEP", digitos(t.end_cep))
    else:
        x = _el(end, "endExt")
        _el(x, "cPais", t.end_pais.strip().upper())
        _el(x, "cEndPost", t.end_cod_postal.strip()[:11] or "0")
        _el(x, "xCidade", t.end_cidade.strip()[:60])
        _el(x, "xEstProvReg", t.end_estado.strip()[:60] or t.end_cidade.strip()[:60])
    _el(end, "xLgr", (t.end_lgr.strip() or "-")[:255])
    _el(end, "nro", (t.end_nro.strip() or "S/N")[:60])
    if t.end_cpl.strip():
        _el(end, "xCpl", t.end_cpl.strip()[:156])
    _el(end, "xBairro", (t.end_bairro.strip() or "-")[:60])


def montar_dps(p: Prestador, t: Tomador, *, ndps: int, valor, competencia: date, descricao: str,
               valor_moeda=None, dh_emi: str | None = None) -> str:
    _validar(p, t, valor_moeda)
    leiaute = leiaute_efetivo(p)
    nt009 = leiaute == LEIAUTE_NT009
    serie = p.serie or "1"

    if dh_emi is None:
        from pynfsenacional import relogio
        dh_emi = relogio.dh_emi()

    dps = etree.Element(f"{{{NS}}}DPS", nsmap={None: NS})
    dps.set("versao", VERSAO_DPS)
    inf = _el(dps, "infDPS")
    inf.set("Id", id_dps(p, ndps, serie))
    _el(inf, "tpAmb", "2" if p.ambiente == "homologacao" else "1")
    _el(inf, "dhEmi", dh_emi)
    _el(inf, "verAplic", VER_APLIC)
    _el(inf, "serie", serie)
    _el(inf, "nDPS", str(ndps))
    _el(inf, "dCompet", competencia.isoformat())
    _el(inf, "tpEmit", "1")
    ibs = json.loads(t.ibscbs_json) if t.ibscbs_json.strip() else None
    if nt009:
        _el(inf, "finNFSe", str((ibs or {}).get("finNFSe", "0")))
    _el(inf, "cLocEmi", p.cod_municipio)

    # prestador
    prest = _el(inf, "prest")
    _el(prest, "CNPJ", digitos(p.cnpj))
    if digitos(p.im):
        _el(prest, "IM", digitos(p.im))
    reg = _el(prest, "regTrib")
    _el(reg, "opSimpNac", str(p.op_simp_nac))
    if p.op_simp_nac == 3 and p.reg_ap_trib_sn:
        _el(reg, "regApTribSN", str(p.reg_ap_trib_sn))
    _el(reg, "regEspTrib", str(p.reg_esp_trib))

    # tomador
    toma = _el(inf, "toma")
    if t.tipo_doc in ("CNPJ", "CPF"):
        _el(toma, t.tipo_doc, digitos(t.documento))
    elif t.tipo_doc == "NIF":
        _el(toma, "NIF", t.documento.strip()[:40])
    else:
        _el(toma, "cNaoNIF", t.c_nao_nif or "0")
    if digitos(t.im):
        _el(toma, "IM", digitos(t.im))
    _el(toma, "xNome", t.nome.strip()[:300])
    _endereco(toma, t)
    if digitos(t.fone):
        _el(toma, "fone", digitos(t.fone)[:20])
    if t.email.strip():
        _el(toma, "email", t.email.strip()[:80])

    # serviço
    serv = _el(inf, "serv")
    lp = _el(serv, "locPrest")
    _el(lp, "cLocPrestacao", digitos(t.c_loc_prestacao) or p.cod_municipio)
    cs = _el(serv, "cServ")
    _el(cs, "cTribNac", digitos(t.c_trib_nac))
    if t.c_trib_mun.strip():
        _el(cs, "cTribMun", t.c_trib_mun.strip())
    if not nt009:
        _el(cs, "xDescServ", descricao.strip()[:2000])
    if digitos(t.c_nbs):
        _el(cs, "cNBS", digitos(t.c_nbs))
    if nt009:
        _el(cs, "xDescServ", descricao.strip()[:2000])
    if t.com_ext:
        ce = _el(serv, "comExt")
        _el(ce, "mdPrestacao", t.md_prestacao)
        _el(ce, "vincPrest", t.vinc_prest)
        _el(ce, "tpMoeda", digitos(t.tp_moeda).zfill(3))
        _el(ce, "vServMoeda", str(dec(valor_moeda)))
        _el(ce, "mecAFComexP", t.mec_af_comex_p.zfill(2))
        _el(ce, "mecAFComexT", t.mec_af_comex_t.zfill(2))
        _el(ce, "movTempBens", t.mov_temp_bens)
        _el(ce, "mdic", t.mdic)
    if t.info_compl.strip():
        _el(_el(serv, "infoCompl"), "xInfComp", t.info_compl.strip()[:2000])

    # valores / tributos
    vals = _el(inf, "valores")
    _el(_el(vals, "vServPrest"), "vServ", str(dec(valor)))
    trib = _el(vals, "trib")
    tm = _el(trib, "tribMun")
    _el(tm, "tribISSQN", str(t.trib_issqn))
    if int(t.trib_issqn) == 3:
        _el(tm, "cPaisResult", t.c_pais_result.strip().upper())
    _el(tm, "tpRetISSQN", str(t.tp_ret_issqn))
    if t.p_aliq.strip():
        _el(tm, "pAliq", str(dec(t.p_aliq)))
    if p.op_simp_nac != 2 and t.piscofins_cst.strip():
        pc = _el(_el(trib, "tribFed"), "piscofins")
        _el(pc, "CST", t.piscofins_cst.strip().zfill(2))
        if t.tp_ret_piscofins.strip():
            _el(pc, "tpRetPisCofins", t.tp_ret_piscofins.strip())
    tt = _el(trib, "totTrib")
    modo = t.tot_trib_modo
    if p.op_simp_nac == 3:
        modo = "pTotTribSN"          # E0712
    elif modo == "pTotTribSN":
        modo = "indTotTrib"
    if modo == "pTotTribSN":
        _el(tt, "pTotTribSN", str(dec(t.p_tot_trib_sn or "0")))
    elif modo == "pTotTrib":
        g = _el(tt, "pTotTrib")
        _el(g, "pTotTribFed", str(dec(t.p_tot_fed or "0")))
        _el(g, "pTotTribEst", str(dec(t.p_tot_est or "0")))
        _el(g, "pTotTribMun", str(dec(t.p_tot_mun or "0")))
    else:
        _el(tt, "indTotTrib", t.ind_tot_trib or "0")

    if ibs:
        g = _el(inf, "IBSCBS")
        if not nt009:
            _el(g, "finNFSe", str(ibs.get("finNFSe", "0")))
        if str(ibs.get("indFinal", "")).strip():
            _el(g, "indFinal", str(ibs["indFinal"]))
        _el(g, "cIndOp", str(ibs["cIndOp"]))
        _el(g, "indDest", str(ibs.get("indDest", "0")))
        tr = _el(_el(g, "valores"), "trib")
        gi = tr if nt009 else _el(tr, "gIBSCBS")
        _el(gi, "CST", str(ibs["CST"]))
        _el(gi, "cClassTrib", str(ibs["cClassTrib"]))

    return etree.tostring(dps, encoding="unicode")


def validar_xsd(xml: str) -> list[str]:
    """Valida contra o XSD oficial empacotado na nfelib (leiaute de produção). Lista de erros."""
    import os

    import nfelib
    base = os.path.join(os.path.dirname(nfelib.__file__), "nfse", "schemas", "v1_0")
    schema = etree.XMLSchema(etree.parse(os.path.join(base, "DPS_v1.01.xsd")))
    doc = etree.fromstring(xml.encode("utf-8"))
    if schema.validate(doc):
        return []
    return [f"linha {e.line}: {e.message}" for e in schema.error_log]
