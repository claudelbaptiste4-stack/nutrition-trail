"""Programmes personnalisés : création, modification, lancement.

Ce fichier se place dans app/programs.py. Il est branché sur l'application
principale avec deux lignes dans app/main.py (déjà présentes dans le nouveau main.py).
"""
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import supabase
from app.mobility import (
    EXERCISE_SECONDS,
    REST_SECONDS,
    duration_label,
    load_catalogue,
    program_seconds,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

MAX_PROGRAMS = 20          # programmes par utilisateur
MAX_ITEMS = 20             # exercices par programme
PAGE_SIZE = 15             # résultats de recherche par page
TEMP_POSITION = 100000     # position provisoire pour échanger deux exercices

TYPES = {"actif": "Actif", "passif": "Passif"}


def current_user_id(request: Request):
    return request.session.get("user_id")


def valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def get_program(user_id, program_id):
    """Renvoie le programme s'il appartient à l'utilisateur, sinon None."""
    if not valid_uuid(program_id):
        return None
    rows = (
        supabase.table("mobility_programs")
        .select("*")
        .eq("id", program_id)
        .eq("user_id", user_id)
        .execute()
        .data
    )
    return rows[0] if rows else None


def program_items(program_id):
    """Exercices du programme, dans l'ordre, avec les données du catalogue."""
    rows = (
        supabase.table("mobility_program_items")
        .select("*")
        .eq("program_id", program_id)
        .order("position")
        .execute()
        .data
    )
    by_id = {ex["id"]: ex for ex in load_catalogue()["exercises"]}
    items = []
    for row in rows:
        exercise = by_id.get(row["exercise_id"])
        if exercise:
            items.append({"item_id": row["id"], "position": row["position"], "exercise": exercise})
    return items


def editor_url(program_id, form=None, extra=None):
    """Adresse de l'éditeur, en gardant les filtres de recherche transmis par le formulaire."""
    params = {}
    if form is not None:
        for key in ("zone", "kind", "q", "page"):
            value = str(form.get(key) or "")
            if value:
                params[key] = value
    if extra:
        params.update(extra)
    query = urlencode(params)
    return f"/mobility/programs/{program_id}" + (f"?{query}" if query else "")


# ------------------------------------------------------------
# Liste des programmes
# ------------------------------------------------------------
@router.get("/mobility/programs")
def programs_list(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    programs = (
        supabase.table("mobility_programs")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    counts = {}
    if programs:
        rows = (
            supabase.table("mobility_program_items")
            .select("program_id")
            .in_("program_id", [p["id"] for p in programs])
            .execute()
            .data
        )
        for row in rows:
            counts[row["program_id"]] = counts.get(row["program_id"], 0) + 1
    for p in programs:
        n = counts.get(p["id"], 0)
        p["count"] = n
        p["duration"] = duration_label(program_seconds(n)) if n else "—"

    return templates.TemplateResponse(
        request=request,
        name="programs_list.html",
        context={"programs": programs, "max_programs": MAX_PROGRAMS, "full": len(programs) >= MAX_PROGRAMS},
    )


@router.post("/mobility/programs/create")
async def programs_create(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    name = str(form.get("name") or "").strip()[:60] or "Mon programme"
    existing = supabase.table("mobility_programs").select("id").eq("user_id", user_id).execute().data
    if len(existing) >= MAX_PROGRAMS:
        return RedirectResponse(url="/mobility/programs", status_code=303)
    created = supabase.table("mobility_programs").insert({"user_id": user_id, "name": name}).execute().data[0]
    return RedirectResponse(url=f"/mobility/programs/{created['id']}", status_code=303)


# ------------------------------------------------------------
# Éditeur d'un programme
# ------------------------------------------------------------
@router.get("/mobility/programs/{program_id}")
def program_editor(
    request: Request,
    program_id: str,
    zone: str = "",
    kind: str = "",
    q: str = "",
    page: int = 1,
    done: int = 0,
):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    program = get_program(user_id, program_id)
    if program is None:
        return RedirectResponse(url="/mobility/programs", status_code=303)

    items = program_items(program_id)
    in_program = {i["exercise"]["id"] for i in items}

    page = max(1, page)
    query = supabase.table("mobility_exercises").select("*", count="exact")
    if zone:
        query = query.eq("zone", zone)
    if kind:
        query = query.eq("type", kind)
    if q:
        query = query.ilike("name", f"%{q}%")
    offset = (page - 1) * PAGE_SIZE
    response = query.order("zone").order("name").range(offset, offset + PAGE_SIZE - 1).execute()
    total = response.count or 0
    pages = max(1, -(-total // PAGE_SIZE))

    filters = {"zone": zone, "kind": kind, "q": q}
    qs = urlencode({k: v for k, v in filters.items() if v})
    zones = sorted({ex["zone"] for ex in load_catalogue()["exercises"]})

    return templates.TemplateResponse(
        request=request,
        name="program_edit.html",
        context={
            "program": program,
            "items": items,
            "duration": duration_label(program_seconds(len(items))) if items else "—",
            "in_program": in_program,
            "results": response.data,
            "total": total,
            "page": page,
            "pages": pages,
            "filters": filters,
            "qs": qs,
            "zones": zones,
            "types": TYPES,
            "max_items": MAX_ITEMS,
            "full": len(items) >= MAX_ITEMS,
            "done": bool(done),
        },
    )


@router.post("/mobility/programs/{program_id}/rename")
async def program_rename(request: Request, program_id: str):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    if get_program(user_id, program_id) is None:
        return RedirectResponse(url="/mobility/programs", status_code=303)
    form = await request.form()
    name = str(form.get("name") or "").strip()[:60]
    if name:
        supabase.table("mobility_programs").update({"name": name}).eq("id", program_id).eq("user_id", user_id).execute()
    return RedirectResponse(url=editor_url(program_id, form), status_code=303)


@router.post("/mobility/programs/{program_id}/add")
async def program_add(request: Request, program_id: str):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    if get_program(user_id, program_id) is None:
        return RedirectResponse(url="/mobility/programs", status_code=303)

    form = await request.form()
    exercise_id = str(form.get("exercise_id") or "")
    known = {ex["id"] for ex in load_catalogue()["exercises"]}
    items = program_items(program_id)
    already = {i["exercise"]["id"] for i in items}
    if exercise_id in known and exercise_id not in already and len(items) < MAX_ITEMS:
        position = max((i["position"] for i in items), default=0) + 1
        supabase.table("mobility_program_items").insert(
            {"program_id": program_id, "exercise_id": exercise_id, "position": position}
        ).execute()
    return RedirectResponse(url=editor_url(program_id, form), status_code=303)


@router.post("/mobility/programs/{program_id}/remove/{item_id}")
async def program_remove(request: Request, program_id: str, item_id: str):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    if get_program(user_id, program_id) is None or not valid_uuid(item_id):
        return RedirectResponse(url="/mobility/programs", status_code=303)

    supabase.table("mobility_program_items").delete().eq("id", item_id).eq("program_id", program_id).execute()
    # Renumérote 1, 2, 3... (de haut en bas, aucune position n'est utilisée deux fois)
    for number, item in enumerate(program_items(program_id), 1):
        if item["position"] != number:
            supabase.table("mobility_program_items").update({"position": number}).eq("id", item["item_id"]).execute()
    form = await request.form()
    return RedirectResponse(url=editor_url(program_id, form), status_code=303)


@router.post("/mobility/programs/{program_id}/move/{item_id}")
async def program_move(request: Request, program_id: str, item_id: str):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    if get_program(user_id, program_id) is None or not valid_uuid(item_id):
        return RedirectResponse(url="/mobility/programs", status_code=303)

    form = await request.form()
    direction = str(form.get("direction") or "")
    items = program_items(program_id)
    index = next((n for n, i in enumerate(items) if i["item_id"] == item_id), None)
    if index is not None and direction in ("up", "down"):
        other = index - 1 if direction == "up" else index + 1
        if 0 <= other < len(items):
            a, b = items[index], items[other]
            table = supabase.table
            # Échange en trois temps, pour ne jamais avoir deux exercices à la même position
            table("mobility_program_items").update({"position": TEMP_POSITION}).eq("id", a["item_id"]).execute()
            table("mobility_program_items").update({"position": a["position"]}).eq("id", b["item_id"]).execute()
            table("mobility_program_items").update({"position": b["position"]}).eq("id", a["item_id"]).execute()
    return RedirectResponse(url=editor_url(program_id, form), status_code=303)


@router.post("/mobility/programs/{program_id}/delete")
def program_delete(request: Request, program_id: str):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    if get_program(user_id, program_id) is not None:
        supabase.table("mobility_programs").delete().eq("id", program_id).eq("user_id", user_id).execute()
    return RedirectResponse(url="/mobility/programs", status_code=303)


# ------------------------------------------------------------
# Lancer un programme personnalisé
# ------------------------------------------------------------
@router.get("/mobility/programs/{program_id}/play")
def program_play(request: Request, program_id: str):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    program = get_program(user_id, program_id)
    if program is None:
        return RedirectResponse(url="/mobility/programs", status_code=303)

    items = program_items(program_id)
    if not items:
        return RedirectResponse(url=f"/mobility/programs/{program_id}", status_code=303)

    plan = [
        {
            "id": i["exercise"]["id"],
            "name": i["exercise"]["name"],
            "zone": i["exercise"]["zone"],
            "instruction": i["exercise"].get("instruction") or "",
            "per_side": bool(i["exercise"].get("per_side")),
        }
        for i in items
    ]
    return templates.TemplateResponse(
        request=request,
        name="mobility_play.html",
        context={
            "program": {"slug": "perso", "title": program["name"], "eyebrow": "Mon programme"},
            "plan": plan,
            "minutes": 0,
            "shuffle": 0,
            "exercise_seconds": EXERCISE_SECONDS,
            "rest_seconds": REST_SECONDS,
            "duration_text": duration_label(program_seconds(len(plan))),
            "back_url": f"/mobility/programs/{program_id}",
            "program_id": program_id,
        },
    )