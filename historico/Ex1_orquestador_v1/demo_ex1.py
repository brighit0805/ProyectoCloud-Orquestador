#!/usr/bin/env python3
"""
demo_ex1.py
------------
Demostración de las tres topologías que pide la presentación parcial:
las dos predefinidas (lineal y anillo) y la EX1-Topology.

La EX1-Topology es un anillo VM1-VM2-VM3-VM4-VM1 con una rama lineal
VM4-VM5-VM6 injertada, dos flavors distintos y salida a Internet
únicamente en VM1 y VM3. Se despliega con reparto round robin sobre tres
workers, tal como lo espera el procedimiento de validación.

Uso:
    python3 demo_ex1.py

Por defecto corre en modo SIMULADO, de modo que no toca ninguna
infraestructura. Para el despliegue real sobre el clúster, reemplazar
``inventario_laboratorio()`` con los URI verdaderos y construir el driver
con ``DriverLinux(modo=ModoDriver.REAL)``.
"""

from __future__ import annotations

import logging

from slice_orchestrator.driver_linux import DriverLinux, ModoDriver
from slice_orchestrator.models import Host, TipoTopologia
from slice_orchestrator.network_manager import VLANAllocator
from slice_orchestrator.orchestrator import (
    DespliegueFallidoError,
    desplegar_slice,
    desplegar_slice_existente,
    eliminar_slice,
)
from slice_orchestrator.topology_manager import (
    calcular_plan_despliegue,
    construir_ex1_topology,
)
from slice_orchestrator.vm_placement import EstrategiaPlacement

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

ANCHO = 78


# ---------------------------------------------------------------------------
# Utilidades de presentación
# ---------------------------------------------------------------------------
def titulo(texto: str) -> None:
    print()
    print("=" * ANCHO)
    print(f"  {texto}")
    print("=" * ANCHO)


def subtitulo(texto: str) -> None:
    print()
    print(f"-- {texto} " + "-" * max(0, ANCHO - len(texto) - 4))


def claves_cortas(slc) -> dict[str, str]:
    """nodo_id -> VM1, VM2, ... en el mismo orden del diagrama."""
    return {n.id: f"VM{i}" for i, n in enumerate(slc.nodos, 1)}


def mostrar_slice(slc) -> None:
    corto = claves_cortas(slc)

    subtitulo("Nodos")
    print(f"   {'VM':<5} {'flavor':<8} {'vCPU':>5} {'RAM':>8} {'disco':>8}  "
          f"{'host':<9} {'imagen':<22} internet")
    for nodo in slc.nodos:
        print(f"   {corto[nodo.id]:<5} {nodo.flavor:<8} {nodo.vcpu:>5} "
              f"{str(nodo.ram_mb) + ' MB':>8} {str(nodo.disco_gb) + ' GB':>8}  "
              f"{str(nodo.host_id or '-'):<9} {nodo.imagen:<22} "
              f"{'si' if nodo.acceso_internet else 'no'}")

    subtitulo("Enlaces y VLAN")
    for enlace in slc.enlaces:
        print(f"   {corto[enlace.nodo_origen]:<5} <-> {corto[enlace.nodo_destino]:<5} "
              f"VLAN {enlace.vlan_id}   {enlace.nombre_red}")


def mostrar_reparto(slc, hosts) -> None:
    subtitulo("Reparto sobre los workers")
    corto = claves_cortas(slc)
    for host in hosts:
        alojadas = [corto[n.id] for n in slc.nodos if n.host_id == host.id]
        print(f"   {host.id:<9} {', '.join(alojadas) or '(vacio)':<20} "
              f"{host.vcpu_usados}/{host.capacidad_vcpu} vCPU   "
              f"{host.ram_usados_mb}/{host.capacidad_ram_mb} MB RAM")


def mostrar_plan(slc) -> None:
    plan = calcular_plan_despliegue(slc)
    subtitulo("Plan de despliegue por olas")
    for i, ola in enumerate(plan["olas"], 1):
        acciones = {paso["accion"] for paso in ola}
        print(f"   Ola {i}: {len(ola):>2} operaciones en paralelo  ({', '.join(sorted(acciones))})")
    total = len(plan["orden_creacion"])
    print(f"   En serie serian {total} pasos; por olas, {len(plan['olas'])}.")


def inventario_laboratorio() -> list[Host]:
    """Tres workers del clúster Linux.

    Reemplazar usuario e IP por los del slice asignado al grupo antes de
    ejecutar en modo REAL. El orden de la lista es el que usa el reparto
    round robin: el primero es el worker 1.
    """
    return [
        Host("worker1", "worker1", "qemu+ssh://USUARIO@IP_WORKER1/system", 16, 32768, "az-lab"),
        Host("worker2", "worker2", "qemu+ssh://USUARIO@IP_WORKER2/system", 16, 32768, "az-lab"),
        Host("worker3", "worker3", "qemu+ssh://USUARIO@IP_WORKER3/system", 16, 32768, "az-lab"),
    ]


