from __future__ import annotations

import csv
import io
import json
import os
import secrets
import urllib.request
from contextlib import asynccontextmanager
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import get_args

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile as StarletteUpload
from starlette.middleware.sessions import SessionMiddleware

from . import dps as dpsmod
from . import importer, sefin
from .db import (DATA_DIR, Nota, Session, Tomador, get_session, init_db, prestador, select)
from .seguranca import ProtecaoOrigem

# Assina o cookie de sessão (mensagens flash e token anti-duplicidade da emissão).
# Opcional: sem ela, uma chave aleatória é gerada a cada início do app.
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)


templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def brl(cents) -> str:
    if cents is None:
        return "—"
    s = f"{cents / 100:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def num(cents) -> str:
    return "" if cents is None else f"{cents / 100:.2f}".replace(".", ",")


MOEDAS = {"220": "USD", "978": "EUR", "540": "GBP", "790": "BRL"}
templates.env.filters["brl"] = brl
templates.env.filters["num"] = num
templates.env.globals["MOEDAS"] = MOEDAS
templates.env.globals["MOCK"] = sefin.MOCK


def cents(v) -> int:
    return int(dpsmod.dec(v) * 100)


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="NFS-e", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=False,
                   max_age=60 * 60 * 24 * 30, same_site="strict")
# Adicionado por último = roda primeiro: recusa Host fora de ALLOWED_HOSTS e POSTs de outros sites.
app.add_middleware(ProtecaoOrigem)


def render(request: Request, name: str, **ctx):
    ctx.setdefault("flash", request.session.pop("flash", None))
    return templates.TemplateResponse(request, name, ctx)


def flash(request: Request, msg: str, tipo: str = "ok"):
    request.session["flash"] = {"msg": msg, "tipo": tipo}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- dashboard
def _notas_validas(s: Session, ambiente="producao"):
    return s.exec(select(Nota).where(Nota.status == "emitida", Nota.ambiente == ambiente)).all()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, ano: int | None = None, s: Session = Depends(get_session)):
    hoje = date.today()
    ano = ano or hoje.year
    p = prestador(s)
    notas = _notas_validas(s)
    por_mes = defaultdict(int)
    por_tomador = defaultdict(int)
    for n in notas:
        if n.competencia.year == ano:
            por_mes[n.competencia.month] += n.valor_cents
            por_tomador[n.tomador_nome] += n.valor_cents
    # RBT12: receita bruta dos 12 meses anteriores ao mês corrente (base do Simples)
    ini = date(hoje.year - 1, hoje.month, 1)
    fim = date(hoje.year, hoje.month, 1)
    rbt12 = sum(n.valor_cents for n in notas if ini <= n.competencia < fim)
    ultimas = s.exec(select(Nota).order_by(Nota.id.desc()).limit(8)).all()
    tomadores = s.exec(select(Tomador).where(Tomador.ativo == True)).all()  # noqa: E712
    anos = sorted({n.competencia.year for n in notas} | {hoje.year}, reverse=True)
    return render(request, "dashboard.html", p=p, ano=ano, anos=anos,
                  meses=[(m, dpsmod.MESES[m - 1], por_mes.get(m, 0)) for m in range(1, 13)],
                  total_ano=sum(por_mes.values()), rbt12=rbt12,
                  por_tomador=sorted(por_tomador.items(), key=lambda x: -x[1]),
                  ultimas=ultimas, tomadores=tomadores, cert=sefin.cert_info())


# ---------------------------------------------------------------- emissão
@app.get("/emitir", response_class=HTMLResponse)
def emitir_form(request: Request, tomador_id: int | None = None, s: Session = Depends(get_session)):
    tomadores = s.exec(select(Tomador).where(Tomador.ativo == True)).all()  # noqa: E712
    if not tomadores:
        flash(request, "Cadastre (ou importe) um tomador antes de emitir.", "erro")
        return RedirectResponse("/tomadores", 303)
    t = s.get(Tomador, tomador_id) if tomador_id else tomadores[0]
    ultima = s.exec(select(Nota).where(Nota.tomador_id == t.id, Nota.status == "emitida")
                    .order_by(Nota.id.desc())).first()
    return render(request, "emitir.html", tomadores=tomadores, t=t, ultima=ultima,
                  hoje=date.today().isoformat(), p=prestador(s))


PTAX = "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"


