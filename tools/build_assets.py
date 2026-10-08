"""Собирает файлы дела из проекта Detective в case/files и case/addressbook.json.

Запускать локально один раз (результат коммитится):
    python tools/build_assets.py [путь к Detective]

Нужны Pillow и pymupdf. На сервере не запускается.
"""

import json
import shutil
import sys
from pathlib import Path

import pymupdf
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent.parent.parent / "Detective"
OUT = ROOT / "case" / "files"
CASE = SRC / "tests" / "24-enchanted-rider"
OLD = SRC / "data" / "old"
RAW = SRC / "data" / "raw"

MAPS = ["SZ", "S", "SV", "Z", "C", "V", "YUZ", "YU", "YUV", "P"]
FONT = Path("C:/Windows/Fonts/times.ttf")
FONT_BOLD = Path("C:/Windows/Fonts/timesbd.ttf")


def to_jpg(src: Path, dst: Path, max_w: int = 1920, quality: int = 84) -> None:
    im = Image.open(src).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, "JPEG", quality=quality, optimize=True, progressive=True)


def pdf_previews(src: Path, stem: str, dst_dir: Path, dpi: int = 130) -> list[str]:
    names = []
    doc = pymupdf.open(src)
    for i, page in enumerate(doc, 1):
        name = f"{stem}_p{i}.jpg"
        pix = page.get_pixmap(dpi=dpi)
        tmp = dst_dir / f"{stem}_p{i}.png"
        pix.save(tmp)
        to_jpg(tmp, dst_dir / name, max_w=1600)
        tmp.unlink()
        names.append(name)
    return names


def build_public() -> None:
    pub = OUT / "public"
    pub.mkdir(parents=True, exist_ok=True)
    assets = CASE / "public" / "assets"
    to_jpg(assets / "brief.png", pub / "brief.jpg")
    to_jpg(assets / "newspaper_p1.png", pub / "newspaper_1.jpg", quality=88)
    to_jpg(assets / "newspaper_p2.png", pub / "newspaper_2.jpg", quality=88)
    to_jpg(RAW / "handbook_old_informants.png", pub / "handbook_1.jpg", quality=86)
    to_jpg(RAW / "handbook_old_hospitals_newspapers.png", pub / "handbook_2.jpg", quality=86)
    for code in MAPS:
        to_jpg(OLD / "map" / f"{code}.png", pub / f"map_{code}.jpg", quality=86)
    overview = pymupdf.open(RAW / "map_old.pdf")[0].get_pixmap(dpi=200)
    overview.save(pub / "map_overview.png")
    to_jpg(pub / "map_overview.png", pub / "map_overview.jpg", quality=86)
    (pub / "map_overview.png").unlink()
    shutil.copyfile(RAW / "map_old.pdf", pub / "map.pdf")


APPENDICES = {
    1: "app01_barley_farm_rentals",
    2: "app02_stud_farm_rentals",
    3: "app03_cemetery_plan",
    4: "app04_rider",
    5: "app05_body_photo",
    6: "app06_brothers_letter",
    7: "app07_scene_report",
    8: "app08_koch_letter",
    9: "app09_pontiac_registry",
    10: "app10_autopsy",
    11: "app11_photofit",
}


def build_appendices() -> dict:
    """Возвращает манифест {n: {download, previews}} для проверки case24.yaml."""
    src = CASE / "secret" / "assets"
    dst = OUT / "appendix"
    dst.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for n, stem in APPENDICES.items():
        out_stem = f"app{n:02d}"
        pdf, png = src / f"{stem}.pdf", src / f"{stem}.png"
        if pdf.exists():
            shutil.copyfile(pdf, dst / f"{out_stem}.pdf")
            previews = pdf_previews(pdf, out_stem, dst)
            download = f"{out_stem}.pdf"
        else:
            to_jpg(png, dst / f"{out_stem}.jpg", quality=90)
            previews = [f"{out_stem}.jpg"]
            download = f"{out_stem}.jpg"
        manifest[n] = {"download": download, "previews": previews}
    return manifest


def load_addressbook() -> list[dict]:
    raw = json.loads((RAW / "addressbook_old.json").read_text(encoding="utf-8"))
    rows = []
    for category, groups in raw.items():
        for group, items in groups.items():
            for item in items:
                rows.append({
                    "a": item["address"].strip(),
                    "t": item["title"].strip(),
                    "c": category.strip(),
                    "g": "" if category == "ЧАСТНЫЕ ЛИЦА" else group.strip(),
                })
    return rows


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def addressbook_html(rows: list[dict]) -> str:
    persons = sorted((r for r in rows if r["c"] == "ЧАСТНЫЕ ЛИЦА"), key=lambda r: r["t"])
    parts = ["<h1>Адресная книга Сейнт-Твинс</h1>", "<h2>Частные лица</h2>"]
    letter = ""
    for r in persons:
        if r["t"][0] != letter:
            letter = r["t"][0]
            parts.append(f"<h3>{esc(letter)}</h3>")
        parts.append(f"<p><b>{esc(r['a'])}</b> {esc(r['t'])}</p>")
    current = None
    for r in rows:
        if r["c"] == "ЧАСТНЫЕ ЛИЦА":
            continue
        if r["c"] != current:
            current = r["c"]
            parts.append(f"<h2>{esc(current.capitalize())}</h2>")
            group = None
        if r["g"] != group:
            group = r["g"]
            if group and group != current:
                parts.append(f"<h3>{esc(group)}</h3>")
        parts.append(f"<p><b>{esc(r['a'])}</b> {esc(r['t'])}</p>")
    return "".join(parts)


CSS = """
@font-face { font-family: book; src: url(times.ttf); }
@font-face { font-family: book; src: url(timesbd.ttf); font-weight: bold; }
* { font-family: book; }
h1 { font-size: 20px; text-align: center; margin: 0 0 10px 0; }
h2 { font-size: 14px; margin: 10px 0 4px 0; border-bottom: 1px solid #000; }
h3 { font-size: 11px; margin: 6px 0 2px 0; }
p { font-size: 9px; margin: 0; }
"""


def build_addressbook_pdf(rows: list[dict], dst: Path) -> None:
    archive = pymupdf.Archive()
    archive.add(FONT.read_bytes(), "times.ttf")
    archive.add(FONT_BOLD.read_bytes(), "timesbd.ttf")
    story = pymupdf.Story(html=addressbook_html(rows), user_css=CSS, archive=archive)
    page = pymupdf.paper_rect("a4")
    margin, gap = 36, 18
    col_w = (page.width - 2 * margin - gap) / 2
    cols = [
        pymupdf.Rect(margin, margin, margin + col_w, page.height - margin),
        pymupdf.Rect(margin + col_w + gap, margin, page.width - margin, page.height - margin),
    ]
    writer = pymupdf.DocumentWriter(str(dst))
    more, col, device = True, 0, None
    while more:
        if col == 0:
            device = writer.begin_page(page)
        more, _ = story.place(cols[col])
        story.draw(device)
        col = (col + 1) % 2
        if col == 0 or not more:
            writer.end_page()
    writer.close()


def main() -> None:
    if not CASE.exists():
        sys.exit(f"Нет папки дела: {CASE}")
    build_public()
    manifest = build_appendices()
    rows = load_addressbook()
    (ROOT / "case" / "addressbook.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")
    build_addressbook_pdf(rows, OUT / "public" / "addressbook.pdf")
    print(json.dumps(manifest, ensure_ascii=False, indent=1))
    print(f"адресов: {len(rows)}")


if __name__ == "__main__":
    main()
