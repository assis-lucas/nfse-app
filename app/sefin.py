"""Assinatura + transmissão ao SEFIN Nacional (delegadas ao PyNFSeNacional, já validado em
produção) e um modo MOCK para testar a interface sem tocar no governo (NFSE_MOCK=1)."""

from __future__ import annotations

import base64
import gzip
import os
import random
import warnings
from dataclasses import dataclass, field
from datetime import datetime

CERT_PATH = os.environ.get("NFSE_CERT_PATH", "")
CERT_SENHA = os.environ.get("NFSE_CERT_SENHA", "")
MOCK = os.environ.get("NFSE_MOCK", "") == "1"


@dataclass
class Resultado:
    ok: bool
    chave: str = ""
    numero: str = ""
    nfse_xml: str = ""
    erro: str = ""
    alertas: list[str] = field(default_factory=list)
    ja_existia: bool = False


def cert_info() -> dict:
    if not CERT_PATH or not os.path.exists(CERT_PATH):
        return {"ok": False, "erro": "NFSE_CERT_PATH não configurado ou arquivo inexistente."}
    try:
        from cryptography.hazmat.primitives.serialization import pkcs12
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _, cert, _ = pkcs12.load_key_and_certificates(open(CERT_PATH, "rb").read(),
                                                          CERT_SENHA.encode())
        return {"ok": True, "titular": cert.subject.rfc4514_string(),
                "validade": cert.not_valid_after_utc.date().isoformat(),
                "vencido": cert.not_valid_after_utc < datetime.now(cert.not_valid_after_utc.tzinfo)}
    except Exception as e:  # senha errada, arquivo inválido…
        return {"ok": False, "erro": f"Não consegui abrir o certificado: {e}"}


def assinar(xml: str) -> str:
    if MOCK and not CERT_PATH:
        return xml
    from pynfsenacional.assinatura import assinar_dps
    return assinar_dps(xml, cert_pfx_path=CERT_PATH, senha=CERT_SENHA)


def _cliente(ambiente: str, cod_mun: str):
    from pynfsenacional.sefin import SefinClient
    return SefinClient(ambiente=ambiente, cert_pfx_path=CERT_PATH, senha=CERT_SENHA,
                       codigo_municipio=cod_mun)


def dps_existente(ambiente: str, cod_mun: str, id_dps: str) -> Resultado | None:
    """Idempotência: se essa DPS já virou nota (ex.: timeout no envio anterior), devolve a nota."""
    if MOCK:
        return None
    c = _cliente(ambiente, cod_mun)
    if not c.verificar_dps(id_dps):
        return None
    chave = c.consultar_dps(id_dps)
    xml = c.consultar_nfse(chave) if chave else None
    import re
    num = re.search(r"<nNFSe>(\d+)</nNFSe>", xml or "")
    return Resultado(ok=True, chave=chave or "", numero=num.group(1) if num else "",
                     nfse_xml=xml or "", ja_existia=True)


def emitir(ambiente: str, cod_mun: str, xml_assinado: str) -> Resultado:
    if MOCK:
        return _mock(xml_assinado)
    r = _cliente(ambiente, cod_mun).emitir(xml_assinado)
    if not r.ok:
        cod = f"[{r.erro_codigo}] " if r.erro_codigo else ""
        return Resultado(ok=False, erro=f"{cod}{r.erro_msg or 'falha na emissão'}")
    return Resultado(ok=True, chave=r.chave_acesso or "", numero=r.numero_nfse or "",
                     nfse_xml=r.nfse_xml or "", alertas=r.alertas)


def _mock(xml_assinado: str) -> Resultado:
    corpo = xml_assinado.split("?>", 1)[-1]
    chave = "".join(random.choice("0123456789") for _ in range(50))
    num = str(random.randint(1, 9999))
    nfse = (f'<?xml version="1.0" encoding="UTF-8"?><NFSe xmlns="http://www.sped.fazenda.gov.br/nfse" '
            f'versao="1.01"><infNFSe Id="NFS{chave}"><nNFSe>{num}</nNFSe>'
            f'<emit><xNome>MOCK</xNome></emit>{corpo}</infNFSe></NFSe>')
    return Resultado(ok=True, chave=chave, numero=num, nfse_xml=nfse,
                     alertas=["MODO MOCK — nada foi enviado ao governo."])


def b64gz(xml: str) -> str:
    return base64.b64encode(gzip.compress(xml.encode())).decode()