def _ptax(sigla: str, ate: date) -> tuple[Decimal, date]:
    """Cotação de compra do último boletim de fechamento PTAX (Banco Central) até a data.
    Fim de semana, feriado ou antes das 13h: vale o fechamento do dia útil anterior."""
    url = (f"{PTAX}CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
           f"?@moeda='{sigla}'&@dataInicial='{ate - timedelta(days=10):%m-%d-%Y}'&@dataFinalCotacao='{ate:%m-%d-%Y}'"
           "&$format=json&$filter=tipoBoletim%20eq%20'Fechamento'&$select=cotacaoCompra,dataHoraCotacao")
    with urllib.request.urlopen(url, timeout=8) as r:
        itens = json.load(r, parse_float=Decimal)["value"]
    if not itens:
        raise ValueError("sem cotação no período")
    return itens[-1]["cotacaoCompra"], date.fromisoformat(itens[-1]["dataHoraCotacao"][:10])


@app.get("/cotacao")
def cotacao(moeda: str, data: str):
    """Usada pela tela de emissão para converter reais ↔ moeda estrangeira enquanto se digita."""
    sigla = MOEDAS.get(moeda)
    if not sigla or sigla == "BRL":
        return JSONResponse({"erro": "Moeda sem cotação."}, 400)
    try:
        dia = min(date.fromisoformat(data), date.today())
    except ValueError:
        return JSONResponse({"erro": "Preencha a data de competência para buscar a cotação."}, 400)
    try:
        taxa, dia_ptax = _ptax(sigla, dia)
    except Exception:
        return JSONResponse({"erro": f"Não deu para buscar a cotação do {sigla} no Banco Central. "
                                     "Preencha os dois valores à mão."}, 502)
    return {"taxa": f"{taxa:.4f}", "data": dia_ptax.isoformat()}


def _form_emissao(form) -> dict:
    return {
        "tomador_id": int(form["tomador_id"]),
        "competencia": date.fromisoformat(form["competencia"]),
        "valor": form["valor"],
        "valor_moeda": form.get("valor_moeda") or None,
        "descricao": form["descricao"],
    }


