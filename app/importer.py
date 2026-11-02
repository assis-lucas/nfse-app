"""Importa um XML de NFS-e (ou de DPS) já emitido e extrai: prestador, tomador + perfil fiscal
e os dados da nota. É o atalho para "não lembro como preenchia": baixe o XML da última nota no
Emissor Nacional e importe aqui — o molde sai idêntico ao que já foi aceito."""

from __future__ import annotations

import json
from datetime import date

from lxml import etree


def _f(node, path: str) -> str:
    if node is None:
        return ""
    parts = "/".join(f"{{*}}{p}" for p in path.split("/"))
    el = node.find(parts)
    return (el.text or "").strip() if el is not None and el.text else ""


def _has(node, path: str) -> bool:
    if node is None:
        return False
    return node.find("/".join(f"{{*}}{p}" for p in path.split("/"))) is not None


def parse(xml: bytes | str) -> dict:
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    inf = root if etree.QName(root).localname == "infDPS" else root.find(".//{*}infDPS")
    if inf is None:
        raise ValueError("XML sem infDPS — não parece uma NFS-e/DPS do padrão nacional.")
    inf_nfse = root.find(".//{*}infNFSe")

    prest = inf.find("{*}prest")
    toma = inf.find("{*}toma")
    serv = inf.find("{*}serv")
    trib = inf.find("{*}valores/{*}trib")

    prestador = {
        "cnpj": _f(prest, "CNPJ"),
        "im": _f(prest, "IM"),
        "cod_municipio": _f(inf, "cLocEmi"),
        "op_simp_nac": int(_f(prest, "regTrib/opSimpNac") or 3),
        "reg_ap_trib_sn": int(_f(prest, "regTrib/regApTribSN")) if _f(prest, "regTrib/regApTribSN") else None,
        "reg_esp_trib": int(_f(prest, "regTrib/regEspTrib") or 0),
        "razao_social": _f(inf_nfse, "emit/xNome"),
    }

    t: dict = {}
    if toma is not None:
        for d in ("CNPJ", "CPF", "NIF"):
            if _f(toma, d):
                t["tipo_doc"], t["documento"] = d, _f(toma, d)
                break
        else:
            t["tipo_doc"], t["c_nao_nif"] = "SEM_NIF", _f(toma, "cNaoNIF") or "0"
        t["nome"] = _f(toma, "xNome")
        t["apelido"] = t["nome"][:40]
        t["im"] = _f(toma, "IM")
        t["email"] = _f(toma, "email")
        t["fone"] = _f(toma, "fone")
        if _has(toma, "end/endNac"):
            t["end_tipo"] = "nacional"
            t["end_cmun"] = _f(toma, "end/endNac/cMun")
            t["end_cep"] = _f(toma, "end/endNac/CEP")
        elif _has(toma, "end/endExt"):
            t["end_tipo"] = "exterior"
            t["end_pais"] = _f(toma, "end/endExt/cPais")
            t["end_cod_postal"] = _f(toma, "end/endExt/cEndPost")
            t["end_cidade"] = _f(toma, "end/endExt/xCidade")
            t["end_estado"] = _f(toma, "end/endExt/xEstProvReg")
        else:
            t["end_tipo"] = "nenhum"
        if t["end_tipo"] != "nenhum":
            t["end_lgr"] = _f(toma, "end/xLgr")
            t["end_nro"] = _f(toma, "end/nro")
            t["end_cpl"] = _f(toma, "end/xCpl")
            t["end_bairro"] = _f(toma, "end/xBairro")

    t["c_trib_nac"] = _f(serv, "cServ/cTribNac")
    t["c_trib_mun"] = _f(serv, "cServ/cTribMun")
    t["c_nbs"] = _f(serv, "cServ/cNBS")
    t["info_compl"] = _f(serv, "infoCompl/xInfComp")
    t["c_loc_prestacao"] = _f(serv, "locPrest/cLocPrestacao")
    t["trib_issqn"] = int(_f(trib, "tribMun/tribISSQN") or 1)
    t["c_pais_result"] = _f(trib, "tribMun/cPaisResult")
    t["tp_ret_issqn"] = int(_f(trib, "tribMun/tpRetISSQN") or 1)
    t["p_aliq"] = _f(trib, "tribMun/pAliq")
    t["piscofins_cst"] = _f(trib, "tribFed/piscofins/CST")
    t["tp_ret_piscofins"] = _f(trib, "tribFed/piscofins/tpRetPisCofins")
    if _f(trib, "totTrib/pTotTribSN"):
        t["tot_trib_modo"], t["p_tot_trib_sn"] = "pTotTribSN", _f(trib, "totTrib/pTotTribSN")
    elif _has(trib, "totTrib/pTotTrib"):
        t["tot_trib_modo"] = "pTotTrib"
        t["p_tot_fed"] = _f(trib, "totTrib/pTotTrib/pTotTribFed")
        t["p_tot_est"] = _f(trib, "totTrib/pTotTrib/pTotTribEst")
        t["p_tot_mun"] = _f(trib, "totTrib/pTotTrib/pTotTribMun")
    else:
        t["tot_trib_modo"], t["ind_tot_trib"] = "indTotTrib", _f(trib, "totTrib/indTotTrib") or "0"
    ce = serv.find("{*}comExt") if serv is not None else None
    t["com_ext"] = ce is not None
    if ce is not None:
        for campo, tag in (("md_prestacao", "mdPrestacao"), ("vinc_prest", "vincPrest"),
                           ("tp_moeda", "tpMoeda"), ("mec_af_comex_p", "mecAFComexP"),
                           ("mec_af_comex_t", "mecAFComexT"), ("mov_temp_bens", "movTempBens"),
                           ("mdic", "mdic")):
            t[campo] = _f(ce, tag)
    ibs = inf.find("{*}IBSCBS")
    if ibs is not None:
        d = {k: _f(ibs, k) for k in ("finNFSe", "indFinal", "cIndOp", "indDest")}
        d["CST"] = _f(ibs, "valores/trib/gIBSCBS/CST") or _f(ibs, "valores/trib/CST")
        d["cClassTrib"] = _f(ibs, "valores/trib/gIBSCBS/cClassTrib") or _f(ibs, "valores/trib/cClassTrib")
        if _f(inf, "finNFSe"):
            d["finNFSe"] = _f(inf, "finNFSe")
        t["ibscbs_json"] = json.dumps({k: v for k, v in d.items() if v})

    desc = _f(serv, "cServ/xDescServ")
    vmoeda = _f(serv, "comExt/vServMoeda")
    nota = {
        "competencia": date.fromisoformat(_f(inf, "dCompet")),
        "valor": _f(inf, "valores/vServPrest/vServ"),
        "valor_moeda": vmoeda or None,
        "tp_moeda": _f(serv, "comExt/tpMoeda"),
        "descricao": desc,
        "serie": _f(inf, "serie"),
        "ndps": int(_f(inf, "nDPS") or 0) or None,
        "id_dps": inf.get("Id", ""),
        "ambiente": "producao" if _f(inf, "tpAmb") == "1" else "homologacao",
        "chave": (inf_nfse.get("Id", "")[3:] if inf_nfse is not None else ""),
        "numero_nfse": _f(inf_nfse, "nNFSe"),
        "is_nfse": inf_nfse is not None,
    }
    return {"prestador": prestador, "tomador": t, "nota": nota, "descricao_original": desc}
