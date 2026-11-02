"""Assinatura + transmissão ao SEFIN Nacional (delegadas ao PyNFSeNacional, já validado em
produção) e um modo MOCK para testar a interface sem tocar no governo (NFSE_MOCK=1)."""

from __future__ import annotations

import base64
import gzip
import os
import random
import tempfile
import warnings
from dataclasses import dataclass, field
from datetime import datetime

from .db import DATA_DIR

MOCK = os.environ.get("NFSE_MOCK", "") == "1"

# Certificado enviado pela tela Config: nome fixo dentro de DATA_DIR, senha num arquivo ao lado
# (ambos 0600). A presença do arquivo de senha é o que indica "veio da interface".
CERT_ENVIADO = os.path.join(DATA_DIR, "certificado.pfx")
SENHA_ENVIADA = os.path.join(DATA_DIR, "certificado.senha")
ORIGENS = {"interface": "enviado pela interface", "ambiente": "variáveis de ambiente"}


def certificado() -> tuple[str, str, str]:
    """(caminho, senha, origem) do certificado em uso, resolvido a cada chamada para que um
    certificado novo valha sem reiniciar o app. O enviado pela interface tem prioridade sobre
    NFSE_CERT_PATH/NFSE_CERT_SENHA. origem: "interface", "ambiente" ou "" (nenhum)."""
    if os.path.exists(SENHA_ENVIADA):
        with open(SENHA_ENVIADA, encoding="utf-8") as f:
            return CERT_ENVIADO, f.read(), "interface"
    caminho = os.environ.get("NFSE_CERT_PATH", "")
    return caminho, os.environ.get("NFSE_CERT_SENHA", ""), "ambiente" if caminho else ""


@dataclass
class Resultado:
    ok: bool
    chave: str = ""
    numero: str = ""
    nfse_xml: str = ""
    erro: str = ""
    alertas: list[str] = field(default_factory=list)
    ja_existia: bool = False


class CertificadoInvalido(ValueError):
    """Mensagem pronta para o usuário. Nunca contém a senha."""


def _abrir_pfx(dados: bytes, senha: str):
    """Abre o PKCS#12 e devolve o certificado. Exige a chave privada junto (o A1 precisa dela)."""
    from cryptography.hazmat.primitives.serialization import pkcs12
    for tentativa in ([senha.encode()] if senha else [None, b""]):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                chave, cert, _ = pkcs12.load_key_and_certificates(dados, tentativa)
            break
        except Exception:
            continue
    else:
        raise CertificadoInvalido("Não consegui abrir o certificado: senha errada ou o arquivo não é "
                                  "um certificado .pfx/.p12 válido.")
    if cert is None or chave is None:
        raise CertificadoInvalido("O arquivo não contém o certificado com a chave privada "
                                  "(o A1 precisa dos dois).")
    return cert


def _resumo(cert) -> dict:
    fim = cert.not_valid_after_utc
    return {"ok": True, "titular": cert.subject.rfc4514_string(),
            "validade": fim.date().isoformat(), "vencido": fim < datetime.now(fim.tzinfo)}


def cert_info() -> dict:
    caminho, senha, origem = certificado()
    info = {"origem": origem, "origem_txt": ORIGENS.get(origem, "")}
    if not caminho:
        return {**info, "ok": False, "erro": "Nenhum certificado configurado. Envie o seu .pfx em "
                "Config (ou defina NFSE_CERT_PATH e NFSE_CERT_SENHA)."}
    if not os.path.exists(caminho):
        return {**info, "ok": False, "erro": f"Arquivo do certificado não encontrado ({caminho})."}
    try:
        with open(caminho, "rb") as f:
            return {**info, **_resumo(_abrir_pfx(f.read(), senha))}
    except Exception as e:
        msg = str(e) if isinstance(e, CertificadoInvalido) else f"Não consegui abrir o certificado: {e}"
        dica = " Envie o certificado de novo em Config." if origem == "interface" else ""
        return {**info, "ok": False, "erro": msg + dica}


TAMANHO_MAX_CERT = 1024 * 1024    # um A1 tem poucos KB


def _escrever_tmp(dados: bytes) -> str:
    """Arquivo temporário 0600 em DATA_DIR (mesmo sistema de arquivos → os.replace atômico)."""
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".certificado-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(dados)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def _remover_antigo_do_ambiente() -> str | None:
    """Apaga o .pfx antigo indicado por NFSE_CERT_PATH, mas só se ele estiver dentro de DATA_DIR
    (nunca mexe em nada fora dele) e não for o próprio arquivo que acabamos de gravar."""
    antigo = os.environ.get("NFSE_CERT_PATH", "")
    if not antigo or os.path.splitext(antigo)[1].lower() not in (".pfx", ".p12"):
        return None
    real, base = os.path.realpath(antigo), os.path.realpath(DATA_DIR)
    if real == os.path.realpath(CERT_ENVIADO) or os.path.commonpath([real, base]) != base \
            or not os.path.isfile(real):
        return None
    os.remove(real)
    return os.path.basename(real)


def salvar_certificado(dados: bytes, senha: str) -> dict:
    """Valida (tamanho, senha, chave privada) e só então grava o certificado e a senha em
    DATA_DIR, com nomes fixos, permissão 0600 e troca atômica. O anterior é substituído.
    Levanta CertificadoInvalido sem gravar nada se algo estiver errado."""
    if not dados:
        raise CertificadoInvalido("Selecione o arquivo do certificado (.pfx ou .p12).")
    if len(dados) > TAMANHO_MAX_CERT:
        raise CertificadoInvalido("Arquivo grande demais (máximo 1 MB). Um certificado A1 tem "
                                  "poucos KB: confira se escolheu o arquivo certo.")
    info = _resumo(_abrir_pfx(dados, senha))
    tmp_cert = _escrever_tmp(dados)
    try:
        tmp_senha = _escrever_tmp(senha.encode("utf-8"))
    except BaseException:
        os.unlink(tmp_cert)
        raise
    os.replace(tmp_cert, CERT_ENVIADO)
    os.replace(tmp_senha, SENHA_ENVIADA)
    info["removido"] = _remover_antigo_do_ambiente()
    return info


def assinar(xml: str) -> str:
    caminho, senha, _ = certificado()
    if MOCK and not caminho:
        return xml
    from pynfsenacional.assinatura import assinar_dps
    return assinar_dps(xml, cert_pfx_path=caminho, senha=senha)


def _cliente(ambiente: str, cod_mun: str):
    from pynfsenacional.sefin import SefinClient
    caminho, senha, _ = certificado()
    return SefinClient(ambiente=ambiente, cert_pfx_path=caminho, senha=senha,
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
