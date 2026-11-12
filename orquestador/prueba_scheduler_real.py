"""
prueba_scheduler_real.py
-------------------------
Demostracion END-TO-END para la reunion con German: despliega un slice real
(scheduler inteligente + driver OpenStack) contra el cluster, usando el flujo
AUTOMATIZADO completo del orquestador (una sola llamada), tal como lo haria
el usuario final.

No es una simulacion: crea VMs reales en Nova, con el host elegido por el
scheduler (filtros + pesadores + funcion objetivo) y la imagen/flavor
resueltos automaticamente (sin mapeo manual).

Uso (en el HeadNode, parado en ~/ProyectoCloud-Orquestador/orquestador,
rama scheduler-objetivo, con el entorno real cargado):

    . .env.real
    PYTHONPATH=. python3 prueba_scheduler_real.py

Parametros opcionales:
    PYTHONPATH=. python3 prueba_scheduler_real.py --slice-id demo-german-1 --num-nodos 2

Al final imprime:
  - el estado del slice (debe decir ACTIVO)
  - la traza de decision del scheduler (que host eligio y por que)
  - el comando exacto para verificarlo en OpenStack
  - el comando exacto para BORRARLO (no borra nada automaticamente)
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
    parser.add_argument("--slice-id", default="demo-german-1",
                         help="Identificador unico del slice (default: demo-german-1)")
    parser.add_argument("--nombre", default="demo-german",
                         help="Nombre base de las VMs (default: demo-german)")
    parser.add_argument("--num-nodos", type=int, default=2,
                         help="Cantidad de VMs en la topologia (default: 2)")
    parser.add_argument("--topologia", default="lineal",
                         choices=["lineal", "anillo", "malla", "arbol"],
                         help="Tipo de topologia del catalogo (default: lineal). "
                              "Bus no esta disponible (no implementado en el sistema).")
    parser.add_argument("--imagen", default="cirros-0.6.2.qcow2",
                         help="Imagen a usar (default: cirros-0.6.2.qcow2)")
    parser.add_argument("--disco-gb", type=float, default=5.0,
                         help="Disco por VM en GB (default: 5.0). Bajalo (ej. 1.0) "
                              "si el FiltroCapacidadDisco rechaza el despliegue por "
                              "falta de disco real en los workers.")
    args = parser.parse_args()

    tipo = TipoTopologia(args.topologia)
    driver = construir_driver()

    print("=" * 70)
    print("1) DESCUBRIENDO HOSTS REALES (Nova os-hypervisors)")
    print("=" * 70)
    hosts = driver.descubrir_hosts()
    for h in hosts:
        print(f"  {h.id:12s} vCPU {h.vcpu_usados}/{h.capacidad_vcpu}  "
              f"RAM {h.ram_usados_mb}/{h.capacidad_ram_mb} MB")
    print()

    print("=" * 70)
    print(f"2) CONSTRUYENDO TOPOLOGIA (slice_id={args.slice_id}, "
          f"{args.num_nodos} VMs, {args.topologia})")
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
    # El modelo Nodo trae 5 GB de disco por defecto (ver models.py); lo
    # sobreescribimos aqui para poder pedir VMs mas chiquitas sin tocar el
    # catalogo de flavors (sugerencia de German: probar con disco reducido
    # mientras el cluster real tiene poco disco libre por worker).
    for n in slc.nodos:
        n.disco_gb = args.disco_gb
    for n in slc.nodos:
        print(f"  {n.nombre}: {n.vcpu} vCPU / {n.ram_mb} MB / {n.disco_gb} GB / {n.imagen}")
    print()

    print("=" * 70)
    print("3) DESPLEGANDO (scheduler OBJETIVO + driver real, UNA sola llamada)")
    print("=" * 70)
    try:
        slc = orchestrator.desplegar_slice_existente(
            slc, hosts,
            driver=driver,
            estrategia=vm_placement.EstrategiaPlacement.OBJETIVO,
        )
    except orchestrator.DespliegueFallidoError as exc:
        print(f"[ERROR] El despliegue fallo y se revirtio solo: {exc}")
        return 1

    print(f"Estado final del slice: {slc.estado}")
    print()

    print("=" * 70)
    print("4) TRAZA DEL SCHEDULER (por que eligio cada host)")
    print("=" * 70)
    resultado = vm_placement.obtener_ultimo_resultado()
    if resultado:
        print(resultado.explicacion_texto())
    print()

    print("=" * 70)
    print("5) VERIFICAR EN OPENSTACK (correr en otra terminal / salir de python)")
    print("=" * 70)
    print("  openstack server list --all-projects | grep " + args.nombre)
    print()
    print("Para BORRAR las VMs y el proyecto creados por esta prueba, usa el CLI")
    print("de OpenStack directamente con los IDs que te de 'server list --all-projects'")
    print("(ver guion de limpieza aparte); este script NO borra nada solo.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
