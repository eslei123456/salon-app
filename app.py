"""
ProManager v6 — Gestão para profissionais autônomos
• Beleza & Estética / Professores & Tutores
• Trial 3 dias · Assinatura mensal R$29,90 via Pix
• Banco de dados SQLite (substitui o JSON de arquivo único)
• Modelo de dados unificado: um único "atendimentos" para os dois tipos de negócio
• Identidade visual em forma de recibo/cupom fiscal
"""
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import plotly.graph_objects as go
import sqlite3, hashlib, secrets, base64, random, string, io, urllib.parse, requests
from datetime import datetime, date, timedelta
from pathlib import Path
from contextlib import contextmanager

try:
    import qrcode
    HAS_QR = True
except ImportError:
    HAS_QR = False

try:
    import libsql_experimental as libsql
    HAS_LIBSQL = True
except ImportError:
    HAS_LIBSQL = False

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTES
# ══════════════════════════════════════════════════════════════════════════════
ADMIN_WHATSAPP = "5575991217749"
PIX_CHAVE      = "08294548537"   # CPF usado como chave Pix do recebedor
VALOR_MENSAL   = 29.90
TRIAL_DIAS     = 3
DB_PATH        = "promanager.db"

def mp_token():
    """Token do Mercado Pago, se configurado em Settings → Secrets no Streamlit Cloud.
    Sem ele, o app cai automaticamente no fluxo manual (QR fixo + código por WhatsApp)."""
    try:
        return st.secrets.get("MP_ACCESS_TOKEN", "")
    except Exception:
        return ""

LABELS = {
    "beleza": {
        "item": "Serviço", "contato": "Cliente", "contato_pl": "Clientes",
        "falta_verbo": "Bolo", "icone": "💅",
        "opcoes": {
            "Manicure": 25, "Pedicure": 30, "Manicure + Pedicure": 50,
            "Corte feminino": 60, "Corte masculino": 35, "Barba": 25,
            "Corte + Barba": 55, "Escova": 45, "Progressiva": 200,
            "Coloração simples": 120, "Coloração c/ mechas": 200,
            "Hidratação": 80, "Sobrancelha": 20, "Depilação": 50,
            "Penteado": 90, "Maquiagem": 100,
        },
    },
    "professor": {
        "item": "Matéria", "contato": "Aluno", "contato_pl": "Alunos",
        "falta_verbo": "Falta", "icone": "📚",
        "opcoes": {m: 50 for m in [
            "Matemática","Português","História","Geografia","Ciências","Física",
            "Química","Biologia","Inglês","Espanhol","Filosofia","Sociologia",
            "Redação","Literatura","Educação Física","Artes","Informática","Outra",
        ]},
    },
}

LIMIAR_CLIENTE_FIEL = 5   # visitas sem faltas a partir daqui já conta como cliente fiel

CATEGORIAS_GASTO = ["Produtos / Insumos","Material didático","Aluguel","Energia elétrica",
    "Internet / Telefone","Plataformas online","Equipamento","Marketing","Pessoal","Outros"]

