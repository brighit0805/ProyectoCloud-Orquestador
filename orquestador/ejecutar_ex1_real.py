#!/usr/bin/env python3
"""Despliega la EX1-Topology en el clúster REAL y la deja activa para la validación.

Se ejecuta desde server4:
    python3 ejecutar_ex1_real.py            # sin interfaz de acceso a Internet
    python3 ejecutar_ex1_real.py --acceso   # VM1 y VM3 con interfaz hacia el headnode
Al terminar guarda estado_ex1.json (lo usan mostrar_vms.py y eliminar_ex1.py).
"""
import json
import sys

import config_cluster as cfg
from slice_orchestrator.driver_linux import DriverLinux, ModoDriver
from slice_orchestrator.models import Host
from slice_orchestrator.network_manager import VLANAllocator
from slice_orchestrator.orchestrator import desplegar_slice_existente
from slice_orchestrator.topology_manager import construir_ex1_topology
from slice_orchestrator.vm_placement import EstrategiaPlacement


def crear_entorno():
    hosts = [Host(w, w, cfg.uri(w), 16, 32768, "az-lab") for w in cfg.WORKERS]
    driver = DriverLinux(
        modo=ModoDriver.REAL, pool_almacenamiento=cfg.POOL_LIBVIRT,
        repo_imagenes=cfg.REPO_IMAGENES, estado_imagenes_path=cfg.ARCHIVO_ESTADO_IMAGENES,
        bridges_por_host={cfg.uri(w): d["bridge"] for w, d in cfg.WORKERS.items()})
    return hosts, driver, VLANAllocator(cfg.VLAN_INICIO, cfg.VLAN_FIN)


def main():
    con_acceso = "--acceso" in sys.argv
    hosts, driver, allocator = crear_entorno()
    print("Construyendo la EX1-Topology...")
    slc = construir_ex1_topology("slice-ex1", cfg.NOMBRE_SLICE, "grupo2@pucp.edu.pe", "az-lab")
    print("Desplegando en modo REAL (round robin)...")
    slc = desplegar_slice_existente(
        slc, hosts, driver, allocator, EstrategiaPlacement.ROUND_ROBIN,
        config_acceso=cfg.config_acceso() if con_acceso else None,
        reintentos=3, espera_reintento_s=3.0)

    print("\n===== DESPLIEGUE COMPLETADO =====")
    estado = {"slice": slc.id, "vms": []}
    for i, n in enumerate(slc.nodos, 1):
        print(f"VM{i} {n.nombre:<14} -> {n.host_id}  {n.imagen}  {n.vcpu} vCPU  {n.ram_mb} MB")
        estado["vms"].append({"nombre": n.nombre, "worker": n.host_id})
    with open("estado_ex1.json", "w") as f:
        json.dump(estado, f, indent=2)
    print("\nSlice ACTIVO. Siguiente: python3 mostrar_vms.py")


if __name__ == "__main__":
    main()