@app.post("/emitir/preview", response_class=HTMLResponse)
async def emitir_preview(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    try:
        d = _form_emissao(form)
        p = prestador(s)
        t = s.get(Tomador, d["tomador_id"])
        desc = dpsmod.render_descricao(d["descricao"], d["competencia"])
        ndps = getattr(p, f"prox_ndps_{p.ambiente}")
        xml = dpsmod.montar_dps(p, t, ndps=ndps, valor=d["valor"], valor_moeda=d["valor_moeda"],
                                competencia=d["competencia"], descricao=desc)
    except Exception as e:
        flash(request, f"Não deu para montar a nota: {e}", "erro")
        return RedirectResponse(f"/emitir?tomador_id={form.get('tomador_id', '')}", 303)
    leiaute = dpsmod.leiaute_efetivo(p)
    erros_xsd = dpsmod.validar_xsd(xml) if leiaute == dpsmod.LEIAUTE_PRODUCAO else None
    token = secrets.token_hex(8)
    request.session["emit_token"] = token
    vm = dpsmod.dec(d["valor_moeda"]) if d["valor_moeda"] else None
    cotacao = (dpsmod.dec(d["valor"]) / vm).quantize(dpsmod.Decimal("0.0001")) if vm else None
    return render(request, "preview.html", p=p, t=t, d=d, desc=desc, xml=xml, ndps=ndps,
                  erros_xsd=erros_xsd, leiaute=leiaute, token=token, cotacao=cotacao,
                  valor_cents=cents(d["valor"]), vm_cents=cents(vm) if vm else None,
                  cert=sefin.cert_info())


def _mesma_nota(nfse_xml: str, valor_cents: int, t: Tomador) -> bool:
    try:
        d = importer.parse(nfse_xml)
    except Exception:
        return False
    return cents(d["nota"]["valor"]) == valor_cents and \
        dpsmod.digitos(d["tomador"].get("documento", "")) == dpsmod.digitos(t.documento)


@app.post("/emitir/confirmar")
async def emitir_confirmar(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    if not form.get("token") or form.get("token") != request.session.pop("emit_token", None):
        flash(request, "Formulário expirado ou enviado duas vezes. Confira o histórico antes de "
                       "tentar de novo.", "erro")
        return RedirectResponse("/notas", 303)
    d = _form_emissao(form)
    p = prestador(s)
    t = s.get(Tomador, d["tomador_id"])
    desc = dpsmod.render_descricao(d["descricao"], d["competencia"])
    campo = f"prox_ndps_{p.ambiente}"
    ndps = getattr(p, campo)
    nota = Nota(tomador_id=t.id, tomador_nome=t.apelido or t.nome, ambiente=p.ambiente,
                serie=p.serie, competencia=d["competencia"], valor_cents=cents(d["valor"]),
                valor_moeda_cents=cents(d["valor_moeda"]) if d["valor_moeda"] else None,
                tp_moeda=t.tp_moeda if t.com_ext else "", descricao=desc)
    try:
        for _ in range(20):
            xml = dpsmod.montar_dps(p, t, ndps=ndps, valor=d["valor"], valor_moeda=d["valor_moeda"],
                                    competencia=d["competencia"], descricao=desc)
            idd = dpsmod.id_dps(p, ndps, p.serie)
            existente = sefin.dps_existente(p.ambiente, p.cod_municipio, idd)
            if existente:
                if _mesma_nota(existente.nfse_xml, nota.valor_cents, t):
                    r = existente          # envio anterior caiu mas a nota foi gerada
                    break
                ndps += 1                  # número já usado por outra nota
                continue
            assinado = sefin.assinar(xml)
            nota.xml_dps = assinado
            r = sefin.emitir(p.ambiente, p.cod_municipio, assinado)
            break
        else:
            raise RuntimeError("20 números de DPS seguidos já estavam em uso — revise o contador.")
    except Exception as e:
        r = sefin.Resultado(ok=False, erro=f"Falha de comunicação: {e}")
    nota.ndps, nota.id_dps = ndps, dpsmod.id_dps(p, ndps, p.serie)
    if r.ok:
        nota.status, nota.chave, nota.numero_nfse, nota.xml_nfse = "emitida", r.chave, r.numero, r.nfse_xml
        setattr(p, campo, ndps + 1)
        s.add(p)
        msg = f"NFS-e {r.numero} emitida." + (" (recuperada de envio anterior)" if r.ja_existia else "")
        if r.alertas:
            msg += " Alertas: " + "; ".join(r.alertas)
        flash(request, msg)
    else:
        nota.status, nota.erro_msg = "erro", r.erro
        flash(request, f"Rejeitada: {r.erro}", "erro")
    s.add(nota)
    s.commit()
    return RedirectResponse(f"/notas/{nota.id}", 303)


# ---------------------------------------------------------------- notas
@app.get("/notas", response_class=HTMLResponse)
def notas(request: Request, ano: int | None = None, tomador_id: int | None = None,
          s: Session = Depends(get_session)):
    q = select(Nota).order_by(Nota.competencia.desc(), Nota.id.desc())
    if tomador_id:
        q = q.where(Nota.tomador_id == tomador_id)
    lista = [n for n in s.exec(q).all() if not ano or n.competencia.year == ano]
    return render(request, "notas.html", notas=lista, ano=ano, tomador_id=tomador_id,
                  tomadores=s.exec(select(Tomador)).all())


@app.get("/notas.csv")
def notas_csv(s: Session = Depends(get_session)):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["competencia", "tomador", "numero", "chave", "valor_brl", "moeda", "valor_moeda",
                "ambiente", "status", "origem", "criada_em"])
    for n in s.exec(select(Nota).order_by(Nota.competencia)).all():
        w.writerow([n.competencia.isoformat(), n.tomador_nome, n.numero_nfse, n.chave,
                    num(n.valor_cents), MOEDAS.get(n.tp_moeda, n.tp_moeda), num(n.valor_moeda_cents),
                    n.ambiente, n.status, n.origem, n.created_at.strftime("%Y-%m-%d %H:%M")])
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=notas.csv"})


@app.get("/notas/{nid}", response_class=HTMLResponse)
def nota(request: Request, nid: int, s: Session = Depends(get_session)):
    return render(request, "nota.html", n=s.get(Nota, nid))


@app.get("/notas/{nid}/xml")
def nota_xml(nid: int, tipo: str = "nfse", s: Session = Depends(get_session)):
    n = s.get(Nota, nid)
    xml = n.xml_nfse if tipo == "nfse" else n.xml_dps
    nome = f"{'nfse' if tipo == 'nfse' else 'dps'}-{n.numero_nfse or n.id}.xml"
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": f"attachment; filename={nome}"})


@app.get("/notas/{nid}/pdf")
def nota_pdf(request: Request, nid: int, s: Session = Depends(get_session)):
    n = s.get(Nota, nid)
    destino = os.path.join(DATA_DIR, "pdf", f"nfse-{n.numero_nfse or n.id}-{n.id}.pdf")
    if not os.path.exists(destino):
        try:
            from pynfsenacional.danfse import render as danfse
            danfse(n.xml_nfse, destino)
        except Exception as e:
            flash(request, f"Não consegui gerar o PDF: {e}", "erro")
            return RedirectResponse(f"/notas/{nid}", 303)
    return FileResponse(destino, media_type="application/pdf", filename=os.path.basename(destino))


@app.post("/notas/{nid}/excluir")
def nota_excluir(request: Request, nid: int, s: Session = Depends(get_session)):
    n = s.get(Nota, nid)
    if n.status == "emitida" and n.origem == "app" and n.ambiente == "producao":
        flash(request, "Nota emitida em produção não se apaga daqui — cancele no Emissor Nacional "
                       "e marque como cancelada.", "erro")
        return RedirectResponse(f"/notas/{nid}", 303)
    s.delete(n)
    s.commit()
    flash(request, "Registro removido.")
    return RedirectResponse("/notas", 303)


@app.post("/notas/{nid}/cancelada")
def nota_cancelada(request: Request, nid: int, s: Session = Depends(get_session)):
    n = s.get(Nota, nid)
    n.status = "cancelada"
    s.add(n)
    s.commit()
    flash(request, "Marcada como cancelada (sai dos totais).")
    return RedirectResponse(f"/notas/{nid}", 303)


# ---------------------------------------------------------------- tomadores
@app.get("/tomadores", response_class=HTMLResponse)
def tomadores(request: Request, s: Session = Depends(get_session)):
    return render(request, "tomadores.html", tomadores=s.exec(select(Tomador)).all())


@app.get("/tomadores/novo", response_class=HTMLResponse)
def tomador_novo(request: Request, exterior: int = 0):
    t = Tomador()
    if exterior:
        t.tipo_doc, t.end_tipo, t.trib_issqn, t.com_ext, t.c_pais_result, t.end_pais = \
            "NIF", "exterior", 3, True, "US", "US"
    return render(request, "tomador_form.html", t=t)


@app.get("/tomadores/{tid}", response_class=HTMLResponse)
def tomador_edit(request: Request, tid: int, s: Session = Depends(get_session)):
    return render(request, "tomador_form.html", t=s.get(Tomador, tid))


BOOL_FIELDS = {"com_ext", "ativo"}


def _aplicar(obj, dados: dict):
    for nome, info in type(obj).model_fields.items():
        if nome in ("id", "created_at") or nome not in dados:
            continue
        v = dados[nome]
        tipo = info.annotation
        if tipo is int or int in get_args(tipo):
            v = int(v) if str(v).strip() != "" else None
        elif tipo is bool:
            v = str(v).lower() in ("1", "true", "on", "yes")
        else:
            v = "" if v is None else str(v).strip()
        setattr(obj, nome, v)


@app.post("/tomadores/salvar")
async def tomador_salvar(request: Request, s: Session = Depends(get_session)):
    form = dict(await request.form())
    for b in BOOL_FIELDS:
        form[b] = form.get(b, "")
    t = s.get(Tomador, int(form["id"])) if form.get("id") else Tomador()
    _aplicar(t, form)
    if t.ibscbs_json.strip():
        try:
            ibs = json.loads(t.ibscbs_json)
            faltam = [k for k in ("cIndOp", "CST", "cClassTrib") if not ibs.get(k)]
            if faltam:
                raise ValueError("faltam " + ", ".join(faltam))
        except Exception as e:
            flash(request, f"IBS/CBS inválido: {e}", "erro")
            return render(request, "tomador_form.html", t=t)
    s.add(t)
    s.commit()
    flash(request, "Tomador salvo.")
    return RedirectResponse("/tomadores", 303)


# ---------------------------------------------------------------- importação
@app.get("/importar", response_class=HTMLResponse)
def importar_form(request: Request):
    return render(request, "importar.html")


@app.post("/importar")
async def importar(request: Request, arquivos: list[UploadFile], atualizar_prestador: str = Form(""),
                   s: Session = Depends(get_session)):
    msgs, erros = [], []
    p = prestador(s)
    for arq in arquivos:
        raw = await arq.read()
        try:
            d = importer.parse(raw)
        except Exception as e:
            erros.append(f"{arq.filename}: {e}")
            continue
        if atualizar_prestador or not p.cnpj:
            for k, v in d["prestador"].items():
                if v not in (None, "") and k != "razao_social":
                    setattr(p, k, v)
            if d["prestador"]["razao_social"] and not p.razao_social:
                p.razao_social = d["prestador"]["razao_social"]
            s.add(p)
        td = d["tomador"]
        doc = (td.get("documento") or "").strip()
        t = next((x for x in s.exec(select(Tomador)).all()
                  if (doc and x.documento.strip() == doc)
                  or (doc and dpsmod.digitos(doc) and dpsmod.digitos(x.documento) == dpsmod.digitos(doc))
                  or (not doc and x.nome == td.get("nome"))), None)
        if t is None:
            t = Tomador()
            _aplicar(t, td)
            # molde de descrição: a descrição original, com o mês trocado por placeholder quando der
            t.descricao_padrao = d["descricao_molde"]
            s.add(t)
            s.flush()
            msgs.append(f"tomador novo: {t.nome}")
        n = d["nota"]
        if n["is_nfse"] and n["chave"] and not s.exec(select(Nota).where(Nota.chave == n["chave"])).first():
            s.add(Nota(tomador_id=t.id, tomador_nome=t.apelido or t.nome, origem="importada",
                       ambiente=n["ambiente"], serie=n["serie"], ndps=n["ndps"], id_dps=n["id_dps"],
                       competencia=n["competencia"], valor_cents=cents(n["valor"]),
                       valor_moeda_cents=cents(n["valor_moeda"]) if n["valor_moeda"] else None,
                       tp_moeda=n["tp_moeda"], descricao=n["descricao"], status="emitida",
                       chave=n["chave"], numero_nfse=n["numero_nfse"], xml_nfse=raw.decode("utf-8", errors="replace")))
            msgs.append(f"nota {n['numero_nfse']} ({n['competencia']:%m/%Y}) importada")
    s.commit()
    flash(request, ("; ".join(msgs) or "Nada novo.") + (" | Erros: " + "; ".join(erros) if erros else ""),
          "erro" if erros and not msgs else "ok")
    return RedirectResponse("/tomadores" if msgs else "/importar", 303)


# ---------------------------------------------------------------- config
@app.get("/config", response_class=HTMLResponse)
def config(request: Request, s: Session = Depends(get_session)):
    p = prestador(s)
    cert = sefin.cert_info()
    return render(request, "config.html", p=p, cert=cert, leiaute=dpsmod.leiaute_efetivo(p),
                  cnpj_diverge=_cnpj_diverge(cert, p))


def _cnpj_diverge(cert: dict, p) -> bool:
    """O certificado é de outro CNPJ que não o do prestador configurado?"""
    a, b = dpsmod.digitos(cert.get("cnpj") or ""), dpsmod.digitos(p.cnpj or "")
    return bool(cert.get("ok") and a and b and a != b)


@app.post("/config")
async def config_salvar(request: Request, s: Session = Depends(get_session)):
    p = prestador(s)
    form = dict(await request.form())
    if form.get("ambiente") == "producao" and p.ambiente != "producao" and not form.get("confirma_prod"):
        flash(request, "Para ir para PRODUÇÃO marque a confirmação.", "erro")
        return RedirectResponse("/config", 303)
    _aplicar(p, form)
    s.add(p)
    s.commit()
    flash(request, "Configuração salva.")
    return RedirectResponse("/config", 303)


@app.post("/config/certificado")
async def config_certificado(request: Request, s: Session = Depends(get_session)):
    # Lê o formulário à mão (sem parâmetros Form()): um erro de validação do FastAPI devolveria
    # os campos enviados no corpo da resposta 422, e um deles é a senha.
    try:
        tamanho = int(request.headers.get("content-length") or 0)
    except ValueError:
        tamanho = 0
    if tamanho > sefin.TAMANHO_MAX_CERT + 64 * 1024:    # nem lê o corpo: arquivo muito grande
        flash(request, "Arquivo grande demais (máximo 1 MB). Um certificado A1 tem poucos KB: "
                       "confira se escolheu o arquivo certo.", "erro")
        return RedirectResponse("/config", 303)
    form = await request.form()
    arquivo, senha = form.get("arquivo"), form.get("senha")
    try:
        dados = await arquivo.read(sefin.TAMANHO_MAX_CERT + 1) if isinstance(arquivo, StarletteUpload) else b""
        info = sefin.salvar_certificado(dados, senha if isinstance(senha, str) else "")
    except sefin.CertificadoInvalido as e:
        flash(request, str(e), "erro")
        return RedirectResponse("/config", 303)
    msg = f"Certificado salvo: {info['titular']}, válido até {info['validade']}."
    alerta = False
    if info["vencido"]:
        msg += " ATENÇÃO: este certificado está VENCIDO e o SEFIN vai recusar as notas."
        alerta = True
    elif info["vence_em_breve"]:
        msg += f" Atenção: ele vence em {info['dias_restantes']} dias."
    if _cnpj_diverge(info, prestador(s)):
        msg += f" ATENÇÃO: o CNPJ do certificado ({info['cnpj']}) é diferente do CNPJ da sua empresa."
        alerta = True
    if info["removido"]:
        msg += f" O certificado anterior ({info['removido']}) foi apagado."
    flash(request, msg, "erro" if alerta else "ok")
    return RedirectResponse("/config", 303)
