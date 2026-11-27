"""
monitor_reconciliador.py
-------------------------
Monitoreo (HLD dominio D) -- reconciliación de estado DESEADO contra estado
REAL. Compara lo que el orquestador tiene registrado como ACTIVO contra lo
que el driver puede confirmar de verdad (``verificar_vm``), para detectar
que una VM se cayó o fue borrada/apagada por fuera del orquestador (p. ej.
directo con ``virsh destroy`` o ``openstack server delete``).

Es deliberadamente de SOLO LECTURA: reporta discrepancias, nunca redespliega
ni corrige nada por su cuenta -- esa decisión (y una posible reconciliación
automática) queda fuera del alcance de este curso (ver HLD §6.5, "Monitoreo"
como última fase).

Reutiliza el mismo método opcional ``verificar_vm(host_uri, nombre_vm)`` que
ya usa ``orchestrator.py`` para la verificación asíncrona post-creación (ver
``_verificar_vm_activa`` en ese archivo) -- devuelve "ACTIVE"/"SHUTOFF"/
"ERROR" en ambos drivers reales. ``DriverLinux`` también lo implementa en
modo SIMULADO (responde desde su registro interno), así este módulo se
puede probar y demostrar sin clúster real.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from slice_orchestrator.models import EstadoSlice, Nodo, Slice


@dataclass
class DiscrepanciaNodo:
    """Una VM del slice cuyo estado real no coincide con lo esperado (ACTIVE)."""
    nodo: str                  # clave corta, p.ej. 'vm2' (composicion.clave_de)
    nombre_vm: str
    host_id: str | None
    real: str                  # "SHUTOFF", "ERROR", "HOST_DESCONOCIDO", ...
    esperado: str = "ACTIVE"


@dataclass
class ReporteReconciliacion:
    """Resultado de reconciliar UN slice contra el estado real del driver."""
    slice_id: str
    nombre: str
    coincide: bool
    discrepancias: list[DiscrepanciaNodo] = field(default_factory=list)
    error: str | None = None   # p.ej. "el driver activo no soporta verificar_vm()"


def reconciliar(slices: Iterable[Slice], hosts_base, driver,
                clave_de: Callable[[Nodo], str]) -> list[ReporteReconciliacion]:
    """Compara cada slice ACTIVO contra lo que el driver confirma como real.

    ``hosts_base`` es la lista de ``Host`` del inventario (mismo patrón que
    ``control_broadcast``/``monitoreo_uso``: se arma ``{host.id: host.uri_libvirt}``).
    ``clave_de`` es ``composicion.clave_de``, inyectado para no crear un ciclo
    de imports entre este módulo y ``composicion.py``.

    No modifica NADA: solo lee (``verificar_vm``) y reporta. Si el driver
    activo no implementa ``verificar_vm`` (ninguno de los dos reales lo
    omite; solo pasaría con un driver nuevo/incompleto), cada slice ACTIVO
    se reporta con ``error`` en vez de intentar adivinar su estado -- nunca
    se asume "coincide" sin haber podido verificarlo de verdad."""
    slices = list(slices)
    if not hasattr(driver, "verificar_vm"):
        return [ReporteReconciliacion(
            slice_id=slc.id, nombre=slc.nombre, coincide=False,
            error="el driver activo no implementa verificar_vm(); no se puede "
                  "confirmar el estado real de sus VMs")
            for slc in slices if slc.estado == EstadoSlice.ACTIVO]

    uris = {h.id: h.uri_libvirt for h in hosts_base}
    reportes: list[ReporteReconciliacion] = []
    for slc in slices:
        if slc.estado != EstadoSlice.ACTIVO:
            continue
        discrepancias: list[DiscrepanciaNodo] = []
        for nodo in slc.nodos:
            host_uri = uris.get(nodo.host_id)
            if host_uri is None:
                discrepancias.append(DiscrepanciaNodo(
                    nodo=clave_de(nodo), nombre_vm=nodo.nombre, host_id=nodo.host_id,
                    real="HOST_DESCONOCIDO"))
                continue
            estado = driver.verificar_vm(host_uri, nodo.nombre)
            if estado != "ACTIVE":
                discrepancias.append(DiscrepanciaNodo(
                    nodo=clave_de(nodo), nombre_vm=nodo.nombre, host_id=nodo.host_id,
                    real=estado))
        reportes.append(ReporteReconciliacion(
            slice_id=slc.id, nombre=slc.nombre, coincide=not discrepancias,
            discrepancias=discrepancias))
    return reportes
