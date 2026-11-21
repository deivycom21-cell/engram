"""Generate the sample PDF used in the demo (examples/demo-notes/patrones-arquitectura.pdf).

Usage:  python scripts/make_demo_pdf.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from pdfgen import make_pdf  # noqa: E402

PAGES = [
    [
        "Patrones de arquitectura para herramientas locales.",
        "Monolito modular: un único proceso con módulos bien separados. Reduce la complejidad operativa frente a los "
        "microservicios y es la opción recomendada cuando un equipo pequeño mantiene el sistema.",
        "Event sourcing: en lugar de guardar solo el estado actual, se registra cada cambio como un evento inmutable. "
        "Permite reconstruir el historial completo y auditar por qué cambió cada dato.",
    ],
    [
        "Bases de datos embebidas: SQLite ofrece transacciones, búsqueda de texto completo con FTS5 y extensiones para "
        "búsqueda vectorial, sin un servidor separado. Es adecuado para aplicaciones local-first.",
        "Los microservicios con colas de mensajes como Redis aportan escalabilidad independiente, pero exigen más memoria "
        "RAM y más trabajo de despliegue.",
    ],
]

if __name__ == "__main__":
    out = ROOT / "examples" / "demo-notes" / "patrones-arquitectura.pdf"
    out.write_bytes(make_pdf(PAGES, title="Patrones de arquitectura para herramientas locales", creation_date="20240420"))
    print(f"wrote {out}")
