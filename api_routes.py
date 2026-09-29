#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Small compatibility API blueprint.

The legacy external Firecrawl configuration endpoints are intentionally kept
registered so old frontend calls receive a clear disabled response instead of a
404 or, worse, mutating runtime state.
"""

from flask import Blueprint, jsonify

from decorators import login_required


api_bp = Blueprint("api", __name__, url_prefix="/api")


def _legacy_firecrawl_disabled_response():
    return jsonify({
        "success": False,
        "configured": False,
        "disabled": True,
        "api_key": "",
        "api_url": "",
        "message": "Legacy Firecrawl API is disabled. Use /api/start-article-crawl.",
        "replacement": "/api/start-article-crawl",
    }), 410


@api_bp.route("/save-api-key", methods=["POST"])
@login_required
def save_api_key():
    """Legacy Firecrawl config endpoint; intentionally disabled."""
    return _legacy_firecrawl_disabled_response()


@api_bp.route("/get-api-key")
@login_required
def get_api_key():
    """Legacy Firecrawl config endpoint; intentionally disabled."""
    return _legacy_firecrawl_disabled_response()
