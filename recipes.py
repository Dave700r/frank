"""Recipe management."""
import sqlite3
import logging

import config

log = logging.getLogger("family-bot.recipes")

RECIPE_DB = config.RECIPE_DB


def _get_conn():
    conn = sqlite3.connect(RECIPE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def search_recipes(query):
    """Search recipes by name, description, cuisine, or notes."""
    conn = _get_conn()
    # Use FTS5 for full-text search
    rows = conn.execute(
        "SELECT r.* FROM recipes r "
        "JOIN recipes_fts ON r.id = recipes_fts.rowid "
        "WHERE recipes_fts MATCH ? ORDER BY rank",
        (query,),
    ).fetchall()
    conn.close()
    return rows


def get_recipe(recipe_id):
    """Get full recipe with ingredients and instructions."""
    conn = _get_conn()
    recipe = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if not recipe:
        conn.close()
        return None

    ingredients = conn.execute(
        "SELECT * FROM ingredients WHERE recipe_id=? ORDER BY step_order",
        (recipe_id,),
    ).fetchall()

    instructions = conn.execute(
        "SELECT * FROM instructions WHERE recipe_id=? ORDER BY step_number",
        (recipe_id,),
    ).fetchall()

    tags = conn.execute(
        "SELECT tag FROM tags WHERE recipe_id=?", (recipe_id,),
    ).fetchall()

    conn.close()
    return {
        "recipe": dict(recipe),
        "ingredients": [dict(i) for i in ingredients],
        "instructions": [dict(i) for i in instructions],
        "tags": [t["tag"] for t in tags],
    }


def list_recipes():
    """List all recipes."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, name, cuisine, prep_time, cook_time, source FROM recipes ORDER BY name"
    ).fetchall()
    conn.close()
    return rows


def add_recipe(name, description=None, cuisine=None, prep_time=None,
               cook_time=None, servings=None, source=None, notes=None,
               ingredients=None, instructions=None, tags=None):
    """Add a new recipe."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO recipes (name, description, cuisine, prep_time, cook_time, servings, source, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, description, cuisine, prep_time, cook_time, servings, source, notes),
    )
    recipe_id = cur.lastrowid

    if ingredients:
        for i, ing in enumerate(ingredients):
            conn.execute(
                "INSERT INTO ingredients (recipe_id, step_order, amount, unit, name, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (recipe_id, i, ing.get("amount"), ing.get("unit"),
                 ing.get("name", ""), ing.get("notes")),
            )

    if instructions:
        for i, inst in enumerate(instructions):
            text = inst if isinstance(inst, str) else inst.get("instruction", "")
            conn.execute(
                "INSERT INTO instructions (recipe_id, step_number, instruction) VALUES (?, ?, ?)",
                (recipe_id, i + 1, text),
            )

    if tags:
        for tag in tags:
            conn.execute(
                "INSERT INTO tags (recipe_id, tag) VALUES (?, ?)",
                (recipe_id, tag),
            )

    # Update FTS index
    conn.execute(
        "INSERT INTO recipes_fts(rowid, name, description, cuisine, notes) VALUES (?, ?, ?, ?, ?)",
        (recipe_id, name, description, cuisine, notes),
    )

    conn.commit()
    conn.close()
    log.info(f"Recipe added: {name} (id={recipe_id})")
    return recipe_id


def format_recipe(recipe_data):
    """Format a recipe for display in Telegram."""
    r = recipe_data["recipe"]
    lines = [f"{r['name']}"]
    if r.get("description"):
        lines.append(f"{r['description']}")
    lines.append("")

    meta = []
    if r.get("cuisine"):
        meta.append(f"Cuisine: {r['cuisine']}")
    if r.get("prep_time"):
        meta.append(f"Prep: {r['prep_time']} min")
    if r.get("cook_time"):
        meta.append(f"Cook: {r['cook_time']} min")
    if r.get("servings"):
        meta.append(f"Serves: {r['servings']}")
    if meta:
        lines.append(" | ".join(meta))
        lines.append("")

    if recipe_data["ingredients"]:
        lines.append("INGREDIENTS:")
        for ing in recipe_data["ingredients"]:
            parts = []
            if ing.get("amount"):
                parts.append(ing["amount"])
            if ing.get("unit"):
                parts.append(ing["unit"])
            parts.append(ing["name"])
            line = "  - " + " ".join(parts)
            if ing.get("notes"):
                line += f" ({ing['notes']})"
            lines.append(line)
        lines.append("")

    if recipe_data["instructions"]:
        lines.append("INSTRUCTIONS:")
        for inst in recipe_data["instructions"]:
            lines.append(f"  {inst['step_number']}. {inst['instruction']}")
        lines.append("")

    if recipe_data["tags"]:
        lines.append(f"Tags: {', '.join(recipe_data['tags'])}")

    if r.get("source"):
        lines.append(f"Source: {r['source']}")

    return "\n".join(lines)
