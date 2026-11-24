#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vm-placement.py
----------------
Entregable del Lab 7 (TEL141, Grupo 2): módulo de VM Placement con la
interfaz exacta que pide la guía (VMRequest -> PlacementDecision),
invocable como script independiente:

    python vm-placement.py input-data.json

IMPORTANTE (honestidad de implementación): este script NO reinventa el
algoritmo de colocación. Es una capa delgada que arma un VMRequest de una
sola VM y llama a la MISMA lógica real que usa el proyecto -- el Scheduler
y asignar_hosts() de slice_orchestrator/vm_placement.py, ya validados
contra el clúster real (ver TEL141_LAB7_IP_G02_completo.docx, casos
3.1.1-3.1.5). Para que el import funcione, este archivo debe vivir junto a
la carpeta 'slice_orchestrator/' (al mismo nivel que cli.py), es decir
dentro de 'orquestador/'.

Para OpenStack: este script corre offline (sin credenciales de Nova), así
que no inventa un nombre de host -- construye los scheduler_hints
(availability_zone) que el proyecto ya usa para forzar la zona, y deja
explícito que el host final dentro de esa zona lo asigna Nova. Esto es
consistente con la arquitectura real: el orquestador decide la zona, Nova
decide el host (ver Informe Previo, pregunta 4.c).

