#!/usr/bin/env python3
"""Demostración local del despliegue lineal y anillo sin API HTTP."""

from __future__ import annotations

import logging

from slice_orchestrator.driver_linux import DriverLinux, ModoDriver
from slice_orchestrator.models import Host, TipoTopologia
from slice_orchestrator.network_manager import VLANAllocator
from slice_orchestrator.orchestrator import (
    DespliegueFallidoError,
    desplegar_slice,
    eliminar_slice,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def hosts_del_laboratorio() -> list[Host]:
    """Inventario de ejemplo; no contiene direcciones confirmadas.

    Antes de usar el modo real, el grupo debe reemplazar usuario, IP,
    capacidad y zona con los datos verificados de su slice VNRT.
    """
    return [
        Host("server-1", "server-1", "qemu+ssh://USUARIO@IP_SERVER1/system", 16, 32768, "az-lab"),
        Host("server-2", "server-2", "qemu+ssh://USUARIO@IP_SERVER2/system", 16, 32768, "az-lab"),
    ]


def mostrar_resultado(slc) -> None:
    print(f"\nSlice {slc.nombre} ({slc.tipo_topologia.value}) - {slc.estado.value}")
    for nodo in slc.nodos:
        print(f"  {nodo.nombre}: host={nodo.host_id}, {nodo.vcpu} vCPU, {nodo.ram_mb} MB")
    for enlace in slc.enlaces:
        print(f"  {enlace.nodo_origen} <-> {enlace.nodo_destino}: VLAN {enlace.vlan_id}")


if __name__ == "__main__":
    hosts = hosts_del_laboratorio()
    driver = DriverLinux(modo=ModoDriver.SIMULADO)
    allocator = VLANAllocator(inicio=3000, fin=3099)

    lineal = desplegar_slice(
        "slice-001", "lab-lineal", "a20213776@pucp.edu.pe",
        TipoTopologia.LINEAL, 4, hosts, "az-lab",
        driver=driver, vlan_allocator=allocator,
    )
    mostrar_resultado(lineal)

    anillo = desplegar_slice(
        "slice-002", "lab-anillo", "a20213776@pucp.edu.pe",
        TipoTopologia.ANILLO, 5, hosts, "az-lab",
        driver=driver, vlan_allocator=allocator,
    )
    mostrar_resultado(anillo)

    try:
        desplegar_slice(
            "slice-003", "lab-sobrecarga", "a20213776@pucp.edu.pe",
            TipoTopologia.LINEAL, 4,
            [Host("limitado", "limitado", "qemu:///system", 2, 2048, "az-lab")],
            "az-lab", vcpu_por_nodo=2,
            driver=driver, vlan_allocator=allocator,
        )
    except (ValueError, DespliegueFallidoError) as exc:
        print(f"\nFalla de capacidad detectada antes del despliegue: {exc}")

    eliminar_slice(lineal, hosts, driver, allocator)
    eliminar_slice(anillo, hosts, driver, allocator)
    print("\nLimpieza completada; capacidad y VLAN liberadas.")
