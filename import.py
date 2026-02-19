import os
import sqlite3
import tempfile
from datetime import datetime

import tkinter as tk
import ttkbootstrap as tb
from ttkbootstrap.dialogs import Messagebox
from ttkbootstrap.constants import *

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.patches import FancyBboxPatch

from reportlab.lib.pagesizes import A4
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer,
    Table as PDFTable, TableStyle,
    Image as PDFImage, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import cm


# =========================
# BASE DE DATOS
# =========================
DB_NAME = "presupuesto.db"

def conectar_db():
    return sqlite3.connect(DB_NAME)

def crear_tabla():
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS movimientos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,
            concepto TEXT NOT NULL,
            monto REAL NOT NULL,
            fecha_hora TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def insertar_movimiento(tipo, concepto, monto):
    fecha_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO movimientos (tipo, concepto, monto, fecha_hora)
        VALUES (?, ?, ?, ?)
    """, (tipo, concepto, monto, fecha_hora))
    conn.commit()
    conn.close()

def obtener_movimientos_mes(yyyy_mm):
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, tipo, concepto, monto, fecha_hora
        FROM movimientos
        WHERE fecha_hora LIKE ?
        ORDER BY id DESC
    """, (f"{yyyy_mm}%",))
    filas = cur.fetchall()
    conn.close()
    return filas

