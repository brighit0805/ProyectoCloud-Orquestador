"""
vm_placement.py
----------------
VM Placement (submódulo de B) Orquestación).

Responsabilidad: dado el plan de Topology Manager, decidir en qué host
físico concreto va cada VM del slice, dentro de una zona de
disponibilidad. Es el módulo que finalmente entrega la orden al
DriverManager (ver arquitectura del Entregable 2).

Criterio usado en este entregable (RF-19/RF-25 del Entregable 1):
balanceo de carga simple por vCPU/RAM disponibles entre los hosts de
la zona de disponibilidad solicitada. No se optimiza más allá de eso
para no sobre-complicar la primera versión de código.
"""

from __future__ import annotations

from .models import Slice, Host


def filtrar_hosts_por_zona(hosts: list[Host], zona: str | None) -> list[Host]:
    """Devuelve solo los hosts que pertenecen a la zona pedida.
    Si no se especifica zona, se consideran todos los hosts disponibles."""
    if zona is None:
        return list(hosts)
    candidatos = [h for h in hosts if h.zona_disponibilidad == zona]
    if not candidatos:
        raise ValueError(f"No hay hosts registrados en la zona de disponibilidad '{zona}'")
    return candidatos


def _host_con_mas_capacidad(hosts: list[Host]) -> Host:
    """Balanceo simple: elige el host con más vCPU disponibles;
    en empate, el que tenga más RAM disponible."""
    return max(hosts, key=lambda h: (h.vcpu_disponibles, h.ram_disponible_mb))


def asignar_hosts(slc: Slice, hosts: list[Host]) -> dict[str, str]:
    """
    Asigna un host a cada nodo del slice, distribuyendo la carga entre
    los servidores disponibles de la zona de disponibilidad del slice.

    No modifica `slc` directamente aquí: devuelve el mapeo nodo_id -> host_id
    para que el llamador decida cuándo confirmarlo (permite simular el
    placement antes de comprometerse a él, útil para pruebas).

    Lanza ValueError si algún nodo no cabe en ningún host disponible,
    de forma que el orquestador pueda abortar ANTES de tocar la
    infraestructura (evita crear recursos huérfanos).
    """
    candidatos = filtrar_hosts_por_zona(hosts, slc.zona_disponibilidad)

    # trabajamos sobre copias para no mutar los objetos Host reales
    # hasta que el despliegue se confirme
    disponibles = {
        h.id: Host(**{**h.__dict__})
        for h in candidatos
    }

    asignacion: dict[str, str] = {}
    for nodo in slc.nodos:
        elegibles = [
            h for h in disponibles.values()
            if h.vcpu_disponibles >= nodo.vcpu and h.ram_disponible_mb >= nodo.ram_mb
        ]
        if not elegibles:
            raise ValueError(
                f"No hay host con capacidad suficiente para el nodo '{nodo.nombre}' "
                f"(requiere {nodo.vcpu} vCPU / {nodo.ram_mb} MB RAM)"
            )
        elegido = _host_con_mas_capacidad(elegibles)
        elegido.vcpu_usados += nodo.vcpu
        elegido.ram_usados_mb += nodo.ram_mb
        asignacion[nodo.id] = elegido.id

    return asignacion


def confirmar_asignacion(slc: Slice, hosts: list[Host], asignacion: dict[str, str]) -> None:
    """Aplica la asignación calculada: escribe host_id en cada nodo del
    slice y descuenta la capacidad usada en los hosts reales (los
    mismos objetos que use el resto del sistema, para que el
    inventario quede consistente)."""
    hosts_por_id = {h.id: h for h in hosts}
    for nodo in slc.nodos:
        host_id = asignacion[nodo.id]
        nodo.host_id = host_id
        host = hosts_por_id[host_id]
        host.vcpu_usados += nodo.vcpu
        host.ram_usados_mb += nodo.ram_mb


def liberar_asignacion(slc: Slice, hosts: list[Host]) -> None:
    """Devuelve al inventario los recursos comprometidos por un slice.

    Es idempotente: luego de liberar, limpia ``host_id`` para evitar un
    segundo descuento accidental durante un reintento de eliminación.
    """
    hosts_por_id = {h.id: h for h in hosts}
    for nodo in slc.nodos:
        if nodo.host_id is None:
            continue
        host = hosts_por_id.get(nodo.host_id)
        if host is not None:
            host.vcpu_usados = max(0, host.vcpu_usados - nodo.vcpu)
            host.ram_usados_mb = max(0, host.ram_usados_mb - nodo.ram_mb)
        nodo.host_id = None
