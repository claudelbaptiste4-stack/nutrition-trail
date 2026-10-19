"""Page Profil : informations personnelles, sports, matériel de mobilité, produits de nutrition.

Ce fichier se place dans app/profile.py. Il est branché sur l'application
principale avec deux lignes dans app/main.py (voir les instructions).
"""
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import supabase

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

SPORT_OPTIONS = [("trail", "Trail / course à pied"), ("velo", "Vélo")]

EQUIPMENT_OPTIONS = [
    ("tapis", "Tapis de sol"),
    ("mur", "Un mur libre"),
    ("chaise", "Chaise, banc ou caisse"),
    ("rouleau", "Rouleau de massage"),
    ("balle", "Balle de massage ou de tennis"),
    ("pistolet", "Pistolet de massage"),
    ("elastique", "Élastique de résistance (avec un point d'ancrage)"),
    ("baton", "Bâton ou manche à balai"),
    ("poids", "Poids léger, disque ou bouteille d'eau"),
    ("sangle", "Sangle ou serviette"),
    ("barre", "Barre"),
    ("ballon", "Ballon"),
]
DEFAULT_EQUIPMENT = ["tapis", "mur", "chaise"]


def current_user_id(request: Request):
    return request.session.get("user_id")


def get_profile(user_id):
    rows = supabase.table("user_profiles").select("*").eq("user_id", user_id).limit(1).execute().data
    return rows[0] if rows else None


def profile_sports(profile):
    """Sports pratiqués ; par défaut trail et vélo tant que rien n'est choisi."""
    valid = {key for key, _ in SPORT_OPTIONS}
    sports = [s for s in ((profile or {}).get("sports") or []) if s in valid]
    return sports or [key for key, _ in SPORT_OPTIONS]


def profile_equipment(profile):
    """Matériel disponible ; avant tout enregistrement, on suppose tapis, mur et chaise."""
    if profile is None:
        return set(DEFAULT_EQUIPMENT)
    return set(profile.get("mobility_equipment") or [])


def to_float(value, low, high):
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if low <= number <= high else None


def age_from(birth_date):
    try:
        born = date.fromisoformat(str(birth_date)[:10])
    except ValueError:
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


@router.get("/profile")
def profile_page(request: Request, saved: int = 0):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    profile = get_profile(user_id)
    owned = supabase.table("user_products").select("product_id").eq("user_id", user_id).execute().data
    ids = [o["product_id"] for o in owned]
    products = []
    if ids:
        products = supabase.table("products").select("id, name, brand").in_("id", ids).order("name").execute().data

    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "profile": profile or {},
            "age": age_from(profile["birth_date"]) if profile and profile.get("birth_date") else None,
            "selected_sports": profile_sports(profile) if profile else [key for key, _ in SPORT_OPTIONS],
            "selected_equipment": sorted(profile_equipment(profile)),
            "sport_options": SPORT_OPTIONS,
            "equipment_options": EQUIPMENT_OPTIONS,
            "products": products,
            "saved": bool(saved),
        },
    )


@router.post("/profile")
async def profile_save(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    valid_sports = {key for key, _ in SPORT_OPTIONS}
    valid_equipment = {key for key, _ in EQUIPMENT_OPTIONS}

    birth_date = None
    raw_birth = str(form.get("birth_date") or "").strip()
    if raw_birth:
        try:
            born = date.fromisoformat(raw_birth)
            if date(1900, 1, 1) <= born <= date.today():
                birth_date = born.isoformat()
        except ValueError:
            birth_date = None

    row = {
        "user_id": user_id,
        "first_name": (str(form.get("first_name") or "").strip()[:60]) or None,
        "last_name": (str(form.get("last_name") or "").strip()[:60]) or None,
        "birth_date": birth_date,
        "height_cm": to_float(form.get("height_cm"), 100, 250),
        "weight_kg": to_float(form.get("weight_kg"), 30, 250),
        "sports": [s for s in form.getlist("sports") if s in valid_sports],
        "mobility_equipment": [e for e in form.getlist("mobility_equipment") if e in valid_equipment],
    }
    supabase.table("user_profiles").upsert(row, on_conflict="user_id").execute()
    return RedirectResponse(url="/profile?saved=1", status_code=303)