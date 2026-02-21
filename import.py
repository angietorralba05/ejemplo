# ============================================================
# IMPORTS
# ============================================================
import os, sqlite3, calendar, secrets, hashlib, smtplib, tempfile, threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.drawing.image import Image as XLImage

from reportlab.lib.pagesizes import A4
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
    Table as PDFTable, TableStyle, PageBreak, Image as PDFImage)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import numpy as np

# ============================================================
# COMPATIBILIDAD Python 3.14 — sin librerías externas de UI
# ============================================================
class Messagebox:
    @staticmethod
    def show_info(msg, title="Info", parent=None):
        messagebox.showinfo(title, msg)
    @staticmethod
    def show_warning(msg, title="Aviso", parent=None):
        messagebox.showwarning(title, msg)
    @staticmethod
    def show_error(msg, title="Error", parent=None):
        messagebox.showerror(title, msg)
    @staticmethod
    def yesno(msg, title="Confirmar", parent=None):
        return "Yes" if messagebox.askyesno(title, msg) else "No"

# Parche: ignorar bootstyle en widgets ttk
def _patch_widget(cls):
    original = cls.__init__
    def new_init(self, *args, **kwargs):
        kwargs.pop("bootstyle", None)
        original(self, *args, **kwargs)
    cls.__init__ = new_init

for _cls in (ttk.Button, ttk.Label, ttk.Entry, ttk.Radiobutton,
             ttk.Separator, ttk.Combobox, ttk.Treeview, ttk.Scrollbar):
    _patch_widget(_cls)

# Alias tb → ttk
tb = ttk

# ============================================================
# BASE DE DATOS
# ============================================================
DB_NAME = "presupuesto.db"

def conectar_db():
    conn = sqlite3.connect(DB_NAME, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def crear_tabla_movimientos():
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS movimientos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL DEFAULT 0,
        tipo TEXT NOT NULL, concepto TEXT NOT NULL,
        monto REAL NOT NULL, fecha_hora TEXT NOT NULL)""")
    conn.commit()
    try:
        cur.execute("ALTER TABLE movimientos ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    conn.close()

def crear_tabla_usuarios():
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        correo TEXT NOT NULL UNIQUE, nombre TEXT NOT NULL,
        password_hash TEXT NOT NULL, salt TEXT NOT NULL,
        foto_path TEXT, reset_code TEXT, reset_expira TEXT)""")
    conn.commit(); conn.close()

