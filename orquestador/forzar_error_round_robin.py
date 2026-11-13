"""
forzar_error_round_robin.py
----------------------------
Variante de prueba_scheduler_real.py para FORZAR, a proposito, el escenario
de "slice reportado como activo con una VM real en ERROR" -- sin nada
simulado, contra el cluster real del curso.

Diferencia con prueba_scheduler_real.py: usa la estrategia ROUND_ROBIN
(la que expone el placement clasico) en vez de OBJETIVO (el scheduler
inteligente). ROUND_ROBIN (como BALANCEADO) NUNCA revisa el disco real al
elegir host -- solo vCPU y RAM -- asi que puede mandar dos VMs con disco
grande al mismo worker aunque ese worker no tenga espacio real para las dos.
Nova acepta la primera y rechaza la segunda con NoValidHost -> ERROR real,
mientras el orquestador (que hoy no verifica el estado real tras crear)
sigue reportando el slice como "activo".

Con 4 nodos en anillo y round_robin, el patron conocido (confirmado en la
EX1) es: VM1 y VM4 van al mismo host. Por eso el default es 4 nodos.

Uso (en el HeadNode, parado en ~/ProyectoCloud-Orquestador/orquestador,
con el entorno real cargado):

    . .env.real
    PYTHONPATH=. python3 forzar_error_round_robin.py

Este script NO borra nada solo.
"""
from __future__ import annotations

import argparse
import sys

from slice_orchestrator.driver_openstack import ConfigOpenStack, DriverOpenStack, ModoOpenStack
from slice_orchestrator.models import TipoTopologia
from slice_orchestrator import topology_manager, vm_placement, orchestrator


def construir_driver() -> DriverOpenStack:
    cfg = ConfigOpenStack.desde_entorno()
    faltan = cfg.faltantes()
    if faltan:
        print(f"[ERROR] Faltan variables de entorno: {', '.join(faltan)}")
        print("        Revisa que corriste '. .env.real' antes de este script.")
        sys.exit(1)
    return DriverOpenStack(modo=ModoOpenStack.REAL, config=cfg)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slice-id", default="forzar-error-1",
                         help="Identificador unico del slice (default: forzar-error-1)")
    parser.add_argument("--nombre", default="forzar-error",
                         help="Nombre base de las VMs (default: forzar-error)")
    parser.add_argument("--num-nodos", type=int, default=4,
                         help="Cantidad de VMs (default: 4, para que VM1 y VM4 "
                              "caigan en el mismo worker con round_robin y 3 workers)")
    parser.add_argument("--topologia", default="anillo",
                         choices=["lineal", "anillo", "malla", "arbol"],
                         help="Tipo de topologia del catalogo (default: anillo)")
    parser.add_argument("--imagen", default="cirros-0.6.2.qcow2",
                         help="Imagen a usar (default: cirros-0.6.2.qcow2)")
    parser.add_argument("--disco-gb", type=float, default=5.0,
                         help="Disco por VM en GB (default: 5.0). Con workers de "
                              "9 GB reales cada uno, dos VMs de 5 GB en el mismo "
                              "worker (10 GB pedidos) ya no caben -> fuerza ERROR "
                              "real en la segunda, aunque round_robin no se entere.")
    args = parser.parse_args()

    tipo = TipoTopologia(args.topologia)
    driver = construir_driver()

    print("=" * 70)
    print("1) DESCUBRIENDO HOSTS REALES (Nova os-hypervisors / Placement)")
    print("=" * 70)
    hosts = driver.descubrir_hosts()
    for h in hosts:
        print(f"  {h.id:12s} vCPU {h.vcpu_usados}/{h.capacidad_vcpu}  "
              f"RAM {h.ram_usados_mb}/{h.capacidad_ram_mb} MB")
    print()

    print("=" * 70)
    print(f"2) CONSTRUYENDO TOPOLOGIA (slice_id={args.slice_id}, "
          f"{args.num_nodos} VMs, {args.topologia}, disco={args.disco_gb} GB c/u)")
    print("=" * 70)
    slc = topology_manager.construir_topologia(
        slice_id=args.slice_id,
        nombre=args.nombre,
        owner="brighit",
        tipo=tipo,
        num_nodos=args.num_nodos,
        vcpu_por_nodo=1,
        ram_mb_por_nodo=512,
        imagen=args.imagen,
    )
    for n in slc.nodos:
        n.disco_gb = args.disco_gb
    for n in slc.nodos:
        print(f"  {n.nombre}: {n.vcpu} vCPU / {n.ram_mb} MB / {n.disco_gb} GB / {n.imagen}")
    print()

    print("=" * 70)
    print("3) DESPLEGANDO con estrategia ROUND_ROBIN (ignora disco a proposito)")
    print("=" * 70)
    try:
        slc = orchestrator.desplegar_slice_existente(
            slc, hosts,
            driver=driver,
            estrategia=vm_placement.EstrategiaPlacement.ROUND_ROBIN,
        )
    except orchestrator.DespliegueFallidoError as exc:
        print(f"[INFO] El despliegue fallo y el orquestador SI lo detecto: {exc}")
        print("       (si ves este mensaje, el rollback automatico ya existe)")
        return 1

    print(f"Estado que REPORTA el orquestador: {slc.estado}")
    for n in slc.nodos:
        print(f"  {n.nombre}: host={n.host_id} estado_reportado={n.estado}")
    print()

    print("=" * 70)
    print("4) VERIFICAR EL ESTADO REAL EN OPENSTACK (correr en otra terminal)")
    print("=" * 70)
    print(f"  openstack server list --all-projects -c Name -c Status -c Flavor | grep {args.nombre}")
    print()
    print("Si el orquestador dijo arriba un estado 'activo'/'ACTIVO' pero aqui "
          "aparece alguna VM en ERROR, confirmamos el bug: el placement clasico "
          "ignora disco y el orquestador no verifica el estado real tras crear.")
    print()
    print("Este script NO borra nada solo. Para limpiar despues, usa "
          "'openstack server delete <id>' con los IDs de 'server list --all-projects'.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
