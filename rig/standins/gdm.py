"""Stands in for GDM, whose presence is how GNOME Shell decides it can lock at all."""
import asyncio

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.constants import PropertyAccess
from dbus_fast.service import ServiceInterface, dbus_property


class Manager(ServiceInterface):
    def __init__(self):
        super().__init__("org.gnome.DisplayManager.Manager")

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "s":
        return "50.0"


async def main():
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    bus.export("/org/gnome/DisplayManager/Manager", Manager())
    await bus.request_name("org.gnome.DisplayManager")
    await asyncio.Event().wait()


asyncio.run(main())