def insertar_movimiento(user_id, tipo, concepto, monto):
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("INSERT INTO movimientos (user_id,tipo,concepto,monto,fecha_hora) VALUES (?,?,?,?,?)",
                (user_id, tipo, concepto, monto, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit(); conn.close()

def eliminar_movimiento_por_id(mov_id, user_id):
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("DELETE FROM movimientos WHERE id=? AND user_id=?", (mov_id, user_id))
    conn.commit(); conn.close()

def obtener_movimientos_rango(user_id, fecha_inicio, fecha_fin):
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("""SELECT id,tipo,concepto,monto,fecha_hora FROM movimientos
                   WHERE user_id=? AND fecha_hora>=? AND fecha_hora<=? ORDER BY id DESC""",
                (user_id, fecha_inicio, fecha_fin))
    filas = cur.fetchall(); conn.close(); return filas

def calcular_totales_rango(user_id, fecha_inicio, fecha_fin):
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(monto),0) FROM movimientos WHERE user_id=? AND tipo='Ingreso' AND fecha_hora>=? AND fecha_hora<=?", (user_id, fecha_inicio, fecha_fin))
    ing = float(cur.fetchone()[0])
    cur.execute("SELECT COALESCE(SUM(monto),0) FROM movimientos WHERE user_id=? AND tipo='Gasto' AND fecha_hora>=? AND fecha_hora<=?", (user_id, fecha_inicio, fecha_fin))
    gas = float(cur.fetchone()[0])
    conn.close(); return ing, gas, ing - gas

def meses_disponibles(user_id):
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("SELECT DISTINCT substr(fecha_hora,1,7) FROM movimientos WHERE user_id=? ORDER BY 1 DESC", (user_id,))
    rows = cur.fetchall(); conn.close()
    return [r[0] for r in rows if r[0]]

def rango_del_mes(yyyy_mm):
    year, month = int(yyyy_mm[:4]), int(yyyy_mm[5:7])
    ultimo = calendar.monthrange(year, month)[1]
    return f"{yyyy_mm}-01 00:00:00", f"{yyyy_mm}-{ultimo:02d} 23:59:59"

def eliminar_cuenta(user_id):
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("DELETE FROM usuarios WHERE id=?", (user_id,))
    conn.commit(); conn.close()

def actualizar_usuario(user_id, nombre, correo, foto_path=None):
    conn = conectar_db(); cur = conn.cursor()
    if foto_path is not None:
        cur.execute("UPDATE usuarios SET nombre=?,correo=?,foto_path=? WHERE id=?",
                    (nombre, correo, foto_path, user_id))
    else:
        cur.execute("UPDATE usuarios SET nombre=?,correo=? WHERE id=?",
                    (nombre, correo, user_id))
    conn.commit(); conn.close()

def cambiar_password_usuario(user_id, nueva_password):
    salt_hex = secrets.token_hex(16)
    pass_hash = _hash_password(nueva_password, salt_hex)
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("UPDATE usuarios SET password_hash=?,salt=? WHERE id=?",
                (pass_hash, salt_hex, user_id))
    conn.commit(); conn.close()

# ============================================================
# SEGURIDAD
# ============================================================
def _hash_password(password, salt_hex):
    salt = bytes.fromhex(salt_hex)
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()

def crear_usuario(correo, nombre, password, foto_path):
    correo = correo.strip().lower(); nombre = nombre.strip()
    if not correo or not nombre or not password: raise ValueError("Faltan datos.")
    salt_hex = secrets.token_hex(16)
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("INSERT INTO usuarios (correo,nombre,password_hash,salt,foto_path) VALUES (?,?,?,?,?)",
                (correo, nombre, _hash_password(password, salt_hex), salt_hex, foto_path))
    conn.commit(); conn.close()

def verificar_login(correo, password):
    correo = correo.strip().lower()
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("SELECT id,correo,nombre,password_hash,salt,foto_path FROM usuarios WHERE correo=?", (correo,))
    row = cur.fetchone(); conn.close()
    if not row: return None
    user_id, correo, nombre, ph, salt, foto_path = row
    if _hash_password(password, salt) != ph: return None
    return {"id": user_id, "correo": correo, "nombre": nombre, "foto_path": foto_path}

def crear_codigo_reset(correo):
    correo = correo.strip().lower()
    codigo = f"{secrets.randbelow(1_000_000):06d}"
    expira = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    conn = conectar_db(); cur = conn.cursor()
    # Buscar ignorando mayúsculas/minúsculas
    cur.execute("SELECT id, correo FROM usuarios WHERE LOWER(correo)=?", (correo,))
    row = cur.fetchone()
    if not row: conn.close(); return None
    correo_real = row[1]  # usar el correo exacto como está en la BD
    cur.execute("UPDATE usuarios SET reset_code=?,reset_expira=? WHERE correo=?", (codigo, expira, correo_real))
    conn.commit(); conn.close(); return codigo

def resetear_password(correo, codigo, nueva_password):
    correo = correo.strip().lower()
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("SELECT reset_code,reset_expira FROM usuarios WHERE correo=?", (correo,))
    row = cur.fetchone()
    if not row or not row[0]: conn.close(); return False
    reset_code, reset_expira = row
    try:
        if datetime.now() > datetime.strptime(reset_expira, "%Y-%m-%d %H:%M:%S"):
            conn.close(); return False
    except Exception: conn.close(); return False
    if codigo.strip() != reset_code: conn.close(); return False
    salt_hex = secrets.token_hex(16)
    cur.execute("UPDATE usuarios SET password_hash=?,salt=?,reset_code=NULL,reset_expira=NULL WHERE correo=?",
                (_hash_password(nueva_password, salt_hex), salt_hex, correo))
    conn.commit(); conn.close(); return True

# ============================================================
# CORREO
# ============================================================
GMAIL_REMITENTE = "recuperaciocorreo967@gmail.com"
GMAIL_APP_PASS  = "xido uqzf moqk lgxa"

def _enviar_html(destinatario, asunto, html):
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = GMAIL_REMITENTE; msg["To"] = destinatario; msg["Subject"] = asunto
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as s:
            s.login(GMAIL_REMITENTE, GMAIL_APP_PASS)
            s.sendmail(GMAIL_REMITENTE, destinatario, msg.as_string())
        return True
    except Exception as e:
        print(f"[Email error] {e}"); return False

def enviar_codigo_por_correo(destinatario, codigo):
    html = f"""<html><body style="font-family:'Segoe UI',Arial;background:#F3F4F6;padding:40px 0;">
    <table width="100%"><tr><td align="center"><table width="480" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.1);">
    <tr><td style="background:linear-gradient(135deg,#7C3AED,#4C1D95);padding:36px 40px;text-align:center;">
    <h1 style="color:#fff;font-size:22px;margin:10px 0 4px;">Recuperar contraseña</h1></td></tr>
    <tr><td style="padding:36px 40px;">
    <p style="color:#374151;">Tu código de verificación:</p>
    <div style="background:#F5F3FF;border:2px dashed #7C3AED;border-radius:12px;padding:20px;text-align:center;">
    <span style="font-size:36px;font-weight:bold;letter-spacing:10px;color:#7C3AED;">{codigo}</span></div>
    <p style="color:#92400E;background:#FEF3C7;border-left:4px solid #F59E0B;padding:12px;border-radius:6px;margin-top:16px;">
    ⏱️ Expira en <strong>10 minutos</strong>.</p></td></tr>
    <tr><td style="background:#F9FAFB;padding:16px 40px;text-align:center;">
    <p style="color:#9CA3AF;font-size:12px;">© 2026 Presupuesto Personal</p></td></tr>
    </table></td></tr></table></body></html>"""
    return _enviar_html(destinatario, "💜 Tu código de recuperación — Presupuesto Personal", html)

def enviar_bienvenida(destinatario, nombre):
    html = f"""<html><body style="font-family:'Segoe UI',Arial;background:#F3F4F6;padding:40px 0;">
    <table width="100%"><tr><td align="center"><table width="480" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.1);">
    <tr><td style="background:linear-gradient(135deg,#7C3AED,#4C1D95);padding:36px 40px;text-align:center;">
    <h1 style="color:#fff;font-size:24px;">¡Bienvenido a Presupuesto Personal!</h1></td></tr>
    <tr><td style="padding:36px 40px;">
    <p style="color:#374151;font-size:16px;">Hola <strong>{nombre}</strong> 👋</p>
    <p style="color:#374151;">Tu cuenta está lista. Ya puedes registrar tus ingresos y gastos.</p>
    <p style="color:#9CA3AF;font-size:13px;">Correo: <strong>{destinatario}</strong></p></td></tr>
    <tr><td style="background:#F9FAFB;padding:16px 40px;text-align:center;">
    <p style="color:#9CA3AF;font-size:12px;">© 2026 Presupuesto Personal</p></td></tr>
    </table></td></tr></table></body></html>"""
    return _enviar_html(destinatario, "💜 ¡Bienvenido a Presupuesto Personal!", html)

def enviar_confirmacion_cambio(destinatario, nombre):
    ahora = datetime.now().strftime("%d/%m/%Y %H:%M")
    html = f"""<html><body style="font-family:'Segoe UI',Arial;background:#F3F4F6;padding:40px 0;">
    <table width="100%"><tr><td align="center"><table width="480" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.1);">
    <tr><td style="background:linear-gradient(135deg,#059669,#065F46);padding:36px 40px;text-align:center;">
    <h1 style="color:#fff;font-size:22px;">¡Contraseña actualizada!</h1></td></tr>
    <tr><td style="padding:36px 40px;">
    <p style="color:#374151;">Hola <strong>{nombre}</strong>, tu contraseña fue actualizada el <strong>{ahora}</strong>.</p>
    <p style="color:#065F46;background:#ECFDF5;border-left:4px solid #10B981;padding:12px;border-radius:6px;">
    🔒 Si no fuiste tú, cambia tu contraseña de inmediato.</p></td></tr>
    <tr><td style="background:#F9FAFB;padding:16px 40px;text-align:center;">
    <p style="color:#9CA3AF;font-size:12px;">© 2026 Presupuesto Personal</p></td></tr>
    </table></td></tr></table></body></html>"""
    return _enviar_html(destinatario, "✅ Contraseña actualizada — Presupuesto Personal", html)

# ============================================================
# LOGIN
# ============================================================
class LoginWindow(tk.Toplevel):
    def __init__(self, master, on_success):
        super().__init__(master)
        self.on_success = on_success
        self.title("Presupuesto — Iniciar sesión")
        self.geometry("480x340"); self.resizable(False, False)
        self.correo_var = tk.StringVar(); self.pass_var = tk.StringVar()
        wrap = ttk.Frame(self, padding=28); wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="💜 Bienvenido", font=("Segoe UI",20,"bold")).pack(pady=(0,4))
        ttk.Label(wrap, text="Ingresa tus credenciales para continuar",
                 font=("Segoe UI",10)).pack(pady=(0,16))
        form = ttk.Frame(wrap); form.pack(fill="x")
        ttk.Label(form, text="📧 Correo", font=("Segoe UI",10)).grid(row=0,column=0,sticky="w",pady=4)
        correo_e = ttk.Entry(form, textvariable=self.correo_var, width=38, font=("Segoe UI",10))
        correo_e.grid(row=0,column=1,padx=8,pady=4)
        ttk.Label(form, text="🔒 Contraseña", font=("Segoe UI",10)).grid(row=1,column=0,sticky="w",pady=4)
        pass_e = ttk.Entry(form, textvariable=self.pass_var, width=38, show="•", font=("Segoe UI",10))
        pass_e.grid(row=1,column=1,padx=8,pady=4)
        pass_e.bind("<Return>", lambda e: self._login())
        btns = ttk.Frame(wrap); btns.pack(fill="x", pady=(16,6))
        ttk.Button(btns, text="Entrar", width=12, command=self._login).pack(side="left",padx=4)
        ttk.Button(btns, text="Crear cuenta", width=14, command=self._open_register).pack(side="left",padx=4)
        ttk.Button(btns, text="Olvidé mi contraseña", command=self._open_reset).pack(side="right",padx=4)
        self.update_idletasks()
        x = (self.winfo_screenwidth()//2) - (self.winfo_width()//2)
        y = (self.winfo_screenheight()//2) - (self.winfo_height()//2)
        self.geometry(f"+{x}+{y}"); self.grab_set(); self.focus_force(); correo_e.focus()

    def _login(self):
        if not self.correo_var.get().strip() or not self.pass_var.get():
            Messagebox.show_warning("Completa todos los campos.", "Atención", parent=self); return
        user = verificar_login(self.correo_var.get(), self.pass_var.get())
        if not user:
            Messagebox.show_error("Correo o contraseña incorrectos.", "Error", parent=self)
            self.pass_var.set(""); return
        self.grab_release(); self.destroy(); self.on_success(user)

    def _open_register(self):
        self.grab_release()
        RegisterWindow(self, on_close=lambda: self.grab_set())

    def _open_reset(self): ResetWindow(self)

# ============================================================
# REGISTRO
# ============================================================
class RegisterWindow(tk.Toplevel):
    def __init__(self, master, on_close=None):
        super().__init__(master)
        self._on_close = on_close
        self.title("Crear cuenta"); self.geometry("520x420"); self.resizable(False, False)
        self.correo_var = tk.StringVar(); self.nombre_var = tk.StringVar()
        self.pass1_var  = tk.StringVar(); self.pass2_var  = tk.StringVar()
        self.foto_path  = None
        wrap = ttk.Frame(self, padding=24); wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="Crear cuenta", font=("Segoe UI",16,"bold")).pack(anchor="w", pady=(0,12))
        form = ttk.Labelframe(wrap, text="Datos de registro", padding=14); form.pack(fill="x")
        for i,(lbl,var,oculto) in enumerate([
            ("Nombre", self.nombre_var, False), ("Correo", self.correo_var, False),
            ("Contraseña", self.pass1_var, True), ("Repite contraseña", self.pass2_var, True)]):
            ttk.Label(form, text=lbl).grid(row=i, column=0, sticky="w", padx=6, pady=5)
            kw = {"show":"•"} if oculto else {}
            ttk.Entry(form, textvariable=var, width=42, **kw).grid(row=i, column=1, padx=6, pady=5)
        foto_row = ttk.Frame(wrap); foto_row.pack(fill="x", pady=10)
        self.lbl_foto = ttk.Label(foto_row, text="Foto de perfil: (opcional)")
        self.lbl_foto.pack(side="left")
        ttk.Button(foto_row, text="Elegir imagen", command=self._pick_foto).pack(side="right")
        self.lbl_estado = tk.Label(wrap, text="", font=("Segoe UI",10,"bold"), fg="#059669")
        self.lbl_estado.pack(anchor="w", pady=(0,4))
        ttk.Button(wrap, text="Crear usuario", width=18, command=self._crear).pack(pady=4)
        self.update_idletasks()
        x = (self.winfo_screenwidth()//2) - (self.winfo_width()//2)
        y = (self.winfo_screenheight()//2) - (self.winfo_height()//2)
        self.geometry(f"+{x}+{y}")
        self.transient(master); self.grab_set(); self.focus_force()
        self.protocol("WM_DELETE_WINDOW", self._cerrar)

    def _cerrar(self):
        self.grab_release()
        if self._on_close: self._on_close()
        self.destroy()

    def _pick_foto(self):
        path = filedialog.askopenfilename(title="Elegir foto",
                  filetypes=[("Imágenes","*.png *.jpg *.jpeg"),("Todos","*.*")])
        if path: self.foto_path = path; self.lbl_foto.config(text=f"Foto: {os.path.basename(path)}")

    def _crear(self):
        nombre = self.nombre_var.get().strip(); correo = self.correo_var.get().strip()
        p1 = self.pass1_var.get(); p2 = self.pass2_var.get()
        if not nombre or not correo:
            self.lbl_estado.config(text="⚠️ Nombre y correo son obligatorios.", fg="#DC2626"); return
        if p1 != p2:
            self.lbl_estado.config(text="❌ Las contraseñas no coinciden.", fg="#DC2626"); return
        if len(p1) < 6:
            self.lbl_estado.config(text="⚠️ Contraseña muy corta (mínimo 6).", fg="#DC2626"); return
        self.lbl_estado.config(text="⏳ Creando cuenta…", fg="#D97706"); self.update()
        try:
            crear_usuario(correo, nombre, p1, self.foto_path)
        except sqlite3.IntegrityError:
            self.lbl_estado.config(text="❌ Ese correo ya está registrado.", fg="#DC2626"); return
        except Exception as e:
            self.lbl_estado.config(text=f"❌ Error: {e}", fg="#DC2626"); return
        self.lbl_estado.config(text="✅ ¡Cuenta creada!", fg="#059669"); self.update()
        threading.Thread(target=enviar_bienvenida, args=(correo, nombre), daemon=True).start()
        self.after(1200, self._cerrar)

# ============================================================
# RECUPERAR CONTRASEÑA
# ============================================================
class ResetWindow(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Recuperar contraseña"); self.resizable(False, False)
        self._correo = ""
        self.correo_var = tk.StringVar(); self.codigo_var = tk.StringVar()
        self.pass1_var  = tk.StringVar(); self.pass2_var  = tk.StringVar()
        self._wrap = ttk.Frame(self, padding=28); self._wrap.pack(fill="both", expand=True)
        self._mostrar_fase1(); self.grab_set()

    def _limpiar(self):
        for w in self._wrap.winfo_children(): w.destroy()

    def _recentrar(self, geo):
        self.geometry(geo); self.update_idletasks()
        x = (self.winfo_screenwidth()//2) - (self.winfo_width()//2)
        y = (self.winfo_screenheight()//2) - (self.winfo_height()//2)
        self.geometry(f"{geo}+{x}+{y}")

    def _mostrar_fase1(self):
        self._limpiar(); self._recentrar("580x280")
        ttk.Label(self._wrap, text="🔐  Recuperar contraseña", font=("Segoe UI",16,"bold")).pack(anchor="w", pady=(0,14))
        f = ttk.Labelframe(self._wrap, text="Paso 1 — Ingresa tu correo registrado", padding=16); f.pack(fill="x")
        row = ttk.Frame(f); row.pack(fill="x")
        ttk.Label(row, text="Correo:", font=("Segoe UI",10)).pack(side="left", padx=(0,8))
        correo_e = ttk.Entry(row, textvariable=self.correo_var, width=36, font=("Segoe UI",10))
        correo_e.pack(side="left", expand=True, fill="x"); correo_e.focus()
        self.lbl_f1 = tk.Label(self._wrap, text="", font=("Segoe UI",10,"bold"), fg="#D97706")
        self.lbl_f1.pack(anchor="w", pady=(10,4))
        btns = ttk.Frame(self._wrap); btns.pack(fill="x", pady=(6,0))
        self.btn_enviar = ttk.Button(btns, text="📨  Enviar código", width=22, command=self._enviar_codigo)
        self.btn_enviar.pack(side="left", padx=(0,8))
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="left")

    def _enviar_codigo(self):
        correo = self.correo_var.get().strip()
        if not correo: self.lbl_f1.config(text="⚠️ Escribe tu correo.", fg="#DC2626"); return
        codigo = crear_codigo_reset(correo)
        if not codigo: self.lbl_f1.config(text="❌ Correo no registrado.", fg="#DC2626"); return
        self.btn_enviar.config(state="disabled")
        self.lbl_f1.config(text="⏳ Enviando…", fg="#D97706"); self.update()
        ok = enviar_codigo_por_correo(correo, codigo)
        self.btn_enviar.config(state="normal")
        self._correo = correo
        if ok: self.lbl_f1.config(text=f"✅ Código enviado a {correo}", fg="#059669")
        else:  self.lbl_f1.config(text=f"⚠️ No se pudo enviar. Código: {codigo}", fg="#DC2626")
        self.after(900, self._mostrar_fase2)

    def _mostrar_fase2(self):
        self._limpiar(); self._recentrar("580x300")
        ttk.Label(self._wrap, text="🔑  Verificar código", font=("Segoe UI",16,"bold")).pack(anchor="w", pady=(0,14))
        f = ttk.Labelframe(self._wrap, text="Paso 2 — Ingresa el código recibido", padding=16); f.pack(fill="x")
        row = ttk.Frame(f); row.pack(fill="x")
        ttk.Label(row, text="Código de 6 dígitos:", font=("Segoe UI",10)).pack(side="left", padx=(0,10))
        cod_e = ttk.Entry(row, textvariable=self.codigo_var, width=14, font=("Segoe UI",16,"bold"), justify="center")
        cod_e.pack(side="left"); cod_e.focus()
        cod_e.bind("<Return>", lambda e: self._validar_codigo())
        self.lbl_f2 = tk.Label(self._wrap, text="Revisa tu correo e ingresa el código.",
                                font=("Segoe UI",10), fg="#6B7280")
        self.lbl_f2.pack(anchor="w", pady=(10,4))
        btns = ttk.Frame(self._wrap); btns.pack(fill="x", pady=(10,0))
        ttk.Button(btns, text="✅  Verificar", width=16, command=self._validar_codigo).pack(side="left", padx=(0,8))
        ttk.Button(btns, text="← Volver", width=10, command=self._mostrar_fase1).pack(side="left")

    def _validar_codigo(self):
        codigo = self.codigo_var.get().strip()
        if not codigo: self.lbl_f2.config(text="⚠️ Ingresa el código.", fg="#DC2626"); return
        conn = conectar_db(); cur = conn.cursor()
        cur.execute("SELECT reset_code,reset_expira FROM usuarios WHERE correo=?", (self._correo,))
        row = cur.fetchone(); conn.close()
        if not row or not row[0]: self.lbl_f2.config(text="❌ Código no encontrado.", fg="#DC2626"); return
        reset_code, reset_expira = row
        try:
            if datetime.now() > datetime.strptime(reset_expira, "%Y-%m-%d %H:%M:%S"):
                self.lbl_f2.config(text="⏰ Código expirado.", fg="#DC2626"); return
        except Exception:
            self.lbl_f2.config(text="❌ Error al verificar.", fg="#DC2626"); return
        if codigo != reset_code: self.lbl_f2.config(text="❌ Código incorrecto.", fg="#DC2626"); return
        self.lbl_f2.config(text="✅ Código verificado.", fg="#059669")
        self.after(700, self._mostrar_fase3)

    def _mostrar_fase3(self):
        self._limpiar(); self._recentrar("580x320")
        ttk.Label(self._wrap, text="🔒  Nueva contraseña", font=("Segoe UI",16,"bold")).pack(anchor="w", pady=(0,14))
        f = ttk.Labelframe(self._wrap, text="Paso 3 — Crea tu nueva contraseña", padding=16); f.pack(fill="x")
        for i,(lbl,var) in enumerate([("Nueva contraseña:", self.pass1_var), ("Repite contraseña:", self.pass2_var)]):
            row = ttk.Frame(f); row.pack(fill="x", pady=6)
            ttk.Label(row, text=lbl, font=("Segoe UI",10), width=18, anchor="w").pack(side="left")
            e = ttk.Entry(row, textvariable=var, show="•", font=("Segoe UI",10), width=30)
            e.pack(side="left", expand=True, fill="x")
            if i == 0: e.focus()
            e.bind("<Return>", lambda ev: self._guardar_password())
        self.lbl_f3 = tk.Label(self._wrap, text="Mínimo 6 caracteres.", font=("Segoe UI",10), fg="#6B7280")
        self.lbl_f3.pack(anchor="w", pady=(10,4))
        btns = ttk.Frame(self._wrap); btns.pack(fill="x", pady=(10,0))
        ttk.Button(btns, text="💾  Guardar contraseña", width=24, command=self._guardar_password).pack(side="left", padx=(0,8))
        ttk.Button(btns, text="Cancelar", width=10, command=self.destroy).pack(side="left")

    def _guardar_password(self):
        p1 = self.pass1_var.get(); p2 = self.pass2_var.get()
        if not p1: self.lbl_f3.config(text="⚠️ Escribe una contraseña.", fg="#DC2626"); return
        if p1 != p2: self.lbl_f3.config(text="❌ No coinciden.", fg="#DC2626"); return
        if len(p1) < 6: self.lbl_f3.config(text="⚠️ Mínimo 6 caracteres.", fg="#DC2626"); return
        ok = resetear_password(self._correo, self.codigo_var.get().strip(), p1)
        if not ok: self.lbl_f3.config(text="❌ Código expirado. Empieza de nuevo.", fg="#DC2626"); return
        conn = conectar_db(); cur = conn.cursor()
        cur.execute("SELECT nombre FROM usuarios WHERE correo=?", (self._correo,))
        row = cur.fetchone(); conn.close()
        nombre = row[0] if row else self._correo
        self.lbl_f3.config(text="✅ Contraseña actualizada.", fg="#059669"); self.update()
        threading.Thread(target=enviar_confirmacion_cambio, args=(self._correo, nombre), daemon=True).start()
        Messagebox.show_info("🎉 ¡Contraseña actualizada!\nYa puedes iniciar sesión.", "¡Listo!", parent=self)
        self.grab_release(); self.destroy()

# ============================================================
# EDITAR PERFIL
# ============================================================
class EditarPerfilWindow(tk.Toplevel):
    def __init__(self, master, usuario, on_update):
        super().__init__(master)
        self.usuario = usuario; self.on_update = on_update
        self.title("Editar perfil"); self.geometry("520x440"); self.resizable(False, False)
        self.nombre_var = tk.StringVar(value=usuario["nombre"])
        self.correo_var = tk.StringVar(value=usuario["correo"])
        self.pass1_var  = tk.StringVar(); self.pass2_var = tk.StringVar()
        self.foto_path  = usuario.get("foto_path")
        wrap = ttk.Frame(self, padding=24); wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="✏️  Editar perfil", font=("Segoe UI",16,"bold")).pack(anchor="w", pady=(0,14))
        form = ttk.Labelframe(wrap, text="Información de la cuenta", padding=14); form.pack(fill="x")
        for i,(lbl,var,oculto) in enumerate([
            ("Nombre", self.nombre_var, False), ("Correo", self.correo_var, False),
            ("Nueva contraseña (opcional)", self.pass1_var, True), ("Repite contraseña", self.pass2_var, True)]):
            ttk.Label(form, text=lbl).grid(row=i, column=0, sticky="w", padx=6, pady=6)
            kw = {"show":"•"} if oculto else {}
            ttk.Entry(form, textvariable=var, width=38, **kw).grid(row=i, column=1, padx=6, pady=6)
        foto_row = ttk.Frame(wrap); foto_row.pack(fill="x", pady=10)
        nombre_foto = os.path.basename(self.foto_path) if self.foto_path else "ninguna"
        self.lbl_foto = ttk.Label(foto_row, text=f"Foto actual: {nombre_foto}")
        self.lbl_foto.pack(side="left")
        ttk.Button(foto_row, text="Cambiar foto", command=self._pick_foto).pack(side="right")
        btns = ttk.Frame(wrap); btns.pack(fill="x", pady=10)
        ttk.Button(btns, text="Guardar cambios", command=self._guardar).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="left", padx=6)
        ttk.Button(btns, text="🗑️ Eliminar cuenta", command=self._eliminar_cuenta).pack(side="right", padx=6)
        self.grab_set()

    def _pick_foto(self):
        path = filedialog.askopenfilename(title="Elegir foto",
                  filetypes=[("Imágenes","*.png *.jpg *.jpeg"),("Todos","*.*")])
        if path: self.foto_path = path; self.lbl_foto.config(text=f"Foto: {os.path.basename(path)}")

    def _eliminar_cuenta(self):
        ok1 = Messagebox.yesno("⚠️ ¿Eliminar tu cuenta?\n\nEsta acción no se puede deshacer.", "Eliminar cuenta", parent=self)
        if ok1 != "Yes": return
        ok2 = Messagebox.yesno(f"🚨 CONFIRMACIÓN FINAL\n\n{self.usuario['nombre']} ({self.usuario['correo']})\n\n¿Confirmas?", "¿Estás seguro?", parent=self)
        if ok2 != "Yes": return
        eliminar_cuenta(self.usuario["id"])
        Messagebox.show_info("Cuenta eliminada.", "Listo")
        self.destroy(); self.on_update(None)

    def _guardar(self):
        nombre = self.nombre_var.get().strip(); correo = self.correo_var.get().strip().lower()
        p1 = self.pass1_var.get(); p2 = self.pass2_var.get()
        if not nombre or not correo:
            Messagebox.show_warning("Nombre y correo son obligatorios.", "Atención", parent=self); return
        if p1 or p2:
            if p1 != p2: Messagebox.show_error("Las contraseñas no coinciden.", "Error", parent=self); return
            if len(p1) < 6: Messagebox.show_warning("Contraseña muy corta.", "Atención", parent=self); return
            cambiar_password_usuario(self.usuario["id"], p1)
        actualizar_usuario(self.usuario["id"], nombre, correo, self.foto_path)
        self.usuario.update({"nombre": nombre, "correo": correo, "foto_path": self.foto_path})
        Messagebox.show_info("¡Perfil actualizado!", "Listo")
        self.on_update(self.usuario); self.destroy()

# ============================================================
# COLORES
# ============================================================
GREEN = "#10B981"; RED = "#EF4444"; BLUE = "#3B82F6"
BG_HEADER = "#0F0E2A"

# ============================================================
# APP PRINCIPAL
# ============================================================
class App(ttk.Frame):
    def __init__(self, root, usuario):
        super().__init__(root)
        self.root = root; self.usuario = usuario
        self.root.title("💜 Presupuesto Personal")
        self.root.geometry("1220x800"); self.root.minsize(1100, 720)
        self.FONT = ("Segoe UI",11); self.FONT_BOLD = ("Segoe UI",11,"bold")
        self.tipo_var     = tk.StringVar(value="Ingreso")
        self.concepto_var = tk.StringVar()
        self.monto_var    = tk.StringVar()
        self.mes_var      = tk.StringVar()
        self._anim_target  = [0, 0, 0]
        self._anim_current = [0.0, 0.0, 0.0]
        self._anim_step    = 0
        self._anim_id      = None
        self._hover_anim_id   = None
        self._pill_bg         = BG_HEADER
        self._menu_abierto    = None
        self._menu_dot_abierto = None
        self.pack(fill="both", expand=True)
        self._build_ui()
        self._refresh()
        self._auto_refresh()

    def _build_ui(self):
        header = tk.Frame(self, bg=BG_HEADER, height=62)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        self._pill_canvas = tk.Canvas(header, width=170, height=46,
                                      bg=BG_HEADER, highlightthickness=0, cursor="hand2")
        self._pill_canvas.pack(side="left", padx=10, pady=8)
        self._foto_img_ref   = None
        self._menu_abierto   = None
        self._dibujar_pill_canvas()

        for ev, fn in [("<Button-1>", lambda e: self._toggle_perfil_menu()),
                       ("<Enter>",    lambda e: self._hover_perfil(True)),
                       ("<Leave>",    lambda e: self._hover_perfil(False))]:
            self._pill_canvas.bind(ev, fn)

        tk.Label(header, text="💜  Presupuesto Personal",
                 bg=BG_HEADER, fg="#C4B5FD", font=("Segoe UI",14,"bold")).pack(side="left", expand=True)

        self._menu_dot_abierto = None
        menu_btn = tk.Label(header, text="⋮", bg=BG_HEADER, fg="#7C6FCD",
                            font=("Segoe UI",22,"bold"), cursor="hand2", padx=16)
        menu_btn.pack(side="right")
        menu_btn.bind("<Button-1>", self._toggle_dot_menu)
        menu_btn.bind("<Enter>", lambda e: menu_btn.config(fg="#C4B5FD"))
        menu_btn.bind("<Leave>", lambda e: menu_btn.config(fg="#7C6FCD"))

        body = ttk.Frame(self, padding=(14,10))
        body.pack(fill="both", expand=True)
        self._build_form(body)
        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=6)
        self._build_filter_bar(body)
        panels = ttk.Frame(body); panels.pack(fill="both", expand=True)
        panels.columnconfigure(0, weight=55); panels.columnconfigure(1, weight=45)
        panels.rowconfigure(0, weight=1)
        self._build_table(panels)
        self._build_charts(panels)

    def _build_form(self, parent):
        form = ttk.Labelframe(parent, text="➕  Nuevo movimiento", padding=10); form.pack(fill="x")
        ttk.Label(form, text="Tipo:", font=self.FONT).grid(row=0,column=0,sticky="w",padx=6)
        ttk.Radiobutton(form, text="Ingreso", variable=self.tipo_var, value="Ingreso").grid(row=0,column=1,padx=6)
        ttk.Radiobutton(form, text="Gasto",   variable=self.tipo_var, value="Gasto").grid(row=0,column=2,padx=6)
        ttk.Label(form, text="Concepto:", font=self.FONT).grid(row=0,column=3,sticky="w",padx=(20,6))
        ttk.Entry(form, textvariable=self.concepto_var, width=28, font=self.FONT).grid(row=0,column=4,padx=6)
        ttk.Label(form, text="Monto $:", font=self.FONT).grid(row=0,column=5,sticky="w",padx=(12,6))
        ttk.Entry(form, textvariable=self.monto_var, width=14, font=self.FONT).grid(row=0,column=6,padx=6)
        ttk.Button(form, text="Registrar", command=self._registrar).grid(row=0,column=7,padx=16)

    def _build_filter_bar(self, parent):
        filt = ttk.Frame(parent); filt.pack(fill="x", pady=(0,6))
        ttk.Label(filt, text="Filtrar por mes:", font=self.FONT).pack(side="left", padx=(0,8))
        self.cb_mes = ttk.Combobox(filt, textvariable=self.mes_var, state="readonly", width=14)
        self.cb_mes.pack(side="left")
        self.cb_mes.bind("<<ComboboxSelected>>", lambda e: self._refresh())
        ttk.Button(filt, text="Ver todo", command=self._ver_todo).pack(side="left", padx=8)
        card_frame = ttk.Frame(filt); card_frame.pack(side="right")
        self.lbl_ing   = tk.Label(card_frame, text="Ingresos: $0",  fg=GREEN, bg="#F0FDF4", font=("Segoe UI",11,"bold"), padx=10, pady=4)
        self.lbl_gas   = tk.Label(card_frame, text="Gastos: $0",    fg=RED,   bg="#FEF2F2", font=("Segoe UI",11,"bold"), padx=10, pady=4)
        self.lbl_saldo = tk.Label(card_frame, text="Saldo: $0",     fg=BLUE,  bg="#EFF6FF", font=("Segoe UI",11,"bold"), padx=10, pady=4)
        for lbl in (self.lbl_ing, self.lbl_gas, self.lbl_saldo): lbl.pack(side="left", padx=8)

    def _build_table(self, parent):
        frame = ttk.Labelframe(parent, text="📋  Movimientos", padding=6)
        frame.grid(row=0, column=0, sticky="nsew", padx=(0,6))
        cols = ("ID","Tipo","Concepto","Monto","Fecha / Hora")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        for col, w in zip(cols, [50,80,220,110,160]):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor="center" if col != "Concepto" else "w")
        self.tree.tag_configure("Ingreso", foreground=GREEN)
        self.tree.tag_configure("Gasto",   foreground=RED)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y"); self.tree.pack(fill="both", expand=True)

    def _build_charts(self, parent):
        frame = ttk.Labelframe(parent, text="📊  Gráficas", padding=6)
        frame.grid(row=0, column=1, sticky="nsew")
        BG = "#0F172A"
        self.fig = Figure(figsize=(5.6,5.4), dpi=96, facecolor=BG)
        self.fig.subplots_adjust(hspace=0.46, top=0.94, bottom=0.09, left=0.13, right=0.97)
        self.ax_bar  = self.fig.add_subplot(211)
        self.ax_line = self.fig.add_subplot(212)
        for ax in (self.ax_bar, self.ax_line): ax.set_facecolor(BG)
        self.canvas_fig = FigureCanvasTkAgg(self.fig, master=frame)
        self.canvas_fig.get_tk_widget().pack(fill="both", expand=True)

    # ── Pill avatar ───────────────────────────────────────
    def _dibujar_pill_canvas(self):
        c = self._pill_canvas; c.delete("all")
        c.config(bg=self._pill_bg)
        AV = 40; ax0 = 6; ay0 = 3
        fp = self.usuario.get("foto_path")
        try:
            from PIL import Image, ImageTk, ImageDraw
            if not fp or not os.path.exists(fp): raise FileNotFoundError
            img  = Image.open(fp).convert("RGBA").resize((AV,AV), Image.LANCZOS)
            mask = Image.new("L",(AV,AV),0)
            ImageDraw.Draw(mask).ellipse((0,0,AV,AV),fill=255)
            img.putalpha(mask)
            self._foto_img_ref = ImageTk.PhotoImage(img)
            c.create_image(ax0+AV//2, ay0+AV//2, image=self._foto_img_ref)
            c.create_oval(ax0, ay0, ax0+AV, ay0+AV, outline="#7C3AED", width=2)
        except Exception:
            self._foto_img_ref = None
            c.create_oval(ax0, ay0, ax0+AV, ay0+AV, fill="#2A1660", outline="#7C3AED", width=2)
            c.create_text(ax0+AV//2, ay0+AV//2, text=self.usuario.get("nombre","?")[0].upper(),
                          fill="white", font=("Segoe UI",14,"bold"))
        nombre_corto = self.usuario.get("nombre","Usuario")
        nombre_corto = nombre_corto[:14]+"…" if len(nombre_corto)>14 else nombre_corto
        c.create_text(ax0+AV+8, ay0+AV//2, text=nombre_corto,
                      fill="#EDE9FE", font=("Segoe UI",9,"bold"), anchor="w")

    def _hover_perfil(self, entering):
        if self._hover_anim_id:
            try: self.root.after_cancel(self._hover_anim_id)
            except Exception: pass
            self._hover_anim_id = None
        C0=(15,14,42); C1=(28,25,65); STEPS=12
        try:
            h=self._pill_bg.lstrip("#"); cur=(int(h[0:2],16),int(h[2:4],16),int(h[4:6],16))
        except Exception: cur=C0
        target=C1 if entering else C0
        def _step(i):
            t=i/STEPS; t=t*(2-t)
            r=int(cur[0]+(target[0]-cur[0])*t)
            g=int(cur[1]+(target[1]-cur[1])*t)
            b=int(cur[2]+(target[2]-cur[2])*t)
            hx="#{:02x}{:02x}{:02x}".format(r,g,b)
            self._pill_bg=hx; self._pill_canvas.config(bg=hx)
            if i<STEPS: self._hover_anim_id=self.root.after(12,lambda:_step(i+1))
            else: self._hover_anim_id=None
        _step(1)

    def _toggle_perfil_menu(self):
        if self._menu_abierto:
            try:
                if self._menu_abierto.winfo_exists():
                    self._on_menu_close(self._menu_abierto); return
            except Exception: pass
        self._show_perfil_menu()

    def _show_perfil_menu(self):
        BG="#0D0C22"; BG2="#13112E"; SEP="#1E1C42"; ACC="#7C3AED"
        menu=tk.Toplevel(self.root)
        menu.overrideredirect(True); menu.attributes("-topmost",True)
        px=self._pill_canvas.winfo_rootx()
        py=self._pill_canvas.winfo_rooty()+self._pill_canvas.winfo_height()+6
        menu.geometry(f"+{px}+{py}")
        self._menu_abierto=menu
        outer=tk.Frame(menu,bg=BG,highlightbackground=ACC,highlightthickness=1)
        outer.pack(fill="both",expand=True)
        top=tk.Frame(outer,bg=BG2,padx=16,pady=14); top.pack(fill="x")
        av_size=52
        av=tk.Canvas(top,width=av_size,height=av_size,bg=BG2,highlightthickness=0,cursor="hand2")
        av.pack(side="left")
        fp=self.usuario.get("foto_path")
        try:
            from PIL import Image,ImageTk,ImageDraw
            if not fp or not os.path.exists(fp): raise FileNotFoundError
            img=Image.open(fp).convert("RGBA").resize((av_size,av_size),Image.LANCZOS)
            mask=Image.new("L",(av_size,av_size),0)
            ImageDraw.Draw(mask).ellipse((0,0,av_size,av_size),fill=255)
            img.putalpha(mask)
            self._menu_foto_ref=ImageTk.PhotoImage(img)
            av.create_image(av_size//2,av_size//2,image=self._menu_foto_ref)
            av.create_oval(1,1,av_size-1,av_size-1,outline=ACC,width=2)
        except Exception:
            self._menu_foto_ref=None
            av.create_oval(1,1,av_size-1,av_size-1,fill="#4C1D95",outline=ACC,width=2)
            av.create_text(av_size//2,av_size//2,text=self.usuario["nombre"][0].upper(),
                           fill="white",font=("Segoe UI",20,"bold"))
        def cambiar_foto(e=None):
            self._on_menu_close(menu); self._cambiar_foto_rapido()
        av.bind("<Button-1>",cambiar_foto)
        info=tk.Frame(top,bg=BG2); info.pack(side="left",padx=(12,0))
        tk.Label(info,text=self.usuario["nombre"],bg=BG2,fg="#EDE9FE",font=("Segoe UI",11,"bold")).pack(anchor="w")
        tk.Label(info,text=self.usuario["correo"],bg=BG2,fg="#6D6A9C",font=("Segoe UI",8)).pack(anchor="w",pady=(3,0))
        lbl_cf=tk.Label(info,text="✎ Cambiar foto",bg=BG2,fg=ACC,font=("Segoe UI",8,"underline"),cursor="hand2")
        lbl_cf.pack(anchor="w",pady=(5,0)); lbl_cf.bind("<Button-1>",cambiar_foto)
        tk.Frame(outer,bg=SEP,height=1).pack(fill="x")
        def item(icon,label,cmd):
            f=tk.Frame(outer,bg=BG,cursor="hand2",padx=16,pady=10); f.pack(fill="x")
            tk.Label(f,text=icon,bg=BG,fg="#A78BFA",font=("Segoe UI",12)).pack(side="left",padx=(0,12))
            tk.Label(f,text=label,bg=BG,fg="#D1D5DB",font=("Segoe UI",10)).pack(side="left")
            def clk(e,_cmd=cmd): self._on_menu_close(menu); _cmd()
            for w in (f,*f.winfo_children()): w.bind("<Button-1>",clk)
        item("✏️","Editar perfil",self._editar_perfil)
        tk.Frame(outer,bg=SEP,height=1).pack(fill="x")
        def item_danger(icon,label,cmd):
            f=tk.Frame(outer,bg=BG,cursor="hand2",padx=16,pady=10); f.pack(fill="x")
            tk.Label(f,text=icon,bg=BG,fg="#F87171",font=("Segoe UI",12)).pack(side="left",padx=(0,12))
            tk.Label(f,text=label,bg=BG,fg="#F87171",font=("Segoe UI",10)).pack(side="left")
            def clk(e,_cmd=cmd): self._on_menu_close(menu); _cmd()
            for w in (f,*f.winfo_children()): w.bind("<Button-1>",clk)
        item_danger("⏻","Cerrar sesión",self._logout)
        def _focus_out(e):
            self.root.after(100,lambda: self._on_menu_close(menu) if self._menu_abierto else None)
        menu.bind("<FocusOut>",_focus_out)
        menu.bind("<Escape>",lambda e:self._on_menu_close(menu))
        self.root.bind("<Configure>",lambda e:self._on_menu_close(menu),add="+")
        menu.focus_set()

    def _on_menu_close(self,menu):
        try: self.root.unbind("<Configure>")
        except Exception: pass
        self._menu_abierto=None
        try:
            if menu.winfo_exists(): menu.destroy()
        except Exception: pass
        if self._hover_anim_id:
            try: self.root.after_cancel(self._hover_anim_id)
            except Exception: pass
            self._hover_anim_id=None
        self._pill_bg=BG_HEADER
        self._pill_canvas.config(bg=BG_HEADER,highlightthickness=0)
        self._dibujar_pill_canvas()

    def _cambiar_foto_rapido(self):
        path=filedialog.askopenfilename(title="Elegir foto",
              filetypes=[("Imágenes","*.png *.jpg *.jpeg"),("Todos","*.*")])
        if not path: return
        actualizar_usuario(self.usuario["id"],self.usuario["nombre"],self.usuario["correo"],path)
        self.usuario["foto_path"]=path; self._dibujar_pill_canvas()

    def _logout(self):
        ok=Messagebox.yesno("¿Cerrar sesión?","Salir",parent=self)
        if ok=="Yes":
            for w in self.root.winfo_children(): w.destroy()
            self.root.withdraw(); abrir_login()

    def _editar_perfil(self):
        EditarPerfilWindow(self,self.usuario,self._on_perfil_actualizado)

    def _on_perfil_actualizado(self,u):
        if u is None:
            for w in self.root.winfo_children(): w.destroy()
            self.root.withdraw(); abrir_login(); return
        self.usuario=u; self._dibujar_pill_canvas()

    # ── Menú ⋮ ───────────────────────────────────────────
    def _toggle_dot_menu(self,event=None):
        if self._menu_dot_abierto:
            try:
                if self._menu_dot_abierto.winfo_exists():
                    self._cerrar_dot_menu(); return
            except Exception: pass
        self._show_dot_menu()

    def _show_dot_menu(self):
        BG="#0D0C22"; SEP="#1E1C42"; ACC="#7C3AED"
        menu=tk.Toplevel(self.root)
        menu.overrideredirect(True); menu.attributes("-topmost",True)
        rw=220
        rx=self.root.winfo_rootx()+self.root.winfo_width()-rw-8
        ry=self.root.winfo_rooty()+62
        menu.geometry(f"+{rx}+{ry}")
        self._menu_dot_abierto=menu
        outer=tk.Frame(menu,bg=BG,highlightbackground=ACC,highlightthickness=1)
        outer.pack(fill="both",expand=True)
        items=[
            ("📊","Exportar a Excel",self._exportar_excel),
            ("📄","Exportar a PDF",  self._exportar_pdf),
            (None,None,None),
            ("🗑️","Eliminar seleccionado",self._eliminar_seleccionado),
        ]
        for icon,label,cmd in items:
            if icon is None:
                tk.Frame(outer,bg=SEP,height=1).pack(fill="x"); continue
            fg_i="#A78BFA" if "Elim" not in label else "#F87171"
            fg_l="#D1D5DB" if "Elim" not in label else "#F87171"
            f=tk.Frame(outer,bg=BG,cursor="hand2",padx=14,pady=9); f.pack(fill="x")
            tk.Label(f,text=icon,bg=BG,fg=fg_i,font=("Segoe UI",11)).pack(side="left",padx=(0,10))
            tk.Label(f,text=label,bg=BG,fg=fg_l,font=("Segoe UI",10)).pack(side="left")
            def clk(e,_cmd=cmd): self._cerrar_dot_menu(); _cmd()
            for w in (f,*f.winfo_children()): w.bind("<Button-1>",clk)
        def _focus_out(e):
            self.root.after(100,lambda: self._cerrar_dot_menu() if self._menu_dot_abierto else None)
        menu.bind("<FocusOut>",_focus_out)
        menu.bind("<Escape>",lambda e:self._cerrar_dot_menu())
        self.root.bind("<Configure>",lambda e:self._cerrar_dot_menu(),add="+")
        menu.focus_set()

    def _cerrar_dot_menu(self):
        try: self.root.unbind("<Configure>")
        except Exception: pass
        if self._menu_dot_abierto:
            try:
                if self._menu_dot_abierto.winfo_exists(): self._menu_dot_abierto.destroy()
            except Exception: pass
            self._menu_dot_abierto=None

    # ── Refresh ───────────────────────────────────────────
    def _auto_refresh(self): self._refresh(); self.root.after(60_000,self._auto_refresh)

    def _refresh(self):
        self._actualizar_meses(); self._cargar_tabla()
        self._actualizar_resumen(); self._animar_graficas()

    def _actualizar_meses(self):
        meses=meses_disponibles(self.usuario["id"]); self.cb_mes["values"]=meses
        if meses and self.mes_var.get() not in meses: self.mes_var.set(meses[0])
        elif not meses: self.mes_var.set("")

    def _rango_actual(self):
        mes=self.mes_var.get()
        return rango_del_mes(mes) if mes else ("0000-00-00 00:00:00","9999-99-99 23:59:59")

    def _ver_todo(self): self.mes_var.set(""); self._refresh()

    def _cargar_tabla(self):
        for row in self.tree.get_children(): self.tree.delete(row)
        inicio,fin=self._rango_actual()
        for mid,tipo,concepto,monto,fh in obtener_movimientos_rango(self.usuario["id"],inicio,fin):
            self.tree.insert("","end",values=(mid,tipo,concepto,f"${monto:,.2f}",fh),tags=(tipo,))

    def _actualizar_resumen(self):
        inicio,fin=self._rango_actual()
        ing,gas,saldo=calcular_totales_rango(self.usuario["id"],inicio,fin)
        self.lbl_ing.config(text=f"Ingresos: ${ing:,.2f}",fg=GREEN)
        self.lbl_gas.config(text=f"Gastos: ${gas:,.2f}",fg=RED)
        self.lbl_saldo.config(text=f"Saldo: ${saldo:,.2f}",fg=BLUE if saldo>=0 else RED)

    # ── Gráficas ──────────────────────────────────────────
    def _animar_graficas(self):
        inicio,fin=self._rango_actual()
        ing,gas,saldo=calcular_totales_rango(self.usuario["id"],inicio,fin)
        self._anim_target=[ing,gas,saldo]
        self._anim_current=[0.0,0.0,0.0]; self._anim_step=0
        self._dibujar_lineas_historicas()
        if self._anim_id:
            try: self.root.after_cancel(self._anim_id)
            except Exception: pass
        self._tick_bar_anim()

    def _tick_bar_anim(self):
        STEPS=20; self._anim_step+=1
        ease=1-(1-self._anim_step/STEPS)**3
        self._anim_current=[v*ease for v in self._anim_target]
        self._dibujar_barras(*self._anim_current)
        self.canvas_fig.draw_idle()
        if self._anim_step<STEPS:
            self._anim_id=self.root.after(30,self._tick_bar_anim)

    def _dibujar_barras(self,ing,gas,saldo):
        BG="#0F172A"; GRID="#1E293B"
        ax=self.ax_bar; ax.clear(); ax.set_facecolor(BG)
        etiquetas=["Ingresos","Gastos","Saldo"]
        valores=[ing,gas,saldo]
        bcolors=["#10B981","#EF4444","#3B82F6" if saldo>=0 else "#EF4444"]
        glow=["#34D399","#F87171","#60A5FA" if saldo>=0 else "#F87171"]
        x=np.arange(len(etiquetas))
        for xi,val,gc in zip(x,valores,glow):
            ax.bar(xi,val,width=0.62,color=gc,alpha=0.18,zorder=1)
        bars=ax.bar(x,valores,width=0.48,color=bcolors,edgecolor="none",zorder=3)
        for bar,gc in zip(bars,glow):
            h=bar.get_height(); xi=bar.get_x(); w=bar.get_width()
            ax.plot([xi,xi+w],[h,h],color=gc,linewidth=2,zorder=4)
        mx=max(abs(v) for v in valores) if any(valores) else 1
        for bar,val,gc in zip(bars,valores,glow):
            ax.text(bar.get_x()+bar.get_width()/2,bar.get_height()+mx*0.04,
                    f"${val:,.0f}",ha="center",va="bottom",fontsize=8,fontweight="bold",color=gc,zorder=5)
        ax.set_xticks(x); ax.set_xticklabels(etiquetas,fontsize=9,color="#94A3B8",fontweight="bold")
        ax.tick_params(axis="y",labelsize=7.5,colors="#475569")
        ax.set_title("Resumen del período",fontsize=10,fontweight="bold",color="#E2E8F0",pad=8)
        for spine in ax.spines.values(): spine.set_color(GRID)
        ax.set_axisbelow(True); ax.yaxis.grid(True,color=GRID,linewidth=0.8,linestyle="--")
        ax.xaxis.grid(False)
        if mx>0: ax.set_ylim(bottom=min(0,min(valores))-mx*0.05,top=max(valores)+mx*0.22)

    def _dibujar_lineas_historicas(self):
        BG="#0F172A"; GRID="#1E293B"
        ax=self.ax_line; ax.clear(); ax.set_facecolor(BG)
        meses=sorted(meses_disponibles(self.usuario["id"]))
        if not meses:
            ax.text(0.5,0.5,"Sin historial aún\nRegistra movimientos para ver el historial",
                    ha="center",va="center",transform=ax.transAxes,fontsize=9,color="#475569",multialignment="center")
            ax.set_title("Historial mensual",fontsize=10,fontweight="bold",color="#94A3B8")
            for spine in ax.spines.values(): spine.set_color(GRID)
            return
        etiquetas_x=[]
        for m in meses:
            try: etiquetas_x.append(datetime.strptime(m,"%Y-%m").strftime("%b %y"))
            except Exception: etiquetas_x.append(m[5:]+"/"+m[2:4])
        ing_vals=[]; gas_vals=[]; sal_vals=[]
        for m in meses:
            ini,fin=rango_del_mes(m); i,g,s=calcular_totales_rango(self.usuario["id"],ini,fin)
            ing_vals.append(i); gas_vals.append(g); sal_vals.append(s)
        x=np.arange(len(meses))
        ax.fill_between(x,ing_vals,alpha=0.15,color="#10B981",interpolate=True)
        ax.plot(x,ing_vals,color="#10B981",linewidth=2.5,marker="o",markersize=7,
                markerfacecolor="#0F172A",markeredgecolor="#10B981",markeredgewidth=2.2,label="Ingresos",zorder=5)
        ax.fill_between(x,gas_vals,alpha=0.12,color="#EF4444",interpolate=True)
        ax.plot(x,gas_vals,color="#EF4444",linewidth=2.2,marker="o",markersize=7,
                markerfacecolor="#0F172A",markeredgecolor="#EF4444",markeredgewidth=2.2,label="Gastos",zorder=5)
        ax.plot(x,sal_vals,color="#3B82F6",linewidth=2.0,linestyle="--",marker="^",markersize=7,
                markerfacecolor="#0F172A",markeredgecolor="#3B82F6",markeredgewidth=2,label="Saldo",zorder=5)
        all_vals=ing_vals+gas_vals+sal_vals
        for xi,(iv,gv,sv) in enumerate(zip(ing_vals,gas_vals,sal_vals)):
            for val,col in [(iv,"#34D399"),(gv,"#F87171"),(sv,"#60A5FA")]:
                lbl=f"${val/1000:.0f}k" if abs(val)>=1000 else f"${val:.0f}"
                ax.annotate(lbl,xy=(xi,val),xytext=(0,9),textcoords="offset points",
                            ha="center",fontsize=7.5,fontweight="bold",color=col)
        if min(sal_vals)<0: ax.axhline(0,color="#334155",linewidth=1,linestyle=":")
        ax.set_xticks(x); ax.set_xticklabels(etiquetas_x,fontsize=8.5,color="#94A3B8",fontweight="bold")
        ax.tick_params(axis="y",labelsize=7.5,colors="#475569")
        ax.set_title("Historial mensual",fontsize=10,fontweight="bold",color="#E2E8F0",pad=8)
        for spine in ax.spines.values(): spine.set_color(GRID)
        ax.set_axisbelow(True)
        ax.yaxis.grid(True,color=GRID,linewidth=0.7,linestyle="--")
        ax.xaxis.grid(True,color=GRID,linewidth=0.3,linestyle=":")
        if all_vals:
            top=max(all_vals); bot=min(all_vals)
            pad=(top-bot)*0.22 if top!=bot else abs(top)*0.3 or 1
            ax.set_ylim(bot-pad*0.3,top+pad)
        leg=ax.legend(loc="upper left",fontsize=8,framealpha=0.25,
                      edgecolor="#334155",facecolor="#1E293B",handlelength=1.8,markerscale=0.9)
        for txt in leg.get_texts(): txt.set_color("#E2E8F0")

    # ── Registrar / Eliminar ──────────────────────────────
    def _registrar(self):
        tipo=self.tipo_var.get(); concepto=self.concepto_var.get().strip()
        monto_s=self.monto_var.get().strip().replace(",",".")
        if not concepto:
            Messagebox.show_warning("Escribe un concepto.","Atención",parent=self); return
        try:
            monto=float(monto_s)
            if monto<=0: raise ValueError
        except ValueError:
            Messagebox.show_error("Monto inválido.","Error",parent=self); return
        if tipo=="Gasto":
            inicio,fin=self._rango_actual()
            ing,gas,saldo=calcular_totales_rango(self.usuario["id"],inicio,fin)
            if monto>saldo:
                Messagebox.show_error(
                    f"Saldo insuficiente.\nDisponible: ${saldo:,.2f}\nIntentaste: ${monto:,.2f}",
                    "Sin fondos",parent=self); return
        insertar_movimiento(self.usuario["id"],tipo,concepto,monto)
        self.concepto_var.set(""); self.monto_var.set("")
        self._refresh()

    def _eliminar_seleccionado(self):
        sel=self.tree.selection()
        if not sel:
            Messagebox.show_warning("Selecciona un movimiento.","Atención",parent=self); return
        mov_id=self.tree.item(sel[0])["values"][0]
        ok=Messagebox.yesno(f"¿Eliminar movimiento #{mov_id}?","Confirmar")
        if ok=="Yes": eliminar_movimiento_por_id(mov_id,self.usuario["id"]); self._refresh()

    # ── Exportaciones ─────────────────────────────────────
    def _generar_imagen_grafica(self):
        tmp_fd,tmp_path=tempfile.mkstemp(suffix=".jpg"); os.close(tmp_fd)
        try:
            from PIL import Image as PILImage
            import io
            buf=io.BytesIO()
            self.fig.savefig(buf,dpi=130,bbox_inches="tight",
                             facecolor=self.fig.get_facecolor(),format="png")
            buf.seek(0)
            PILImage.open(buf).convert("RGB").save(tmp_path,format="JPEG",quality=95)
        except ImportError:
            self.fig.savefig(tmp_path,dpi=130,bbox_inches="tight",facecolor="#0F172A",format="jpeg")
        return tmp_path

    def _nombre_extracto(self):
        mes=self.mes_var.get() or datetime.now().strftime("%Y-%m")
        return f"Extracto_{self.usuario['nombre'].replace(' ','_')}_{mes}"

    def _exportar_excel(self):
        inicio,fin=self._rango_actual()
        filas=obtener_movimientos_rango(self.usuario["id"],inicio,fin)
        ing,gas,saldo=calcular_totales_rango(self.usuario["id"],inicio,fin)
        path=filedialog.asksaveasfilename(defaultextension=".xlsx",
                   initialfile=self._nombre_extracto()+".xlsx",
                   filetypes=[("Excel","*.xlsx")],title="Guardar extracto Excel")
        if not path: return
        try:
            wb=Workbook(); ws=wb.active; ws.title="Movimientos"
            ws.merge_cells("A1:E1"); ws["A1"]=f"Extracto — {self.usuario['nombre']}"
            ws["A1"].font=Font(bold=True,size=14,color="7C3AED")
            ws["A1"].alignment=Alignment(horizontal="center")
            ws.merge_cells("A2:E2")
            ws["A2"]=f"Período: {inicio[:10]} → {fin[:10]}  |  {datetime.now().strftime('%d/%m/%Y %H:%M')}"
            ws["A2"].font=Font(italic=True,size=10,color="6B7280")
            ws["A2"].alignment=Alignment(horizontal="center")
            ws.merge_cells("A3:E3"); ws["A3"]=""
            ws.merge_cells("A4:B4"); ws["A4"]="Resumen"
            ws["A4"].font=Font(bold=True,size=11,color="FFFFFF")
            ws["A4"].fill=PatternFill("solid",fgColor="7C3AED")
            ws["A4"].alignment=Alignment(horizontal="center")
            for off,(lbl,val,color) in enumerate([("Ingresos",ing,"059669"),("Gastos",gas,"DC2626"),("Saldo",saldo,"2563EB")]):
                r=5+off
                ws.cell(row=r,column=1,value=lbl).font=Font(bold=True,color=color)
                c=ws.cell(row=r,column=2,value=val)
                c.number_format='"$"#,##0.00'; c.font=Font(bold=True,color=color)
            fill_h=PatternFill("solid",fgColor="4C1D95")
            for col,h in enumerate(["ID","Tipo","Concepto","Monto","Fecha / Hora"],1):
                cell=ws.cell(row=10,column=col,value=h)
                cell.font=Font(bold=True,color="FFFFFF"); cell.fill=fill_h
                cell.alignment=Alignment(horizontal="center")
            fill_ing=PatternFill("solid",fgColor="D1FAE5")
            fill_gas=PatternFill("solid",fgColor="FEE2E2")
            for r,(mid,tipo,concepto,monto,fh) in enumerate(filas,11):
                ws.cell(row=r,column=1,value=mid); ws.cell(row=r,column=2,value=tipo)
                ws.cell(row=r,column=3,value=concepto)
                ws.cell(row=r,column=4,value=monto).number_format='"$"#,##0.00'
                ws.cell(row=r,column=5,value=fh)
                f=fill_ing if tipo=="Ingreso" else fill_gas
                for c in range(1,6): ws.cell(row=r,column=c).fill=f
            for col_w,width in zip("ABCDE",[8,12,38,16,22]):
                ws.column_dimensions[col_w].width=width
            ws2=wb.create_sheet("Gráficas")
            ws2.merge_cells("A1:J1"); ws2["A1"]=f"Gráficas — {self.usuario['nombre']}"
            ws2["A1"].font=Font(bold=True,size=13,color="7C3AED")
            ws2["A1"].alignment=Alignment(horizontal="center")
            img_path=self._generar_imagen_grafica()
            try:
                xl_img=XLImage(img_path); xl_img.anchor="A3"
                xl_img.width=700; xl_img.height=500; ws2.add_image(xl_img)
                wb.save(path)
                Messagebox.show_info(f"✅ Excel guardado:\n{path}","Exportado")
            except Exception as e:
                Messagebox.show_error(f"Error Excel:\n{e}","Error")
            finally:
                try: os.remove(img_path)
                except Exception: pass
        except PermissionError:
            Messagebox.show_error("Archivo abierto en Excel. Ciérralo e intenta de nuevo.","Permiso denegado")
        except Exception as e:
            Messagebox.show_error(f"Error:\n{e}","Error")

    def _exportar_pdf(self):
        inicio,fin=self._rango_actual()
        filas=obtener_movimientos_rango(self.usuario["id"],inicio,fin)
        ing,gas,saldo=calcular_totales_rango(self.usuario["id"],inicio,fin)
        path=filedialog.asksaveasfilename(defaultextension=".pdf",
                   initialfile=self._nombre_extracto()+".pdf",
                   filetypes=[("PDF","*.pdf")],title="Guardar extracto PDF")
        if not path: return
        try:
            doc=SimpleDocTemplate(path,pagesize=A4,leftMargin=1.8*cm,rightMargin=1.8*cm,
                                  topMargin=2*cm,bottomMargin=2*cm)
            styles=getSampleStyleSheet(); story=[]
            title_s=ParagraphStyle("t",parent=styles["Title"],
                                    textColor=colors.HexColor("#7C3AED"),fontSize=18,spaceAfter=4)
            sub_s=ParagraphStyle("s",parent=styles["Normal"],
                                  textColor=colors.HexColor("#6B7280"),fontSize=10,spaceAfter=2,alignment=1)
            story.append(Paragraph("Extracto de Presupuesto",title_s))
            story.append(Paragraph(self.usuario["nombre"],sub_s))
            story.append(Paragraph(f"Período: <b>{inicio[:10]}</b> → <b>{fin[:10]}</b>  |  {datetime.now().strftime('%d/%m/%Y %H:%M')}",sub_s))
            story.append(Spacer(1,0.5*cm))
            t_res=PDFTable([["Concepto","Monto"],
                             ["Ingresos totales",f"${ing:,.2f}"],
                             ["Gastos totales",f"${gas:,.2f}"],
                             ["Saldo disponible",f"${saldo:,.2f}"]],colWidths=[8*cm,5*cm])
            t_res.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#4C1D95")),
                ("TEXTCOLOR",(0,0),(-1,0),colors.white),
                ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
                ("FONTSIZE",(0,0),(-1,-1),10),("ALIGN",(1,0),(1,-1),"RIGHT"),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F5F3FF")]),
                ("TEXTCOLOR",(0,1),(0,1),colors.HexColor("#059669")),
                ("TEXTCOLOR",(0,2),(0,2),colors.HexColor("#DC2626")),
                ("TEXTCOLOR",(0,3),(0,3),colors.HexColor("#2563EB")),
                ("FONTNAME",(0,1),(-1,-1),"Helvetica-Bold"),
                ("BOX",(0,0),(-1,-1),0.8,colors.HexColor("#7C3AED")),
                ("INNERGRID",(0,0),(-1,-1),0.25,colors.HexColor("#E5E7EB")),
                ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
                ("LEFTPADDING",(0,0),(-1,-1),10)]))
            story.append(t_res); story.append(Spacer(1,0.6*cm))
            if filas:
                story.append(Paragraph("Detalle de movimientos",
                    ParagraphStyle("h3",parent=styles["Heading3"],textColor=colors.HexColor("#4C1D95"))))
                story.append(Spacer(1,0.2*cm))
                data=[["ID","Tipo","Concepto","Monto","Fecha"]]+[
                    [str(mid),tipo,concepto,f"${monto:,.2f}",fh[:16]]
                    for mid,tipo,concepto,monto,fh in filas]
                t=PDFTable(data,colWidths=[1.2*cm,2.5*cm,7*cm,3*cm,4*cm])
                ts2=TableStyle([
                    ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#4C1D95")),
                    ("TEXTCOLOR",(0,0),(-1,0),colors.white),
                    ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
                    ("FONTSIZE",(0,0),(-1,-1),8),
                    ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F9FAFB")]),
                    ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#D1D5DB")),
                    ("INNERGRID",(0,0),(-1,-1),0.25,colors.HexColor("#E5E7EB")),
                    ("ALIGN",(3,0),(3,-1),"RIGHT"),
                    ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4)])
                for i,(_,tipo,*_r) in enumerate(filas,1):
                    ts2.add("BACKGROUND",(0,i),(-1,i),
                            colors.HexColor("#D1FAE5") if tipo=="Ingreso" else colors.HexColor("#FEE2E2"))
                t.setStyle(ts2); story.append(t)
            else:
                story.append(Paragraph("No hay movimientos en este período.",styles["Normal"]))
            story.append(PageBreak())
            story.append(Paragraph("Gráficas del período",
                ParagraphStyle("gt",parent=styles["Heading2"],textColor=colors.HexColor("#7C3AED"))))
            story.append(Spacer(1,0.4*cm))
            img_path=self._generar_imagen_grafica()
            try:
                page_w=A4[0]-3.6*cm
                story.append(PDFImage(img_path,width=page_w,height=page_w*0.72))
                doc.build(story)
                Messagebox.show_info(f"✅ PDF guardado:\n{path}","Exportado")
            except Exception as e:
                Messagebox.show_error(f"Error PDF:\n{e}","Error")
            finally:
                try: os.remove(img_path)
                except Exception: pass
        except Exception as e:
            Messagebox.show_error(f"Error:\n{e}","Error")

# ============================================================
# MAIN
# ============================================================
def iniciar_app(usuario):
    for w in root.winfo_children(): w.destroy()
    App(root, usuario)

def abrir_login():
    win = LoginWindow(root, _on_login_ok)
    win.protocol("WM_DELETE_WINDOW", root.destroy)

def _on_login_ok(user):
    root.deiconify(); iniciar_app(user)

if __name__ == "__main__":
    crear_tabla_movimientos()
    crear_tabla_usuarios()
    root = tk.Tk()
    root.withdraw()
    style = ttk.Style(root)
    try: style.theme_use("clam")
    except Exception: pass
    style.configure(".", font=("Segoe UI", 10))
    style.configure("TButton", padding=6)
    style.configure("TEntry", padding=4)
    style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
    abrir_login()
    root.mainloop()