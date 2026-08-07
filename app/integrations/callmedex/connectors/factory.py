"""Connector Registry and Factory Pattern implementation for dynamic EMR connector resolution.

Enables both Hospital Bot and CallMedEx to instantiate any supported laboratory connector
(MocDoc, Crelio, CloudLIMS, etc.) dynamically by string type without hardcoding concrete classes.
"""

import logging
from typing import Dict, Type, Optional, Any
from app.integrations.callmedex.connectors.base.connector import BaseLaboratoryConnector
from app.integrations.callmedex.api.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


class ConnectorRegistry:
    """Thread-safe registry mapping connector type keys to concrete connector classes."""

    _registry: Dict[str, Type[BaseLaboratoryConnector]] = {}

    @classmethod
    def register(cls, connector_type: str, connector_cls: Type[BaseLaboratoryConnector]) -> None:
        """Register a concrete connector class under a normalized string key."""
        key = connector_type.lower().strip()
        cls._registry[key] = connector_cls
        logger.info(f"Registered EMR connector driver '{key}' -> {connector_cls.__name__}")

    @classmethod
    def get(cls, connector_type: str) -> Type[BaseLaboratoryConnector]:
        """Retrieve the registered connector class for a given type key."""
        key = connector_type.lower().strip()
        if key not in cls._registry:
            supported = list(cls._registry.keys())
            raise ConfigurationError(
                f"Unsupported connector type '{connector_type}'. Supported connectors: {supported}"
            )
        return cls._registry[key]

    @classmethod
    def is_supported(cls, connector_type: str) -> bool:
        """Return True if connector_type is registered."""
        return connector_type.lower().strip() in cls._registry

    @classmethod
    def list_supported(cls) -> list[str]:
        """Return list of supported connector type keys."""
        return list(cls._registry.keys())


class ConnectorFactory:
    """Factory method creating configured connector instances."""

    @staticmethod
    def create(
        connector_type: str,
        selector_provider: Optional[Any] = None,
        browser_session: Optional[Any] = None,
        **kwargs: Any,
    ) -> BaseLaboratoryConnector:
        """Instantiate the registered connector for connector_type."""
        connector_cls = ConnectorRegistry.get(connector_type)
        return connector_cls(
            selector_provider=selector_provider,
            browser_session=browser_session,
            **kwargs,
        )