Nota sobre 'etiquetas' (afinidad/anti-afinidad): el proyecto real YA
implementa afinidad/anti-afinidad, pero a nivel de SLICE COMPLETO (mira la
topología -- qué nodos están conectados entre sí -- para decidir quién va
cerca de quién). Esa es, de hecho, la forma correcta según la rúbrica
oficial del proyecto ("el algoritmo considera slices como un todo, no VMs
individuales"). Esta guía, en cambio, pide decidir UNA VM a la vez, sin
slice ni topología alrededor -- así que no hay nada estructural que mirar.
Por eso aquí se implementa una versión simplificada y explícita (una
etiqueta libre en el VMRequest, no inferida de ninguna topología), solo
para cumplir el formato puntual que pide este ejercicio -- no reemplaza ni
compite con la afinidad real del proyecto.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# --- hacer importable slice_orchestrator/ sin tocar el paquete real ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from slice_orchestrator.models import Host, Nodo, Slice, TipoTopologia  # noqa: E402
from slice_orchestrator.vm_placement import (  # noqa: E402
    asignar_hosts, EstrategiaPlacement,
)


# --------------------------------------------------------------- interfaz
@dataclass
class VMRequest:
    """Entrada del módulo (tal como la pide la guía del Lab 7)."""
    cpu: int                          # vCPUs requeridas
    ram: int                          # RAM requerida, en MB
    plataforma: str                   # "linux" | "openstack"
    zone: Optional[str] = None        # zona de disponibilidad (opcional en Linux,
                                       # obligatoria en OpenStack, por diseño del proyecto)
    disco: int = 0                    # disco requerido, en GB (0 = no especificado, no filtra)
    etiquetas: dict[str, Any] = field(default_factory=dict)   # metadatos personalizados (ver Tips abajo)
    nombre: str = "vm-solicitada"      # etiqueta/metadato libre, para trazabilidad

    # Claves soportadas dentro de 'etiquetas' (todas opcionales):
    #   evitar_hosts: list[str]  -- anti-afinidad simplificada: no colocar en estos hosts
    #   preferir_host: str       -- afinidad simplificada: usar este host si tiene capacidad
    #   requiere_acceso_internet: bool -- bandera de política de seguridad simplificada
    #                              (se registra en la salida; el enforcement real de reglas
    #                              de seguridad vive en Network Manager / R5 del proyecto,
    #                              no en VM Placement)


@dataclass
class PlacementDecision:
    """Salida del módulo (tal como la pide la guía del Lab 7)."""
    host: Optional[str]               # host elegido (None si se rechaza o si lo decide Nova)
    plataforma: str                   # plataforma usada
    args: dict[str, Any] = field(default_factory=dict)   # argumentos adicionales


# ------------------------------------------------ inventario simulado Linux
# Mismo esquema de capacidad que usa el resto del proyecto en modo
# simulado (3 workers, 16 vCPU / 32768 MB, zona "az-lab").
def _inventario_linux_por_defecto() -> list[Host]:
    return [
        Host(id=f"worker{i}", nombre=f"worker{i}",
             uri_libvirt=f"qemu+ssh://ubuntu@worker{i}/system",
             capacidad_vcpu=16, capacidad_ram_mb=32768,
             zona_disponibilidad="az-lab")
        for i in (1, 2, 3)
    ]


def _host_desde_dict(d: dict) -> Host:
    return Host(
        id=d["id"], nombre=d.get("nombre", d["id"]),
        uri_libvirt=d.get("uri_libvirt", f"qemu+ssh://ubuntu@{d['id']}/system"),
        capacidad_vcpu=d["capacidad_vcpu"], capacidad_ram_mb=d["capacidad_ram_mb"],
        zona_disponibilidad=d.get("zona_disponibilidad", "az-lab"),
        vcpu_usados=d.get("vcpu_usados", 0), ram_usados_mb=d.get("ram_usados_mb", 0),
        capacidad_disco_gb=d.get("capacidad_disco_gb", 0.0),
        disco_usado_gb=d.get("disco_usado_gb", 0.0),
    )


# --------------------------------------------------------- función principal
def decide_vm_placement(request: VMRequest,
                         inventario_linux: Optional[list[Host]] = None) -> PlacementDecision:
    """Punto de entrada principal del módulo (lo que pide la guía).

    Recibe un VMRequest, invoca internamente la lógica real según la
    plataforma, y retorna un PlacementDecision.
    """
    etiquetas = request.etiquetas or {}
    nota_seguridad = None
    if etiquetas.get("requiere_acceso_internet"):
        # Reglas de seguridad simplificadas (la guía lo permite simplificado):
        # aquí solo se registra la necesidad; el enforcement real (reglas de
        # firewall/Security Groups) lo hace Network Manager (R5), no este módulo.
        nota_seguridad = ("VM marcada con requiere_acceso_internet=True: se debe aplicar "
                           "la política de salida/entrada a Internet correspondiente "
                           "(ver módulo Network Manager / R5 del proyecto; este módulo "
                           "de Placement solo registra la bandera, no la aplica).")

    if request.plataforma == "linux":
        hosts = list(inventario_linux) if inventario_linux is not None else _inventario_linux_por_defecto()

        # Anti-afinidad simplificada: descartar hosts explícitamente evitados.
        evitar = set(etiquetas.get("evitar_hosts", []))
        if evitar:
            hosts = [h for h in hosts if h.id not in evitar]
            if not hosts:
                return PlacementDecision(
                    host=None, plataforma="linux",
                    args={"rechazado": True,
                          "motivo": f"Todos los hosts candidatos están en evitar_hosts={sorted(evitar)}"},
                )

        # Afinidad simplificada: si se pide un host preferido y tiene capacidad
        # real (CPU/RAM/disco), se usa directo sin pasar por el balanceo general.
        preferido = etiquetas.get("preferir_host")
        if preferido:
            for h in hosts:
                cabe_disco = request.disco <= 0 or h.capacidad_disco_gb <= 0 or h.disco_disponible_gb >= request.disco
                if h.id == preferido and h.vcpu_disponibles >= request.cpu \
                        and h.ram_disponible_mb >= request.ram and cabe_disco:
                    args = {"estrategia": "afinidad explícita (preferir_host)"}
                    if nota_seguridad:
                        args["nota_seguridad"] = nota_seguridad
                    return PlacementDecision(host=h.id, plataforma="linux", args=args)

        slc = Slice(
            id="lab7-demo", nombre="lab7-demo", owner="lab7",
            tipo_topologia=TipoTopologia.LINEAL,
            nodos=[Nodo(id="vm1", nombre=request.nombre, vcpu=request.cpu,
                        ram_mb=request.ram, disco_gb=request.disco)],
            zona_disponibilidad=request.zone,
        )
        try:
            # Reutiliza EXACTAMENTE la misma función que usa el orquestador real
            # (incluye el filtro de disco, FiltroCapacidadDisco, ya existente ahí).
            asignacion = asignar_hosts(slc, hosts, estrategia=EstrategiaPlacement.OBJETIVO)
        except ValueError as exc:
            return PlacementDecision(
                host=None, plataforma="linux",
                args={"rechazado": True, "motivo": str(exc)},
            )
        args = {"estrategia": "objetivo (Scheduler real: filtros CPU/RAM/disco + funcion objetivo)"}
        if nota_seguridad:
            args["nota_seguridad"] = nota_seguridad
        return PlacementDecision(host=asignacion["vm1"], plataforma="linux", args=args)

    elif request.plataforma == "openstack":
        # Decisión de diseño del proyecto (ver Informe Previo, 3.b): la zona
        # de disponibilidad es OBLIGATORIA para desplegar en OpenStack.
        if not request.zone:
            return PlacementDecision(
                host=None, plataforma="openstack",
                args={
                    "rechazado": True,
                    "motivo": (
                        "La zona de disponibilidad es obligatoria para desplegar en "
                        "OpenStack (regla de diseño del proyecto, validada contra el "
                        "clúster real el 13-nov -- ver caso 3.1.2)."
                    ),
                },
            )
        args = {
            "scheduler_hints": {"availability_zone": request.zone},
            "nota": (
                "El orquestador fuerza la zona; el host concreto dentro de esa "
                "zona lo asigna Nova (Placement API) al desplegar -- este script "
                "corre offline y no tiene credenciales reales de Nova."
            ),
        }
        if request.disco > 0:
            args["nota_disco"] = (
                f"disco={request.disco} GB se resuelve con el flavor que arma el driver "
                "OpenStack real (auto-creado o existente); Nova no recibe el disco "
                "como scheduler_hint, lo recibe como parte del flavor."
            )
        if nota_seguridad:
            args["nota_seguridad"] = nota_seguridad
        return PlacementDecision(host=None, plataforma="openstack", args=args)

    else:
        raise ValueError(f"Plataforma desconocida: {request.plataforma!r} (use 'linux' u 'openstack')")


# --------------------------------------------------------------------- CLI
def _imprimir_caso(numero: int, nombre_caso: str, req: VMRequest, decision: PlacementDecision) -> None:
    print(f"\n--- Caso {numero}: {nombre_caso} ---")
    print("Input  (VMRequest):     ", asdict(req))
    print("Output (PlacementDecision):", asdict(decision))


def main() -> None:
    if len(sys.argv) != 2:
        print("Uso: python vm-placement.py input-data.json", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        casos = json.load(f)

    for i, caso in enumerate(casos, start=1):
        nombre_caso = caso.get("nombre_caso", f"caso {i}")
        inventario_demo = caso.get("inventario_demo")
        hosts_demo = [_host_desde_dict(h) for h in inventario_demo] if inventario_demo else None

        req = VMRequest(
            cpu=caso["cpu"], ram=caso["ram"], plataforma=caso["plataforma"],
            zone=caso.get("zone"), disco=caso.get("disco", 0),
            etiquetas=caso.get("etiquetas", {}),
            nombre=caso.get("nombre", "vm-solicitada"),
        )
        decision = decide_vm_placement(req, inventario_linux=hosts_demo)
        _imprimir_caso(i, nombre_caso, req, decision)


if __name__ == "__main__":
    main()
