#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extrae imágenes de los PDF de Ponte a Brillar y prepara su asignación al catálogo.

Uso desde la carpeta raíz del repositorio:
    python -m pip install pymupdf pillow
    python extraer_imagenes_pdf.py

Luego abre imagenes/contact-sheet.html, identifica las fotos y edita
imagenes/mapeo_imagenes.json. Asigna a cada ruta HTML el archivo extraído correcto.
Por ejemplo:
    "img/oreja-lobulo.jpg": "imagenes/_extraidas/catalogo_piercing_p003_img02.jpg"

Cuando termines:
    python extraer_imagenes_pdf.py --aplicar

El modo --aplicar copia las fotos a imagenes/catalogo/ y actualiza las rutas
correspondientes dentro de index.html. Los PDF originales nunca se modifican.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
import unicodedata
from io import BytesIO
from pathlib import Path

try:
    import fitz  # PyMuPDF
    from PIL import Image
except ImportError as exc:
    print("Faltan dependencias. Ejecuta: python -m pip install pymupdf pillow")
    raise SystemExit(1) from exc

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
IMAGE_ROOT = ROOT / "imagenes"
RAW_DIR = IMAGE_ROOT / "_extraidas"
FINAL_DIR = IMAGE_ROOT / "catalogo"
MAP_FILE = IMAGE_ROOT / "mapeo_imagenes.json"
CONTACT_FILE = IMAGE_ROOT / "contact-sheet.html"

# Captura referencias de imágenes de catálogo y de las dos secciones de servicio.
IMAGE_REF = re.compile(r'''["'](img/[^"']+\.(?:jpe?g|png|webp|gif))["']''', re.I)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower() or "pdf"


def save_web_image(data: bytes, dest_no_suffix: Path) -> Path:
    """Normaliza el recurso a JPEG o PNG compatible con los navegadores."""
    with Image.open(BytesIO(data)) as source:
        source.load()
        alpha = source.mode in ("RGBA", "LA") or "transparency" in source.info
        # Evita subir fotografías gigantes al repositorio; 1800 px bastan para web.
        source.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
        if alpha:
            dest = dest_no_suffix.with_suffix(".png")
            source.convert("RGBA").save(dest, "PNG", optimize=True)
        else:
            dest = dest_no_suffix.with_suffix(".jpg")
            source.convert("RGB").save(
                dest, "JPEG", quality=88, optimize=True, progressive=True
            )
    return dest


