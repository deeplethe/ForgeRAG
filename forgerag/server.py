"""
Server surface — the FastAPI app factory.

Typical usage:

    from forgerag.server import create_app
    app = create_app()       # uses $FORGERAG_CONFIG / ./forgerag.yaml
    # uvicorn.run(app, host="0.0.0.0", port=8000)

Or construct from an explicit config + state:

    from forgerag.config import load_config
    from api.state import AppState      # internal, will be exposed later
    cfg = load_config("myconfig.yaml")
    state = AppState(cfg)
    app = create_app(cfg=cfg, state=state)
"""

from api.app import create_app

__all__ = ["create_app"]
