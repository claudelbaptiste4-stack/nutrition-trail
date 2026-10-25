"""Module Mobilité : score, test d'entrée, programmes (5, 8 ou 10 min) et lecteur avec minuteur.

Ce fichier se place dans app/mobility.py. Il est branché sur l'application
principale avec deux lignes dans app/main.py (déjà faites à l'étape précédente).
"""
import random
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import supabase
from app.profile import get_profile, profile_equipment, profile_sports

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# ------------------------------------------------------------
# Réglages
# ------------------------------------------------------------
EXERCISE_SECONDS = 60         # chaque exercice dure 1 minute (30 s par côté s'il se fait des deux côtés)
REST_SECONDS = 10             # pause entre deux exercices, pour voir le suivant (et avant le premier)
DURATIONS = (5, 8, 10)        # durées proposées à l'utilisateur, en minutes
DEFAULT_MINUTES = 8
CACHE_SECONDS = 300           # le catalogue est relu toutes les 5 minutes au plus
MAX_PER_ZONE = 1              # un seul exercice par zone (muscle) dans un programme ; assoupli seulement s'il manque d'exercices
RECENT_SESSIONS = 3           # les exercices des 3 dernières séances d'un programme passent en dernier
ASYMMETRY_ALERT = 15          # écart gauche-droite (en points) à signaler
RETEST_DAYS = 42              # refaire le test environ toutes les 6 semaines pour suivre la progression

