import os
import csv
import io
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from app.database import supabase
import gpxpy

app = FastAPI(title="Nutrition Trail")
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET"))
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


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


@app.get("/")
def health_check(request: Request):
    if current_user_id(request):
        return RedirectResponse(url="/races/view", status_code=303)
    return RedirectResponse(url="/login", status_code=303)

@app.get("/register")
def register_form(request: Request):
    return templates.TemplateResponse(request=request, name="register.html", context={})

@app.post("/register")
def register(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        supabase.auth.sign_up({"email": email, "password": password})
    except Exception as e:
        return templates.TemplateResponse(
            request=request, name="register.html", context={"error": str(e)}
        )
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
        result = supabase.auth.sign_in_with_password({"email": email, "password": password})
    except Exception:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Email ou mot de passe incorrect"}
        )
    request.session["user_id"] = result.user.id
    request.session["email"] = result.user.email
    return RedirectResponse(url="/races/view", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

@app.get("/products")
def get_products():
    response = supabase.table("products").select("*").execute()
    return response.data

@app.get("/products/view")
def view_products(request: Request):
    response = supabase.table("products").select("*").execute()
    return templates.TemplateResponse(
        request=request,
        name="products.html",
        context={"products": response.data}
    )

@app.post("/products/add")
def add_product(
    name: str = Form(...),
    type: str = Form(...),
    carbs_g: float = Form(0),
    sodium_mg: float = Form(0),
    caffeine_mg: float = Form(0)
):
    supabase.table("products").insert({
        "name": name,
        "type": type,
        "carbs_g": carbs_g,
        "sodium_mg": sodium_mg,
        "caffeine_mg": caffeine_mg
    }).execute()
    return RedirectResponse(url="/products/view", status_code=303)

@app.get("/products/import")
def import_products_form(request: Request):
    return templates.TemplateResponse(request=request, name="import_products.html", context={})

@app.post("/products/import")
async def import_products(csv_file: UploadFile = File(...)):
    content = await csv_file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    count = 0
    for row in reader:
        supabase.table("products").insert({
            "name": row.get("name"),
            "brand": row.get("brand") or None,
            "type": row.get("type"),
            "carbs_g": float(row["carbs_g"]) if row.get("carbs_g") else 0,
            "sodium_mg": float(row["sodium_mg"]) if row.get("sodium_mg") else 0,
            "caffeine_mg": float(row["caffeine_mg"]) if row.get("caffeine_mg") else 0,
            "weight_g": float(row["weight_g"]) if row.get("weight_g") else None,
            "volume_ml": float(row["volume_ml"]) if row.get("volume_ml") else None
        }).execute()
        count += 1

    return RedirectResponse(url="/products/view", status_code=303)
    
@app.get("/races/view")
def view_races(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
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
        return RedirectResponse(url="/login", status_code=303)

    gpx_data = None
    if gpx_file and gpx_file.filename:
        content = await gpx_file.read()
        gpx_data = content.decode("utf-8")
        gpx = gpxpy.parse(gpx_data)
        distance_km = gpx.length_3d() / 1000
        uphill, _ = gpx.get_uphill_downhill()
        elevation_gain_m = uphill

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
        return RedirectResponse(url="/login", status_code=303)
    supabase.table("races").delete().eq("id", race_id).eq("user_id", user_id).execute()
    return RedirectResponse(url="/races/view", status_code=303)

@app.get("/races/{race_id}")
def race_detail(request: Request, race_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    response = supabase.table("races").select("*").eq("id", race_id).eq("user_id", user_id).execute()
    if not response.data:
        return RedirectResponse(url="/races/view", status_code=303)
    race = response.data[0]

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

@app.post("/races/{race_id}/ravitos/add")
def add_ravito(race_id: int, name: str = Form(...), position_km: float = Form(...)):
    supabase.table("ravitos").insert({
        "race_id": race_id,
        "name": name,
        "position_km": position_km
    }).execute()
    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)

@app.post("/races/{race_id}/ravitos/remove/{ravito_id}")
def remove_ravito(race_id: int, ravito_id: int):
    supabase.table("ravitos").delete().eq("id", ravito_id).execute()
    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)

@app.get("/races/{race_id}/plan")
def view_plan(request: Request, race_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    race_response = supabase.table("races").select("*").eq("id", race_id).eq("user_id", user_id).execute()
    if not race_response.data:
        return RedirectResponse(url="/races/view", status_code=303)
    race = race_response.data[0]

    plan_response = supabase.table("plans").select("*").eq("race_id", race_id).execute()
    if plan_response.data:
        plan = plan_response.data[0]
    else:
        insert_response = supabase.table("plans").insert({"race_id": race_id, "mode": "manuel"}).execute()
        plan = insert_response.data[0]

    items = supabase.table("plan_items").select("*").eq("plan_id", plan["id"]).order("position_km").execute().data
    products = supabase.table("products").select("*").execute().data
    products_by_id = {p["id"]: p for p in products}
    ravitos = supabase.table("ravitos").select("*").eq("race_id", race_id).order("position_km").execute().data

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
            "products": products,
            "plan_items": plan_items_full,
            "totals": totals,
            "ravitos": ravitos,
            "shopping_list": shopping_list,
            "segments": segments
        }
    )

