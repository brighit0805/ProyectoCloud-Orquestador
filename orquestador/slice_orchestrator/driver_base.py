"""
driver_base.py
--------------
Contrato común de los drivers de plataforma. El orquestador solo depende de
estos métodos, de modo que ``DriverLinux`` y ``DriverOpenStack`` son
intercambiables (R3: "mapear el diseño del orquestador del driver Linux al
driver OpenStack").

``host_uri`` es la "ubicación" opaca de un host: en Linux es la URI de libvirt
(``qemu+ssh://...``) y en OpenStack ``openstack://<zona>[:<host>]``.

Métodos opcionales (los usa el orquestador solo si el driver los tiene):
    preparar_slice(slc)       crea lo propio del slice (p. ej. el proyecto OpenStack)
    liberar_slice(slice_id)   lo libera al eliminar o al revertir un despliegue
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DriverPlataforma(Protocol):
    def crear_red(self, host_uri: str, nombre_red: str, vlan_id: int) -> str: ...

    def eliminar_red(self, host_uri: str, nombre_red: str) -> None: ...

    def crear_vm(self, host_uri: str, nombre_vm: str, vcpu: int, ram_mb: int,
                 disco_gb: float, imagen: str, redes: list[tuple[str, int]],
                 macs: list[str] | None = None, semilla: dict[str, str] | None = None,
                 acceso: dict | None = None) -> str: ...

    def eliminar_vm(self, host_uri: str, nombre_vm: str) -> None: ...

    def asegurar_imagen(self, host_uri: str, imagen: str) -> bool: ...

    def liberar_imagen_si_huerfana(self, host_uri: str, imagen: str) -> bool: ...
