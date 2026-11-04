"""CLI entry point: ``agent-landscape`` or ``python -m agent_landscape``."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .config import Settings
from .web.app import create_app


class _Server(uvicorn.Server):
    """Persists state on Ctrl+C / SIGTERM *before* uvicorn closes connections and cancels running work –
    otherwise running missions would be saved as failed instead of 'interrupted'."""

    def __init__(self, config: uvicorn.Config, app) -> None:  # noqa: ANN001
        super().__init__(config)
        self._app = app

    def handle_exit(self, sig: int, frame) -> None:  # noqa: ANN001
        try:
            self._app.state.runtime.prepare_shutdown()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("Could not persist state on shutdown")
        super().handle_exit(sig, frame)


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Landscape – A2A orchestration + strategy map UI")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--mock", action="store_true", help="force mock LLM mode")
    parser.add_argument("--db", default=None, help="SQLite file for persistence, or 'off' (default: DB_PATH or "
                        "data/landscape.db; mock mode: data/landscape-mock.db)")
    args = parser.parse_args()

    overrides: dict = {}
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    if args.mock:
        overrides["mock"] = True
    if args.db is not None:
        overrides["db_path"] = args.db
    settings = Settings.from_env(**overrides)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    mode = "MOCK (LiteLLM mock_response)" if settings.mock else f"LIVE model={settings.default_model}" + (
        f" via proxy {settings.proxy_url}" if settings.proxy_url else "")
    print(f"\n  ⬢ Agent Landscape  →  http://{settings.host}:{settings.port}\n"
          f"    LLM: {mode}\n    A2A commander card: {settings.public_url}/.well-known/agent-card.json\n"
          f"    Database: {settings.db_path or 'off (in-memory only)'}\n")
    app = create_app(settings)
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning",
                            timeout_graceful_shutdown=3)
    _Server(config, app).run()


if __name__ == "__main__":
    main()