@app.post("/races/{race_id}/plan/add")
def add_plan_item(
    race_id: int,
    product_id: int = Form(...),
    position_km: float = Form(None),
    position_min: float = Form(None),
    quantity: float = Form(1)
):
    race = supabase.table("races").select("*").eq("id", race_id).execute().data[0]
    pace = None
    if race.get("distance_km") and race.get("duration_target_min"):
        pace = race["duration_target_min"] / race["distance_km"]

    if position_km is not None and position_min is None and pace:
        position_min = position_km * pace
    if position_min is not None and position_km is None and pace:
        position_km = position_min / pace

    plan = supabase.table("plans").select("*").eq("race_id", race_id).execute().data[0]
    supabase.table("plan_items").insert({
        "plan_id": plan["id"],
        "product_id": product_id,
        "position_km": position_km or 0,
        "position_min": position_min,
        "quantity": quantity
    }).execute()
    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)

@app.post("/races/{race_id}/plan/update/{item_id}")
def update_plan_item_time(race_id: int, item_id: int, position_min: float = Form(...)):
    supabase.table("plan_items").update({"position_min": position_min}).eq("id", item_id).execute()
    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)

@app.post("/races/{race_id}/plan/remove/{item_id}")
def remove_plan_item(race_id: int, item_id: int):
    supabase.table("plan_items").delete().eq("id", item_id).execute()
    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)

@app.post("/races/{race_id}/plan/generate")
def generate_plan(
    race_id: int,
    carbs_g_per_h: float = Form(60),
    sodium_mg_per_h: float = Form(500)
):
    race = supabase.table("races").select("*").eq("id", race_id).execute().data[0]
    plan_response = supabase.table("plans").select("*").eq("race_id", race_id).execute()
    if plan_response.data:
        plan = plan_response.data[0]
    else:
        plan = supabase.table("plans").insert({"race_id": race_id, "mode": "auto"}).execute().data[0]

    supabase.table("plan_items").delete().eq("plan_id", plan["id"]).execute()

    products = supabase.table("products").select("*").execute().data
    if not products or not race.get("duration_target_min") or not race.get("distance_km"):
        return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)

    interval_min = 30
    n_intakes = max(1, round(race["duration_target_min"] / interval_min))
    distance_step = race["distance_km"] / (n_intakes + 1)
    time_step = race["duration_target_min"] / (n_intakes + 1)
    carbs_target = carbs_g_per_h * (interval_min / 60)

    for i in range(1, n_intakes + 1):
        best_product = min(products, key=lambda p: abs((p.get("carbs_g") or 0) - carbs_target))
        supabase.table("plan_items").insert({
            "plan_id": plan["id"],
            "product_id": best_product["id"],
            "position_km": round(distance_step * i, 1),
            "position_min": round(time_step * i, 1),
            "quantity": 1
        }).execute()

    return RedirectResponse(url=f"/races/{race_id}/plan", status_code=303)