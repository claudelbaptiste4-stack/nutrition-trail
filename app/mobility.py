"""Module Mobilité : accueil (score et programmes), test d'entrée, détail d'un programme.

Ce fichier se place dans app/mobility.py. Il est branché sur l'application
principale avec deux lignes dans app/main.py (voir les instructions).
"""
import random
import time
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import supabase

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# ------------------------------------------------------------
# Réglages
# ------------------------------------------------------------
CACHE_SECONDS = 300           # le catalogue est relu toutes les 5 minutes au plus
MAX_PER_ZONE = 2              # au plus 2 exercices par zone dans un programme
FREE_EQUIPMENT = {"tapis", "mur", "chaise"}   # matériel supposé disponible pour tout le monde
ASYMMETRY_ALERT = 15          # écart gauche-droite (en points) à signaler

PROGRAMS = [
    {"slug": "matin", "title": "Mobilité du matin", "eyebrow": "Matin",
     "description": "Réveille tes articulations avant la journée.",
     "moment": "reveil", "kind": "actif", "sport": None, "seconds": 480, "accent": "sage"},
    {"slug": "soir", "title": "Étirements du soir", "eyebrow": "Soir",
     "description": "Détends les muscles sollicités et prépare une bonne récupération.",
     "moment": "soir", "kind": "passif", "sport": None, "seconds": 600, "accent": "peach"},
    {"slug": "activation-course", "title": "Activation avant la course à pied", "eyebrow": "Avant la sortie · Course à pied",
     "description": "Prépare chevilles, hanches et fessiers avant de courir.",
     "moment": "avant_sortie", "kind": "actif", "sport": "trail", "seconds": 300, "accent": "sage"},
    {"slug": "activation-velo", "title": "Activation avant le vélo", "eyebrow": "Avant la sortie · Vélo",
     "description": "Prépare hanches, dos et épaules avant de rouler.",
     "moment": "avant_sortie", "kind": "actif", "sport": "velo", "seconds": 300, "accent": "lavender"},
    {"slug": "recuperation", "title": "Récupération après l'effort", "eyebrow": "Après la sortie",
     "description": "Retour au calme et étirements après une sortie.",
     "moment": "apres_sortie", "kind": "passif", "sport": None, "seconds": 480, "accent": "lavender"},
]

TEST_HELP = {
    "genou_mur": "Pied à plat devant un mur, orteils à distance du mur. Fléchis le genou pour le faire toucher le mur sans décoller le talon. Éloigne le pied petit à petit et note la distance maximale (bout du gros orteil au mur) à laquelle le genou touche encore. Teste chaque côté.",
    "flexion_assise": "Assis au sol, jambes tendues, pieds contre un mur. Penche-toi lentement en avant sans plier les genoux. Mesure la distance entre le bout des doigts et le bout des orteils : positive si tu dépasses les orteils, négative si tu ne les atteins pas (exemple : -10 pour 10 cm avant les orteils).",
    "flechisseurs_hanche": "Assis au bord d'un lit ou d'un banc, allonge-toi en ramenant un genou vers la poitrine avec les mains. Laisse l'autre jambe pendre, détendue. Mesure la distance entre l'arrière de la cuisse qui pend et le support. Teste chaque côté.",
    "talon_fesse": "À plat ventre, plie un genou pour rapprocher le talon de la fesse, sans cambrer le bas du dos. Mesure la distance entre le talon et la fesse. Teste chaque côté.",
    "position_4": "Assis au sol, dos contre un mur, pose une cheville sur le genou opposé et détends la hanche. Mesure la distance entre le genou de la jambe croisée et le sol. Teste chaque côté.",
    "ouverture_cote": "Allongé sur le côté, genoux fléchis à 90 degrés et joints, bras tendus devant toi. Ouvre le bras du dessus vers l'arrière en tournant la poitrine, sans décoller les genoux. Mesure la distance entre la main et le sol. Teste chaque côté.",
    "bras_mur": "Dos, fessiers et bas du dos contre le mur. Lève les bras tendus au-dessus de la tête, dos des mains vers le mur, sans cambrer. Mesure la distance entre le poignet et le mur. Teste chaque côté.",
    "menton_epaule": "Assis dos droit, épaules relâchées. Tourne lentement la tête sur le côté, sans lever l'épaule. Mesure la distance entre le menton et l'épaule. Teste chaque côté.",
    "squat_profond": "Pieds largeur d'épaules, talons au sol, descends en squat en t'asseyant sur un siège ou une boîte dont tu règles la hauteur. Note la hauteur la plus basse (en cm) à laquelle tu restes talons au sol et dos droit.",
}