def extract_all_pdfs() -> list[dict]:
    pdfs = sorted(ROOT.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(
            "No se encontraron PDF en la raíz. Descarga/clona el repositorio completo."
        )
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    seen_hashes: dict[str, str] = {}

    for pdf_path in pdfs:
        print(f"Extrayendo: {pdf_path.name}")
        doc = fitz.open(pdf_path)
        pdf_slug = slugify(pdf_path.stem)
        for page_index, page in enumerate(doc, start=1):
            page_images = page.get_images(full=True)
            extracted_count = 0
            seen_xrefs: set[int] = set()

            for image_index, info in enumerate(page_images, start=1):
                xref = int(info[0])
                smask = int(info[1]) if len(info) > 1 else 0
                if not xref or xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    image_info = doc.extract_image(xref)
                    raw = image_info["image"]
                    if smask:
                        try:
                            base = fitz.Pixmap(doc, xref)
                            mask = fitz.Pixmap(doc, smask)
                            raw = fitz.Pixmap(base, mask).tobytes("png")
                        except Exception:
                            pass
                    digest = hashlib.sha256(raw).hexdigest()
                    if digest in seen_hashes:
                        rel = seen_hashes[digest]
                    else:
                        stem = f"{pdf_slug}_p{page_index:03d}_img{image_index:02d}"
                        saved = save_web_image(raw, RAW_DIR / stem)
                        rel = saved.relative_to(ROOT).as_posix()
                        seen_hashes[digest] = rel
                    records.append({
                        "archivo": rel,
                        "pdf": pdf_path.name,
                        "pagina": page_index,
                        "ancho": int(image_info.get("width", 0)),
                        "alto": int(image_info.get("height", 0)),
                        "tipo": "imagen incrustada",
                    })
                    extracted_count += 1
                except Exception as error:
                    print(f"  Aviso: no se pudo extraer xref {xref}: {error}")

            # Si la página no contiene imágenes incrustadas, genera una vista de página.
            # Esto cubre PDF donde la composición completa fue guardada como una sola página.
            if extracted_count == 0:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                raw = pix.tobytes("png")
                digest = hashlib.sha256(raw).hexdigest()
                if digest in seen_hashes:
                    rel = seen_hashes[digest]
                else:
                    stem = f"{pdf_slug}_p{page_index:03d}_pagina"
                    saved = save_web_image(raw, RAW_DIR / stem)
                    rel = saved.relative_to(ROOT).as_posix()
                    seen_hashes[digest] = rel
                records.append({
                    "archivo": rel,
                    "pdf": pdf_path.name,
                    "pagina": page_index,
                    "ancho": pix.width,
                    "alto": pix.height,
                    "tipo": "página renderizada (sin imágenes incrustadas)",
                })
        doc.close()

    # Deduplicación conservando el primer origen de cada archivo.
    unique: list[dict] = []
    seen: set[str] = set()
    for item in records:
        if item["archivo"] not in seen:
            unique.append(item)
            seen.add(item["archivo"])
    print(f"Total de archivos visuales únicos: {len(unique)}")
    return unique


def write_contact_sheet(records: list[dict]) -> None:
    cards = []
    for index, item in enumerate(records, 1):
        relative = Path(item["archivo"]).relative_to(IMAGE_ROOT).as_posix()
        label = (
            f"{index:03d} · {item['pdf']} · pág. {item['pagina']} · "
            f"{item['ancho']}×{item['alto']} · {item['tipo']}"
        )
        src = html.escape(relative, quote=True)
        cards.append(
            '<article class="card">'
            f'<a href="{src}" target="_blank" rel="noopener"><img src="{src}" '
            f'alt="{html.escape(label, quote=True)}" loading="lazy"></a>'
            f'<p>{html.escape(label)}</p><code>{html.escape(item["archivo"])}</code>'
            '</article>'
        )
    page = '''<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Imágenes extraídas | Ponte a Brillar</title><style>
*{box-sizing:border-box}body{margin:0;padding:24px;background:#faf7f2;color:#3b2a20;
font:14px/1.5 system-ui,sans-serif}header{max-width:1400px;margin:0 auto 22px}
h1{font-family:Georgia,serif;font-weight:500}.grid{max-width:1400px;margin:auto;
display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.card{padding:12px;background:#fff;border:1px solid #dfd2bd;border-radius:14px;min-width:0}
.card img{width:100%;height:190px;object-fit:contain;background:#f6f1e8;border-radius:8px}
.card p{margin:10px 0 4px;font-weight:600}.card code{display:block;overflow-wrap:anywhere;
font-size:11px;color:#6b5b50}
</style></head><body><header><h1>Imágenes extraídas de los PDF</h1>
<p>Identifica cada fotografía. Copia su ruta desde la columna inferior y asígnala
al nombre de perforación correspondiente en <b>mapeo_imagenes.json</b>.</p></header>
<main class="grid">''' + "\n".join(cards) + "</main></body></html>"
    CONTACT_FILE.write_text(page, encoding="utf-8")


def prepare_mapping() -> dict[str, str | None]:
    if not INDEX.exists():
        raise FileNotFoundError("No se encontró index.html en la raíz.")
    source = INDEX.read_text(encoding="utf-8")
    refs = sorted(set(IMAGE_REF.findall(source)))
    old: dict[str, str | None] = {}
    if MAP_FILE.exists():
        old = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    mapping = {ref: old.get(ref) for ref in refs}
    IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    MAP_FILE.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return mapping


def apply_mapping(mapping: dict[str, str | None]) -> None:
    missing = []
    operations: list[tuple[str, Path, Path]] = []
    raw_root = RAW_DIR.resolve()

    for html_ref, selected in mapping.items():
        if not selected:
            missing.append(html_ref)
            continue
        src = (ROOT / selected).resolve()
        if not src.is_relative_to(raw_root) or not src.is_file():
            missing.append(f"{html_ref} → archivo no válido/no encontrado: {selected}")
            continue
        extension = src.suffix.lower()
        target = FINAL_DIR / (Path(html_ref).stem + extension)
        operations.append((html_ref, src, target))

    if missing:
        print("\nNo se aplicó nada. Asigna todas las imágenes antes de continuar:")
        for item in missing:
            print(" -", item)
        print(f"Edita {MAP_FILE.relative_to(ROOT)} y vuelve a ejecutar con --aplicar.")
        return

    source_html = INDEX.read_text(encoding="utf-8")
    updated = source_html
    for html_ref, src, target in operations:
        FINAL_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        new_ref = target.relative_to(ROOT).as_posix()
        updated = updated.replace(html_ref, new_ref)

    backup = ROOT / "index.html.respaldo"
    if not backup.exists():
        shutil.copy2(INDEX, backup)
    INDEX.write_text(updated, encoding="utf-8")
    print(f"\nRutas enlazadas: {len(operations)}")
    print(f"Imágenes copiadas a: {FINAL_DIR.relative_to(ROOT)}")
    print(f"Respaldo del HTML: {backup.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true", help="aplica el JSON al index.html")
    args = parser.parse_args()
    try:
        records = extract_all_pdfs()
        write_contact_sheet(records)
        mapping = prepare_mapping()
        print(f"Galería visual: {CONTACT_FILE.relative_to(ROOT)}")
        print(f"Mapa de imágenes: {MAP_FILE.relative_to(ROOT)}")
        pending = sum(value is None for value in mapping.values())
        print(f"Referencias de imagen detectadas: {len(mapping)}")
        print(f"Asignaciones pendientes: {pending}")
        if args.aplicar:
            apply_mapping(mapping)
        else:
            print("Abre la galería, completa el JSON y vuelve a ejecutar con --aplicar.")
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