PROGRAMS = [
    {"slug": "matin", "title": "Mobilité du matin", "eyebrow": "Matin",
     "description": "Réveille tes articulations avant la journée.",
     "moment": "reveil", "kind": "actif", "sport": None, "accent": "sage"},
    {"slug": "soir", "title": "Étirements du soir", "eyebrow": "Soir",
     "description": "Détends les muscles sollicités et prépare une bonne récupération.",
     "moment": "soir", "kind": "passif", "sport": None, "accent": "peach"},
    {"slug": "activation-course", "title": "Activation avant la course à pied", "eyebrow": "Avant la sortie · Course à pied",
     "description": "Prépare chevilles, hanches et fessiers avant de courir.",
     "moment": "avant_sortie", "kind": "actif", "sport": "trail", "accent": "sage"},
    {"slug": "activation-velo", "title": "Activation avant le vélo", "eyebrow": "Avant la sortie · Vélo",
     "description": "Prépare hanches, dos et épaules avant de rouler.",
     "moment": "avant_sortie", "kind": "actif", "sport": "velo", "accent": "lavender"},
    {"slug": "recuperation", "title": "Récupération après l'effort", "eyebrow": "Après la sortie",
     "description": "Retour au calme et étirements après une sortie.",
     "moment": "apres_sortie", "kind": "passif", "sport": None, "accent": "lavender"},
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


def parse_iso(text):
    """Lit une date renvoyée par Supabase ('2026-10-15T10:00:00.123+00:00') ; None si illisible."""
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def retest_info(taken_at):
    """Jours écoulés depuis le dernier test, et date conseillée du prochain."""
    when = parse_iso(taken_at)
    if when is None:
        return None
    days = (datetime.now(timezone.utc) - when).days
    return {
        "days": days,
        "weeks": days // 7,
        "due": days >= RETEST_DAYS,
        "next_date": (when + timedelta(days=RETEST_DAYS)).strftime("%d/%m/%Y"),
    }


def format_date(iso_text):
    """'2026-10-15T10:00:00+00:00' -> '15/10/2026'."""
    try:
        year, month, day = str(iso_text)[:10].split("-")
        return f"{day}/{month}/{year}"
    except ValueError:
        return ""


def weight_for(test, sports):
    """Poids d'un test selon les sports pratiqués (le plus élevé si plusieurs)."""
    weights = []
    if "trail" in sports:
        weights.append(test.get("weight_trail") or 1)
    if "velo" in sports:
        weights.append(test.get("weight_bike") or 1)
    return max(weights) if weights else 1


def required_equipment(text):
    """Traduit le matériel noté sur un exercice en matériels du profil utilisateur."""
    if not text:
        return set()
    needs = set()
    for part in text.lower().split(","):
        part = part.strip()
        if not part or part == "aucun" or "facultatif" in part:
            continue
        if "rouleau" in part or "serviette roulée" in part:
            needs.add("rouleau")
        elif "sangle" in part or "serviette" in part:
            needs.add("sangle")
        if "balle" in part:
            needs.add("balle")
        if "pistolet" in part:
            needs.add("pistolet")
        if "élastique" in part or "ancrage" in part:
            needs.add("elastique")
        if "bâton" in part:
            needs.add("baton")
        if re.search(r"\bbarre\b", part):
            needs.add("barre")
        if "disque" in part or "poids" in part or "bouteille" in part:
            needs.add("poids")
        if "ballon" in part:
            needs.add("ballon")
        if "tapis" in part:
            needs.add("tapis")
        if re.search(r"\bmur\b", part):
            needs.add("mur")
        if re.search(r"chaise|caisse|banc|\blit\b|marche|step", part):
            needs.add("chaise")
    return needs


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


def recent_by_slug(user_id):
    """Exercices des dernières séances terminées, regroupés par programme."""
    sessions = (
        supabase.table("mobility_sessions")
        .select("id, moment")
        .eq("user_id", user_id)
        .order("started_at", desc=True)
        .limit(20)
        .execute()
        .data
    )
    if not sessions:
        return {}
    counts, keep = {}, []
    for s in sessions:
        slug = s.get("moment")
        counts[slug] = counts.get(slug, 0) + 1
        if counts[slug] <= RECENT_SESSIONS:
            keep.append(s)
    items = (
        supabase.table("mobility_session_items")
        .select("session_id, exercise_id")
        .in_("session_id", [s["id"] for s in keep])
        .execute()
        .data
    )
    by_session = {}
    for item in items:
        by_session.setdefault(item["session_id"], set()).add(item["exercise_id"])
    result = {}
    for s in keep:
        result.setdefault(s.get("moment"), set()).update(by_session.get(s["id"], set()))
    return result


def visible_programs(sports):
    """Masque l'activation d'un sport que l'utilisateur ne pratique pas."""
    return [p for p in PROGRAMS if not p["sport"] or p["sport"] in sports]


def build_program(program, user_id, minutes, shuffle, sports, equipment, assessment, recent_ids):
    """Choisit les exercices d'un programme (1 exercice = 1 minute, 10 s de pause entre deux).

    - filtres : moment, type (actif/passif), sport, matériel disponible ;
    - priorité : plus la zone liée a un score bas (et un poids élevé), plus l'exercice remonte ;
    - les exercices faits lors des dernières séances de ce programme passent en dernier ;
    - une seule fois la même zone dans un programme (sauf s'il manque d'exercices), jamais deux zones identiques à la suite ;
    - le choix reste stable dans la journée, sauf si l'utilisateur demande d'autres exercices (shuffle).
    """
    cat = load_catalogue()
    weights = {t["code"]: weight_for(t, sports) for t in cat["tests"]}
    priority = {}
    if assessment:
        for code, result in assessment["results"].items():
            if result.get("zone_score") is not None:
                priority[code] = (100 - float(result["zone_score"])) * weights.get(code, 1)

    candidates = []
    for ex in cat["exercises"]:
        if program["moment"] not in (ex.get("moments") or []) or ex.get("type") != program["kind"]:
            continue
        ex_sports = set(ex.get("sports") or [])
        if program["sport"] and program["sport"] not in ex_sports:
            continue
        if not ex_sports & set(sports):
            continue
        if not required_equipment(ex.get("equipment")) <= equipment:
            continue
        candidates.append(ex)

    rng = random.Random(f"{user_id}-{program['slug']}-{date.today().isoformat()}-{minutes}-{shuffle}")
    rng.shuffle(candidates)

    def bucket(ex):
        return round(max((priority.get(code, 0) for code in cat["links"].get(ex["id"], [])), default=0) / 20)

    candidates.sort(key=lambda ex: (ex["id"] in recent_ids, -bucket(ex)))

    count = exercise_count(minutes)
    chosen, chosen_ids, per_zone = [], set(), {}
    for cap in (MAX_PER_ZONE, MAX_PER_ZONE + 1, 99):
        for ex in candidates:
            if len(chosen) >= count:
                break
            if ex["id"] in chosen_ids or per_zone.get(ex["zone"], 0) >= cap:
                continue
            chosen.append(ex)
            chosen_ids.add(ex["id"])
            per_zone[ex["zone"]] = per_zone.get(ex["zone"], 0) + 1
        if len(chosen) >= count:
            break
    return spread_zones(chosen)


def exercise_count(minutes):
    """Nombre d'exercices qui tiennent dans la durée choisie, pauses de REST_SECONDS comprises."""
    return max(1, (minutes * 60 + REST_SECONDS) // (EXERCISE_SECONDS + REST_SECONDS))


def program_seconds(count):
    """Durée totale d'un programme : exercices + pauses entre eux (la pause avant le premier n'est pas comptée)."""
    return count * EXERCISE_SECONDS + max(0, count - 1) * REST_SECONDS


def duration_label(seconds):
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} min" if rest == 0 else f"{minutes} min {rest:02d}"


def spread_zones(items):
    """Ordonne les exercices pour ne jamais enchaîner deux fois la même zone, quand c'est possible."""
    remaining, ordered = list(items), []
    while remaining:
        last = ordered[-1]["zone"] if ordered else None
        pick = next((ex for ex in remaining if ex["zone"] != last), remaining[0])
        ordered.append(pick)
        remaining.remove(pick)
    return ordered


def clean_minutes(value):
    return value if value in DURATIONS else DEFAULT_MINUTES


def find_program(slug):
    return next((p for p in PROGRAMS if p["slug"] == slug), None)


# ------------------------------------------------------------
# Pages
# ------------------------------------------------------------
@router.get("/mobility")
def mobility_home(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    profile = get_profile(user_id)
    sports, equipment = profile_sports(profile), profile_equipment(profile)
    assessment = latest_assessment(user_id)
    recent = recent_by_slug(user_id)
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
    for program in visible_programs(sports):
        chosen = build_program(program, user_id, DEFAULT_MINUTES, 0, sports, equipment, assessment, recent.get(program["slug"], set()))
        programs.append({**program, "count": len(chosen), "duration": duration_label(program_seconds(len(chosen)))})

    global_score = assessment.get("global_score") if assessment else None
    global_label, global_level = level_of(global_score)
    return templates.TemplateResponse(
        request=request,
        name="mobility_home.html",
        context={
            "assessment": assessment,
            "assessment_date": format_date(assessment["taken_at"]) if assessment else "",
            "global_score": global_score,
            "global_label": global_label,
            "global_level": global_level,
            "zones": zones,
            "programs": programs,
            "asymmetry_alert": ASYMMETRY_ALERT,
            "retest": retest_info(assessment["taken_at"]) if assessment else None,
        },
    )


@router.get("/mobility/program/{slug}")
def mobility_program(
    request: Request,
    slug: str,
    minutes: int = Query(DEFAULT_MINUTES, alias="min"),
    v: int = 0,
    done: int = 0,
):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    program = find_program(slug)
    if program is None:
        return RedirectResponse(url="/mobility", status_code=303)

    minutes = clean_minutes(minutes)
    profile = get_profile(user_id)
    chosen = build_program(
        program, user_id, minutes, v, profile_sports(profile), profile_equipment(profile),
        latest_assessment(user_id), recent_by_slug(user_id).get(slug, set()),
    )
    return templates.TemplateResponse(
        request=request,
        name="mobility_program.html",
        context={
            "program": program,
            "exercises": chosen,
            "minutes": minutes,
            "durations": DURATIONS,
            "shuffle": v,
            "done": bool(done),
            "duration_text": duration_label(program_seconds(len(chosen))),
            "exercise_seconds": EXERCISE_SECONDS,
            "rest_seconds": REST_SECONDS,
        },
    )


@router.get("/mobility/play/{slug}")
def mobility_play(
    request: Request,
    slug: str,
    minutes: int = Query(DEFAULT_MINUTES, alias="min"),
    v: int = 0,
):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    program = find_program(slug)
    if program is None:
        return RedirectResponse(url="/mobility", status_code=303)

    minutes = clean_minutes(minutes)
    profile = get_profile(user_id)
    chosen = build_program(
        program, user_id, minutes, v, profile_sports(profile), profile_equipment(profile),
        latest_assessment(user_id), recent_by_slug(user_id).get(slug, set()),
    )
    if not chosen:
        return RedirectResponse(url=f"/mobility/program/{slug}?min={minutes}&v={v}", status_code=303)

    plan = [
        {
            "id": ex["id"],
            "name": ex["name"],
            "zone": ex["zone"],
            "instruction": ex.get("instruction") or "",
            "per_side": bool(ex.get("per_side")),
        }
        for ex in chosen
    ]
    return templates.TemplateResponse(
        request=request,
        name="mobility_play.html",
        context={
            "program": program,
            "plan": plan,
            "minutes": minutes,
            "shuffle": v,
            "exercise_seconds": EXERCISE_SECONDS,
            "rest_seconds": REST_SECONDS,
            "duration_text": duration_label(program_seconds(len(chosen))),
            "back_url": f"/mobility/program/{slug}?min={minutes}&v={v}",
            "program_id": None,
        },
    )


@router.post("/mobility/session/complete")
async def mobility_session_complete(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()

    # Séance d'un programme personnalisé
    program_id = str(form.get("program_id") or "")
    if program_id:
        return await complete_custom_session(user_id, program_id, form)

    slug = str(form.get("slug") or "")
    program = find_program(slug)
    if program is None:
        return RedirectResponse(url="/mobility", status_code=303)

    try:
        minutes = clean_minutes(int(form.get("minutes") or DEFAULT_MINUTES))
    except ValueError:
        minutes = DEFAULT_MINUTES
    known = {ex["id"] for ex in load_catalogue()["exercises"]}
    ids = list(dict.fromkeys(i for i in str(form.get("exercise_ids") or "").split(",") if i in known))[:30]

    if ids:
        session = (
            supabase.table("mobility_sessions")
            .insert({
                "user_id": user_id,
                "source": "proposee",
                "moment": slug,
                "planned_seconds": program_seconds(len(ids)),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            .execute()
            .data[0]
        )
        items = [
            {"session_id": session["id"], "position": n, "exercise_id": ex_id,
             "planned_seconds": EXERCISE_SECONDS, "completed": True}
            for n, ex_id in enumerate(ids, 1)
        ]
        supabase.table("mobility_session_items").insert(items).execute()
    return RedirectResponse(url=f"/mobility/program/{slug}?min={minutes}&done=1", status_code=303)


async def complete_custom_session(user_id, program_id, form):
    """Enregistre la séance d'un programme personnalisé (uniquement avec les exercices de ce programme)."""
    try:
        uuid.UUID(program_id)
    except ValueError:
        return RedirectResponse(url="/mobility/programs", status_code=303)
    owned = (
        supabase.table("mobility_programs").select("id")
        .eq("id", program_id).eq("user_id", user_id).execute().data
    )
    if not owned:
        return RedirectResponse(url="/mobility/programs", status_code=303)

    rows = supabase.table("mobility_program_items").select("exercise_id").eq("program_id", program_id).execute().data
    allowed = {r["exercise_id"] for r in rows}
    ids = list(dict.fromkeys(i for i in str(form.get("exercise_ids") or "").split(",") if i in allowed))[:30]
    if ids:
        session = (
            supabase.table("mobility_sessions")
            .insert({
                "user_id": user_id,
                "source": "programme",
                "program_id": program_id,
                "moment": "perso",
                "planned_seconds": program_seconds(len(ids)),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            .execute()
            .data[0]
        )
        items = [
            {"session_id": session["id"], "position": n, "exercise_id": ex_id,
             "planned_seconds": EXERCISE_SECONDS, "completed": True}
            for n, ex_id in enumerate(ids, 1)
        ]
        supabase.table("mobility_session_items").insert(items).execute()
    return RedirectResponse(url=f"/mobility/programs/{program_id}?done=1", status_code=303)


@router.get("/mobility/test")
def mobility_test_form(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="mobility_test.html",
        context={"tests": load_catalogue()["tests"], "help": TEST_HELP, "error": None},
    )


@router.post("/mobility/test")
async def mobility_test_submit(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    tests = load_catalogue()["tests"]
    sports = profile_sports(get_profile(user_id))

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
        weight = weight_for(t, sports)
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
        .insert({"user_id": user_id, "sports": sports, "global_score": global_score})
        .execute()
        .data[0]
    )
    for r in results:
        r["assessment_id"] = assessment["id"]
    supabase.table("mobility_assessment_results").insert(results).execute()
    return RedirectResponse(url="/mobility", status_code=303)