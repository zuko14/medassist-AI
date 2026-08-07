"""Connectors package initialization.

Registers default laboratory connectors into ConnectorRegistry.
"""

from app.integrations.callmedex.connectors.factory import (
    ConnectorRegistry,
    ConnectorFactory,
)
from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.connectors.crelio.connector import CrelioConnector
from app.integrations.callmedex.connectors.cloudlims.connector import CloudLIMSConnector

# Register default EMR connectors
ConnectorRegistry.register("mocdoc", MocDocConnector)
ConnectorRegistry.register("crelio", CrelioConnector)
ConnectorRegistry.register("cloudlims", CloudLIMSConnector)

__all__ = [
    "ConnectorRegistry",
    "ConnectorFactory",
    "MocDocConnector",
    "CrelioConnector",
    "CloudLIMSConnector",
]
