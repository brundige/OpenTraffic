from .base import MAX_CHANNEL, Controller
from .discovery import find_adapters
from .luxcom import LuxcomEMHDLC
from .network import add_address
from .simulator import SimulatorController
from .slot import ControllerSlot

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

        if settings.controller_local_address:
            # The address the adapter sends to; addresses do not
            # survive a reboot unless re-added, so add it every start.
            add_address(settings.controller_interface, settings.controller_local_address)

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
    "ControllerSlot",
    "LuxcomEMHDLC",
    "SimulatorController",
    "find_adapters",
    "make_controller",
]