# ══════════════════════════════════════════════════════════════════════════════
# BANCO DE DADOS
# ══════════════════════════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS contas(
    usuario TEXT PRIMARY KEY, nome TEXT, senha_hash TEXT, senha_salt TEXT,
    tipo TEXT, profissao TEXT, negocio TEXT, cor TEXT, whatsapp TEXT,
    trial_fim TEXT, ativo INTEGER DEFAULT 0, validade TEXT,
    codigo_ativacao TEXT, notif_enviada INTEGER DEFAULT 0, app_url TEXT,
    cpf TEXT, slogan TEXT, mp_payment_id TEXT, foto_base64 TEXT
);
CREATE TABLE IF NOT EXISTS servicos(
    id INTEGER PRIMARY KEY AUTOINCREMENT, usuario TEXT, nome TEXT, valor REAL
);
CREATE TABLE IF NOT EXISTS contatos(
    id INTEGER PRIMARY KEY AUTOINCREMENT, usuario TEXT, nome TEXT, telefone TEXT,
    item_fav TEXT, visitas INTEGER DEFAULT 0, faltas INTEGER DEFAULT 0,
    gasto_total REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS atendimentos(
    id INTEGER PRIMARY KEY AUTOINCREMENT, usuario TEXT, contato TEXT, item TEXT,
    valor REAL, data TEXT, hora TEXT, duracao REAL DEFAULT 1, local TEXT,
    status TEXT DEFAULT 'aguardando', obs TEXT, origem TEXT DEFAULT 'profissional'
);
CREATE TABLE IF NOT EXISTS gastos(
    id INTEGER PRIMARY KEY AUTOINCREMENT, usuario TEXT, descricao TEXT,
    categoria TEXT, valor REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS metas(
    id INTEGER PRIMARY KEY AUTOINCREMENT, usuario TEXT, nome TEXT, valor REAL,
    inicio TEXT, concluida INTEGER DEFAULT 0
);
"""

def turso_config():
    """Credenciais do Turso, se configuradas em Settings → Secrets.
    Sem elas, cai no SQLite local — que funciona, mas perde os dados quando
    o servidor gratuito do Streamlit Cloud reinicia."""
    try:
        url = st.secrets.get("TURSO_DATABASE_URL", "")
        token = st.secrets.get("TURSO_AUTH_TOKEN", "")
        return (url, token) if (url and token and HAS_LIBSQL) else (None, None)
    except Exception:
        return (None, None)

@contextmanager
def db():
    url, token = turso_config()
    if url:
        con = libsql.connect("promanager_replica.db", sync_url=url, auth_token=token)
        con.sync()
    else:
        con = sqlite3.connect(DB_PATH)
        con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
    finally:
        # commit acontece aqui no finally (não logo após o yield) de propósito:
        # se algum st.rerun() disparar DENTRO do "with db() as con:" por engano,
        # a exceção do Streamlit ainda passa por este finally antes de fechar a
        # conexão — então a gravação nunca mais fica pra trás de novo.
        try:
            con.commit()
            if url:
                con.sync()
        except Exception:
            pass
        con.close()

def init_db():
    with db() as con:
        for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            con.execute(stmt)
        for stmt in [
            "ALTER TABLE atendimentos ADD COLUMN origem TEXT DEFAULT 'profissional'",
            "ALTER TABLE contas ADD COLUMN app_url TEXT",
            "ALTER TABLE contas ADD COLUMN cpf TEXT",
            "ALTER TABLE contas ADD COLUMN slogan TEXT",
            "ALTER TABLE contas ADD COLUMN mp_payment_id TEXT",
            "ALTER TABLE contas ADD COLUMN foto_base64 TEXT",
        ]:
            try:
                con.execute(stmt)
            except Exception:
                pass  # coluna já existe

def _linhas_para_dicts(cursor, linhas):
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, l)) for l in linhas]

def q(con, sql, params=()):
    cur = con.execute(sql, params)
    return _linhas_para_dicts(cur, cur.fetchall())

def q1(con, sql, params=()):
    cur = con.execute(sql, params)
    linhas = _linhas_para_dicts(cur, cur.fetchall())
    return linhas[0] if linhas else None

def servicos_da_conta(usuario, tipo):
    """Catálogo de serviços/valores do próprio profissional. Se ele nunca mexeu,
    semeia com o catálogo padrão do tipo de negócio na primeira vez."""
    with db() as con:
        linhas = q(con, "SELECT * FROM servicos WHERE usuario=? ORDER BY nome", (usuario,))
        if not linhas:
            for nome, valor in LABELS[tipo]["opcoes"].items():
                con.execute("INSERT INTO servicos(usuario,nome,valor) VALUES (?,?,?)", (usuario, nome, valor))
            linhas = q(con, "SELECT * FROM servicos WHERE usuario=? ORDER BY nome", (usuario,))
    return {r["nome"]: r["valor"] for r in linhas}

def hash_senha(senha, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.sha256((salt + senha).encode()).hexdigest()
    return h, salt

def gerar_codigo():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=14))

def wa_link(numero, mensagem):
    return f"https://wa.me/{numero}?text={urllib.parse.quote(mensagem)}"

def so_digitos(s):
    return "".join(c for c in (s or "") if c.isdigit())

def cpf_valido(cpf):
    """Validação oficial do CPF (dígitos verificadores) — recusa sequências óbvias tipo 111.111.111-11."""
    cpf = so_digitos(cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for i in (9, 10):
        soma = sum(int(cpf[n]) * ((i + 1) - n) for n in range(i))
        dv = (soma * 10 % 11) % 10
        if dv != int(cpf[i]):
            return False
    return True

def cpf_fmt(cpf):
    d = so_digitos(cpf)
    return f"{d[0:3]}.{d[3:6]}.{d[6:9]}-{d[9:11]}" if len(d) == 11 else cpf

# ── Mercado Pago Pix — cobrança automática. Sem MP_ACCESS_TOKEN configurado em
#    Settings → Secrets, essas funções não são chamadas e o app usa o fluxo manual. ──
def mp_criar_cobranca(conta):
    token = mp_token()
    if not token:
        return None
    try:
        r = requests.post("https://api.mercadopago.com/v1/payments",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "transaction_amount": VALOR_MENSAL,
                "payment_method_id": "pix",
                "description": f"ProManager — assinatura mensal ({conta['usuario']})",
                "payer": {
                    "email": f"{conta['usuario']}@promanager.app",
                    "first_name": conta["nome"].split()[0],
                    "identification": {"type": "CPF", "number": so_digitos(conta.get("cpf") or "00000000000")},
                },
            }, timeout=15)
        r.raise_for_status()
        data = r.json()
        with db() as con:
            con.execute("UPDATE contas SET mp_payment_id=? WHERE usuario=?", (str(data["id"]), conta["usuario"]))
        return data
    except Exception:
        return None

def mp_status_pagamento(payment_id):
    token = mp_token()
    if not token or not payment_id:
        return None
    try:
        r = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}",
                          headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS DE FORMATAÇÃO
# ══════════════════════════════════════════════════════════════════════════════
def brl(v):
    v = v or 0
    sinal = "-" if v < 0 else ""
    return f"{sinal}R$ {abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def ini(n):
    return "".join(p[0].upper() for p in n.strip().split()[:2]) or "?"

PALETA_AVATAR = ["#2fa574", "#00b8d9", "#7c5cff", "#e0a940", "#ff6b9d", "#00d4ff", "#5cc9c9"]

def cor_avatar(nome):
    return PALETA_AVATAR[sum(map(ord, nome)) % len(PALETA_AVATAR)]

def page_header(icon, title, subtitle=""):
    sub = f'<div class="ph-sub">{subtitle}</div>' if subtitle else ""
    st.markdown(f"""<div class="page-header">
        <div class="ph-icon">{icon}</div>
        <div><div class="ph-title">{title}</div>{sub}</div>
    </div>""", unsafe_allow_html=True)

def empty_state(icon, texto):
    st.markdown(f"""<div class="empty-state">
        <div class="es-icon">{icon}</div><div class="es-txt">{texto}</div>
    </div>""", unsafe_allow_html=True)

def gerar_qr_pix(chave, valor, nome="ProManager"):
    if not HAS_QR:
        return None, chave
    def tlv(tag, val): return f"{tag}{len(val):02d}{val}"
    merchant = tlv("00", "BR.GOV.BCB.PIX") + tlv("01", chave)
    payload = (tlv("00","01") + tlv("26", merchant) + "5204000053039865" +
        f"54{len(f'{valor:.2f}'):02d}{valor:.2f}" + "5802BR" + tlv("59", nome[:25]) +
        tlv("60","SAO PAULO") + tlv("62", tlv("05","***")) + "6304")
    crc = 0xFFFF
    for c in payload.encode():
        crc ^= c << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
    payload += f"{crc & 0xFFFF:04X}"
    qr = qrcode.QRCode(version=1, box_size=6, border=3)
    qr.add_data(payload); qr.make(fit=True)
    img = qr.make_image(fill_color="#12151b", back_color="#f1ead6")
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return buf.getvalue(), payload

def ativar_conta(usuario, dias=30):
    with db() as con:
        con.execute(
            "UPDATE contas SET ativo=1, validade=?, codigo_ativacao=?, notif_enviada=0 WHERE usuario=?",
            ((date.today()+timedelta(days=dias)).isoformat(), gerar_codigo(), usuario))

def trial_valido(conta):
    try: return date.fromisoformat(conta["trial_fim"]) >= date.today()
    except Exception: return False

def assinatura_valida(conta):
    if not conta.get("ativo") or not conta.get("validade"): return False
    try: return date.fromisoformat(conta["validade"]) >= date.today()
    except Exception: return False

def acesso_ok(conta): return trial_valido(conta) or assinatura_valida(conta)

def dias_restantes(iso):
    try: return max(0, (date.fromisoformat(iso) - date.today()).days)
    except Exception: return 0

def horarios_livres(usuario, data_iso):
    todos = [f"{hh:02d}:{mm:02d}" for hh in range(7, 21) for mm in (0, 30)]
    with db() as con:
        ocupados = {r["hora"] for r in q(con,
            "SELECT hora FROM atendimentos WHERE usuario=? AND data=? AND status!='falta'",
            (usuario, data_iso))}
    return [h for h in todos if h not in ocupados]

# ══════════════════════════════════════════════════════════════════════════════
# INIT
# ══════════════════════════════════════════════════════════════════════════════
# init_db() cria tabelas e roda ALTER TABLE de migração — precisa acontecer só
# UMA VEZ por servidor, não a cada clique. st.cache_resource garante isso: sem
# ele, todo clique reabria o banco e refazia essas checagens à toa.
@st.cache_resource
def _preparar_banco_uma_vez():
    init_db()
    return True

_preparar_banco_uma_vez()
if "usuario_logado" not in st.session_state: st.session_state.usuario_logado = None
hoje_iso = date.today().isoformat()

st.set_page_config(page_title="ProManager", page_icon="🧾", layout="wide", initial_sidebar_state="expanded")

# Todo HTML deste app é enviado via <container>.markdown(..., unsafe_allow_html=True) com
# linhas indentadas (por legibilidade no código-fonte). O Markdown, por padrão, trata uma
# linha com 4+ espaços no início como bloco de código — e mostra o HTML cru em vez de
# renderizar. Este remendo tira a indentação de cada linha antes de mandar pro Streamlit.
# É aplicado na CLASSE DeltaGenerator (não só em st.markdown) porque colunas, sidebar,
# expanders etc. são objetos próprios com seu próprio .markdown() — corrigir só st.markdown
# não bastava e deixava passar batido em c_head.markdown(), st.sidebar.markdown() etc.
from streamlit.delta_generator import DeltaGenerator
_markdown_original = DeltaGenerator.markdown
def _markdown_sem_indentacao(self, body="", *args, **kwargs):
    if kwargs.get("unsafe_allow_html") and isinstance(body, str) and "\n" in body:
        body = "\n".join(linha.lstrip() for linha in body.split("\n"))
    return _markdown_original(self, body, *args, **kwargs)
DeltaGenerator.markdown = _markdown_sem_indentacao

# ══════════════════════════════════════════════════════════════════════════════
# CSS
# ══════════════════════════════════════════════════════════════════════════════
def apply_css():
    st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700&family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

html,body,[class*="css"]{font-family:'Inter',sans-serif!important;background:#12151b!important;color:#ece6d7!important;}
.stApp{background:#12151b!important;}
#MainMenu,footer{visibility:hidden!important;height:0!important;}

/* MENU SEMPRE ABERTO — remove totalmente a opção de recolher/expandir */
header[data-testid="stHeader"] {
    background: transparent !important;
    z-index: 99999 !important;
}

[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapseButton"],
button[aria-label="Open sidebar"],
button[aria-label="Close sidebar"],
button[aria-label="Collapse sidebar"] {
    display: none !important;
    visibility: hidden !important;
    pointer-events: none !important;
}
[data-testid="stSidebar"] {
    min-width: 260px !important;
    max-width: 260px !important;
    transform: none !important;
    visibility: visible !important;
}
[data-testid="stSidebar"][aria-expanded="false"] {
    min-width: 260px !important;
    margin-left: 0 !important;
}

.block-container{padding-top:1.6rem!important;max-width:1180px!important;}
[data-testid="stSidebar"]{background:#1b1f27!important;border-right:1px solid #ffffff12!important;}
[data-testid="stSidebar"] *{color:#ece6d7!important;}
[data-testid="stSidebar"] .block-container{padding-top:1rem!important;}
h1,h2,h3,h4,h5{font-family:'Space Grotesk',sans-serif!important;color:#ece6d7!important;}
label,p,.stMarkdown p{color:#ece6d7!important;}
hr{border-color:#ffffff14!important;margin:12px 0!important;}
.stButton>button{background:#1f6f52!important;color:#f1ead6!important;border:none!important;border-radius:10px!important;font-weight:600!important;font-size:13px!important;padding:10px 22px!important;transition:background .15s!important;}
.stButton>button:hover{background:#2fa574!important;}

[data-testid="stSidebar"] .stButton>button{justify-content:flex-start!important;}
[data-testid="stSidebar"] button[kind="secondary"]{
    background:transparent!important;color:#8b8f99!important;border:1px solid transparent!important;
    text-align:left!important;font-weight:500!important;padding:9px 12px!important;border-radius:9px!important;
}
[data-testid="stSidebar"] button[kind="secondary"]:hover{background:#ffffff0d!important;color:#ece6d7!important;}
[data-testid="stSidebar"] button[kind="primary"]{
    background:#1f6f5235!important;color:#ece6d7!important;border:1px solid #2fa57450!important;
    text-align:left!important;font-weight:600!important;padding:9px 12px!important;border-radius:9px!important;
}
[data-testid="stSidebar"] .stButton{margin-bottom:2px!important;}
.stTabs [data-baseweb="tab-list"]{background:#1b1f27!important;border-radius:12px!important;padding:4px!important;gap:4px!important;}
.stTabs [data-baseweb="tab"]{color:#8b8f99!important;border-radius:9px!important;font-size:13px!important;padding:9px 18px!important;}
.stTabs [aria-selected="true"]{background:#1f6f52!important;color:#f1ead6!important;}
.stTextInput>div>div,.stSelectbox>div>div,.stNumberInput>div>div,.stDateInput>div>div{
    background:#20242d!important;border:1px solid #ffffff18!important;border-radius:10px!important;color:#ece6d7!important;}
input,textarea,select{color:#ece6d7!important;}
.stDataFrame{border-radius:12px!important;overflow:hidden!important;border:1px solid #ffffff14!important;}
.stDataFrame th{background:#20242d!important;color:#ece6d7!important;}
.stDataFrame td{background:#1b1f27!important;color:#ece6d7!important;}
[data-testid="stExpander"]{background:#1b1f27!important;border:1px solid #ffffff14!important;border-radius:12px!important;}
[data-testid="stForm"]{background:transparent!important;border:none!important;padding:0!important;}

.receipt{background:#f1ead6;color:#12151b;border-radius:14px 14px 0 0;padding:20px 24px 14px;margin-top:6px;}
.receipt-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;}
.receipt-head .lbl{font-size:10.5px;letter-spacing:1.5px;text-transform:uppercase;color:#6b6552;font-weight:600;}
.receipt-head .val{font-family:'Space Grotesk';font-weight:700;font-size:15px;}
.dashed{border-top:1.5px dashed #6b655260;margin:8px 0;}
.line-item{display:flex;align-items:center;gap:10px;padding:6px 0;font-size:13px;}
.line-item .time{font-family:'IBM Plex Mono';font-size:12px;color:#6b6552;width:42px;flex-shrink:0;}
.line-item .who{flex:1;min-width:0;}
.line-item .who b{font-weight:600;}
.line-item .who span{display:block;font-size:11px;color:#6b6552;}
.line-item .val{font-family:'IBM Plex Mono';font-weight:600;font-size:13px;width:66px;text-align:right;flex-shrink:0;}
.stamp{font-family:'Space Grotesk';font-size:9px;font-weight:700;letter-spacing:.4px;padding:2px 7px;border-radius:4px;border:1.4px solid;transform:rotate(-6deg);display:inline-block;flex-shrink:0;margin-left:6px;}
.stamp.pago{color:#1f6f52;border-color:#1f6f52;}
.stamp.aguarda{color:#a8801c;border-color:#a8801c;}
.stamp.falta{color:#c0463a;border-color:#c0463a;}
.receipt-total{display:flex;justify-content:space-between;align-items:center;padding-top:10px;}
.receipt-total .t{font-size:10.5px;letter-spacing:1px;text-transform:uppercase;color:#6b6552;font-weight:600;}
.receipt-total .v{font-family:'Space Grotesk';font-weight:700;font-size:20px;}
.perf{height:14px;background:#12151b;-webkit-mask:radial-gradient(circle 5px at 12px 0,transparent 98%,#000 100%) repeat-x;mask:radial-gradient(circle 5px at 12px 0,transparent 98%,#000 100%) repeat-x;-webkit-mask-size:22px 14px;mask-size:22px 14px;border-radius:0 0 14px 14px;margin-bottom:14px;}

.ticket{background:#1b1f27;border-radius:12px;padding:14px 16px;position:relative;}
.ticket::before,.ticket::after{content:"";position:absolute;top:50%;transform:translateY(-50%);width:11px;height:11px;border-radius:50%;background:#12151b;}
.ticket::before{left:-6px;}
.ticket::after{right:-6px;}
.ticket .l{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8b8f99;font-weight:600;margin-bottom:6px;}
.ticket .v{font-family:'Space Grotesk';font-size:19px;font-weight:700;color:#ece6d7;}
.ticket .v.green{color:#2fa574;}
.ticket .v.red{color:#d9584a;}
.ticket .s{font-size:11px;color:#8b8f99;margin-top:3px;}

.link-card{background:linear-gradient(135deg,#1b1f27,#20242d);border:1px solid #2fa57430;border-radius:14px;padding:18px 22px;margin-bottom:16px;display:flex;align-items:center;gap:16px;}
.link-card .icn{width:42px;height:42px;border-radius:11px;background:#2fa57420;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:19px;}
.link-card .txt b{font-size:14px;display:block;margin-bottom:2px;}
.link-card .txt span{font-size:12px;color:#8b8f99;}

.origem-cliente{font-size:10px;color:#2fa574;background:#2fa57418;padding:2px 8px;border-radius:10px;font-weight:600;margin-left:6px;}

.agenda-card{background:#1b1f27;border-radius:12px;padding:12px 16px;display:flex;align-items:center;gap:12px;margin-bottom:8px;border-left:2px dashed #ffffff20;}
.agenda-card .time{font-family:'IBM Plex Mono';font-size:13px;color:#8b8f99;width:46px;flex-shrink:0;}
.agenda-card .init{width:32px;height:32px;border-radius:9px;background:#ffffff0f;display:flex;align-items:center;justify-content:center;font-family:'Space Grotesk';font-size:12px;font-weight:700;flex-shrink:0;}
.agenda-card .info{flex:1;min-width:0;}
.agenda-card .info b{font-size:13.5px;font-weight:600;}
.agenda-card .info span{display:block;font-size:11.5px;color:#8b8f99;}
.agenda-card .val{font-family:'IBM Plex Mono';font-weight:600;font-size:13px;}

.al{border-radius:12px;padding:12px 16px;margin-bottom:8px;display:flex;gap:10px;align-items:flex-start;font-size:13px;line-height:1.6;}
.al-g{background:#2fa57414;border:1px solid #2fa57440;color:#7fd6b3;}
.al-a{background:#e0a94014;border:1px solid #e0a94040;color:#f0c877;}
.al-r{background:#d9584a14;border:1px solid #d9584a40;color:#f0968b;}

.pix-banner{background:#1b1f27;border-radius:14px;padding:14px 20px;display:flex;align-items:center;gap:14px;border:1px solid #2fa57430;margin-top:8px;}
.pix-banner b{font-size:13.5px;display:block;}
.pix-banner span{font-size:12px;color:#8b8f99;}

.slot{padding:6px 8px;border-radius:7px;font-family:'IBM Plex Mono';font-size:11px;font-weight:600;border:1px solid;display:inline-block;margin:2px;}
.sl{background:#2fa57414;color:#7fd6b3;border-color:#2fa57440;}
.so{background:#ece6d71a;color:#ece6d7;border-color:#ece6d740;}
.sb2{background:#d9584a14;color:#f0968b;border-color:#d9584a40;}

.fin-card{background:#1b1f27;border-radius:14px;padding:18px 20px;margin-bottom:10px;}
.fin-card h3{font-family:'Space Grotesk';font-size:13.5px;margin:0 0 12px;font-weight:700;}
.fin-row{display:flex;justify-content:space-between;align-items:center;padding:7px 0;font-size:13px;border-bottom:1px solid #ffffff0d;}
.fin-row:last-child{border-bottom:none;}
.fin-row .l{color:#8b8f99;}
.fin-row .v{font-family:'IBM Plex Mono';font-weight:600;}
.meta-track{height:8px;background:#ffffff0d;border-radius:4px;overflow:hidden;margin-top:8px;}
.meta-fill{height:100%;border-radius:4px;}

html,body{
    background:
        radial-gradient(1000px 640px at 12% -8%, #17352c66 0%, transparent 55%),
        radial-gradient(900px 560px at 100% 0%, #1c2a5566 0%, transparent 55%),
        radial-gradient(700px 500px at 50% 110%, #2a1c4d40 0%, transparent 60%),
        #0d0f14 !important;
}
.stApp{background:transparent!important;}
.block-container{padding-top:2.1rem!important;padding-bottom:3rem!important;}

[data-testid="stHorizontalBlock"]{gap:18px!important;margin-bottom:18px!important;}
[data-testid="stVerticalBlock"]>[data-testid="stElementContainer"]{margin-bottom:2px!important;}
.stMarkdown h5{margin:28px 0 14px!important;font-size:15px!important;letter-spacing:.3px!important;}
hr{margin:18px 0!important;}

.brand-title{
    font-family:'Orbitron',sans-serif!important;
    background:linear-gradient(120deg,#2fa574 0%,#00d4ff 50%,#8a6bff 100%);
    -webkit-background-clip:text;background-clip:text;color:transparent!important;
}

.ticket{
    padding:18px 20px!important;margin-bottom:2px!important;
    background:linear-gradient(160deg,#1b2029,#161a22)!important;
    border:1px solid #ffffff10!important;
    box-shadow:0 12px 28px -18px rgba(47,165,116,.45), inset 0 1px 0 #ffffff08!important;
    transition:transform .18s ease, box-shadow .18s ease!important;
}
.ticket:hover{transform:translateY(-3px)!important;box-shadow:0 16px 34px -14px rgba(0,212,255,.35), inset 0 1px 0 #ffffff10!important;}
.ticket .v{font-family:'Orbitron',sans-serif!important;letter-spacing:.3px!important;}
.ticket .v.green{background:linear-gradient(120deg,#2fa574,#00d4ff)!important;-webkit-background-clip:text!important;background-clip:text!important;color:transparent!important;}
.ticket .v.red{background:linear-gradient(120deg,#d9584a,#ff8a6b)!important;-webkit-background-clip:text!important;background-clip:text!important;color:transparent!important;}

.fin-card,.link-card,.pix-banner,.agenda-card,.al{margin-bottom:16px!important;}
.fin-card,.link-card{
    background:linear-gradient(160deg,#1b2029,#161a22)!important;
    border:1px solid #ffffff10!important;
    box-shadow:0 12px 30px -20px rgba(124,92,255,.35)!important;
}
.link-card .icn{background:linear-gradient(135deg,#2fa57440,#00d4ff30)!important;}
.receipt{box-shadow:0 16px 42px -20px rgba(0,0,0,.55)!important;}
.line-item{padding:8px 0!important;}
.slot{margin:3px!important;padding:7px 9px!important;}
.agenda-card{border-left:2px solid transparent!important;border-image:linear-gradient(180deg,#2fa574,#00d4ff) 1!important;}

.stButton>button{
    background:linear-gradient(120deg,#1f6f52,#0d8a6f)!important;
    box-shadow:0 6px 18px -6px rgba(47,165,116,.55)!important;
}
.stButton>button:hover{
    background:linear-gradient(120deg,#2fa574,#00b8e0)!important;
    box-shadow:0 8px 24px -6px rgba(0,212,255,.55)!important;
}
[data-testid="stSidebar"] button[kind="primary"]{
    background:linear-gradient(120deg,#1f6f5240,#00d4ff20)!important;
    border:1px solid #2fa57460!important;
    box-shadow:0 0 18px -6px rgba(47,165,116,.55), inset 0 0 0 1px #ffffff08!important;
}
[data-testid="stSidebar"] button[kind="secondary"]:hover{box-shadow:0 0 14px -8px rgba(0,212,255,.4)!important;}

@keyframes fadeInUp{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}
.ticket,.fin-card,.link-card,.agenda-card,.al,.pix-banner,.receipt,.contact-card,.goal-card{animation:fadeInUp .45s ease both;}

.stMarkdown h5{position:relative;padding-left:15px;}
.stMarkdown h5::before{content:"";position:absolute;left:0;top:3px;bottom:3px;width:4px;border-radius:3px;
    background:linear-gradient(180deg,#2fa574,#00d4ff,#8a6bff);}

.page-header{display:flex;align-items:center;gap:14px;margin:4px 0 22px;}
.page-header .ph-icon{
    width:46px;height:46px;border-radius:13px;flex-shrink:0;display:flex;align-items:center;justify-content:center;
    font-size:21px;background:linear-gradient(150deg,#2fa57430,#00d4ff20);border:1px solid #2fa57445;
    box-shadow:0 8px 20px -10px rgba(47,165,116,.5);
}
.page-header .ph-title{font-family:'Space Grotesk';font-size:19px;font-weight:700;}
.page-header .ph-sub{font-size:12.5px;color:#8b8f99;margin-top:1px;}

.chip{display:inline-block;font-size:10.5px;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:.2px;margin-left:6px;}
.chip-g{background:#2fa57420;color:#7fd6b3;border:1px solid #2fa57450;}
.chip-a{background:#e0a94020;color:#f0c877;border:1px solid #e0a94050;}
.chip-r{background:#d9584a20;color:#f0968b;border:1px solid #d9584a50;}

.contact-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:14px;margin-bottom:8px;}
.contact-card{
    background:linear-gradient(160deg,#1b2029,#161a22);border:1px solid #ffffff10;border-radius:14px;
    padding:16px;box-shadow:0 10px 26px -18px rgba(0,0,0,.6);transition:transform .15s,box-shadow .15s;
}
.contact-card:hover{transform:translateY(-3px);box-shadow:0 14px 30px -14px rgba(0,212,255,.3);}
.contact-card .cc-top{display:flex;align-items:center;gap:12px;margin-bottom:12px;}
.contact-card .cc-avatar{
    width:40px;height:40px;border-radius:11px;display:flex;align-items:center;justify-content:center;
    font-family:'Space Grotesk';font-weight:700;font-size:14px;flex-shrink:0;border:1px solid;
}
.contact-card .cc-name{font-weight:600;font-size:14px;line-height:1.3;}
.contact-card .cc-meta{font-size:11.5px;color:#8b8f99;margin-top:1px;}
.contact-card .cc-stats{display:flex;gap:14px;border-top:1px solid #ffffff0d;padding-top:10px;}
.contact-card .cc-stat{flex:1;text-align:center;}
.contact-card .cc-stat b{display:block;font-family:'Orbitron';font-size:14px;}
.contact-card .cc-stat span{font-size:9.5px;color:#8b8f99;text-transform:uppercase;letter-spacing:.5px;}

.goal-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;margin-bottom:8px;}
.goal-card{
    background:linear-gradient(160deg,#1b2029,#161a22);border:1px solid #ffffff10;border-radius:14px;
    padding:18px 20px;box-shadow:0 10px 26px -18px rgba(124,92,255,.4);
}
.goal-card .gc-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;}
.goal-card .gc-name{font-weight:700;font-family:'Space Grotesk';font-size:14px;}
.goal-card .gc-pct{font-family:'Orbitron';font-size:16px;font-weight:700;}
.goal-card .gc-vals{font-size:11.5px;color:#8b8f99;margin-bottom:10px;}
.goal-track{height:10px;background:#ffffff0d;border-radius:6px;overflow:hidden;}
.goal-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#2fa574,#00d4ff);transition:width .4s ease;}
.goal-fill.done{background:linear-gradient(90deg,#2fa574,#8a6bff);}
.goal-card .gc-note{font-size:11.5px;color:#8b8f99;margin-top:9px;}

.legend-row{display:flex;align-items:center;gap:8px;font-size:12.5px;padding:5px 0;}
.legend-row .dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;}
.legend-row .lbl{flex:1;color:#ece6d7;}
.legend-row .val{font-family:'IBM Plex Mono';font-weight:600;color:#8b8f99;}

.empty-state{
    text-align:center;padding:34px 20px;border-radius:14px;border:1px dashed #ffffff1c;
    background:#ffffff05;margin-bottom:8px;
}
.empty-state .es-icon{font-size:26px;margin-bottom:8px;opacity:.85;}
.empty-state .es-txt{font-size:12.5px;color:#8b8f99;}

[data-testid="stProgress"] > div > div{background:linear-gradient(90deg,#2fa574,#00d4ff)!important;}
</style>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# TELA DE LOGIN / CADASTRO
# ══════════════════════════════════════════════════════════════════════════════
def tela_login():
    apply_css()
    st.markdown("""
    <div style='text-align:center;padding:2.4rem 0 1.6rem;'>
        <div class='brand-title' style='font-size:2.8rem;font-weight:700;'>ProManager</div>
        <div style='font-size:12px;color:#8b8f99;letter-spacing:2.5px;text-transform:uppercase;margin-top:6px;'>Gestão para profissionais autônomos</div>
    </div>""", unsafe_allow_html=True)

    if "_tela" not in st.session_state:
        st.session_state["_tela"] = "login"

    _, mid, _ = st.columns([1, 1.3, 1])
    with mid:
        if st.session_state["_tela"] == "login":
            st.markdown('<div class="ticket">', unsafe_allow_html=True)
            st.markdown("##### Entrar")
            with st.form("f_login"):
                usuario = st.text_input("Usuário")
                senha = st.text_input("Senha", type="password")
                if st.form_submit_button("Entrar", use_container_width=True):
                    with db() as con:
                        conta = q1(con, "SELECT * FROM contas WHERE usuario=?", (usuario,))
                    if not conta:
                        st.error("Usuário não encontrado.")
                    else:
                        h, _ = hash_senha(senha, conta["senha_salt"])
                        if h != conta["senha_hash"]:
                            st.error("Senha incorreta.")
                        else:
                            st.session_state.usuario_logado = usuario
                            st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
            st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)
            if st.button("✨ Criar conta grátis", use_container_width=True, key="ir_cadastro"):
                st.session_state["_tela"] = "cadastro"
                st.rerun()

        else:
            st.markdown('<div class="ticket">', unsafe_allow_html=True)
            st.markdown("##### Criar conta grátis")
            with st.form("f_cadastro"):
                tipo_disp = st.selectbox("Tipo de conta", ["💅 Beleza / Estética", "📚 Professor / Tutor"])
                tipo = "beleza" if "Beleza" in tipo_disp else "professor"
                c_nome = st.text_input("Nome completo")
                c_cpf = st.text_input("CPF", placeholder="000.000.000-00")
                c_usuario = st.text_input("Usuário (sem espaços)")
                c_whats = st.text_input("WhatsApp")
                c_prof = st.text_input("Especialidade / área", placeholder="Ex: Manicure, Matemática...")
                c_neg = st.text_input("Nome do negócio")
                c_cor = st.color_picker("Cor de destaque", value="#1f6f52")
                c_senha = st.text_input("Criar senha", type="password")
                c_conf = st.text_input("Confirmar senha", type="password")
                if st.form_submit_button("Criar conta", use_container_width=True):
                    erros = []
                    if not c_nome.strip(): erros.append("Informe seu nome.")
                    if not cpf_valido(c_cpf): erros.append("CPF inválido.")
                    if not c_usuario.strip() or " " in c_usuario: erros.append("Usuário inválido.")
                    if not c_whats.strip(): erros.append("Informe seu WhatsApp.")
                    if len(c_senha) < 4: erros.append("Senha com mínimo 4 caracteres.")
                    if c_senha != c_conf: erros.append("Senhas não conferem.")
                    with db() as con:
                        if q1(con, "SELECT 1 FROM contas WHERE usuario=?", (c_usuario,)):
                            erros.append("Usuário já existe.")
                        if cpf_valido(c_cpf) and q1(con, "SELECT 1 FROM contas WHERE cpf=?", (so_digitos(c_cpf),)):
                            erros.append("Já existe uma conta cadastrada com esse CPF.")
                    if erros:
                        for e in erros: st.error(e)
                    else:
                        h, salt = hash_senha(c_senha)
                        with db() as con:
                            con.execute("""INSERT INTO contas(usuario,nome,senha_hash,senha_salt,tipo,profissao,
                                negocio,cor,whatsapp,trial_fim,codigo_ativacao,cpf) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (c_usuario.strip(), c_nome.strip(), h, salt, tipo, c_prof.strip() or tipo,
                                 c_neg.strip() or c_nome.strip(), c_cor, c_whats.strip(),
                                 (date.today()+timedelta(days=TRIAL_DIAS)).isoformat(), gerar_codigo(),
                                 so_digitos(c_cpf)))
                        st.session_state["_tela"] = "login"
                        st.success(f"Conta criada! Faça login com {c_usuario.strip()}. Trial de {TRIAL_DIAS} dias iniciado.")
            st.markdown('</div>', unsafe_allow_html=True)
            st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)
            if st.button("← Voltar para login", use_container_width=True, key="ir_login"):
                st.session_state["_tela"] = "login"
                st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# TELA DE BLOQUEIO
