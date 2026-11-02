"""Modelos e banco (SQLite via SQLModel). Valores monetários em CENTAVOS (int)."""

from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
TZ = ZoneInfo("America/Sao_Paulo")


def agora() -> datetime:
    return datetime.now(TZ)


engine = create_engine(f"sqlite:///{os.path.join(DATA_DIR, 'nfse.db')}",
                       connect_args={"check_same_thread": False})


class Prestador(SQLModel, table=True):
    """Sua empresa (linha única, id=1)."""
    id: int = Field(default=1, primary_key=True)
    razao_social: str = ""
    cnpj: str = ""
    im: str = ""                         # inscrição municipal (opcional)
    cod_municipio: str = ""              # IBGE, 7 dígitos (ex.: 3550308 = São Paulo/SP)
    op_simp_nac: int = 3                 # 1 não optante | 2 MEI | 3 ME/EPP
    reg_ap_trib_sn: Optional[int] = 1    # só ME/EPP (1 = tributos federais e municipal pelo SN)
    reg_esp_trib: int = 0
    ambiente: str = "homologacao"        # homologacao | producao
    leiaute: str = "auto"                # auto | 20260209 (produção) | 20261020 (NT 009)
    serie: str = "1"
    prox_ndps_homologacao: int = 1
    prox_ndps_producao: int = 1


class Tomador(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    apelido: str = ""
    nome: str = ""
    ativo: bool = True
    # identificação: CNPJ | CPF | NIF | SEM_NIF
    tipo_doc: str = "CNPJ"
    documento: str = ""
    c_nao_nif: str = "0"                 # só SEM_NIF: 0 não informado | 1 dispensado | 2 não exigência
    im: str = ""
    email: str = ""
    fone: str = ""
    # endereço: nenhum | nacional | exterior
    end_tipo: str = "nenhum"
    end_cep: str = ""
    end_cmun: str = ""
    end_pais: str = ""                   # ISO alpha-2 (US, PT...)
    end_cod_postal: str = ""
    end_cidade: str = ""
    end_estado: str = ""
    end_lgr: str = ""
    end_nro: str = ""
    end_cpl: str = ""
    end_bairro: str = ""
    # ---- perfil do serviço / tributação (o "molde" da nota) ----
    c_trib_nac: str = ""                 # 6 dígitos (ex.: 010101)
    c_trib_mun: str = ""
    c_nbs: str = ""
    descricao_padrao: str = "Serviços de desenvolvimento de software - {mes_nome}/{ano}"
    info_compl: str = ""
    c_loc_prestacao: str = ""            # IBGE; vazio = município do prestador
    trib_issqn: int = 1                  # 1 tributável | 2 imunidade | 3 exportação | 4 não incidência
    c_pais_result: str = ""              # exportação: país onde se verifica o resultado
    tp_ret_issqn: int = 1
    p_aliq: str = ""                     # alíquota ISS (opcional)
    piscofins_cst: str = "08"            # vazio = omite tribFed
    tp_ret_piscofins: str = ""
    tot_trib_modo: str = "pTotTribSN"    # pTotTribSN | indTotTrib | pTotTrib
    p_tot_trib_sn: str = "0.00"
    ind_tot_trib: str = "0"
    p_tot_fed: str = ""
    p_tot_est: str = ""
    p_tot_mun: str = ""
    # comércio exterior (grupo comExt)
    com_ext: bool = False
    md_prestacao: str = "1"              # 1 transfronteiriço
    vinc_prest: str = "0"                # 0 sem vínculo
    tp_moeda: str = "220"                # BACEN: 220 USD | 978 EUR | 540 GBP
    mec_af_comex_p: str = "01"
    mec_af_comex_t: str = "01"
    mov_temp_bens: str = "1"
    mdic: str = "0"
    # IBS/CBS (opcional) — JSON com cIndOp, indDest, CST, cClassTrib, finNFSe, indFinal
    ibscbs_json: str = ""
    created_at: datetime = Field(default_factory=agora)

    @property
    def exterior(self) -> bool:
        return self.tipo_doc in ("NIF", "SEM_NIF") or self.end_tipo == "exterior"


class Nota(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tomador_id: Optional[int] = Field(default=None, foreign_key="tomador.id")
    tomador_nome: str = ""
    origem: str = "app"                  # app | importada
    ambiente: str = "homologacao"
    serie: str = ""
    ndps: Optional[int] = None
    id_dps: str = ""
    competencia: date
    valor_cents: int
    valor_moeda_cents: Optional[int] = None
    tp_moeda: str = ""
    descricao: str = ""
    status: str = "erro"                 # emitida | erro | cancelada
    chave: str = ""
    numero_nfse: str = ""
    xml_dps: str = ""
    xml_nfse: str = ""
    erro_msg: str = ""
    created_at: datetime = Field(default_factory=agora)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        if not s.get(Prestador, 1):
            s.add(Prestador(id=1))
            s.commit()


def get_session():
    with Session(engine) as s:
        yield s


def prestador(s: Session) -> Prestador:
    return s.get(Prestador, 1)


__all__ = ["Prestador", "Tomador", "Nota", "engine", "init_db", "get_session", "prestador",
           "Session", "select"]
