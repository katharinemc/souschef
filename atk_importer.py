"""
atk_importer.py

ATK recipe import via Chrome MCP.
NOT IMPLEMENTED IN PHASE 1 — stub only.

Full specification in prd_v1_5.md section 8.

Phase 1 usage: this module is imported by reply_handler when an import_atk
intent is parsed. The handler catches NotImplementedError and logs a message.
"""

import logging

log = logging.getLogger(__name__)


def find_experiment_candidates(recipe_library: dict, count: int = 3) -> list[dict]:
    """
    Browse ATK using Chrome MCP and return experiment candidates.

    Args:
        recipe_library: Existing recipes dict {id: recipe_dict}.
        count:          Number of candidates to surface.

    Returns:
        List of candidate dicts with keys: name, atk_url, description, cook_time, servings.

    NOT IMPLEMENTED IN PHASE 1 — requires Chrome MCP integration.
    """
    raise NotImplementedError(
        "ATK import requires Chrome MCP — Phase 1 stub only. "
        "See prd_v1_5.md section 8 for the full specification."
    )


def import_recipe(atk_url: str, output_dir: str) -> dict:
    """
    Import a single ATK recipe by URL.

    Args:
        atk_url:    Full URL to the ATK recipe page.
        output_dir: Directory to write the YAML file to (e.g. 'recipes_yaml/').

    Returns:
        The imported recipe dict (also written to output_dir as a YAML file).

    NOT IMPLEMENTED IN PHASE 1 — requires Chrome MCP integration.
    """
    raise NotImplementedError(
        "ATK import requires Chrome MCP — Phase 1 stub only. "
        "See prd_v1_5.md section 8 for the full specification."
    )