# ------------------------------------------------------------
# Outils
# ------------------------------------------------------------
def current_user_id(request: Request):
    return request.session.get("user_id")


_cache = {"at": 0.0, "exercises": [], "links": {}, "tests": []}


def load_catalogue():
    """Charge catalogue, liens exercice-test et tests (mis en cache quelques minutes)."""
    now = time.time()
    if _cache["exercises"] and now - _cache["at"] < CACHE_SECONDS:
        return _cache
    exercises = supabase.table("mobility_exercises").select("*").execute().data
    links = supabase.table("mobility_exercise_tests").select("*").execute().data
    tests = supabase.table("mobility_tests").select("*").order("position").execute().data
    by_exercise = {}
    for link in links:
        by_exercise.setdefault(link["exercise_id"], []).append(link["test_code"])
    _cache.update(at=now, exercises=exercises, links=by_exercise, tests=tests)
    return _cache


def score_cm(value, worst, target):
    """Score de 0 à 100 : 0 pour la mesure « pire », 100 pour la mesure « cible »."""
    if value is None or worst is None or target is None or float(worst) == float(target):
        return None
    score = (float(value) - float(worst)) / (float(target) - float(worst)) * 100
    return round(max(0.0, min(100.0, score)), 1)


def level_of(score):
    if score is None:
        return "Pas de mesure", "none"
    if score < 40:
        return "À travailler", "low"
    if score < 70:
        return "Correct", "mid"
    return "Bon", "high"


def to_float(value):
    if value is None:
        return None
    value = str(value).strip().replace(",", ".")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def format_date(iso_text):
    """'2026-10-15T10:00:00+00:00' -> '15/10/2026'."""
    try:
        year, month, day = str(iso_text)[:10].split("-")
        return f"{day}/{month}/{year}"
    except ValueError:
        return ""


def equipment_ok(equipment):
    if not equipment:
        return True
    tokens = [t.strip().lower() for t in equipment.split(",")]
    return all(t in FREE_EQUIPMENT for t in tokens)


