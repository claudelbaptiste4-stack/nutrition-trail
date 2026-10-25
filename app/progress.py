"""Page Progression : évolution du score, comparaison par zone, régularité des séances.

Ce fichier se place dans app/progress.py. Il est branché sur l'application
principale avec deux lignes dans app/main.py (déjà présentes dans le nouveau main.py).
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import supabase
from app.mobility import (
    PROGRAMS,
    duration_label,
    format_date,
    load_catalogue,
    parse_iso,
    retest_info,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

STABLE_BAND = 3        # en dessous de 3 points d'écart, on parle de « stable » (une mesure faite seul varie)
MAX_POINTS = 12        # nombre de tests affichés sur la courbe
WEEKS = 8              # nombre de semaines affichées pour la régularité
RECENT_SESSIONS = 5

# Géométrie de la courbe (en unités du dessin SVG)
CHART_W, CHART_H = 320, 140
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 30, 12, 12, 26


def current_user_id(request: Request):
    return request.session.get("user_id")


def trend_of(delta):
    if delta is None:
        return "none"
    if abs(delta) < STABLE_BAND:
        return "stable"
    return "up" if delta > 0 else "down"


def short_date(iso_text):
    """'2026-10-15T...' -> '15/10'."""
    full = format_date(iso_text)
    return full[:5] if full else ""


def build_chart(assessments):
    """Calcule les points de la courbe du score moyen (dessin SVG fait dans le template)."""
    plot_w = CHART_W - PAD_LEFT - PAD_RIGHT
    plot_h = CHART_H - PAD_TOP - PAD_BOTTOM
    shown = assessments[-MAX_POINTS:]
    points = []
    for n, a in enumerate(shown):
        score = float(a.get("global_score") or 0)
        x = PAD_LEFT + (plot_w / 2 if len(shown) == 1 else plot_w * n / (len(shown) - 1))
        y = PAD_TOP + (1 - score / 100) * plot_h
        points.append({"x": round(x, 1), "y": round(y, 1), "score": round(score), "label": short_date(a["taken_at"])})
    grid = [
        {"value": v, "y": round(PAD_TOP + (1 - v / 100) * plot_h, 1)}
        for v in (0, 50, 100)
    ]
    polyline = " ".join(f"{p['x']},{p['y']}" for p in points)
    return {"points": points, "grid": grid, "polyline": polyline, "left": PAD_LEFT, "right": CHART_W - PAD_RIGHT,
            "label_y": CHART_H - 8, "width": CHART_W, "height": CHART_H}


@router.get("/mobility/progress")
def mobility_progress(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    # ---------- Tests (du plus ancien au plus récent)
    assessments = (
        supabase.table("mobility_assessments")
        .select("*")
        .eq("user_id", user_id)
        .order("taken_at")
        .limit(200)
        .execute()
        .data
    )

    latest = assessments[-1] if assessments else None
    global_delta = None
    if len(assessments) >= 2:
        global_delta = round(float(latest["global_score"] or 0) - float(assessments[-2]["global_score"] or 0), 1)

    zones = []
    if len(assessments) >= 2:
        first = assessments[0]
        rows = (
            supabase.table("mobility_assessment_results")
            .select("*")
            .in_("assessment_id", [first["id"], latest["id"]])
            .execute()
            .data
        )
        by_key = {(r["assessment_id"], r["test_code"]): r.get("zone_score") for r in rows}
        for t in load_catalogue()["tests"]:
            before = by_key.get((first["id"], t["code"]))
            after = by_key.get((latest["id"], t["code"]))
            delta = round(float(after) - float(before), 1) if before is not None and after is not None else None
            zones.append({
                "name": t["zone"],
                "before": round(float(before)) if before is not None else None,
                "after": round(float(after)) if after is not None else None,
                "delta": delta,
                "trend": trend_of(delta),
            })

    # ---------- Séances
    sessions = (
        supabase.table("mobility_sessions")
        .select("*")
        .eq("user_id", user_id)
        .order("started_at", desc=True)
        .limit(200)
        .execute()
        .data
    )
    today = datetime.now(timezone.utc).date()
    dated = []
    for s in sessions:
        when = parse_iso(s.get("started_at"))
        if when is not None:
            dated.append((when.date(), s))

    last7 = sum(1 for d, _ in dated if 0 <= (today - d).days < 7)
    last30 = sum(1 for d, _ in dated if 0 <= (today - d).days < 30)

    monday = today - timedelta(days=today.weekday())
    weeks = []
    for back in range(WEEKS - 1, -1, -1):
        start = monday - timedelta(days=7 * back)
        count = sum(1 for d, _ in dated if start <= d < start + timedelta(days=7))
        weeks.append({"label": start.strftime("%d/%m"), "count": count})
    peak = max((w["count"] for w in weeks), default=0)
    for w in weeks:
        w["height"] = round(w["count"] / peak * 100) if peak else 0

    titles = {p["slug"]: p["title"] for p in PROGRAMS}
    custom_ids = [s["program_id"] for _, s in dated[:RECENT_SESSIONS] if s.get("program_id")]
    custom_names = {}
    if custom_ids:
        rows = supabase.table("mobility_programs").select("id, name").in_("id", custom_ids).execute().data
        custom_names = {r["id"]: r["name"] for r in rows}
    recent = []
    for d, s in dated[:RECENT_SESSIONS]:
        if s.get("program_id") and s["program_id"] in custom_names:
            title = custom_names[s["program_id"]]
        else:
            title = titles.get(s.get("moment"), "Programme personnalisé" if s.get("moment") == "perso" else "Séance")
        recent.append({
            "title": title,
            "date": d.strftime("%d/%m/%Y"),
            "duration": duration_label(s["planned_seconds"]) if s.get("planned_seconds") else "",
        })

    return templates.TemplateResponse(
        request=request,
        name="mobility_progress.html",
        context={
            "assessments_count": len(assessments),
            "latest": latest,
            "latest_date": format_date(latest["taken_at"]) if latest else "",
            "global_delta": global_delta,
            "global_trend": trend_of(global_delta),
            "chart": build_chart(assessments) if assessments else None,
            "zones": zones,
            "retest": retest_info(latest["taken_at"]) if latest else None,
            "last7": last7,
            "last30": last30,
            "total_sessions": len(dated),
            "weeks": weeks,
            "recent": recent,
            "stable_band": STABLE_BAND,
        },
    )