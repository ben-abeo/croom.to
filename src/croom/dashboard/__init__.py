"""
Dashboard client module for Croom.

Provides communication between Croom devices and the management dashboard:
- WebSocket real-time connection
- Metrics reporting
- Remote command execution
- Configuration sync
"""

from croom.dashboard.client import DashboardClient
from croom.dashboard.metrics import MetricsCollector, DeviceMetrics

__all__ = [
    "DashboardClient",
    "MetricsCollector",
    "DeviceMetrics",
]