# ══════════════════════════════════════════════════════════════════════════════
def tela_bloqueio(conta):
    apply_css()
    usuario = conta["usuario"]
    motivo = "renovação" if conta["ativo"] else "trial"
    primeiro_nome = conta["nome"].split()[0]

    st.markdown(f"""
    <div style='max-width:640px;margin:2rem auto 1.4rem;text-align:center;'>
        <div style='font-size:2.2rem;margin-bottom:.3rem;'>🙏</div>
        <div style='font-family:Space Grotesk;font-size:1.7rem;font-weight:700;'>Obrigado por usar o ProManager, {primeiro_nome}!</div>
        <div style='color:#8b8f99;font-size:13.5px;margin-top:8px;line-height:1.7;'>
            {"Seu período de teste chegou ao fim." if motivo=="trial" else "Sua assinatura venceu."}
            Espero que tenha te ajudado a organizar {conta['negocio']}.<br>
            Pra continuar com acesso completo, é só <b style='color:#2fa574;'>{brl(VALOR_MENSAL)}/mês</b> — o valor de um lanche.
        </div>
    </div>""", unsafe_allow_html=True)

    token = mp_token()

    if token:
        # ── fluxo automático: cobrança real via Mercado Pago, checada a cada recarregamento ──
        pagamento = mp_status_pagamento(conta.get("mp_payment_id"))
        if not pagamento or pagamento.get("status") in ("cancelled", "rejected"):
            pagamento = mp_criar_cobranca(conta)

        if pagamento and pagamento.get("status") == "approved":
            ativar_conta(usuario)
            st.success("Pagamento identificado automaticamente! Sua conta foi liberada por 30 dias. 🎉")
            st.balloons()
            st.session_state.usuario_logado = usuario
            st.rerun()
        elif pagamento:
            qr_b64 = pagamento.get("point_of_interaction", {}).get("transaction_data", {}).get("qr_code_base64")
            copia_cola = pagamento.get("point_of_interaction", {}).get("transaction_data", {}).get("qr_code", "")
            _, mid, _ = st.columns([1, 1.2, 1])
            with mid:
                st.markdown('<div class="ticket">', unsafe_allow_html=True)
                st.markdown("##### Pague com Pix e a liberação acontece sozinha")
                if qr_b64:
                    st.image(base64.b64decode(qr_b64), width=220)
                if copia_cola:
                    st.text_area("Copia e cola:", value=copia_cola, height=90)
                st.caption("⏳ Verificando o pagamento automaticamente... assim que cair, a página libera sozinha.")
                st.markdown('</div>', unsafe_allow_html=True)
            # recarrega a página a cada 8s pra checar se o pagamento já caiu
            components.html("<script>setTimeout(()=>{try{window.parent.location.reload();}catch(e){}}, 8000);</script>",
                             height=0, width=0)
        else:
            st.error("Não consegui gerar a cobrança automática agora. Tente atualizar a página, ou fale no WhatsApp abaixo.")

    else:
        # ── fluxo manual: QR fixo + código liberado por você pelo WhatsApp ──
        codigo = conta["codigo_ativacao"]
        msg_cliente = (f"Olá! Quero assinar o ProManager.\nNome: {conta['nome']}\nUsuário: @{usuario}\n"
                       f"Acabei de fazer o Pix de R$ {VALOR_MENSAL:.2f} para {PIX_CHAVE}. Aguardo o código.")
        link_cliente = wa_link(ADMIN_WHATSAPP, msg_cliente)
        qr_bytes, payload = gerar_qr_pix(PIX_CHAVE, VALOR_MENSAL)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="ticket">', unsafe_allow_html=True)
            st.markdown("##### QR Code Pix")
            if qr_bytes: st.image(qr_bytes, width=180)
            st.text_area("Copia e cola:", value=payload, height=80)
            st.caption(f"Chave Pix (CPF): {cpf_fmt(PIX_CHAVE)}")
            st.markdown('</div>', unsafe_allow_html=True)
        with c2:
            st.markdown('<div class="ticket">', unsafe_allow_html=True)
            st.markdown("##### Fale comigo pelo WhatsApp")
            st.markdown(f"[📲 Enviar comprovante e falar com o suporte]({link_cliente})")
            st.caption(f"WhatsApp: {ADMIN_WHATSAPP[2:4]} {ADMIN_WHATSAPP[4:9]}-{ADMIN_WHATSAPP[9:]}")
            with st.form("f_ativar"):
                cod = st.text_input("Código de ativação recebido")
                if st.form_submit_button("Ativar minha conta", use_container_width=True):
                    if cod.strip().upper() == codigo.upper():
                        ativar_conta(usuario)
                        st.success("Conta ativada por 30 dias!")
                        st.balloons()
                        st.rerun()
                    else:
                        st.error("Código inválido.")
            st.markdown('</div>', unsafe_allow_html=True)
        st.caption("💡 Essa liberação está manual porque a conta do Mercado Pago ainda não foi conectada. "
                   "Configurando MP_ACCESS_TOKEN em Settings → Secrets, a liberação passa a ser automática.")

    if st.button("← Voltar ao login"):
        st.session_state.usuario_logado = None
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
NAV_ITEMS = [("inicio", "🏠", "Início"), ("agenda", "📅", "Agenda"),
             ("contatos", "👥", None), ("financeiro", "💳", "Financeiro"),
             ("relatorios", "📊", "Relatórios"), ("metas", "🎯", "Metas"), ("ajustes", "⚙️", "Ajustes")]

