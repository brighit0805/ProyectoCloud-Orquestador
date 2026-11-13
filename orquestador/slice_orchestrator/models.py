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
    BORRADOR = "borrador"      # diseñado y persistido, sin desplegar ni reservar recursos
    CREANDO = "creando"
    ACTIVO = "activo"
    ERROR = "error"
    ELIMINANDO = "eliminando"
    ELIMINADO = "eliminado"


class TipoTopologia(str, Enum):
    """Topologías predefinidas del catálogo, más MIXTA para las composiciones.

    MIXTA no es una sexta forma del catálogo: es el tipo que recibe un slice
    construido a partir de una topología base más uno o más injertos, como la
    EX1-Topology (anillo de 4 nodos con una rama lineal injertada sobre VM4).
    """

    LINEAL = "lineal"
    ANILLO = "anillo"
    MALLA = "malla"
    ARBOL = "arbol"
    # Un único segmento de difusión compartido por todos los nodos (no es
    # punto a punto como las demás): se modela como una cadena de n-1
    # enlaces (igual que LINEAL) que comparten una sola VLAN/red -- ver
    # network_manager.asignar_redes() y direccionamiento.interfaces_de().
    BUS = "bus"
    MIXTA = "mixta"
    # Grafo editado a mano que ya no coincide con ninguna figura del catálogo
    # (R1B, composición de topologías arbitrarias).
    ARBITRARIA = "arbitraria"


@dataclass(frozen=True)
class Flavor:
    """Perfil inmutable de recursos de una VM.

    Los valores de ubuntu y cirros son los que especifica la EX1-Topology.
    """

    nombre: str
    vcpu: int
    ram_mb: int
    disco_gb: float
    imagen: str


CATALOGO_FLAVORS: dict[str, Flavor] = {
    "ubuntu": Flavor("ubuntu", vcpu=1, ram_mb=512, disco_gb=2.2,
                     imagen="ubuntu-22.04.qcow2"),
    "cirros": Flavor("cirros", vcpu=1, ram_mb=512, disco_gb=1.0,
                     imagen="cirros-0.6.2.qcow2"),
}

FLAVOR_POR_DEFECTO = "ubuntu"


def obtener_flavor(nombre: str) -> Flavor:
    """Devuelve el flavor del catálogo o lanza ValueError si no existe."""
    try:
        return CATALOGO_FLAVORS[nombre]
    except KeyError:
        disponibles = ", ".join(sorted(CATALOGO_FLAVORS))
        raise ValueError(
            f"El flavor '{nombre}' no está en el catálogo. Disponibles: {disponibles}"
        ) from None


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
    # --- campos del scheduler (Lab 7) ---
    capacidad_disco_gb: float = 0.0       # 0 = no informado (sin filtro por disco)
    disco_usado_gb: float = 0.0
    activo: bool = True                   # False = host en mantenimiento / caído
    ultima_medicion: float | None = None  # timestamp UNIX de la última lectura del monitor
    overcommit_vcpu: float = 1.0          # ratio de overcommit (ej: 4.0 = 4:1)
    overcommit_ram: float = 1.0

    @property
    def vcpu_disponibles(self) -> int:
        return self.capacidad_vcpu - self.vcpu_usados

    @property
    def ram_disponible_mb(self) -> int:
        return self.capacidad_ram_mb - self.ram_usados_mb

    @property
    def vcpu_efectivos(self) -> float:
        """vCPU totales teniendo en cuenta el overcommit."""
        return self.capacidad_vcpu * self.overcommit_vcpu

    @property
    def ram_efectiva_mb(self) -> float:
        """RAM total teniendo en cuenta el overcommit."""
        return self.capacidad_ram_mb * self.overcommit_ram

    @property
    def vcpu_disponibles_con_overcommit(self) -> float:
        return self.vcpu_efectivos - self.vcpu_usados

    @property
    def ram_disponible_con_overcommit_mb(self) -> float:
        return self.ram_efectiva_mb - self.ram_usados_mb

    @property
    def disco_disponible_gb(self) -> float:
        return self.capacidad_disco_gb - self.disco_usado_gb


@dataclass
class Nodo:
    """Una VM dentro de un slice."""
    id: str
    nombre: str
    vcpu: int = 1
    ram_mb: int = 512
    disco_gb: float = 5
    imagen: str = "generic-linux.qcow2"
    host_id: str | None = None       # lo asigna VM Placement
    estado: str = "pendiente"
    ip: str | None = None
    flavor: str = FLAVOR_POR_DEFECTO     # nombre del flavor del catálogo
    acceso_internet: bool = False        # salida a Internet y SSH entrante (R5)

    @classmethod
    def desde_flavor(cls, id: str, nombre: str, flavor: str,
                     acceso_internet: bool = False) -> "Nodo":
        """Crea el nodo tomando vcpu, RAM, disco e imagen del catálogo."""
        f = obtener_flavor(flavor)
        return cls(
            id=id, nombre=nombre,
            vcpu=f.vcpu, ram_mb=f.ram_mb, disco_gb=f.disco_gb, imagen=f.imagen,
            flavor=f.nombre, acceso_internet=acceso_internet,
        )


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
