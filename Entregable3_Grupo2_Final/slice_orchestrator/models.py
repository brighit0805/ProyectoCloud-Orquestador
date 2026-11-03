"""
models.py
---------
Entidades del dominio del orquestador, ya definidas en el Entregable 1
(Slice, Nodo/VM, Enlace, Host) y usadas por todos los módulos del
Entregable 3. Son simples dataclasses: no hay lógica de negocio aquí,
solo la forma de los datos que viajan entre Topology Manager,
VM Placement, Network Manager y los Drivers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EstadoSlice(str, Enum):
    CREANDO = "creando"
    ACTIVO = "activo"
    ERROR = "error"
    ELIMINANDO = "eliminando"
    ELIMINADO = "eliminado"


class TipoTopologia(str, Enum):
    LINEAL = "lineal"
    ANILLO = "anillo"


@dataclass
class Host:
    """Servidor físico Linux del clúster (Dell PowerEdge R630 en el proyecto real)."""
    id: str
    nombre: str
    uri_libvirt: str          # ej: "qemu+ssh://user@10.60.3.11/system"
    capacidad_vcpu: int
    capacidad_ram_mb: int
    zona_disponibilidad: str = "az-default"
    vcpu_usados: int = 0
    ram_usados_mb: int = 0

    @property
    def vcpu_disponibles(self) -> int:
        return self.capacidad_vcpu - self.vcpu_usados

    @property
    def ram_disponible_mb(self) -> int:
        return self.capacidad_ram_mb - self.ram_usados_mb


@dataclass
class Nodo:
    """Una VM dentro de un slice."""
    id: str
    nombre: str
    vcpu: int = 1
    ram_mb: int = 512
    disco_gb: int = 5
    imagen: str = "generic-linux.qcow2"
    host_id: str | None = None       # lo asigna VM Placement
    estado: str = "pendiente"
    ip: str | None = None


@dataclass
class Enlace:
    """Conexión L2 entre dos nodos del slice."""
    id: str
    nodo_origen: str   # id de Nodo
    nodo_destino: str  # id de Nodo
    nombre_red: str | None = None    # lo asigna Network Manager
    vlan_id: int | None = None       # VLAN lógica única del enlace


@dataclass
class Slice:
    id: str
    nombre: str
    owner: str
    tipo_topologia: TipoTopologia
    nodos: list[Nodo] = field(default_factory=list)
    enlaces: list[Enlace] = field(default_factory=list)
    estado: EstadoSlice = EstadoSlice.CREANDO
    zona_disponibilidad: str | None = None

    def nodo(self, nodo_id: str) -> Nodo:
        for n in self.nodos:
            if n.id == nodo_id:
                return n
        raise KeyError(f"Nodo {nodo_id} no existe en el slice {self.id}")
