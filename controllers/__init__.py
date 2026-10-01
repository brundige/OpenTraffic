from .base import MAX_CHANNEL, Controller
from .luxcom import LuxcomEMHDLC
from .simulator import SimulatorController

CONTROLLER_KINDS = ("simulator", "luxcom")


def make_controller(settings) -> Controller:

    if settings.controller == "simulator":
        return SimulatorController()

    if settings.controller == "luxcom":

        if not settings.controller_host:
            raise ValueError(
                "controller 'luxcom' needs controller_host "
                "(the EM-HDLC's address)"
            )

        return LuxcomEMHDLC(
            settings.controller_host,
            port=settings.controller_port,
            listen_port=settings.controller_listen_port,
            forward_port=settings.controller_forward_port,
            timeout=settings.controller_timeout,
        )

    raise ValueError(
        f"Unknown controller {settings.controller!r}, "
        f"expected one of {', '.join(CONTROLLER_KINDS)}"
    )


__all__ = [
    "CONTROLLER_KINDS",
    "MAX_CHANNEL",
    "Controller",
    "LuxcomEMHDLC",
    "SimulatorController",
    "make_controller",
]