def render_sidebar(conta, L):
    if "nav" not in st.session_state:
        st.session_state.nav = "inicio"
    with st.sidebar:
        if conta.get("foto_base64"):
            avatar_html = f"<img src='data:image/jpeg;base64,{conta['foto_base64']}' style='width:58px;height:58px;border-radius:14px;object-fit:cover;margin:0 auto;display:block;border:2px solid {conta['cor']};'>"
        else:
            avatar_html = f"""<div style='width:58px;height:58px;border-radius:14px;background:{conta['cor']};display:flex;
                align-items:center;justify-content:center;font-family:Space Grotesk;font-size:20px;
                font-weight:700;color:#12151b;margin:0 auto;'>{ini(conta['nome'])}</div>"""
        slogan_html = (f"<div style='font-size:10.5px;color:#5c7fa8;font-style:italic;margin-top:2px;'>“{conta['slogan']}”</div>"
                        if conta.get('slogan') else
                        "<div style='font-size:10px;color:#5c6470;margin-top:2px;'>💬 adicione um slogan em Ajustes</div>")
        st.markdown(f"""<div style='text-align:center;padding:6px 0 14px;'>
            {avatar_html}
            <div style='font-family:Space Grotesk;font-weight:700;margin-top:8px;font-size:14px;'>{conta['nome']}</div>
            <div style='font-size:11.5px;color:#8b8f99;'>{L['icone']} {conta['profissao']}</div>
            {slogan_html}
        </div>""", unsafe_allow_html=True)

        if assinatura_valida(conta):
            d = dias_restantes(conta["validade"])
            st.markdown(f'<div class="al al-g">Ativo — {d} dia(s)</div>', unsafe_allow_html=True)
        elif trial_valido(conta):
            d = dias_restantes(conta["trial_fim"])
            st.markdown(f'<div class="al al-a">Trial — {d} dia(s)</div>', unsafe_allow_html=True)

        st.markdown('<div style="height:6px;"></div>', unsafe_allow_html=True)
        for key, icon, label in NAV_ITEMS:
            lbl = label or L["contato_pl"]
            ativo = st.session_state.nav == key
            if st.button(f"{icon}  {lbl}", key=f"nav_{key}", use_container_width=True,
                         type="primary" if ativo else "secondary"):
                st.session_state.nav = key
                st.rerun()

        st.markdown("<div style='height:14px;'></div><hr>", unsafe_allow_html=True)
        if st.button("↩  Sair da conta", use_container_width=True, key="nav_sair"):
            st.session_state.usuario_logado = None
            st.session_state.nav = "inicio"
            st.rerun()
        st.markdown("<div style='text-align:center;font-size:9.5px;color:#3a4048;margin-top:10px;'>build 2026-08-29-c</div>", unsafe_allow_html=True)