def latest_assessment(user_id):
    rows = (
        supabase.table("mobility_assessments")
        .select("*")
        .eq("user_id", user_id)
        .order("taken_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        return None
    assessment = rows[0]
    results = (
        supabase.table("mobility_assessment_results")
        .select("*")
        .eq("assessment_id", assessment["id"])
        .execute()
        .data
    )
    assessment["results"] = {r["test_code"]: r for r in results}
    return assessment


def build_program(program, user_id, assessment):
    """Choisit les exercices d'un programme.

    - filtre : moment, type (actif/passif), sport, matériel disponible ;
    - priorité : plus la zone liée a un score bas (et un poids élevé), plus l'exercice remonte ;
    - sans test, le choix change simplement chaque jour ;
    - on remplit le temps visé, avec au plus MAX_PER_ZONE exercices par zone.
    """
    cat = load_catalogue()
    weights = {t["code"]: max(t.get("weight_trail") or 1, t.get("weight_bike") or 1) for t in cat["tests"]}
    priority = {}
    if assessment:
        for code, result in assessment["results"].items():
            if result.get("zone_score") is not None:
                priority[code] = (100 - float(result["zone_score"])) * weights.get(code, 1)

    candidates = []
    for ex in cat["exercises"]:
        if program["moment"] not in (ex.get("moments") or []):
            continue
        if ex.get("type") != program["kind"]:
            continue
        if program["sport"] and program["sport"] not in (ex.get("sports") or []):
            continue
        if not equipment_ok(ex.get("equipment")) or not ex.get("duration_seconds"):
            continue
        candidates.append(ex)

    rng = random.Random(f"{user_id}-{program['slug']}-{date.today().isoformat()}")
    rng.shuffle(candidates)

    def exercise_priority(ex):
        return max((priority.get(code, 0) for code in cat["links"].get(ex["id"], [])), default=0)

    candidates.sort(key=lambda ex: -round(exercise_priority(ex) / 20))

    chosen, total, per_zone = [], 0, {}
    for ex in candidates:
        if per_zone.get(ex["zone"], 0) >= MAX_PER_ZONE:
            continue
        if total + ex["duration_seconds"] > program["seconds"]:
            continue
        chosen.append(ex)
        total += ex["duration_seconds"]
        per_zone[ex["zone"]] = per_zone.get(ex["zone"], 0) + 1
    return chosen, total


# ------------------------------------------------------------
# Pages
# ------------------------------------------------------------
@router.get("/mobility")
def mobility_home(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    assessment = latest_assessment(user_id)
    cat = load_catalogue()

    zones = []
    if assessment:
        for t in cat["tests"]:
            result = assessment["results"].get(t["code"]) or {}
            score = result.get("zone_score")
            label, level = level_of(score)
            zones.append({
                "name": t["zone"],
                "test": t["name"],
                "score": score,
                "label": label,
                "level": level,
                "asymmetry": result.get("asymmetry") or 0,
            })

    programs = []
    for program in PROGRAMS:
        chosen, total = build_program(program, user_id, assessment)
        programs.append({**program, "count": len(chosen), "minutes": round(total / 60) if chosen else 0})

    return templates.TemplateResponse(
        request=request,
        name="mobility_home.html",
        context={
            "assessment": assessment,
            "assessment_date": format_date(assessment["taken_at"]) if assessment else "",
            "global_score": assessment.get("global_score") if assessment else None,
            "global_label": level_of(assessment.get("global_score") if assessment else None)[0],
            "global_level": level_of(assessment.get("global_score") if assessment else None)[1],
            "zones": zones,
            "programs": programs,
            "asymmetry_alert": ASYMMETRY_ALERT,
        },
    )


@router.get("/mobility/program/{slug}")
def mobility_program(request: Request, slug: str):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    program = next((p for p in PROGRAMS if p["slug"] == slug), None)
    if program is None:
        return RedirectResponse(url="/mobility", status_code=303)

    assessment = latest_assessment(user_id)
    chosen, total = build_program(program, user_id, assessment)
    return templates.TemplateResponse(
        request=request,
        name="mobility_program.html",
        context={
            "program": program,
            "exercises": chosen,
            "minutes": round(total / 60) if chosen else 0,
            "personalized": assessment is not None,
        },
    )


@router.get("/mobility/test")
def mobility_test_form(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    tests = load_catalogue()["tests"]
    return templates.TemplateResponse(
        request=request,
        name="mobility_test.html",
        context={"tests": tests, "help": TEST_HELP, "error": None},
    )


@router.post("/mobility/test")
async def mobility_test_submit(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    tests = load_catalogue()["tests"]

    results = []
    weighted_total, weight_sum = 0.0, 0
    for t in tests:
        left = to_float(form.get(f"{t['code']}_left"))
        right = to_float(form.get(f"{t['code']}_right")) if t.get("per_side") else None
        if left is None:
            continue
        left_score = score_cm(left, t["worst_cm"], t["target_cm"])
        right_score = score_cm(right, t["worst_cm"], t["target_cm"]) if right is not None else None
        if right_score is None:
            zone_score, asymmetry = left_score, 0
        else:
            zone_score = round((left_score + right_score) / 2, 1)
            asymmetry = round(abs(left_score - right_score), 1)
        results.append({
            "test_code": t["code"],
            "left_cm": left,
            "right_cm": right,
            "left_score": left_score,
            "right_score": right_score,
            "zone_score": zone_score,
            "asymmetry": asymmetry,
        })
        weight = max(t.get("weight_trail") or 1, t.get("weight_bike") or 1)
        weighted_total += zone_score * weight
        weight_sum += weight

    if not results:
        return templates.TemplateResponse(
            request=request,
            name="mobility_test.html",
            context={"tests": tests, "help": TEST_HELP, "error": "Renseigne au moins une mesure pour calculer ton score."},
        )

    global_score = round(weighted_total / weight_sum, 1)
    assessment = (
        supabase.table("mobility_assessments")
        .insert({"user_id": user_id, "sports": ["trail", "velo"], "global_score": global_score})
        .execute()
        .data[0]
    )
    for r in results:
        r["assessment_id"] = assessment["id"]
    supabase.table("mobility_assessment_results").insert(results).execute()
    return RedirectResponse(url="/mobility", status_code=303)