# ---------------------------------------------------------------------------
# Demostraciones
# ---------------------------------------------------------------------------
def demo_predefinidas(driver, allocator) -> None:
    titulo("TOPOLOGIAS PREDEFINIDAS")

    for slice_id, nombre, tipo, nodos in [
        ("slice-lineal", "demo-lineal", TipoTopologia.LINEAL, 4),
        ("slice-anillo", "demo-anillo", TipoTopologia.ANILLO, 4),
    ]:
        hosts = inventario_laboratorio()
        slc = desplegar_slice(
            slice_id, nombre, "grupo2@pucp.edu.pe", tipo, nodos, hosts, "az-lab",
            driver=driver, vlan_allocator=allocator,
        )
        print()
        print(f"   {tipo.value.upper()} — {len(slc.nodos)} nodos, "
              f"{len(slc.enlaces)} enlaces, estado {slc.estado.value}")
        corto = claves_cortas(slc)
        for enlace in slc.enlaces:
            print(f"      {corto[enlace.nodo_origen]} <-> {corto[enlace.nodo_destino]}"
                  f"   VLAN {enlace.vlan_id}")
        eliminar_slice(slc, hosts, driver, allocator)


def demo_ex1(driver, allocator) -> None:
    titulo("EX1-TOPOLOGY")

    hosts = inventario_laboratorio()

    # 1) Construccion: anillo de 4 + rama lineal de 2 injertada sobre VM4
    slc = construir_ex1_topology(
        slice_id="slice-ex1",
        nombre="demo-ex1",
        owner="grupo2@pucp.edu.pe",
        zona_disponibilidad="az-lab",
    )
    print(f"\n   Tipo resultante: {slc.tipo_topologia.value}  "
          f"({len(slc.nodos)} nodos, {len(slc.enlaces)} enlaces)")
    print("   Composicion: anillo VM1-VM2-VM3-VM4-VM1 + rama lineal VM4-VM5-VM6")
    mostrar_plan(slc)

    # 2) Despliegue con reparto round robin
    slc = desplegar_slice_existente(
        slc, hosts, driver, allocator, EstrategiaPlacement.ROUND_ROBIN
    )

    mostrar_slice(slc)
    mostrar_reparto(slc, hosts)

    subtitulo("Nodos con salida a Internet y SSH entrante")
    publicos = [f"VM{i}" for i, n in enumerate(slc.nodos, 1) if n.acceso_internet]
    print(f"   {', '.join(publicos)}   (el resto queda aislado)")

    # 3) Eliminacion y liberacion de recursos
    eliminar_slice(slc, hosts, driver, allocator)
    subtitulo("Despues de eliminar el slice")
    print(f"   estado          : {slc.estado.value}")
    print(f"   VMs en el driver: {len(driver.vms_simuladas)}")
    print(f"   redes activas   : {len(driver.redes_simuladas)}")
    print(f"   vCPU ocupados   : {sum(h.vcpu_usados for h in hosts)}")
    print(f"   RAM ocupada     : {sum(h.ram_usados_mb for h in hosts)} MB")


def demo_rollback() -> None:
    titulo("MANEJO DE ERRORES — rollback transaccional")

    hosts = inventario_laboratorio()
    # el driver falla a proposito en la quinta operacion, a mitad del despliegue
    driver = DriverLinux(fallar_en_operacion=5)
    allocator = VLANAllocator(3000, 3099)

    slc = construir_ex1_topology("slice-fallo", "demo-fallo", "grupo2@pucp.edu.pe", "az-lab")
    print("\n   Se fuerza un fallo del driver en la quinta operacion.")
    try:
        desplegar_slice_existente(slc, hosts, driver, allocator,
                                  EstrategiaPlacement.ROUND_ROBIN)
    except DespliegueFallidoError as exc:
        print(f"   Error capturado: {exc}")

    subtitulo("Estado tras el rollback")
    print(f"   estado del slice: {slc.estado.value}")
    print(f"   VMs huerfanas   : {len(driver.vms_simuladas)}")
    print(f"   redes huerfanas : {len(driver.redes_simuladas)}")
    print(f"   vCPU ocupados   : {sum(h.vcpu_usados for h in hosts)}")
    print(f"   VLAN reservadas : {len(allocator._reservas)}")
    print("\n   No queda ningun recurso a medio crear.")


if __name__ == "__main__":
    driver = DriverLinux(modo=ModoDriver.SIMULADO)
    allocator = VLANAllocator(inicio=3000, fin=3099)

    demo_predefinidas(driver, allocator)
    demo_ex1(driver, allocator)
    demo_rollback()

    print()
    print("=" * ANCHO)
    print("  Demostracion completada en modo SIMULADO.")
    print("=" * ANCHO)
