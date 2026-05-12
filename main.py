"""
Gestionale semplice — single-file FastAPI app.
Deploy su Render: un solo servizio web, nessuna configurazione DNS.

Variabili ambiente da impostare su Render:
  ADMIN_EMAIL          ceraldigroupsrl@gmail.com
  ADMIN_PASSWORD_HASH  (hash bcrypt della password)
  SECRET_KEY           (stringa casuale lunga, es. uuid4)
  MONGO_URI            (opzionale — se vuoi persistenza MongoDB Atlas)
"""
from __future__ import annotations

import os
import jwt
import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "ceraldigroupsrl@gmail.com")
ADMIN_PASSWORD_HASH = os.getenv(
    "ADMIN_PASSWORD_HASH",
    "$2b$12$vYnu0jbr.Z3eRRbTbTAsYuvG99zKhSarWowGpRVH9knuBkWlJaQ6m",  # hash locale dev
)
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-use-a-long-random-string")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

MONGO_URL = os.getenv("MONGO_URL", "")
DB_NAME   = os.getenv("DB_NAME", "Gestionale")

# ── Database (opzionale) ──────────────────────────────────────────────────────

db = None
if MONGO_URL:
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        _client = AsyncIOMotorClient(MONGO_URL)
        db = _client[DB_NAME]
        print(f"[INFO] MongoDB connesso: {DB_NAME}")
    except Exception as e:
        print(f"[WARN] MongoDB non disponibile: {e}")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Gestionale", docs_url=None, redoc_url=None)

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _make_token(email: str) -> str:
    payload = {
        "sub": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _verify_token(token: str) -> Optional[str]:
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return data.get("sub")
    except jwt.PyJWTError:
        return None


def _set_cookie(response: Response, token: str) -> None:
    is_prod = os.getenv("RENDER") == "true"
    response.set_cookie("auth_token", token, httponly=True, samesite="strict",
                        secure=is_prod, max_age=TOKEN_EXPIRE_HOURS * 3600, path="/")
    response.set_cookie("session_active", "1", httponly=False, samesite="strict",
                        secure=is_prod, max_age=TOKEN_EXPIRE_HOURS * 3600, path="/")


def get_current_user(request: Request) -> str:
    token = request.cookies.get("auth_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Non autenticato")
    email = _verify_token(token)
    if not email:
        raise HTTPException(status_code=401, detail="Token non valido")
    return email

# ── Models ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str

# ── Auth routes ───────────────────────────────────────────────────────────────

@app.post("/api/login")
async def login(body: LoginRequest):
    if body.email != ADMIN_EMAIL:
        raise HTTPException(status_code=401, detail="Credenziali errate")
    if not bcrypt.checkpw(body.password.encode(), ADMIN_PASSWORD_HASH.encode()):
        raise HTTPException(status_code=401, detail="Credenziali errate")
    token = _make_token(body.email)
    response = JSONResponse({"ok": True})
    _set_cookie(response, token)
    return response


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("auth_token", path="/")
    response.delete_cookie("session_active", path="/")
    return response


@app.get("/api/me")
async def me(user: str = Depends(get_current_user)):
    return {"email": user}

# ── Data routes (MongoDB opzionale) ──────────────────────────────────────────

@app.get("/api/records")
async def list_records(user: str = Depends(get_current_user)):
    if db is None:
        return {"records": [], "note": "MongoDB non configurato"}
    docs = await db["records"].find({}).sort("_id", -1).limit(100).to_list(100)
    for d in docs:
        d["_id"] = str(d["_id"])
    return {"records": docs}


@app.post("/api/records")
async def create_record(request: Request, user: str = Depends(get_current_user)):
    body = await request.json()
    if db is None:
        return {"ok": False, "note": "MongoDB non configurato"}
    body["created_at"] = datetime.now(timezone.utc).isoformat()
    body["created_by"] = user
    result = await db["records"].insert_one(body)
    return {"ok": True, "id": str(result.inserted_id)}

# ── Frontend (SPA inline) ─────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gestionale</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',sans-serif;background:#f0f2f5;color:#1a1a2e}
  #app{min-height:100vh;display:flex;flex-direction:column}

  /* Login */
  #login-screen{flex:1;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#1a1a2e,#16213e)}
  .login-box{background:#fff;border-radius:16px;padding:40px;width:360px;box-shadow:0 20px 60px rgba(0,0,0,.3)}
  .login-box h1{font-size:1.6rem;margin-bottom:8px;color:#1a1a2e}
  .login-box p{color:#666;margin-bottom:28px;font-size:.9rem}
  .field{margin-bottom:16px}
  .field label{display:block;font-size:.8rem;font-weight:600;color:#444;margin-bottom:6px}
  .field input{width:100%;padding:10px 14px;border:1.5px solid #ddd;border-radius:8px;font-size:.95rem;transition:border .2s}
  .field input:focus{outline:none;border-color:#4f46e5}
  .btn-primary{width:100%;padding:12px;background:#4f46e5;color:#fff;border:none;border-radius:8px;font-size:1rem;font-weight:600;cursor:pointer;transition:background .2s}
  .btn-primary:hover{background:#4338ca}
  .btn-primary:disabled{background:#a5b4fc;cursor:not-allowed}
  .error-msg{background:#fee2e2;color:#dc2626;padding:10px 14px;border-radius:8px;font-size:.85rem;margin-bottom:16px;display:none}

  /* Dashboard */
  #dashboard{flex:1;display:none;flex-direction:column}
  .topbar{background:#fff;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 1px 3px rgba(0,0,0,.1)}
  .topbar h2{font-size:1.1rem;color:#1a1a2e}
  .topbar-right{display:flex;align-items:center;gap:12px}
  .user-badge{background:#eef2ff;color:#4f46e5;padding:6px 14px;border-radius:20px;font-size:.8rem;font-weight:600}
  .btn-logout{background:none;border:1.5px solid #dc2626;color:#dc2626;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:.8rem;font-weight:600}
  .btn-logout:hover{background:#fee2e2}

  .content{flex:1;padding:24px;max-width:1100px;margin:0 auto;width:100%}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:28px}
  .card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
  .card-label{font-size:.75rem;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:.05em}
  .card-value{font-size:2rem;font-weight:700;color:#1a1a2e;margin-top:4px}
  .card-sub{font-size:.8rem;color:#4f46e5;margin-top:2px}

  .panel{background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:20px}
  .panel h3{font-size:1rem;font-weight:700;margin-bottom:16px;color:#1a1a2e}
  .form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
  .form-row input,.form-row select{padding:9px 12px;border:1.5px solid #ddd;border-radius:8px;font-size:.9rem;width:100%}
  .form-row input:focus,.form-row select:focus{outline:none;border-color:#4f46e5}
  .btn-add{background:#4f46e5;color:#fff;border:none;border-radius:8px;padding:9px 20px;cursor:pointer;font-size:.9rem;font-weight:600}
  .btn-add:hover{background:#4338ca}

  table{width:100%;border-collapse:collapse;font-size:.875rem}
  th{text-align:left;padding:10px 12px;background:#f8fafc;color:#666;font-weight:600;border-bottom:1px solid #e5e7eb}
  td{padding:10px 12px;border-bottom:1px solid #f1f5f9;color:#374151}
  tr:hover td{background:#fafbff}
  .badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.75rem;font-weight:600}
  .badge-entrata{background:#dcfce7;color:#16a34a}
  .badge-uscita{background:#fee2e2;color:#dc2626}
  .empty-state{text-align:center;padding:40px;color:#aaa}

  #status-bar{position:fixed;bottom:16px;right:16px;background:#1a1a2e;color:#fff;padding:10px 18px;border-radius:8px;font-size:.85rem;display:none;z-index:999;box-shadow:0 4px 12px rgba(0,0,0,.2)}
</style>
</head>
<body>
<div id="app">

<!-- LOGIN -->
<div id="login-screen">
  <div class="login-box">
    <h1>Gestionale</h1>
    <p>Accedi al tuo account aziendale</p>
    <div class="error-msg" id="login-error"></div>
    <div class="field"><label>Email</label><input type="email" id="email" placeholder="email@azienda.it" autocomplete="username"></div>
    <div class="field"><label>Password</label><input type="password" id="password" autocomplete="current-password"></div>
    <button class="btn-primary" id="login-btn" onclick="doLogin()">Accedi</button>
  </div>
</div>

<!-- DASHBOARD -->
<div id="dashboard">
  <div class="topbar">
    <h2>📊 Gestionale</h2>
    <div class="topbar-right">
      <span class="user-badge" id="user-email">—</span>
      <button class="btn-logout" onclick="doLogout()">Esci</button>
    </div>
  </div>

  <div class="content">
    <!-- KPI cards -->
    <div class="cards">
      <div class="card">
        <div class="card-label">Entrate totali</div>
        <div class="card-value" id="kpi-entrate">€ 0</div>
        <div class="card-sub" id="kpi-entrate-n">0 movimenti</div>
      </div>
      <div class="card">
        <div class="card-label">Uscite totali</div>
        <div class="card-value" id="kpi-uscite">€ 0</div>
        <div class="card-sub" id="kpi-uscite-n">0 movimenti</div>
      </div>
      <div class="card">
        <div class="card-label">Saldo</div>
        <div class="card-value" id="kpi-saldo">€ 0</div>
        <div class="card-sub">entrate - uscite</div>
      </div>
      <div class="card">
        <div class="card-label">Totale record</div>
        <div class="card-value" id="kpi-total">0</div>
        <div class="card-sub">nel database</div>
      </div>
    </div>

    <!-- Nuovo record -->
    <div class="panel">
      <h3>➕ Nuovo movimento</h3>
      <div class="form-row">
        <input type="text" id="rec-desc" placeholder="Descrizione">
        <input type="number" id="rec-importo" placeholder="Importo (€)" step="0.01">
      </div>
      <div class="form-row">
        <select id="rec-tipo"><option value="entrata">Entrata</option><option value="uscita">Uscita</option></select>
        <input type="date" id="rec-data">
      </div>
      <button class="btn-add" onclick="addRecord()">Salva movimento</button>
    </div>

    <!-- Lista record -->
    <div class="panel">
      <h3>📋 Movimenti</h3>
      <div id="records-container"><div class="empty-state">Caricamento...</div></div>
    </div>
  </div>
</div>

</div><!-- #app -->
<div id="status-bar"></div>

<script>
const $ = id => document.getElementById(id);

function showStatus(msg, ms=2500) {
  const el = $('status-bar');
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', ms);
}

function fmtEur(n) {
  return '€ ' + Number(n||0).toLocaleString('it-IT', {minimumFractionDigits:2, maximumFractionDigits:2});
}

// ── Auth ─────────────────────────────────────────────────────────────────────

async function doLogin() {
  const btn = $('login-btn');
  const err = $('login-error');
  err.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Accesso in corso...';
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({email: $('email').value, password: $('password').value}),
      credentials: 'include'
    });
    if (!res.ok) {
      const d = await res.json().catch(()=>({}));
      err.textContent = d.detail || 'Credenziali errate';
      err.style.display = 'block';
    } else {
      await loadDashboard();
    }
  } catch(e) {
    err.textContent = 'Errore di rete';
    err.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Accedi';
  }
}

async function doLogout() {
  await fetch('/api/logout', {method:'POST', credentials:'include'});
  location.reload();
}

// ── Dashboard ─────────────────────────────────────────────────────────────────

let allRecords = [];

async function loadDashboard() {
  try {
    const me = await fetch('/api/me', {credentials:'include'});
    if (!me.ok) { showLogin(); return; }
    const {email} = await me.json();
    $('user-email').textContent = email;
    $('login-screen').style.display = 'none';
    $('dashboard').style.display = 'flex';
    $('rec-data').value = new Date().toISOString().slice(0,10);
    await loadRecords();
  } catch(e) { showLogin(); }
}

function showLogin() {
  $('login-screen').style.display = 'flex';
  $('dashboard').style.display = 'none';
}

async function loadRecords() {
  const res = await fetch('/api/records', {credentials:'include'});
  const data = await res.json();
  allRecords = data.records || [];
  renderRecords();
  updateKPIs();
}

function updateKPIs() {
  const entrate = allRecords.filter(r=>r.tipo==='entrata');
  const uscite  = allRecords.filter(r=>r.tipo==='uscita');
  const sumE = entrate.reduce((a,r)=>a+(parseFloat(r.importo)||0),0);
  const sumU = uscite.reduce((a,r)=>a+(parseFloat(r.importo)||0),0);
  $('kpi-entrate').textContent = fmtEur(sumE);
  $('kpi-entrate-n').textContent = entrate.length + ' movimenti';
  $('kpi-uscite').textContent = fmtEur(sumU);
  $('kpi-uscite-n').textContent = uscite.length + ' movimenti';
  $('kpi-saldo').textContent = fmtEur(sumE - sumU);
  $('kpi-saldo').style.color = (sumE-sumU) >= 0 ? '#16a34a' : '#dc2626';
  $('kpi-total').textContent = allRecords.length;
}

function renderRecords() {
  const c = $('records-container');
  if (!allRecords.length) {
    c.innerHTML = '<div class="empty-state">Nessun movimento registrato</div>';
    return;
  }
  c.innerHTML = '<table><thead><tr><th>Data</th><th>Descrizione</th><th>Tipo</th><th>Importo</th></tr></thead><tbody>' +
    allRecords.map(r => `
      <tr>
        <td>${r.data||'—'}</td>
        <td>${r.descrizione||'—'}</td>
        <td><span class="badge badge-${r.tipo||'entrata'}">${r.tipo||'—'}</span></td>
        <td><strong>${fmtEur(r.importo)}</strong></td>
      </tr>`).join('') +
    '</tbody></table>';
}

async function addRecord() {
  const desc = $('rec-desc').value.trim();
  const importo = parseFloat($('rec-importo').value);
  const tipo = $('rec-tipo').value;
  const data = $('rec-data').value;
  if (!desc || isNaN(importo) || importo <= 0) {
    showStatus('⚠️ Compila descrizione e importo'); return;
  }
  const res = await fetch('/api/records', {
    method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({descrizione:desc, importo, tipo, data})
  });
  const r = await res.json();
  if (r.ok) {
    $('rec-desc').value=''; $('rec-importo').value='';
    showStatus('✅ Movimento salvato');
    await loadRecords();
  } else {
    showStatus('❌ ' + (r.note||'Errore'));
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

window.addEventListener('DOMContentLoaded', async () => {
  if (document.cookie.includes('session_active=1')) {
    await loadDashboard();
  }
  $('password').addEventListener('keydown', e => { if(e.key==='Enter') doLogin(); });
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.get("/{path:path}", response_class=HTMLResponse)
async def catch_all(path: str):
    if path.startswith("api/"):
        raise HTTPException(status_code=404)
    return HTML