def aba_ajustes(conta):
    usuario = conta["usuario"]
    page_header("⚙️", "Ajustes", "Personalize seu negócio e sua conta")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown('<div class="fin-card">', unsafe_allow_html=True)
        st.markdown("**Dados do negócio**")
        if conta.get("foto_base64"):
            st.image(base64.b64decode(conta["foto_base64"]), width=90)
        foto = st.file_uploader("Foto ou logo do estabelecimento", type=["jpg", "jpeg", "png"], key="cfg_foto")
        if foto is not None:
            with db() as con:
                con.execute("UPDATE contas SET foto_base64=? WHERE usuario=?",
                            (base64.b64encode(foto.read()).decode(), usuario))
            st.success("Foto atualizada!"); st.rerun()
        nn = st.text_input("Nome do negócio", value=conta["negocio"], key="cfg_nn")
        slg = st.text_input("Slogan (aparece no menu e no link de agendamento)",
                             value=conta.get("slogan") or "", key="cfg_slogan",
                             placeholder="Ex: Seu sorriso, minha prioridade")
        nco = st.color_picker("Cor de destaque", value=conta["cor"], key="cfg_cor")
        if st.button("Salvar alterações"):
            with db() as con:
                con.execute("UPDATE contas SET negocio=?, cor=?, slogan=? WHERE usuario=?",
                            (nn, nco, slg.strip(), usuario))
            st.success("Salvo!"); st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    with c2:
        st.markdown('<div class="fin-card">', unsafe_allow_html=True)
        st.markdown("**Trocar senha**")
        with st.form("f_senha"):
            sa = st.text_input("Senha atual", type="password")
            sn = st.text_input("Nova senha", type="password")
            if st.form_submit_button("Trocar senha"):
                h, _ = hash_senha(sa, conta["senha_salt"])
                if h != conta["senha_hash"]:
                    st.error("Senha atual incorreta.")
                elif len(sn) < 4:
                    st.error("Mínimo 4 caracteres.")
                else:
                    h2, salt2 = hash_senha(sn)
                    with db() as con:
                        con.execute("UPDATE contas SET senha_hash=?, senha_salt=? WHERE usuario=?",
                                    (h2, salt2, usuario))
                    st.success("Senha trocada!")
        st.markdown('</div>', unsafe_allow_html=True)

    L = LABELS[conta["tipo"]]
    st.markdown(f"##### 🏷️ Meus {L['item'].lower()}s e valores")
    st.caption(f"O que estiver aqui é o que o cliente vê e o preço que já vem preenchido, tanto na sua Agenda quanto no link público de agendamento.")
    with db() as con:
        servicos = q(con, "SELECT * FROM servicos WHERE usuario=? ORDER BY nome", (usuario,))
    if not servicos:
        servicos_da_conta(usuario, conta["tipo"])  # semeia o catálogo padrão na primeira vez
        st.rerun()

    for s in servicos:
        cn, cv, cd = st.columns([3, 1.3, 0.6])
        cn.markdown(f"<div style='padding-top:8px;'>{s['nome']}</div>", unsafe_allow_html=True)
        cv.markdown(f"<div style='padding-top:8px;text-align:right;font-family:IBM Plex Mono;color:#2fa574;'>{brl(s['valor'])}</div>", unsafe_allow_html=True)
        if cd.button("🗑", key=f"del_serv_{s['id']}"):
            with db() as con:
                con.execute("DELETE FROM servicos WHERE id=?", (s["id"],))
            st.rerun()

    with st.expander(f"➕ Adicionar {L['item'].lower()}"):
        with st.form("f_servico", clear_on_submit=True):
            sn2 = st.text_input(f"Nome do {L['item'].lower()}")
            sv2 = st.number_input("Valor (R$)", min_value=0.0, step=5.0, value=50.0)
            if st.form_submit_button("Adicionar"):
                if sn2.strip():
                    with db() as con:
                        con.execute("INSERT INTO servicos(usuario,nome,valor) VALUES (?,?,?)",
                                    (usuario, sn2.strip(), float(sv2)))
                    st.success("Adicionado!"); st.rerun()
                else:
                    st.error("Informe um nome.")

# ══════════════════════════════════════════════════════════════════════════════
# PÁGINA PÚBLICA DE AGENDAMENTO
# ══════════════════════════════════════════════════════════════════════════════
def tela_publica_agendamento(usuario_prof):
    apply_css()
    with db() as con:
        conta = q1(con, "SELECT * FROM contas WHERE usuario=?", (usuario_prof,))
    if not conta:
        st.error("Link inválido — este profissional não foi encontrado.")
        return

    L = LABELS[conta["tipo"]]
    catalogo = servicos_da_conta(usuario_prof, conta["tipo"])
    slogan_html = f"<div style='font-size:11.5px;color:#5c7fa8;font-style:italic;margin-top:3px;'>“{conta['slogan']}”</div>" if conta.get("slogan") else ""
    foto_html = (f"<img src='data:image/jpeg;base64,{conta['foto_base64']}' style='width:64px;height:64px;border-radius:16px;object-fit:cover;margin:0 auto 8px;display:block;border:2px solid {conta['cor']};'>"
                 if conta.get("foto_base64") else "")
    st.markdown(f"""<div style='text-align:center;padding:1.8rem 0 1.4rem;'>
        {foto_html}
        <div class='brand-title' style='font-size:2rem;font-weight:700;'>{conta['negocio']}</div>
        {slogan_html}
        <div style='font-size:12.5px;color:#8b8f99;margin-top:4px;'>{L['icone']} agende seu horário — sem precisar ligar ou chamar no WhatsApp</div>
    </div>""", unsafe_allow_html=True)

    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.markdown('<div class="ticket">', unsafe_allow_html=True)
        opcoes_fmt = [f"{nome} — {brl(valor)}" for nome, valor in catalogo.items()]
        escolha = st.selectbox(L["item"], opcoes_fmt) if opcoes_fmt else None
        item = escolha.rsplit(" — ", 1)[0] if escolha else None
        d = st.date_input("Escolha o dia", value=date.today(), min_value=date.today(),
                           max_value=date.today() + timedelta(days=60))
        livres = horarios_livres(usuario_prof, d.isoformat())
        if not livres:
            st.warning("Não há horários livres nesse dia — tente outra data.")
            h = None
        else:
            h = st.selectbox("Horário disponível", livres)
        nome = st.text_input("Seu nome completo")
        tel = st.text_input("Seu WhatsApp")

        if st.button("Confirmar agendamento", use_container_width=True, disabled=not livres or not item):
            if not nome.strip() or not tel.strip():
                st.error("Preencha nome e WhatsApp.")
            else:
                horario_ocupado = False
                with db() as con:
                    ainda_livre = not q1(con, "SELECT 1 FROM atendimentos WHERE usuario=? AND data=? AND hora=? AND status!='falta'",
                                         (usuario_prof, d.isoformat(), h))
                    if not ainda_livre:
                        horario_ocupado = True
                    else:
                        existe = q1(con, "SELECT id FROM contatos WHERE usuario=? AND nome=?", (usuario_prof, nome.strip()))
                        if not existe:
                            con.execute("INSERT INTO contatos(usuario,nome,telefone,item_fav) VALUES (?,?,?,?)",
                                        (usuario_prof, nome.strip(), tel.strip(), item))
                        else:
                            con.execute("UPDATE contatos SET telefone=? WHERE id=?", (tel.strip(), existe["id"]))
                        con.execute("""INSERT INTO atendimentos(usuario,contato,item,valor,data,hora,status,origem)
                            VALUES (?,?,?,?,?,?,'aguardando','cliente')""",
                            (usuario_prof, nome.strip(), item, float(catalogo.get(item, 0)), d.isoformat(), h))
                # o "with" já fechou aqui em cima — o commit no banco já aconteceu de verdade
                # antes de recarregar a página (por isso o rerun fica fora do bloco)
                if horario_ocupado:
                    st.error("Esse horário acabou de ser preenchido — escolha outro.")
                else:
                    st.session_state["_agendado_ok"] = f"{nome.strip()} · {item} · {d.strftime('%d/%m')} às {h}"
                    st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

        if st.session_state.get("_agendado_ok"):
            info = st.session_state.pop("_agendado_ok")
            st.markdown(f"""<div class="receipt" style="margin-top:16px;">
                <div class="receipt-head"><span class="lbl">Agendamento solicitado</span><span class="val">✓</span></div>
                <div class="dashed"></div>
                <div style="font-size:13px;padding:8px 0;">{info}</div>
                <div class="dashed"></div>
                <div style="font-size:11.5px;color:#6b6552;padding-top:6px;">
                    {conta['nome'].split()[0]} vai confirmar com você em breve.
                </div>
            </div>""", unsafe_allow_html=True)
            wa = wa_link(conta["whatsapp"], f"Olá {conta['nome'].split()[0]}! Acabei de agendar {item} pelo link.")
            st.markdown(f"[📲 Avisar pelo WhatsApp também]({wa})")