def calcular_totales_mes(yyyy_mm):
    conn = conectar_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT COALESCE(SUM(monto),0)
        FROM movimientos
        WHERE tipo='Ingreso' AND fecha_hora LIKE ?
    """, (f"{yyyy_mm}%",))
    ingresos = float(cur.fetchone()[0])

    cur.execute("""
        SELECT COALESCE(SUM(monto),0)
        FROM movimientos
        WHERE tipo='Gasto' AND fecha_hora LIKE ?
    """, (f"{yyyy_mm}%",))
    gastos = float(cur.fetchone()[0])

    conn.close()
    return ingresos, gastos, ingresos - gastos

def meses_disponibles():
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT substr(fecha_hora,1,7)
        FROM movimientos
        ORDER BY 1 DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]

# =========================
# APP
# =========================
class App:

    def __init__(self, root):
        self.root = root
        self.root.title("💜 Presupuesto - Ingresos y Gastos")
        self.root.geometry("980x760")
        self.root.minsize(980, 760)

        self.FONT = ("Segoe UI", 11)
        self.FONT_BOLD = ("Segoe UI", 11, "bold")

        self.main = tb.Frame(self.root, padding=14)
        self.main.pack(fill="both", expand=True)

        # ==== REGISTRO ====
        frame_registro = tb.Labelframe(self.main, text="Registrar movimiento", padding=12)
        frame_registro.pack(fill="x", pady=10)

        tb.Label(frame_registro, text="Tipo:", font=self.FONT).grid(row=0, column=0, padx=5)
        self.tipo_var = tk.StringVar(value="Ingreso")

        tb.Combobox(
            frame_registro,
            textvariable=self.tipo_var,
            values=["Ingreso", "Gasto"],
            state="readonly",
            width=12
        ).grid(row=0, column=1)

        tb.Label(frame_registro, text="Concepto:", font=self.FONT).grid(row=0, column=2, padx=5)
        self.concepto_var = tk.StringVar()
        tb.Entry(frame_registro, textvariable=self.concepto_var, width=30).grid(row=0, column=3)

        tb.Label(frame_registro, text="Monto:", font=self.FONT).grid(row=0, column=4, padx=5)
        self.monto_var = tk.StringVar()
        tb.Entry(frame_registro, textvariable=self.monto_var, width=15).grid(row=0, column=5)

        tb.Button(
            frame_registro,
            text="➕ Agregar",
            command=self.agregar,
            bootstyle="primary"
        ).grid(row=0, column=6, padx=5)

        # ==== SELECTOR MES ====
        frame_mes = tb.Frame(self.main)
        frame_mes.pack(fill="x", pady=(2, 6))

        tb.Label(frame_mes, text="Mes para exportar (YYYY-MM):",
                 font=self.FONT_BOLD).pack(side="left", padx=(0, 8))

        self.mes_var = tk.StringVar()
        self.cb_mes = tb.Combobox(frame_mes,
                                  textvariable=self.mes_var,
                                  state="readonly",
                                  width=12)
        self.cb_mes.pack(side="left")

        tb.Button(frame_mes,
                  text="🔄 Actualizar meses",
                  command=self.cargar_meses,
                  bootstyle="info-outline").pack(side="left", padx=8)

        # ==== TOTALES ====
        frame_totales = tb.Frame(self.main)
        frame_totales.pack(fill="x")

        self.lbl_ingresos = tb.Label(frame_totales, font=self.FONT_BOLD,
                                     foreground="#2563EB")
        self.lbl_ingresos.pack(side="left", padx=10)

        self.lbl_gastos = tb.Label(frame_totales, font=self.FONT_BOLD,
                                   foreground="#DC2626")
        self.lbl_gastos.pack(side="left", padx=10)

        self.lbl_saldo = tb.Label(frame_totales, font=self.FONT_BOLD)
        self.lbl_saldo.pack(side="left", padx=10)

        # ==== TABLA ====
        columnas = ("id", "tipo", "concepto", "monto", "fecha_hora")
        self.tree = tb.Treeview(self.main,
                                columns=columnas,
                                show="headings",
                                height=9)
        self.tree.pack(fill="both", expand=True, pady=(10, 6))

        for col in columnas:
            self.tree.heading(col, text=col.capitalize())

        self.tree.column("id", width=60, anchor="center")
        self.tree.column("tipo", width=90, anchor="center")
        self.tree.column("concepto", width=320, anchor="w")
        self.tree.column("monto", width=120, anchor="e")
        self.tree.column("fecha_hora", width=180, anchor="center")

        # ==== GRAFICAS ====
        frame_graficas = tb.Frame(self.main)
        frame_graficas.pack(fill="both", expand=False)

        # Gráfica 1
        self.frame_g1 = tb.Labelframe(frame_graficas,
                                      text="Gráfica 1: Ingresos vs Gastos",
                                      padding=10)
        self.frame_g1.pack(side="left", fill="both", expand=True, padx=(0, 8))

        self.fig1 = Figure(figsize=(4.2, 2.6), dpi=100)
        self.ax1 = self.fig1.add_subplot(111)
        self.canvas1 = FigureCanvasTkAgg(self.fig1, master=self.frame_g1)
        self.canvas1.get_tk_widget().pack(fill="both", expand=True)

        # Gráfica 2
        self.frame_g2 = tb.Labelframe(frame_graficas,
                                      text="Gráfica 2: Ingresos, Gastos y Saldo",
                                      padding=10)
        self.frame_g2.pack(side="right", fill="both", expand=True, padx=(8, 0))

        self.fig2 = Figure(figsize=(4.2, 2.6), dpi=100)
        self.ax2 = self.fig2.add_subplot(111)
        self.canvas2 = FigureCanvasTkAgg(self.fig2, master=self.frame_g2)
        self.canvas2.get_tk_widget().pack(fill="both", expand=True)

        # ==== BOTONES EXPORTAR ====
        btns = tb.Frame(self.main)
        btns.pack(fill="x", pady=10)

        tb.Button(
            btns,
            text="💾 Exportar Excel (mes seleccionado)",
            command=self.exportar_excel_mes,
            bootstyle="danger-outline"
        ).pack(side="left", padx=6)

        tb.Button(
            btns,
            text="🧾 Exportar PDF (mes seleccionado)",
            command=self.exportar_pdf_mes,
            bootstyle="secondary-outline"
        ).pack(side="left", padx=6)

        self.cargar_meses()
        self.cargar_tabla()

    # ======================
    # FUNCIONES PRINCIPALES
    # ======================

    def parse_monto(self, texto):
        t = texto.strip().replace(" ", "")
        t = t.replace(".", "").replace(",", "")
        return float(t)

    def agregar(self):
        tipo = self.tipo_var.get()
        concepto = self.concepto_var.get().strip()
        monto_txt = self.monto_var.get().strip()

        if not concepto or not monto_txt:
            Messagebox.show_warning("Completa los campos", "Error")
            return

        try:
            monto = self.parse_monto(monto_txt)
        except:
            Messagebox.show_error("Monto inválido", "Error")
            return

        insertar_movimiento(tipo, concepto, monto)
        self.concepto_var.set("")
        self.monto_var.set("")
        self.cargar_meses()
        self.cargar_tabla()

    def cargar_meses(self):
        ms = meses_disponibles()
        if not ms:
            ms = [datetime.now().strftime("%Y-%m")]
        self.cb_mes["values"] = ms
        self.mes_var.set(ms[0])

    def cargar_tabla(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        conn = conectar_db()
        cur = conn.cursor()
        cur.execute("SELECT id, tipo, concepto, monto, fecha_hora FROM movimientos ORDER BY id DESC")
        filas = cur.fetchall()
        conn.close()

        ingresos = 0
        gastos = 0

        for fila in filas:
            self.tree.insert("", "end", values=fila)
            if fila[1] == "Ingreso":
                ingresos += fila[3]
            else:
                gastos += fila[3]

        saldo = ingresos - gastos

        self.lbl_ingresos.config(text=f"Ingresos: {ingresos:,.0f}")
        self.lbl_gastos.config(text=f"Gastos: {gastos:,.0f}")
        self.lbl_saldo.config(text=f"Saldo: {saldo:,.0f}",
                              foreground="#16A34A" if saldo >= 0 else "#DC2626")

        self._dibujar_barras_redondeadas(
            self.ax1,
            ["Ingresos", "Gastos"],
            [ingresos, gastos],
            ["#22C55E", "#EF4444"]
        )
        self.canvas1.draw()

        self._dibujar_barras_redondeadas(
            self.ax2,
            ["Ingresos", "Gastos", "Saldo"],
            [ingresos, gastos, saldo],
            ["#22C55E", "#EF4444", "#2563EB" if saldo >= 0 else "#DC2626"]
        )
        self.canvas2.draw()

    def _dibujar_barras_redondeadas(self, ax, etiquetas, valores, colores):
        ax.clear()
        bar_width = 0.6
        max_val = max(valores) if valores else 1

        for i, (val, color) in enumerate(zip(valores, colores)):
            patch = FancyBboxPatch(
                (i - bar_width/2, 0),
                bar_width,
                val,
                boxstyle="round,pad=0.02,rounding_size=0.15",
                linewidth=0,
                facecolor=color
            )
            ax.add_patch(patch)

        ax.set_xticks(range(len(etiquetas)))
        ax.set_xticklabels(etiquetas)
        ax.set_ylim(0, max_val * 1.25)
        ax.spines[['top', 'right', 'left']].set_visible(False)
        ax.grid(axis="y", linestyle="--", alpha=0.3)

    # =========================
    # EXPORTAR EXCEL
    # =========================
    def exportar_excel_mes(self):
        yyyy_mm = self.mes_var.get()
        if not yyyy_mm:
            Messagebox.show_warning("Selecciona un mes", "Atención")
            return

        filas = obtener_movimientos_mes(yyyy_mm)
        ingresos, gastos, saldo = calcular_totales_mes(yyyy_mm)

        path = f"movimientos_{yyyy_mm}.xlsx"

        tmp_dir = tempfile.mkdtemp(prefix="presupuesto_xlsx_")
        img1 = os.path.join(tmp_dir, "grafica1.png")
        img2 = os.path.join(tmp_dir, "grafica2.png")

        self.fig1.savefig(img1, dpi=160, bbox_inches="tight")
        self.fig2.savefig(img2, dpi=160, bbox_inches="tight")

        wb = Workbook()
        ws = wb.active
        ws.title = f"Mov_{yyyy_mm}"

        ws["A1"] = f"Presupuesto - Movimientos del mes {yyyy_mm}"
        ws["A1"].font = Font(bold=True, size=14)
        ws.merge_cells("A1:E1")

        ws["A3"] = "Total Ingresos"
        ws["B3"] = ingresos
        ws["A4"] = "Total Gastos"
        ws["B4"] = gastos
        ws["A5"] = "Saldo"
        ws["B5"] = saldo

        headers = ["ID", "Tipo", "Concepto", "Monto", "Fecha y Hora"]
        start_row = 7

        for col, h in enumerate(headers, start=1):
            ws.cell(row=start_row, column=col, value=h)

        for i, fila in enumerate(filas, start=start_row+1):
            for j, val in enumerate(fila, start=1):
                ws.cell(i, j, val)

        ws_g = wb.create_sheet("Gráficas")

        xlimg1 = XLImage(img1)
        xlimg1.width = 700
        xlimg1.height = 350
        ws_g.add_image(xlimg1, "A2")

        xlimg2 = XLImage(img2)
        xlimg2.width = 700
        xlimg2.height = 350
        ws_g.add_image(xlimg2, "A20")

        wb.save(path)

        Messagebox.show_info(f"Excel exportado:\n{path}", "Éxito")

    # =========================
    # EXPORTAR PDF BONITO
    # =========================
    def exportar_pdf_mes(self):
        yyyy_mm = self.mes_var.get()
        if not yyyy_mm:
            Messagebox.show_warning("Selecciona un mes", "Atención")
            return

        filas = obtener_movimientos_mes(yyyy_mm)
        ingresos, gastos, saldo = calcular_totales_mes(yyyy_mm)

        path = f"movimientos_{yyyy_mm}.pdf"

        tmp_dir = tempfile.mkdtemp(prefix="presupuesto_pdf_")
        img1 = os.path.join(tmp_dir, "grafica1.png")
        img2 = os.path.join(tmp_dir, "grafica2.png")

        self.fig1.savefig(img1, dpi=180, bbox_inches="tight")
        self.fig2.savefig(img2, dpi=180, bbox_inches="tight")

        doc = SimpleDocTemplate(
            path,
            pagesize=A4,
            rightMargin=2*cm,
            leftMargin=2*cm,
            topMargin=2*cm,
            bottomMargin=2*cm
        )

        styles = getSampleStyleSheet()
        story = []

        story.append(Paragraph("Presupuesto - Reporte Mensual", styles["Title"]))
        story.append(Spacer(1, 12))

        resumen = PDFTable([
            ["Total Ingresos", f"{ingresos:,.2f}"],
            ["Total Gastos", f"{gastos:,.2f}"],
            ["Saldo", f"{saldo:,.2f}"]
        ])

        story.append(resumen)
        story.append(Spacer(1, 12))

        story.append(PDFImage(img1, width=16*cm, height=7*cm))
        story.append(Spacer(1, 10))
        story.append(PDFImage(img2, width=16*cm, height=7*cm))

        story.append(PageBreak())

        headers = ["ID", "Tipo", "Concepto", "Monto", "Fecha"]
        data = [headers]

        for f in filas:
            data.append([str(x) for x in f])

        tabla = PDFTable(data)
        story.append(tabla)

        doc.build(story)

        Messagebox.show_info(f"PDF exportado:\n{path}", "Éxito")


# =========================
# INICIO DE LA APP
# =========================
if __name__ == "__main__":
    crear_tabla()

    root = tb.Window(themename="flatly")
    app = App(root)

    root.mainloop()
