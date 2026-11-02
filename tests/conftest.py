"""Ambiente comum dos testes. Roda antes de qualquer `import app...`: o app lê as variáveis
de ambiente ao ser importado. Tudo em modo MOCK (nada vai ao governo) e num DATA_DIR temporário."""
import os
import subprocess
import tempfile

import pytest

TMP = tempfile.mkdtemp()
os.environ.update(DATA_DIR=TMP, NFSE_MOCK="1", SECRET_KEY="x" * 32,
                  ALLOWED_HOSTS="localhost,127.0.0.1,[::1],testserver")


def _gerar_pfx(destino: str, senha: str, cn: str = "EMPRESA TESTE:12345678000195", dias: int = 30) -> str:
    """Certificado autoassinado de teste (dados fictícios), via openssl."""
    base = destino + ".tmp"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", base + ".key",
                    "-out", base + ".crt", "-days", str(dias), "-nodes", "-subj", f"/CN={cn}"],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkcs12", "-export", "-inkey", base + ".key", "-in", base + ".crt",
                    "-out", destino, "-passout", f"pass:{senha}"], check=True, capture_output=True)
    os.remove(base + ".key")
    os.remove(base + ".crt")
    return destino


# Certificado "das variáveis de ambiente" (o jeito antigo, que continua valendo).
CERT = _gerar_pfx(os.path.join(TMP, "teste.pfx"), "1234")
os.environ.update(NFSE_CERT_PATH=CERT, NFSE_CERT_SENHA="1234")


@pytest.fixture
def gerar_pfx():
    return _gerar_pfx
