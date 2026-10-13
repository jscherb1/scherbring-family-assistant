"""Meal Planner section: browse recipes, and add, edit and delete them.

Writes go through the shared service in scripts/recipes.py (via data/recipes.py), the same code
the `meal-planner` subagent's CLI uses. This router only does web plumbing. Writes are guarded by
the same-origin check the ask box uses; access control is the network (Tailscale-only bind).
"""
import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..data import activity, common, recipes
from ..web import render, templates
from .ask import _same_origin

router = APIRouter()
service = recipes.service


def _blocked(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Blocked: the request didn't come from this site."}, status_code=403)
    return None


def _facets() -> dict:
    return recipes.list_recipes().get("facets", {})


def _form_page(request: Request, *, heading: str, action: str, values: dict, error: str | None = None,
               cancel: str = "/meals", status_code: int = 200):
    return render(request, "meals/form.html", "meals", "recipes", heading, status_code=status_code,
                  heading=heading, action=action, v=values, error=error, cancel=cancel,
                  facets=_facets())


def _not_found(request: Request):
    return render(request, "meals/not_found.html", "meals", "recipes", "Recipe not found", status_code=404)


@router.get("/meals", include_in_schema=False)
def recipe_list(request: Request, q: str = "", meal_type: str = "", protein: str = "",
                tag: str = "", sort: str = "title"):
    data = recipes.list_recipes(q, meal_type, protein, tag, sort)
    filters = {"q": q, "meal_type": meal_type, "protein": protein, "tag": tag, "sort": sort}
    return render(request, "meals/list.html", "meals", "recipes", "Recipes",
                  data=data, filters=filters)


@router.get("/meals/results", include_in_schema=False)
def recipe_results(request: Request, q: str = "", meal_type: str = "", protein: str = "",
                   tag: str = "", sort: str = "title"):
    """htmx partial: just the result grid for the current filters."""
    data = recipes.list_recipes(q, meal_type, protein, tag, sort)
    return templates.TemplateResponse(request, "meals/_results.html", {"data": data})


@router.get("/meals/plan", include_in_schema=False)
def plan(request: Request):
    return render(request, "meals/plan.html", "meals", "plan", "Menu & Insights",
                  data=activity.get_meal_insights())


@router.get("/api/meals/plan")
def api_plan():
    return activity.get_meal_insights()


@router.get("/meals/new", include_in_schema=False)
def recipe_new(request: Request):
    return _form_page(request, heading="Add a recipe", action="/meals", values=recipes.form_values())


@router.post("/meals", include_in_schema=False)
async def recipe_create(request: Request):
    if (resp := _blocked(request)):
        return resp
    form = dict(await request.form())
    try:
        created = service.add_recipe(db_path=common.db_path(), **recipes.form_to_fields(form))
    except service.RecipeError as exc:
        return _form_page(request, heading="Add a recipe", action="/meals", values=form,
                          error=exc.message, status_code=400)
    return RedirectResponse(f"/meals/{created['id']}", status_code=303)


@router.get("/meals/{recipe_id}/edit", include_in_schema=False)
def recipe_edit(request: Request, recipe_id: str):
    try:
        recipe = recipes.get_recipe(recipe_id)
    except (FileNotFoundError, sqlite3.OperationalError):
        recipe = None
    if recipe is None:
        return _not_found(request)
    return _form_page(request, heading=f"Edit {recipe['title']}", action=f"/meals/{recipe_id}/update",
                      values=recipes.form_values(recipe), cancel=f"/meals/{recipe_id}")


@router.post("/meals/{recipe_id}/update", include_in_schema=False)
async def recipe_update(request: Request, recipe_id: str):
    if (resp := _blocked(request)):
        return resp
    form = dict(await request.form())
    try:
        service.update_recipe(recipe_id, db_path=common.db_path(), **recipes.form_to_fields(form))
    except service.RecipeError as exc:
        if exc.code == 1:
            return _not_found(request)
        return _form_page(request, heading="Edit recipe", action=f"/meals/{recipe_id}/update",
                          values=form, error=exc.message, cancel=f"/meals/{recipe_id}", status_code=400)
    return RedirectResponse(f"/meals/{recipe_id}", status_code=303)


@router.post("/meals/{recipe_id}/delete", include_in_schema=False)
def recipe_delete(request: Request, recipe_id: str):
    if (resp := _blocked(request)):
        return resp
    try:
        service.delete_recipe(recipe_id, db_path=common.db_path())
    except service.RecipeError:
        return _not_found(request)
    return RedirectResponse("/meals", status_code=303)


@router.get("/meals/{recipe_id}", include_in_schema=False)
def recipe_detail(request: Request, recipe_id: str):
    try:
        recipe = recipes.get_recipe(recipe_id)
    except (FileNotFoundError, sqlite3.OperationalError):
        recipe = None
    if recipe is None:
        return _not_found(request)
    return render(request, "meals/detail.html", "meals", "recipes", recipe["title"],
                  recipe=recipe)


@router.get("/api/recipes")
def api_recipes(q: str = "", meal_type: str = "", protein: str = "", tag: str = "",
                sort: str = "title"):
    return recipes.list_recipes(q, meal_type, protein, tag, sort)


@router.get("/api/recipes/{recipe_id}")
def api_recipe(recipe_id: str):
    try:
        recipe = recipes.get_recipe(recipe_id)
    except (FileNotFoundError, sqlite3.OperationalError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if recipe is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    return recipe
