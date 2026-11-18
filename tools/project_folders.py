"""Папки проектов в OneDrive: для принятых открытых проектов — `ecoCRM/projects and pricing/<Название> <AW…>`.

Запускается на Mac Егора (launchd, раз в час): OneDrive синхронизирует папку, Лола кладёт туда файлы.
Ключ Airtable — из .env бота (тот же AIRTABLE_TOKEN), в вывод не попадает. В Airtable ничего не пишет.

  python tools/project_folders.py            # только список: что создал бы и что уже есть
  python tools/project_folders.py --create   # создать недостающие папки

Принятый проект — открытый (Status не Won/Lost) и с менеджером в Notified / Pre-registered / Registered.
Папка «уже есть», если в имени есть код AW… проекта или проект записан в .project_folders.json
(старые папки без кода — «Inspira», «MR Group iCity» — сопоставлены вручную, их не переименовываем).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path.home() / "Library/CloudStorage/OneDrive-AndreuWorld/ecoCRM/projects and pricing"
MAP_FILE = ROOT / ".project_folders.json"     # {id проекта: имя папки} — для папок без кода в имени
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
BASE = "appD8N8DAgcTAqfL1"
TABLE = "tbl3ezrJ2Zssu4Aoz"
F = {"name": "fldxdxuL2j3aBKGBZ", "ref": "fld9H3DLTtTOTluOb", "status": "flddwm09GKIgI34Xk",
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
    recs = table.all(formula=formula, fields=[F["name"], F["ref"], F["status"]], use_field_ids=True)
    return [{"id": r["id"], "name": (r["fields"].get(F["name"]) or "").strip(), "ref": r["fields"].get(F["ref"]) or ""}
            for r in recs]


def existing() -> tuple[set[str], dict]:
    folders = {p.name for p in ROOT.iterdir() if p.is_dir()} if ROOT.exists() else set()
    mapping = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else {}
    return folders, mapping


def plan() -> tuple[list[tuple[dict, str]], list[tuple[dict, str]]]:
    """([(проект, папка уже есть)], [(проект, папку создать)])."""
    folders, mapping = existing()
    have, todo = [], []
    for p in sorted(accepted_projects(), key=lambda x: x["ref"]):
        hit = mapping.get(p["id"]) or next((f for f in folders if p["ref"] and p["ref"] in f), None)
        if hit and hit in folders:
            have.append((p, hit))
        else:
            todo.append((p, folder_name(p["name"], p["ref"])))
    return have, todo


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true", help="создать недостающие папки")
    args = ap.parse_args()
    if not ROOT.exists():
        sys.exit(f"Нет папки {ROOT} — OneDrive не подключён?")
    have, todo = plan()
    if not args.create:
        print(f"Принятых открытых проектов: {len(have) + len(todo)}; папка есть: {len(have)}; создать: {len(todo)}")
        for p, f in have:
            print(f"  есть   {p['ref']}  →  {f}")
        for p, f in todo:
            print(f"  СОЗДАТЬ {p['ref']}  →  {f}")
        return
    for p, f in todo:
        (ROOT / f).mkdir(exist_ok=True)
        print(f"создал: {f}")


if __name__ == "__main__":
    main()
