"""ASGI application entry point."""

from ai_agent.api import create_app
from ai_agent.config import Settings
from ai_agent.observability.logging import configure_logging

settings = Settings()
settings.validate_runtime()
configure_logging(settings.log_level)
app = create_app(settings)
