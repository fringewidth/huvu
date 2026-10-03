"""`uv run huvu` starts the backend with huvu.toml (or $HUVU_CONFIG)."""

from __future__ import annotations

import logging

import uvicorn

from . import config as config_module
from .http import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = config_module.load()
    uvicorn.run(create_app(config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
