"""
network_manager.py
-------------------
Network Manager (submódulo de B) Orquestación).

Responsabilidad en esta primera versión de código: para cada Enlace del
slice, decidir el nombre de red/puente (bridge) que lo representará en
libvirt, y asignarle una VLAN dedicada (red tipo "provider", según
RF-26 del Entregable 1 — no túneles self-service).

Nota de alcance: en esta entrega no se implementan todavía los
sub-componentes de seguridad (Security Group Manager, Broadcast
Control) definidos en la arquitectura del Entregable 2 — el enunciado
de este laboratorio pide explícitamente "sin APIs, solo funciones" de
despliegue lineal/anillo; VLAN Allocator es el único sub-componente que
se necesita para que dos VMs en hosts distintos puedan verse por red.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Slice, TipoTopologia


@dataclass
class VLANAllocator:
    """Reserva VLAN sin repetirlas mientras el proceso esté activo.

    El rango debe reemplazarse por el asignado oficialmente al grupo.
    En una versión posterior, estas reservas se persistirán en la base
    de datos para coordinar varios procesos del orquestador.
    """

    inicio: int = 3000
    fin: int = 3999
    _reservas: dict[str, list[int]] = field(default_factory=dict)

    def reservar(self, slice_id: str, cantidad: int) -> list[int]:
        if slice_id in self._reservas:
            reservas = self._reservas[slice_id]
            if len(reservas) != cantidad:
                raise ValueError("El slice ya tiene una cantidad distinta de VLAN reservadas")
            return list(reservas)

        ocupadas = {vlan for valores in self._reservas.values() for vlan in valores}
        libres = [vlan for vlan in range(self.inicio, self.fin + 1) if vlan not in ocupadas]
        if len(libres) < cantidad:
            raise RuntimeError("No quedan VLAN suficientes en el rango configurado")
        self._reservas[slice_id] = libres[:cantidad]
        return list(self._reservas[slice_id])

    def liberar(self, slice_id: str) -> None:
        self._reservas.pop(slice_id, None)

    def restaurar(self, slice_id: str, vlans: list[int]) -> None:
        """Marca como ocupadas las VLAN de un slice ya desplegado (al reconstruir
        el estado desde el almacenamiento persistente)."""
        ocupadas = {v for k, valores in self._reservas.items() if k != slice_id for v in valores}
        repetidas = ocupadas.intersection(vlans)
        if repetidas:
            raise RuntimeError(f"VLAN ya reservadas por otro slice: {sorted(repetidas)}")
        self._reservas[slice_id] = list(vlans)


def asignar_redes(slc: Slice, allocator: VLANAllocator) -> dict[str, str]:
    """
    Genera un nombre de red único y determinístico por cada enlace del
    slice, con una VLAN dedicada. Devuelve un diccionario
    enlace_id -> nombre_red, que luego se usa tanto para crear los
    bridges (Fase de creación) como para conectar cada VM a su
    interfaz correcta (Driver Linux).
    """
    redes: dict[str, str] = {}

    if slc.tipo_topologia == TipoTopologia.BUS:
        # Un único dominio de difusión para todo el slice: UNA sola VLAN,
        # no una por enlace. Todos los pares de la cadena (ver
        # topology_manager._figura_a_pares) comparten el mismo nombre_red.
        vlan_id = allocator.reservar(slc.id, 1)[0]
        nombre_red = f"br-{slc.id}-bus-vlan{vlan_id}"
        for enlace in slc.enlaces:
            enlace.nombre_red = nombre_red
            enlace.vlan_id = vlan_id
            redes[enlace.id] = nombre_red
        return redes

    vlans = allocator.reservar(slc.id, len(slc.enlaces))
    for i, (enlace, vlan_id) in enumerate(zip(slc.enlaces, vlans)):
        nombre_red = f"br-{slc.id}-{i}-vlan{vlan_id}"
        enlace.nombre_red = nombre_red
        enlace.vlan_id = vlan_id
        redes[enlace.id] = nombre_red
    return redes


def redes_por_nodo(slc: Slice) -> dict[str, list[str]]:
    """
    Para cada nodo, devuelve la lista de nombres de red a los que debe
    conectarse (un nodo puede participar en 1 o 2 enlaces, según si es
    extremo o intermedio de la topología lineal, o siempre 2 en anillo).
    Esto es lo que el Driver Linux usa para saber cuántas interfaces
    crear por VM y a qué bridge conectar cada una.
    """
    resultado: dict[str, list[str]] = {n.id: [] for n in slc.nodos}
    for enlace in slc.enlaces:
        if enlace.nombre_red is None:
            raise RuntimeError(
                "Los enlaces deben tener red asignada antes de calcular "
                "las interfaces por nodo — llama primero a asignar_redes()."
            )
        # En un bus, varios pares de la cadena comparten el mismo nombre_red
        # (ver asignar_redes); sin el chequeo, un nodo intermedio quedaría
        # con DOS interfaces repetidas sobre la misma red en vez de una sola.
        if enlace.nombre_red not in resultado[enlace.nodo_origen]:
            resultado[enlace.nodo_origen].append(enlace.nombre_red)
        if enlace.nombre_red not in resultado[enlace.nodo_destino]:
            resultado[enlace.nodo_destino].append(enlace.nombre_red)
    return resultado
