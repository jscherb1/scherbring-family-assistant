"""Meal Planner section: browse and view recipes (read-only)."""
import sqlite3

from fastapi import APIRouter, HTTPException, Request

from ..data import activity, recipes
from ..web import render, templates

router = APIRouter()


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


@router.get("/meals/{recipe_id}", include_in_schema=False)
def recipe_detail(request: Request, recipe_id: str):
    try:
        recipe = recipes.get_recipe(recipe_id)
    except (FileNotFoundError, sqlite3.OperationalError):
        recipe = None
    if recipe is None:
        return render(request, "meals/not_found.html", "meals", "recipes", "Recipe not found",
                      status_code=404)
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