# ══════════════════════════════════════════════════════════════════════════════
# PAINEL ÚNICO — "Início"
# ══════════════════════════════════════════════════════════════════════════════
def painel_inicio(conta, L):
    usuario = conta["usuario"]
    with db() as con:
        hoje_at = q(con, "SELECT * FROM atendimentos WHERE usuario=? AND data=? ORDER BY hora", (usuario, hoje_iso))
        conf_mes = q(con, "SELECT * FROM atendimentos WHERE usuario=? AND status='confirmado' AND substr(data,1,7)=?",
                     (usuario, hoje_iso[:7]))
        gastos_mes = q(con, "SELECT * FROM gastos WHERE usuario=? AND substr(data,1,7)=?", (usuario, hoje_iso[:7]))
        s0 = (date.today()-timedelta(days=date.today().weekday())).isoformat()
        conf_sem = q(con, "SELECT * FROM atendimentos WHERE usuario=? AND status='confirmado' AND data>=?",
                     (usuario, s0))
        proximos = q(con, "SELECT * FROM atendimentos WHERE usuario=? AND status='aguardando' AND data>=? ORDER BY data,hora LIMIT 5",
                     (usuario, hoje_iso))
        todos_at = q(con, "SELECT * FROM atendimentos WHERE usuario=?", (usuario,))

    c_head, c_av = st.columns([5, 1])
    slogan_html = f"<div style='font-size:12px;color:#5c7fa8;font-style:italic;margin-top:1px;'>“{conta['slogan']}”</div>" if conta.get("slogan") else ""
    c_head.markdown(f"""<div style='padding:.4rem 0 .6rem;'>
        <div class='brand-title' style='font-size:1.6rem;font-weight:700;'>{conta['negocio']}</div>
        {slogan_html}
        <div style='font-size:12.5px;color:#8b8f99;margin-top:2px;'>{datetime.today().strftime('%A, %d de %B')}</div>
    </div>""", unsafe_allow_html=True)
    c_av.markdown(f"""<div style='display:flex;justify-content:flex-end;padding-top:.5rem;'>
        {f"<img src='data:image/jpeg;base64,{conta['foto_base64']}' style='width:42px;height:42px;border-radius:11px;object-fit:cover;border:1px solid {conta['cor']};'>"
          if conta.get('foto_base64') else
          f"<div style='width:42px;height:42px;border-radius:11px;background:{conta['cor']};display:flex;align-items:center;justify-content:center;font-family:Space Grotesk;font-weight:700;font-size:14px;color:#12151b;'>{ini(conta['nome'])}</div>"}
    </div>""", unsafe_allow_html=True)

    with st.expander("🔗 Seu link de agendamento — clientes marcam sozinhos", expanded=not conta.get("app_url")):
        url_salva = conta.get("app_url")

        if url_salva:
            st.caption(f"🔗 Endereço salvo na sua conta: **{url_salva}**")
        else:
            st.caption("✍️ Cole abaixo o endereço que aparece na barra do navegador quando você acessa o app (não o localhost).")

        if "_base_url" not in st.session_state:
            st.session_state["_base_url"] = url_salva or ""

        cbase, csave = st.columns([4, 1])
        base = cbase.text_input("Endereço do seu app (o que aparece na barra do navegador)",
                                 key="_base_url", placeholder="https://seu-app.streamlit.app",
                                 help="Esse é o domínio que fica na barra do navegador quando VOCÊ acessa o app publicado.")
        csave.markdown("<div style='height:28px;'></div>", unsafe_allow_html=True)
        if csave.button("💾 Salvar", use_container_width=True):
            limpo = base.strip().rstrip("/")
            if not limpo.startswith(("http://", "https://")):
                st.error("A URL precisa começar com http:// ou https://")
            else:
                with db() as con:
                    con.execute("UPDATE contas SET app_url=? WHERE usuario=?", (limpo, conta["usuario"]))
                st.success("Salvo! Esse endereço agora é usado sempre no seu link de agendamento.")
                st.rerun()

        base_valida = base.strip().startswith(("http://", "https://")) and "seu-app.streamlit.app" not in base and "localhost" not in base
        if not base_valida:
            st.warning("⚠️ Preencha e salve o endereço real do seu app publicado acima — enquanto isso, o link abaixo **não vai funcionar** pros seus clientes.")
        link = f"{base.strip().rstrip('/')}/?agendar={conta['usuario']}" if base.strip() else ""
        cc1, cc2 = st.columns([3, 1])
        cc1.text_input("Link para compartilhar", value=link, key="_link_display", disabled=not base_valida)
        if HAS_QR and base_valida:
            qr = qrcode.QRCode(version=1, box_size=5, border=2)
            qr.add_data(link); qr.make(fit=True)
            img = qr.make_image(fill_color="#12151b", back_color="#f1ead6")
            buf = io.BytesIO(); img.save(buf, format="PNG")
            cc2.image(buf.getvalue(), width=90)
        if base_valida:
            st.caption("Mande esse link pelo WhatsApp, Instagram na bio, ou deixe o QR Code impresso no balcão.")

    total_conf = sum(a["valor"] for a in hoje_at if a["status"] == "confirmado")
    html = f"""<div class="receipt">
        <div class="receipt-head"><span class="lbl">Recibo de hoje</span><span class="val">{len(hoje_at)} atendimento(s)</span></div>
        <div class="dashed"></div>"""
    if not hoje_at:
        html += "<div style='color:#6b6552;font-size:13px;padding:10px 0;'>Nenhum atendimento agendado para hoje.</div>"
    for a in hoje_at:
        cls = {"confirmado": "pago", "aguardando": "aguarda", "falta": "falta"}[a["status"]]
        rot = {"confirmado": "pago", "aguardando": "aguarda", "falta": L["falta_verbo"].lower()}[a["status"]]
        html += f"""<div class="line-item">
            <span class="time">{a['hora']}</span>
            <div class="who"><b>{a['contato']}</b><span>{a['item']}</span></div>
            <span class="val">{brl(a['valor'])}</span>
            <span class="stamp {cls}">{rot}</span>
        </div>"""
    html += f"""<div class="dashed"></div>
        <div class="receipt-total"><span class="t">Confirmado hoje</span><span class="v">{brl(total_conf)}</span></div>
    </div>"""
    st.markdown(html, unsafe_allow_html=True)
    st.markdown('<div class="perf"></div>', unsafe_allow_html=True)

    ocupados = {a["hora"]: a for a in hoje_at}
    slots_html = '<div style="margin:14px 0 6px;">'
    for hh in range(7, 21):
        for mm in (0, 30):
            hstr = f"{hh:02d}:{mm:02d}"
            a = ocupados.get(hstr)
            if a and a["status"] == "falta":
                cls, tip = "sb2", f"{hstr} · {a['contato']} ({L['falta_verbo'].lower()})"
            elif a:
                cls, tip = "so", f"{hstr} · {a['contato']}"
            else:
                cls, tip = "sl", f"{hstr} · livre"
            slots_html += f'<span class="slot {cls}" title="{tip}">{hstr}</span>'
    slots_html += '</div>'
    st.markdown("##### Horários de hoje", unsafe_allow_html=True)
    st.markdown(slots_html, unsafe_allow_html=True)
    st.caption("🟩 ocupado · ⬜ livre · 🟥 falta — passe o mouse num horário pra ver quem está agendado")

    rec_sem = sum(a["valor"] for a in conf_sem)
    rec_total = sum(a["valor"] for a in todos_at if a["status"] == "confirmado")
    rec_mes = sum(a["valor"] for a in conf_mes)
    gt_mes = sum(g["valor"] for g in gastos_mes)
    lucro_mes = rec_mes - gt_mes
    dias_assin = dias_restantes(conta["validade"]) if assinatura_valida(conta) else dias_restantes(conta["trial_fim"])

    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(f'<div class="ticket"><div class="l">Receita da semana</div><div class="v green">{brl(rec_sem)}</div><div class="s">confirmados</div></div>', unsafe_allow_html=True)
    c2.markdown(f'<div class="ticket"><div class="l">Lucro do mês</div><div class="v {"green" if lucro_mes>=0 else "red"}">{brl(lucro_mes)}</div><div class="s">receita − gastos</div></div>', unsafe_allow_html=True)
    c3.markdown(f'<div class="ticket"><div class="l">Receita total</div><div class="v">{brl(rec_total)}</div><div class="s">histórico</div></div>', unsafe_allow_html=True)
    c4.markdown(f'<div class="ticket"><div class="l">{"Assinatura" if assinatura_valida(conta) else "Trial"}</div><div class="v">{dias_assin} dias</div><div class="s">restantes</div></div>', unsafe_allow_html=True)

    ticket_medio = (rec_mes / len(conf_mes)) if conf_mes else 0
    fin_html = f"""<div class="fin-card"><h3>Financeiro do mês</h3>
        <div class="fin-row"><span class="l">Receita do mês</span><span class="v" style="color:#2fa574;">{brl(rec_mes)}</span></div>
        <div class="fin-row"><span class="l">Gasto do mês</span><span class="v" style="color:#d9584a;">{brl(gt_mes)}</span></div>
        <div class="fin-row"><span class="l">Lucro do mês</span><span class="v" style="color:{'#2fa574' if lucro_mes>=0 else '#d9584a'};">{brl(lucro_mes)}</span></div>"""
    if lucro_mes < 0 and ticket_medio > 0:
        qtd = max(1, int(abs(lucro_mes) / ticket_medio) + 1)
        fin_html += f"""<div class="fin-row"><span class="l">Falta pra ficar no positivo</span>
            <span class="v" style="color:#e0a940;">{brl(abs(lucro_mes))} · ~{qtd} atendimento(s)</span></div>"""
    elif lucro_mes < 0:
        fin_html += f"""<div class="fin-row"><span class="l">Falta pra ficar no positivo</span>
            <span class="v" style="color:#e0a940;">{brl(abs(lucro_mes))}</span></div>"""
    fin_html += "</div>"
    st.markdown(fin_html, unsafe_allow_html=True)

    with db() as con:
        meta = q1(con, "SELECT * FROM metas WHERE usuario=? AND concluida=0 ORDER BY id LIMIT 1", (usuario,))
    if meta:
        atual = sum(a["valor"] for a in todos_at if a["status"] == "confirmado" and a["data"] >= meta["inicio"])
        pct = min(100, int(atual / max(meta["valor"], 1) * 100))
        falta = max(0, meta["valor"] - atual)
        cor = "#2fa574" if pct >= 100 else "#e0a940"
        st.markdown(f"""<div class="fin-card"><h3>Meta — {meta['nome']}</h3>
            <div class="fin-row"><span class="l">Progresso</span><span class="v">{brl(atual)} / {brl(meta['valor'])} ({pct}%)</span></div>
            <div class="meta-track"><div class="meta-fill" style="width:{pct}%;background:{cor};"></div></div>
            <div style="font-size:12px;color:#8b8f99;margin-top:8px;">
                {"Meta batida! 🎉" if pct>=100 else f"Faltam <b style='color:#ece6d7;'>{brl(falta)}</b> pra bater a meta."}
            </div>
        </div>""", unsafe_allow_html=True)
    else:
        st.caption("Nenhuma meta ativa — cadastre uma na aba Metas para acompanhar aqui.")

    st.markdown("<br>##### Próximos atendimentos", unsafe_allow_html=True)
    if not proximos:
        empty_state("📅", "Nada agendado além de hoje.")
    for a in proximos:
        st.markdown(f"""<div class="agenda-card">
            <span class="time">{a['data'][8:10]}/{a['data'][5:7]} {a['hora']}</span>
            <div class="init">{ini(a['contato'])}</div>
            <div class="info"><b>{a['contato']}</b><span>{a['item']}</span></div>
            <span class="val">{brl(a['valor'])}</span>
        </div>""", unsafe_allow_html=True)

    if assinatura_valida(conta):
        st.markdown(f"""<div class="pix-banner">
            <div><b>Renovação em {dias_restantes(conta['validade'])} dias</b>
            <span>Cobrança de {brl(VALOR_MENSAL)} via Pix — libere pelo menu de ativação quando vencer</span></div>
        </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# ABA AGENDA
# ══════════════════════════════════════════════════════════════════════════════
def aba_agenda(conta, L):
    usuario = conta["usuario"]
    catalogo = servicos_da_conta(usuario, conta["tipo"])
    with db() as con:
        contatos = [r["nome"] for r in q(con, "SELECT nome FROM contatos WHERE usuario=? ORDER BY nome", (usuario,))]
        hoje_at = q(con, "SELECT * FROM atendimentos WHERE usuario=? AND data=? ORDER BY hora", (usuario, hoje_iso))

    page_header("🗓️", "Agenda", "Novos agendamentos e o dia de hoje")
    cf, cl = st.columns(2)
    with cf:
        st.markdown(f"##### Novo {L['item'].lower()}")

        d = st.date_input("Data do agendamento", value=date.today(), key="_agenda_data")
        if d == date.today():
            rotulo_dia = "📍 Hoje"
        elif d == date.today() + timedelta(days=1):
            rotulo_dia = "📍 Amanhã"
        else:
            rotulo_dia = f"📍 {d.strftime('%A, %d/%m')}"
        st.caption(rotulo_dia)

        livres = horarios_livres(usuario, d.isoformat())

        with st.form("f_novo_at", clear_on_submit=True):
            escolha = st.selectbox(L["contato"], ["-- Novo --"] + contatos)
            novo_nome = st.text_input(f"Nome do novo {L['contato'].lower()}") if escolha == "-- Novo --" else None
            item = st.selectbox(L["item"], list(catalogo.keys()) or ["Cadastre em Ajustes"])
            valor = st.number_input("Valor (R$)", min_value=0.0, value=float(catalogo.get(item, 50)), step=5.0)
            if livres:
                h = st.selectbox(f"Horário disponível — {rotulo_dia.replace('📍 ', '')}", livres)
            else:
                h = None
                st.warning("Não sobrou horário livre nesse dia — escolha outra data acima.")
            dur = st.number_input("Duração (h)", min_value=0.5, value=1.0, step=0.5)
            obs = st.text_input("Observação")
            if st.form_submit_button("Agendar", use_container_width=True, disabled=not livres):
                nome_final = (novo_nome or "").strip() if escolha == "-- Novo --" else escolha
                if not nome_final:
                    st.error("Informe o nome.")
                else:
                    ocupado = False
                    with db() as con:
                        ja_ocupado = q1(con, "SELECT 1 FROM atendimentos WHERE usuario=? AND data=? AND hora=? AND status!='falta'",
                                        (usuario, d.isoformat(), h))
                        if ja_ocupado:
                            ocupado = True
                        else:
                            if escolha == "-- Novo --":
                                con.execute("INSERT INTO contatos(usuario,nome,item_fav) VALUES (?,?,?)",
                                            (usuario, nome_final, item))
                            con.execute("""INSERT INTO atendimentos(usuario,contato,item,valor,data,hora,duracao,status,obs)
                                VALUES (?,?,?,?,?,?,?, 'aguardando', ?)""",
                                (usuario, nome_final, item, float(valor), d.isoformat(), h, dur, obs))
                    # commit já aconteceu antes daqui — só então recarrega
                    if ocupado:
                        st.error(f"Esse horário ({h}) acabou de ser ocupado — escolha outro.")
                    else:
                        st.success(f"{nome_final} — {item} em {d.strftime('%d/%m')} às {h}")
                        st.rerun()

    with cl:
        st.markdown("##### Agenda de hoje")
        st.caption("🔗 ao lado do nome = a pessoa agendou sozinha pelo link, sem você mexer em nada.")
        if not hoje_at:
            empty_state("📭", "Nenhum atendimento hoje.")
        for a in hoje_at:
            c1, c2, c3, c4, c5 = st.columns([1, 2, 1.3, 1.4, 1.3])
            c1.write(a["hora"])
            tag = " 🔗" if a["origem"] == "cliente" else ""
            c2.write(a["contato"] + tag)
            c3.write(brl(a["valor"]))
            c4.write({"confirmado": "✓ confirmado", "aguardando": "⏳ aguardando", "falta": f"✗ {L['falta_verbo'].lower()}"}[a["status"]])
            if a["status"] == "aguardando":
                b1, b2, b3 = c5.columns(3)
                if b1.button("✓", key=f"ok_{a['id']}", help="Confirmar"):
                    with db() as con:
                        con.execute("UPDATE atendimentos SET status='confirmado' WHERE id=?", (a["id"],))
                        con.execute("UPDATE contatos SET visitas=visitas+1, gasto_total=gasto_total+? WHERE usuario=? AND nome=?",
                                    (a["valor"], usuario, a["contato"]))
                    st.rerun()
                if b2.button("✗", key=f"no_{a['id']}", help=f"Marcar {L['falta_verbo'].lower()}"):
                    with db() as con:
                        con.execute("UPDATE atendimentos SET status='falta' WHERE id=?", (a["id"],))
                        con.execute("UPDATE contatos SET faltas=faltas+1 WHERE usuario=? AND nome=?", (usuario, a["contato"]))
                    st.rerun()
                if b3.button("🗑", key=f"del_{a['id']}", help="Excluir este agendamento"):
                    with db() as con:
                        con.execute("DELETE FROM atendimentos WHERE id=?", (a["id"],))
                    st.rerun()
            else:
                if c5.button("🗑 excluir", key=f"del_{a['id']}", help="Excluir este agendamento", use_container_width=True):
                    with db() as con:
                        con.execute("DELETE FROM atendimentos WHERE id=?", (a["id"],))
                    st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# ABA CONTATOS
# ══════════════════════════════════════════════════════════════════════════════
def aba_contatos(conta, L):
    usuario = conta["usuario"]
    with db() as con:
        contatos = q(con, "SELECT * FROM contatos WHERE usuario=? ORDER BY visitas DESC", (usuario,))

    page_header("👥", f"{L['contato_pl']} cadastrados", f"{len(contatos)} no total")

    if contatos:
        cards = ""
        for c in contatos:
            if c["faltas"] >= 3:
                chip_cls, chip_txt = "chip-r", "🚨 risco"
            elif c["faltas"] >= 1:
                chip_cls, chip_txt = "chip-a", "⚠️ atenção"
            elif c["visitas"] >= LIMIAR_CLIENTE_FIEL:
                chip_cls, chip_txt = "chip-g", "⭐ fiel"
            else:
                chip_cls, chip_txt = "chip-g", "✅ ok"
            cor = cor_avatar(c["nome"])
            tel_html = (f'<a href="{wa_link(so_digitos(c["telefone"]), f"Olá {c["nome"].split()[0]}!")}" target="_blank" style="color:#7fd6b3;text-decoration:none;">📲 {c["telefone"]}</a>'
                        if c["telefone"] else "sem telefone")
            cards += f"""<div class="contact-card">
                <div class="cc-top">
                    <div class="cc-avatar" style="background:{cor}22;color:{cor};border-color:{cor}55;">{ini(c['nome'])}</div>
                    <div style="min-width:0;">
                        <div class="cc-name">{c['nome']}<span class="chip {chip_cls}">{chip_txt}</span></div>
                        <div class="cc-meta">{tel_html} · {c['item_fav'] or L['item']+' não definido'}</div>
                    </div>
                </div>
                <div class="cc-stats">
                    <div class="cc-stat"><b>{c['visitas']}</b><span>Visitas</span></div>
                    <div class="cc-stat"><b>{c['faltas']}</b><span>{L['falta_verbo']}s</span></div>
                    <div class="cc-stat"><b>{brl(c['gasto_total'])}</b><span>Gasto</span></div>
                </div>
            </div>"""
        st.markdown(f'<div class="contact-grid">{cards}</div>', unsafe_allow_html=True)
    else:
        empty_state("🗂️", f"Nenhum {L['contato'].lower()} cadastrado ainda — cadastre o primeiro abaixo.")

    with st.expander(f"➕ Cadastrar {L['contato'].lower()}"):
        with st.form("f_contato", clear_on_submit=True):
            n = st.text_input("Nome")
            t = st.text_input("Telefone")
            fav = st.selectbox(f"{L['item']} favorito", list(servicos_da_conta(usuario, conta['tipo']).keys()))
            if st.form_submit_button("Cadastrar"):
                if n.strip():
                    with db() as con:
                        con.execute("INSERT INTO contatos(usuario,nome,telefone,item_fav) VALUES (?,?,?,?)",
                                    (usuario, n.strip(), t.strip(), fav))
                    st.success("Cadastrado!"); st.rerun()
                else:
                    st.error("Informe o nome.")

# ══════════════════════════════════════════════════════════════════════════════
# ABA FINANCEIRO
# ══════════════════════════════════════════════════════════════════════════════
def aba_financeiro(conta):
    usuario = conta["usuario"]
    with db() as con:
        gastos = q(con, "SELECT * FROM gastos WHERE usuario=? ORDER BY data DESC", (usuario,))
        conf = q(con, "SELECT * FROM atendimentos WHERE usuario=? AND status='confirmado'", (usuario,))

    page_header("💹", "Financeiro", "Receita, gastos e pra onde seu dinheiro está indo")

    rt = sum(a["valor"] for a in conf); gt = sum(g["valor"] for g in gastos); lucro = rt - gt
    c1, c2, c3 = st.columns(3)
    c1.markdown(f'<div class="ticket"><div class="l">💰 Receita total</div><div class="v green">{brl(rt)}</div></div>', unsafe_allow_html=True)
    c2.markdown(f'<div class="ticket"><div class="l">🧾 Gastos</div><div class="v red">{brl(gt)}</div></div>', unsafe_allow_html=True)
    c3.markdown(f'<div class="ticket"><div class="l">📈 Lucro líquido</div><div class="v {"green" if lucro>=0 else "red"}">{brl(lucro)}</div></div>', unsafe_allow_html=True)

    st.markdown("##### Registrar gasto")
    with st.form("f_gasto", clear_on_submit=True):
        d = st.text_input("Descrição")
        cat = st.selectbox("Categoria", CATEGORIAS_GASTO)
        v = st.number_input("Valor (R$)", min_value=0.0, step=5.0)
        dt = st.date_input("Data", value=date.today())
        if st.form_submit_button("Registrar"):
            if d.strip() and v > 0:
                with db() as con:
                    con.execute("INSERT INTO gastos(usuario,descricao,categoria,valor,data) VALUES (?,?,?,?,?)",
                                (usuario, d.strip(), cat, float(v), dt.isoformat()))
                st.success("Registrado!"); st.rerun()
            else:
                st.error("Preencha descrição e valor.")

    if gastos:
        cores_pizza = ["#2fa574","#00b8d9","#7c5cff","#e0a940","#d9584a","#ff6b9d","#5cc9c9","#8b8f99","#c9a86c"]
        cm = {}
        for g in gastos: cm[g["categoria"]] = cm.get(g["categoria"], 0) + g["valor"]
        categorias_ord = sorted(cm.items(), key=lambda x: -x[1])

        st.markdown("##### Para onde foi o dinheiro")
        cg, cleg = st.columns([1.3, 1])
        with cg:
            fig = go.Figure(go.Pie(labels=[c for c, _ in categorias_ord], values=[v for _, v in categorias_ord], hole=.6,
                marker=dict(colors=cores_pizza, line=dict(color="#0d0f14", width=2)),
                textfont=dict(color="#ece6d7"), textinfo="percent"))
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#ece6d7"), margin=dict(t=10,b=10,l=10,r=10), height=260, showlegend=False,
                annotations=[dict(text=f"{brl(sum(v for _,v in categorias_ord))}<br><span style='font-size:10px;color:#8b8f99;'>total</span>",
                                   x=0.5, y=0.5, showarrow=False, font=dict(size=15, color="#ece6d7", family="Orbitron"))])
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        with cleg:
            st.markdown('<div style="padding-top:8px;">', unsafe_allow_html=True)
            legend_html = ""
            for i, (cat, val) in enumerate(categorias_ord):
                legend_html += f"""<div class="legend-row">
                    <span class="dot" style="background:{cores_pizza[i % len(cores_pizza)]};"></span>
                    <span class="lbl">{cat}</span><span class="val">{brl(val)}</span>
                </div>"""
            st.markdown(legend_html, unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)

        st.markdown("##### Histórico de gastos")
        df = pd.DataFrame([{"Data": g["data"], "Descrição": g["descricao"], "Categoria": g["categoria"], "Valor": brl(g["valor"])} for g in gastos])
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        empty_state("🧾", "Nenhum gasto registrado ainda — comece registrando um acima.")

# ══════════════════════════════════════════════════════════════════════════════
# ABA METAS
# ══════════════════════════════════════════════════════════════════════════════
def aba_relatorios(conta, L):
    usuario = conta["usuario"]
    with db() as con:
        conf = q(con, "SELECT * FROM atendimentos WHERE usuario=? AND status='confirmado'", (usuario,))
        gastos = q(con, "SELECT * FROM gastos WHERE usuario=?", (usuario,))

    page_header("📊", "Relatórios", "Histórico completo do seu negócio, mês a mês")

    if not conf and not gastos:
        empty_state("📊", "Ainda não há atendimentos confirmados nem gastos registrados pra gerar relatórios.")
        return

    rec_total = sum(a["valor"] for a in conf)
    gt_total = sum(g["valor"] for g in gastos)
    lucro_total = rec_total - gt_total

    c1, c2, c3 = st.columns(3)
    c1.markdown(f'<div class="ticket"><div class="l">💰 Faturamento total</div><div class="v green">{brl(rec_total)}</div><div class="s">todo o histórico</div></div>', unsafe_allow_html=True)
    c2.markdown(f'<div class="ticket"><div class="l">🧾 Gasto total</div><div class="v red">{brl(gt_total)}</div><div class="s">todo o histórico</div></div>', unsafe_allow_html=True)
    c3.markdown(f'<div class="ticket"><div class="l">📈 Lucro total</div><div class="v {"green" if lucro_total>=0 else "red"}">{brl(lucro_total)}</div><div class="s">receita − gastos</div></div>', unsafe_allow_html=True)

    # agregação por mês
    meses = {}
    for a in conf:
        m = a["data"][:7]
        meses.setdefault(m, {"receita": 0.0, "qtd": 0})
        meses[m]["receita"] += a["valor"]
        meses[m]["qtd"] += 1
    for g in gastos:
        m = g["data"][:7]
        meses.setdefault(m, {"receita": 0.0, "qtd": 0})
        meses[m].setdefault("gasto", 0.0)
    for m in meses:
        meses[m].setdefault("gasto", 0.0)
    for g in gastos:
        meses[g["data"][:7]]["gasto"] += g["valor"]

    mes_recorde = max(meses.items(), key=lambda kv: kv[1]["qtd"])[0] if meses else None
    mes_recorde_fat = max(meses.items(), key=lambda kv: kv[1]["receita"])[0] if meses else None

    # serviço/item com maior retorno
    por_item = {}
    for a in conf:
        por_item[a["item"]] = por_item.get(a["item"], 0.0) + a["valor"]
    item_campeao = max(por_item.items(), key=lambda kv: kv[1]) if por_item else None

    # categoria de gasto que mais pesou
    por_cat = {}
    for g in gastos:
        por_cat[g["categoria"]] = por_cat.get(g["categoria"], 0.0) + g["valor"]
    cat_campea = max(por_cat.items(), key=lambda kv: kv[1]) if por_cat else None

    def nome_mes(m):
        try: return datetime.strptime(m, "%Y-%m").strftime("%B/%Y")
        except Exception: return m

    st.markdown("<br>##### Destaques", unsafe_allow_html=True)
    d1, d2, d3, d4 = st.columns(4)
    d1.markdown(f'<div class="fin-card"><h3>📅 Mês mais movimentado</h3><div style="font-size:15px;font-weight:700;">{nome_mes(mes_recorde) if mes_recorde else "—"}</div><div style="font-size:11.5px;color:#8b8f99;">{meses[mes_recorde]["qtd"] if mes_recorde else 0} atendimentos</div></div>', unsafe_allow_html=True)
    d2.markdown(f'<div class="fin-card"><h3>💵 Mês de maior faturamento</h3><div style="font-size:15px;font-weight:700;">{nome_mes(mes_recorde_fat) if mes_recorde_fat else "—"}</div><div style="font-size:11.5px;color:#8b8f99;">{brl(meses[mes_recorde_fat]["receita"]) if mes_recorde_fat else brl(0)}</div></div>', unsafe_allow_html=True)
    d3.markdown(f'<div class="fin-card"><h3>🏆 {L["item"]} com mais retorno</h3><div style="font-size:15px;font-weight:700;">{item_campeao[0] if item_campeao else "—"}</div><div style="font-size:11.5px;color:#8b8f99;">{brl(item_campeao[1]) if item_campeao else brl(0)} gerados</div></div>', unsafe_allow_html=True)
    d4.markdown(f'<div class="fin-card"><h3>🧾 Categoria que mais pesou</h3><div style="font-size:15px;font-weight:700;">{cat_campea[0] if cat_campea else "—"}</div><div style="font-size:11.5px;color:#8b8f99;">{brl(cat_campea[1]) if cat_campea else brl(0)} gastos</div></div>', unsafe_allow_html=True)

    st.markdown("<br>##### Receita × Gasto × Lucro por mês", unsafe_allow_html=True)
    ordenados = sorted(meses.items())
    fig = go.Figure()
    fig.add_trace(go.Bar(name="Receita", x=[nome_mes(m) for m, _ in ordenados], y=[v["receita"] for _, v in ordenados], marker_color="#2fa574"))
    fig.add_trace(go.Bar(name="Gasto", x=[nome_mes(m) for m, _ in ordenados], y=[v["gasto"] for _, v in ordenados], marker_color="#d9584a"))
    fig.add_trace(go.Scatter(name="Lucro", x=[nome_mes(m) for m, _ in ordenados], y=[v["receita"]-v["gasto"] for _, v in ordenados],
                              mode="lines+markers", line=dict(color="#00d4ff", width=2)))
    fig.update_layout(barmode="group", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#ece6d7"), margin=dict(t=10, b=10, l=10, r=10), height=320,
        legend=dict(font=dict(color="#ece6d7")), xaxis=dict(gridcolor="#ffffff10"), yaxis=dict(gridcolor="#ffffff10"))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    st.markdown("##### Ranking de serviços por retorno", unsafe_allow_html=True)
    rank = sorted(por_item.items(), key=lambda kv: -kv[1])
    df_rank = pd.DataFrame([{L["item"]: nome, "Total gerado": brl(valor), "Vezes realizado": sum(1 for a in conf if a["item"] == nome)} for nome, valor in rank])
    st.dataframe(df_rank, use_container_width=True, hide_index=True)

def aba_metas(conta):
    usuario = conta["usuario"]
    with db() as con:
        metas = q(con, "SELECT * FROM metas WHERE usuario=? AND concluida=0", (usuario,))
        conf = q(con, "SELECT * FROM atendimentos WHERE usuario=? AND status='confirmado'", (usuario,))

    page_header("🎯", "Metas", "Defina um alvo e acompanhe o progresso em tempo real")

    with st.expander("➕ Adicionar meta", expanded=not metas):
        with st.form("f_meta", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            n = c1.text_input("Nome da meta")
            v = c2.number_input("Valor alvo (R$)", min_value=1.0, value=500.0, step=50.0)
            ini_d = c3.date_input("Data início", value=date.today())
            if st.form_submit_button("Adicionar meta"):
                if n.strip():
                    with db() as con:
                        con.execute("INSERT INTO metas(usuario,nome,valor,inicio) VALUES (?,?,?,?)",
                                    (usuario, n.strip(), float(v), ini_d.isoformat()))
                    st.rerun()

    if not metas:
        empty_state("🎯", "Nenhuma meta ativa — cadastre uma acima para começar a acompanhar.")
    else:
        cards = ""
        for m in metas:
            atual = sum(a["valor"] for a in conf if a["data"] >= m["inicio"])
            pct = min(100, int(atual / max(m["valor"], 1) * 100))
            done = pct >= 100
            nota = "Meta batida! 🎉" if done else f"Faltam <b style='color:#ece6d7;'>{brl(max(0, m['valor']-atual))}</b> pra bater a meta."
            cards += f"""<div class="goal-card">
                <div class="gc-top"><span class="gc-name">🎯 {m['nome']}</span>
                    <span class="gc-pct" style="color:{'#2fa574' if done else '#00d4ff'};">{pct}%</span></div>
                <div class="gc-vals">{brl(atual)} de {brl(m['valor'])}</div>
                <div class="goal-track"><div class="goal-fill {'done' if done else ''}" style="width:{pct}%;"></div></div>
                <div class="gc-note">{nota}</div>
            </div>"""
        st.markdown(f'<div class="goal-grid">{cards}</div>', unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# ROTEADOR
# ══════════════════════════════════════════════════════════════════════════════
usuario_publico = st.query_params.get("agendar")

if usuario_publico:
    tela_publica_agendamento(usuario_publico)
    st.stop()

usuario = st.session_state.usuario_logado

if not usuario:
    tela_login()
else:
    with db() as con:
        conta = q1(con, "SELECT * FROM contas WHERE usuario=?", (usuario,))
    if not conta:
        st.session_state.usuario_logado = None; st.rerun()
    elif not acesso_ok(conta):
        apply_css()
        tela_bloqueio(conta)
    else:
        apply_css()
        L = LABELS[conta["tipo"]]
        render_sidebar(conta, L)

        nav = st.session_state.get("nav", "inicio")
        if nav == "inicio": painel_inicio(conta, L)
        elif nav == "agenda": aba_agenda(conta, L)
        elif nav == "contatos": aba_contatos(conta, L)
        elif nav == "financeiro": aba_financeiro(conta)
        elif nav == "relatorios": aba_relatorios(conta, L)
        elif nav == "metas": aba_metas(conta)
        elif nav == "ajustes": aba_ajustes(conta)
