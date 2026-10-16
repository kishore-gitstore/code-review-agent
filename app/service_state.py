"""
app/service_state.py — the review service's on/off switch.

"Stopped" here means the agent stops *accepting* new reviews (webhooks are
acknowledged and ignored, manual triggers are refused with 503). Reviews that
are already running are left alone to finish — cancel them individually from
the dashboard if they should stop too. The web UI itself stays up, because an
admin who stopped the service must still be able to sign in and start it again;
a switch that kills the process hosting its own "on" button is a one-way door.

State is in memory: a process restart comes back up running.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ServiceState:
    running: bool = True
    changed_by: str = ""
    changed_at: str = ""

    def set(self, running: bool, by: str) -> None:
        self.running = running
        self.changed_by = by
        self.changed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def as_dict(self) -> dict:
        return {
            "state": "running" if self.running else "stopped",
            "changed_by": self.changed_by,
            "changed_at": self.changed_at,
        }


service = ServiceState()
