"""Папки проектов в OneDrive: для принятых открытых проектов — `ecoCRM/projects and pricing/<Название> <AW…>`.

Запускается на Mac Егора (launchd, раз в час): OneDrive синхронизирует папку, Лола кладёт туда файлы.
Ключ Airtable — из .env бота (тот же AIRTABLE_TOKEN), в вывод не попадает. В Airtable ничего не пишет.

  python tools/project_folders.py            # только список: что создал бы и что уже есть
  python tools/project_folders.py --create   # создать недостающие и перенести папки не в своём месте

Проекты Лолы (партнёр Astros / Aluframe или Owner = Lola Sapaeva) — в «ASTROS ALUFRAME», остальные — в «ANDREU WORLD».

Принятый проект — открытый (Status не Won/Lost) и с менеджером в Notified / Pre-registered / Registered.
Папка «уже есть», если в имени есть код AW… проекта или проект записан в .project_folders.json
(старые папки без кода — «Inspira», «MR Group iCity» — сопоставлены вручную, их не переименовываем).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / "Library/CloudStorage/OneDrive-AndreuWorld/ecoCRM/projects and pricing"
# Структура Егора от 18.11.2026: проекты Лолы (@aw_eeu) — в «ASTROS ALUFRAME» (у неё доступ),
# остальные — в «ANDREU WORLD» (у неё доступа нет). В корне — общие папки (прайсы, AW HUB), их не трогаем.
LOLA_DIR = ROOT / "ASTROS ALUFRAME"
EGOR_DIR = ROOT / "ANDREU WORLD"
LOLA_PARTNERS = {"rec2DJSCpnhZORiyB", "recYQinVKoMACGVc1"}   # Aluframe, Astros
LOLA_OWNER = "Lola Sapaeva"
MAP_FILE = ROOT / ".project_folders.json"     # {id проекта: имя папки} — для папок без кода в имени
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
BASE = "appD8N8DAgcTAqfL1"
TABLE = "tbl3ezrJ2Zssu4Aoz"
F = {"name": "fldxdxuL2j3aBKGBZ", "ref": "fld9H3DLTtTOTluOb", "status": "flddwm09GKIgI34Xk",
     "partner": "fldh6aWJ5jmn3EYkA", "owner": "fldx02XpxAcZixuWj",
     "notified": "fldJRkBhuJzyg7QIX", "pre": "fldr4KkTqqgDZm7u6", "reg": "fldGaXBjJetxthhqv"}
MAX_NAME = 80


def _token() -> str:
    token = os.environ.get("AIRTABLE_TOKEN", "")
    if not token and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("AIRTABLE_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not token:
        sys.exit("Нет AIRTABLE_TOKEN (ни в окружении, ни в .env бота)")
    return token


def folder_name(name: str, ref: str) -> str:
    """«TBANK Telegraph AW260266»: без символов, недопустимых в OneDrive, не длиннее 80 знаков."""
    clean = re.sub(r'[\\/:*?"<>|#%]+', " ", name or "")
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    room = MAX_NAME - len(ref) - 1
    if len(clean) > room:
        clean = clean[:room].rsplit(" ", 1)[0].rstrip(" .—-") if " " in clean[:room] else clean[:room]
    return f"{clean} {ref}".strip()


def accepted_projects() -> list[dict]:
    from pyairtable import Api
    table = Api(_token()).table(BASE, TABLE)
    formula = (f"AND(OR(COUNTA({{{F['notified']}}})>0, COUNTA({{{F['pre']}}})>0, COUNTA({{{F['reg']}}})>0), "
               f"{{{F['status']}}}!='Won', {{{F['status']}}}!='Lost')")
    recs = table.all(formula=formula, fields=[F["name"], F["ref"], F["status"], F["partner"], F["owner"]],
                     use_field_ids=True)
    return [{"id": r["id"], "name": (r["fields"].get(F["name"]) or "").strip(), "ref": r["fields"].get(F["ref"]) or "",
             "lola": bool(set(r["fields"].get(F["partner"]) or []) & LOLA_PARTNERS)
                     or r["fields"].get(F["owner"]) == LOLA_OWNER}
            for r in recs]


def existing() -> tuple[dict, dict]:
    """{имя папки: путь} в корне и в папке Лолы; карта проект → папка для старых папок без кода."""
    folders: dict[str, Path] = {}
    for base in (LOLA_DIR, EGOR_DIR):
        if base.exists():
            folders.update({p.name: p for p in base.iterdir() if p.is_dir()})
    mapping = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else {}
    return folders, mapping


def plan() -> tuple[list, list, list]:
    """([(проект, путь)] на месте, [(проект, откуда, куда)] перенести, [(проект, путь)] создать)."""
    folders, mapping = existing()
    ok, move, todo = [], [], []
    for p in sorted(accepted_projects(), key=lambda x: x["ref"]):
        base = LOLA_DIR if p["lola"] else EGOR_DIR
        hit = mapping.get(p["id"]) or next((f for f in folders if p["ref"] and p["ref"] in f), None)
        if hit and hit in folders:
            src = folders[hit]
            (ok if src.parent == base else move).append((p, src) if src.parent == base else (p, src, base / hit))
        else:
            todo.append((p, base / folder_name(p["name"], p["ref"])))
    return ok, move, todo


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true", help="создать недостающие папки и перенести чужие")
    args = ap.parse_args()
    if not (LOLA_DIR.exists() and EGOR_DIR.exists()):
        sys.exit(f"Нет папок {LOLA_DIR.name} / {EGOR_DIR.name} в {ROOT} — OneDrive не подключён или папки переименованы")
    ok, move, todo = plan()
    if not args.create:
        print(f"Принятых открытых проектов: {len(ok) + len(move) + len(todo)}; на месте: {len(ok)}; "
              f"перенести: {len(move)}; создать: {len(todo)}")
        for p, src, dst in move:
            print(f"  ПЕРЕНЕСТИ {p['ref']}  {src.relative_to(ROOT)}  →  {dst.relative_to(ROOT)}")
        for p, dst in todo:
            print(f"  СОЗДАТЬ   {p['ref']}  →  {dst.relative_to(ROOT)}")
        return
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    for p, src, dst in move:
        if dst.exists():
            print(f"{stamp} не перенёс {src.name}: в {dst.parent.name} уже есть папка с таким именем")
            continue
        shutil.move(str(src), str(dst))
        print(f"{stamp} перенёс {p['ref']}: {src.relative_to(ROOT)} → {dst.relative_to(ROOT)}")
    for p, dst in todo:
        dst.mkdir(parents=True, exist_ok=True)
        print(f"{stamp} создал {dst.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
