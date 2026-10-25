import csv
import io
import os
from urllib.parse import urlencode

import gpxpy
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.database import supabase, supabase_auth
from app.mobility import router as mobility_router
from app.profile import router as profile_router
from app.programs import router as programs_router
from app.progress import router as progress_router

# ------------------------------------------------------------
# Application et sécurité de base
# ------------------------------------------------------------
SESSION_SECRET = os.environ.get("SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("La variable d'environnement SESSION_SECRET est manquante.")

# Render définit la variable RENDER : en ligne on exige HTTPS pour le cookie de session
IS_PRODUCTION = bool(os.environ.get("RENDER"))

app = FastAPI(title="PeakFlex")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=IS_PRODUCTION,
    same_site="lax",
    max_age=14 * 24 * 3600,
)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(mobility_router)
app.include_router(profile_router)
app.include_router(programs_router)
app.include_router(progress_router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


MAX_CSV_BYTES = 1_000_000
MAX_CSV_ROWS = 500
MAX_GPX_BYTES = 10_000_000
PRODUCT_TYPES = {"gel", "barre", "boisson", "electrolytes"}


# ------------------------------------------------------------
# Outils
# ------------------------------------------------------------
def format_minutes(minutes):
    if minutes is None:
        return None
    total = round(minutes)
    h = total // 60
    m = total % 60
    if h > 0:
        return f"{h}h{m:02d}"
    return f"{m}min"


def current_user_id(request: Request):
    return request.session.get("user_id")


def to_login():
    return RedirectResponse(url="/login", status_code=303)


def clean_number(value, low, high, default=0.0):
    """Renvoie la valeur si elle est un nombre compris entre low et high, sinon la valeur par défaut."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or not (low <= number <= high):
        return default
    return number


def get_owned_race(user_id, race_id):
    """Renvoie la course si elle appartient à l'utilisateur, sinon None."""
    rows = supabase.table("races").select("*").eq("id", race_id).eq("user_id", user_id).execute().data
    return rows[0] if rows else None


def get_or_create_plan(race_id, mode):
    rows = supabase.table("plans").select("*").eq("race_id", race_id).execute().data
    if rows:
        return rows[0]
    return supabase.table("plans").insert({"race_id": race_id, "mode": mode}).execute().data[0]


# ------------------------------------------------------------
# Connexion et inscription
# ------------------------------------------------------------
@app.get("/")
def health_check(request: Request):
    if current_user_id(request):
        return RedirectResponse(url="/home", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/register")
def register_form(request: Request):
    return templates.TemplateResponse(request=request, name="register.html", context={})


@app.post("/register")
def register(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        result = supabase_auth.auth.sign_up({"email": email, "password": password})
    except Exception:
        return templates.TemplateResponse(
            request=request, name="register.html",
            context={"error": "Inscription impossible : vérifie ton e-mail et ton mot de passe (trop court, ou compte déjà existant)."}
        )
    # Si Supabase ne demande pas de confirmation par e-mail, on connecte tout de suite
    if result.session and result.user:
        request.session.clear()
        request.session["user_id"] = result.user.id
        request.session["email"] = result.user.email
        return RedirectResponse(url="/home", status_code=303)
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"message": "Compte créé ! Connecte-toi."}
    )


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        result = supabase_auth.auth.sign_in_with_password({"email": email, "password": password})
    except Exception:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Email ou mot de passe incorrect"}
        )
    request.session.clear()
    request.session["user_id"] = result.user.id
    request.session["email"] = result.user.email
    return RedirectResponse(url="/home", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/home")
def home(request: Request):
    if not current_user_id(request):
        return to_login()
    return templates.TemplateResponse(request=request, name="home.html", context={})


# ------------------------------------------------------------
# Produits (base partagée, réservée aux utilisateurs connectés)
# ------------------------------------------------------------
@app.get("/products")
def get_products(request: Request):
    if not current_user_id(request):
        return to_login()
    response = supabase.table("products").select("*").execute()
    return JSONResponse(response.data)


@app.get("/products/view")
def view_products(request: Request):
    if not current_user_id(request):
        return to_login()
    response = supabase.table("products").select("*").execute()
    return templates.TemplateResponse(
        request=request,
        name="products.html",
        context={"products": response.data}
    )


@app.post("/products/add")
def add_product(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    carbs_g: float = Form(0),
    sodium_mg: float = Form(0),
    caffeine_mg: float = Form(0)
):
    if not current_user_id(request):
        return to_login()
    name = name.strip()[:120]
    if not name or type not in PRODUCT_TYPES:
        return RedirectResponse(url="/products/view", status_code=303)
    supabase.table("products").insert({
        "name": name,
        "type": type,
        "carbs_g": clean_number(carbs_g, 0, 1000),
        "sodium_mg": clean_number(sodium_mg, 0, 20000),
        "caffeine_mg": clean_number(caffeine_mg, 0, 2000)
    }).execute()
    return RedirectResponse(url="/products/view", status_code=303)


@app.get("/products/import")
def import_products_form(request: Request):
    if not current_user_id(request):
        return to_login()
    return templates.TemplateResponse(request=request, name="import_products.html", context={})


@app.post("/products/import")
async def import_products(request: Request, csv_file: UploadFile = File(...)):
    if not current_user_id(request):
        return to_login()
    content = await csv_file.read()
    if len(content) > MAX_CSV_BYTES:
        return RedirectResponse(url="/products/import", status_code=303)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return RedirectResponse(url="/products/import", status_code=303)

    def optional_number(value, high):
        if value is None or str(value).strip() == "":
            return None
        number = clean_number(str(value).replace(",", "."), 0, high, default=None)
        return number

    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for index, row in enumerate(reader):
        if index >= MAX_CSV_ROWS:
            break
        name = (row.get("name") or "").strip()[:120]
        product_type = (row.get("type") or "").strip()[:40]
        if not name or not product_type:
            continue
        rows.append({
            "name": name,
            "brand": (row.get("brand") or "").strip()[:80] or None,
            "type": product_type,
            "carbs_g": optional_number(row.get("carbs_g"), 1000) or 0,
            "sodium_mg": optional_number(row.get("sodium_mg"), 20000) or 0,
            "caffeine_mg": optional_number(row.get("caffeine_mg"), 2000) or 0,
            "weight_g": optional_number(row.get("weight_g"), 5000),
            "volume_ml": optional_number(row.get("volume_ml"), 5000)
        })

    for start in range(0, len(rows), 100):
        supabase.table("products").insert(rows[start:start + 100]).execute()

    return RedirectResponse(url="/products/view", status_code=303)


@app.get("/my-products")
def my_products_view(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()
    products = supabase.table("products").select("*").execute().data
    owned = supabase.table("user_products").select("product_id").eq("user_id", user_id).execute().data
    owned_ids = {o["product_id"] for o in owned}
    for p in products:
        p["owned"] = p["id"] in owned_ids
    return templates.TemplateResponse(
        request=request,
        name="my_products.html",
        context={"products": products}
    )


@app.post("/my-products/update")
async def my_products_update(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()
    form = await request.form()
    valid_ids = {p["id"] for p in supabase.table("products").select("id").execute().data}
    product_ids = set()
    for pid in form.getlist("product_ids"):
        try:
            number = int(pid)
        except (TypeError, ValueError):
            continue
        if number in valid_ids:
            product_ids.add(number)
    supabase.table("user_products").delete().eq("user_id", user_id).execute()
    if product_ids:
        supabase.table("user_products").insert(
            [{"user_id": user_id, "product_id": pid} for pid in sorted(product_ids)]
        ).execute()
    return RedirectResponse(url="/my-products", status_code=303)


# ------------------------------------------------------------
# Courses
# ------------------------------------------------------------
@app.get("/races/view")
def view_races(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()
    response = supabase.table("races").select("*").eq("user_id", user_id).execute()
    return templates.TemplateResponse(
        request=request,
        name="races.html",
        context={"races": response.data}
    )


@app.post("/races/add")
async def add_race(
    request: Request,
    name: str = Form(...),
    distance_km: float = Form(0),
    elevation_gain_m: float = Form(0),
    duration_target_min: float = Form(0),
    temperature_c: float = Form(0),
    gpx_file: UploadFile = File(None)
):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()

    name = name.strip()[:120]
    if not name:
        return RedirectResponse(url="/races/view", status_code=303)
    distance_km = clean_number(distance_km, 0, 2000)
    elevation_gain_m = clean_number(elevation_gain_m, 0, 50000)
    duration_target_min = clean_number(duration_target_min, 0, 10000)
    temperature_c = clean_number(temperature_c, -40, 60)

    gpx_data = None
    if gpx_file and gpx_file.filename:
        content = await gpx_file.read()
        if len(content) <= MAX_GPX_BYTES:
            try:
                text = content.decode("utf-8")
                gpx = gpxpy.parse(text)
                gpx_distance = gpx.length_3d() / 1000
                uphill, _ = gpx.get_uphill_downhill()
                distance_km = gpx_distance
                elevation_gain_m = uphill
                gpx_data = text
            except Exception:
                gpx_data = None

    supabase.table("races").insert({
        "name": name,
        "distance_km": round(distance_km, 1),
        "elevation_gain_m": round(elevation_gain_m),
        "duration_target_min": duration_target_min,
        "temperature_c": temperature_c,
        "gpx_data": gpx_data,
        "user_id": user_id
    }).execute()
    return RedirectResponse(url="/races/view", status_code=303)


@app.post("/races/{race_id}/delete")
def delete_race(request: Request, race_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()
    supabase.table("races").delete().eq("id", race_id).eq("user_id", user_id).execute()
    return RedirectResponse(url="/races/view", status_code=303)


@app.get("/races/{race_id}")
def race_detail(request: Request, race_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()

    race = get_owned_race(user_id, race_id)
    if not race:
        return RedirectResponse(url="/races/view", status_code=303)

    points = []
    profile = []
    if race.get("gpx_data"):
        gpx = gpxpy.parse(race["gpx_data"])
        for track in gpx.tracks:
            for segment in track.segments:
                cumulative_km = 0
                previous_point = None
                for point in segment.points:
                    points.append([point.latitude, point.longitude])
                    if previous_point is not None:
                        cumulative_km += previous_point.distance_3d(point) / 1000
                    profile.append({
                        "km": round(cumulative_km, 2),
                        "elevation": round(point.elevation) if point.elevation is not None else None,
                        "lat": point.latitude,
                        "lon": point.longitude
                    })
                    previous_point = point
        if len(profile) > 300:
            step = len(profile) // 300
            profile = profile[::step]

    return templates.TemplateResponse(
        request=request,
        name="race_detail.html",
        context={"race": race, "points": points, "profile": profile}
    )


# ------------------------------------------------------------
# Ravitaillements et plan (chaque action vérifie que la course appartient à l'utilisateur)
# ------------------------------------------------------------
@app.post("/races/{race_id}/ravitos/add")
def add_ravito(request: Request, race_id: int, name: str = Form(...), position_km: float = Form(...)):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()
    if not get_owned_race(user_id, race_id):
        return RedirectResponse(url="/races/view", status_code=303)
    name = name.strip()[:80]
    if name:
        supabase.table("ravitos").insert({
            "race_id": race_id,
            "name": name,
            "position_km": clean_number(position_km, 0, 2000)
        }).execute()
    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)


@app.post("/races/{race_id}/ravitos/remove/{ravito_id}")
def remove_ravito(request: Request, race_id: int, ravito_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()
    if not get_owned_race(user_id, race_id):
        return RedirectResponse(url="/races/view", status_code=303)
    supabase.table("ravitos").delete().eq("id", ravito_id).eq("race_id", race_id).execute()
    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)


@app.get("/races/{race_id}/plan")
def view_plan(request: Request, race_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()

    race = get_owned_race(user_id, race_id)
    if not race:
        return RedirectResponse(url="/races/view", status_code=303)

    plan = get_or_create_plan(race_id, "manuel")

    items = supabase.table("plan_items").select("*").eq("plan_id", plan["id"]).order("position_km").execute().data
    all_products = supabase.table("products").select("*").execute().data
    products_by_id = {p["id"]: p for p in all_products}
    ravitos = supabase.table("ravitos").select("*").eq("race_id", race_id).order("position_km").execute().data

    owned = supabase.table("user_products").select("product_id").eq("user_id", user_id).execute().data
    owned_ids = {o["product_id"] for o in owned}
    my_products = [p for p in all_products if p["id"] in owned_ids]
    using_all_products = len(my_products) == 0
    dropdown_products = my_products if my_products else all_products

    totals = {"carbs_g": 0, "sodium_mg": 0, "caffeine_mg": 0}
    plan_items_full = []
    for item in items:
        product = products_by_id.get(item["product_id"])
        if product:
            qty = item["quantity"]
            plan_items_full.append({
                "id": item["id"],
                "position_km": item["position_km"],
                "position_min": item.get("position_min"),
                "time_display": format_minutes(item.get("position_min")),
                "product": product,
                "quantity": qty
            })
            totals["carbs_g"] += product.get("carbs_g", 0) * qty
            totals["sodium_mg"] += product.get("sodium_mg", 0) * qty
            totals["caffeine_mg"] += product.get("caffeine_mg", 0) * qty

    shopping_list = {}
    for item in plan_items_full:
        pid = item["product"]["id"]
        if pid not in shopping_list:
            shopping_list[pid] = {"product": item["product"], "quantity": 0}
        shopping_list[pid]["quantity"] += item["quantity"]
    shopping_list = sorted(shopping_list.values(), key=lambda x: x["product"]["name"])

    boundaries = [0] + [r["position_km"] for r in ravitos] + [race.get("distance_km") or 0]
    boundary_names = ["Départ"] + [r["name"] for r in ravitos] + ["Arrivée"]
    segments = []
    for i in range(len(boundaries) - 1):
        start_km = boundaries[i]
        end_km = boundaries[i + 1]
        is_last = i == len(boundaries) - 2
        if is_last:
            segment_entries = [it for it in plan_items_full if start_km <= it["position_km"] <= end_km]
        else:
            segment_entries = [it for it in plan_items_full if start_km <= it["position_km"] < end_km]
        segments.append({
            "start_name": boundary_names[i],
            "end_name": boundary_names[i + 1],
            "start_km": start_km,
            "end_km": end_km,
            "entries": segment_entries
        })

    return templates.TemplateResponse(
        request=request,
        name="plan.html",
        context={
            "race": race,
            "products": dropdown_products,
            "using_all_products": using_all_products,
            "plan_items": plan_items_full,
            "totals": totals,
            "ravitos": ravitos,
            "shopping_list": shopping_list,
            "segments": segments
        }
    )


@app.post("/races/{race_id}/plan/add")
def add_plan_item(
    request: Request,
    race_id: int,
    product_id: int = Form(...),
    position_km: float = Form(None),
    position_min: float = Form(None),
    quantity: float = Form(1)
):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()
    race = get_owned_race(user_id, race_id)
    if not race:
        return RedirectResponse(url="/races/view", status_code=303)
    if not supabase.table("products").select("id").eq("id", product_id).execute().data:
        return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)

    pace = None
    if race.get("distance_km") and race.get("duration_target_min"):
        pace = race["duration_target_min"] / race["distance_km"]

    if position_km is not None and position_min is None and pace:
        position_min = position_km * pace
    if position_min is not None and position_km is None and pace:
        position_km = position_min / pace

    plan = get_or_create_plan(race_id, "manuel")
    supabase.table("plan_items").insert({
        "plan_id": plan["id"],
        "product_id": product_id,
        "position_km": clean_number(position_km, 0, 2000) if position_km is not None else 0,
        "position_min": clean_number(position_min, 0, 10000, default=None) if position_min is not None else None,
        "quantity": clean_number(quantity, 0.1, 50, default=1)
    }).execute()
    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)


@app.post("/races/{race_id}/plan/update/{item_id}")
def update_plan_item_time(request: Request, race_id: int, item_id: int, position_min: float = Form(...)):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()
    if not get_owned_race(user_id, race_id):
        return RedirectResponse(url="/races/view", status_code=303)
    plan = get_or_create_plan(race_id, "manuel")
    value = clean_number(position_min, 0, 10000, default=None)
    if value is not None:
        supabase.table("plan_items").update({"position_min": value}).eq("id", item_id).eq("plan_id", plan["id"]).execute()
    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)


@app.post("/races/{race_id}/plan/remove/{item_id}")
def remove_plan_item(request: Request, race_id: int, item_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()
    if not get_owned_race(user_id, race_id):
        return RedirectResponse(url="/races/view", status_code=303)
    plan = get_or_create_plan(race_id, "manuel")
    supabase.table("plan_items").delete().eq("id", item_id).eq("plan_id", plan["id"]).execute()
    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)


@app.post("/races/{race_id}/plan/generate")
def generate_plan(
    request: Request,
    race_id: int,
    carbs_g_per_h: float = Form(60),
    sodium_mg_per_h: float = Form(500)
):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()

    race = get_owned_race(user_id, race_id)
    if not race:
        return RedirectResponse(url="/races/view", status_code=303)
    plan = get_or_create_plan(race_id, "auto")

    carbs_g_per_h = clean_number(carbs_g_per_h, 0, 200, default=60)

    supabase.table("plan_items").delete().eq("plan_id", plan["id"]).execute()

    all_products = supabase.table("products").select("*").execute().data
    owned = supabase.table("user_products").select("product_id").eq("user_id", user_id).execute().data
    owned_ids = {o["product_id"] for o in owned}
    products = [p for p in all_products if p["id"] in owned_ids] or all_products

    if not products or not race.get("duration_target_min") or not race.get("distance_km"):
        return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)

    interval_min = 30
    n_intakes = max(1, round(race["duration_target_min"] / interval_min))
    distance_step = race["distance_km"] / (n_intakes + 1)
    time_step = race["duration_target_min"] / (n_intakes + 1)
    carbs_target = carbs_g_per_h * (interval_min / 60)

    new_items = []
    for i in range(1, n_intakes + 1):
        best_product = min(products, key=lambda p: abs((p.get("carbs_g") or 0) - carbs_target))
        new_items.append({
            "plan_id": plan["id"],
            "product_id": best_product["id"],
            "position_km": round(distance_step * i, 1),
            "position_min": round(time_step * i, 1),
            "quantity": 1
        })
    supabase.table("plan_items").insert(new_items).execute()

    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)


# ------------------------------------------------------------
# Module Mobilité : bibliothèque d'exercices
# ------------------------------------------------------------
MOBILITY_PAGE_SIZE = 40
MOBILITY_ZONES = [
    "Adducteurs", "Avant-bras et poignets", "Bas du dos et obliques", "Cervicales",
    "Cheville", "Colonne thoracique", "Fessiers et hanches", "Fléchisseurs de hanche",
    "Ischio-jambiers", "Pectoraux et grands dorsaux", "Pieds et tibial antérieur",
    "Quadriceps", "Squat et mobilité globale", "Épaules",
]
MOBILITY_TYPES = {"actif": "Actif", "passif": "Passif"}
MOBILITY_MOMENTS = {
    "reveil": "Réveil",
    "avant_sortie": "Avant sortie",
    "apres_sortie": "Après sortie",
    "soir": "Soir",
    "jour_repos": "Jour de repos",
    "pause_bureau": "Pause bureau",
}
MOBILITY_SPORTS = {"trail": "Trail", "velo": "Vélo"}


@app.get("/mobility/library")
def mobility_library(
    request: Request,
    zone: str = "",
    kind: str = "",
    moment: str = "",
    sport: str = "",
    q: str = "",
    page: int = 1,
):
    user_id = current_user_id(request)
    if not user_id:
        return to_login()

    page = max(1, page)
    query = supabase.table("mobility_exercises").select("*", count="exact")
    if zone:
        query = query.eq("zone", zone)
    if kind:
        query = query.eq("type", kind)
    if moment:
        query = query.contains("moments", [moment])
    if sport:
        query = query.contains("sports", [sport])
    if q:
        query = query.ilike("name", f"%{q}%")

    offset = (page - 1) * MOBILITY_PAGE_SIZE
    response = (
        query.order("zone")
        .order("name")
        .range(offset, offset + MOBILITY_PAGE_SIZE - 1)
        .execute()
    )
    total = response.count or 0
    pages = max(1, -(-total // MOBILITY_PAGE_SIZE))

    filters = {"zone": zone, "kind": kind, "moment": moment, "sport": sport, "q": q}
    qs = urlencode({k: v for k, v in filters.items() if v})

    return templates.TemplateResponse(
        request=request,
        name="mobility_library.html",
        context={
            "exercises": response.data,
            "total": total,
            "page": page,
            "pages": pages,
            "filters": filters,
            "qs": qs,
            "zones": MOBILITY_ZONES,
            "types": MOBILITY_TYPES,
            "moments": MOBILITY_MOMENTS,
            "sports": MOBILITY_SPORTS,
        },
    )