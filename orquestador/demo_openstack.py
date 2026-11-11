"""
demo_openstack.py
-----------------
Despliega y elimina un slice (anillo de 3 nodos) con el DriverOpenStack en modo
SIMULADO y muestra, en orden, cada petición REST que el driver enviaría a
Keystone, Neutron y Nova. No necesita clúster ni credenciales.

    python3 demo_openstack.py                  # despliegue + eliminación
    python3 demo_openstack.py --fallo          # falla en una operación y hace rollback
    python3 demo_openstack.py --fallo --reintentos 2   # la misma falla, pero se recupera
"""

import argparse
import json

from slice_orchestrator.driver_openstack import ConfigOpenStack, DriverOpenStack, SesionSimulada
from slice_orchestrator.models import TipoTopologia
from slice_orchestrator.network_manager import VLANAllocator
from slice_orchestrator.orchestrator import (
    DespliegueFallidoError,
    desplegar_slice_existente,
    eliminar_slice,
)
from slice_orchestrator.topology_manager import construir_topologia
from slice_orchestrator.vm_placement import EstrategiaPlacement


def mostrar(comandos, desde=0):
    for metodo, ruta, cuerpo in comandos[desde:]:
        resumen = json.dumps(cuerpo, ensure_ascii=False)[:90] if cuerpo else ""
        print(f"  {metodo:6s} {ruta:38s} {resumen}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fallo", action="store_true", help="inyecta una falla en la operación 6 del driver")
    ap.add_argument("--reintentos", type=int, default=1)
    args = ap.parse_args()

    cfg = ConfigOpenStack(dominio_id="dom", proyecto_admin_id="adm", usuario_admin_id="u-admin",
                          password_admin="-", rol_admin_id="rol-admin")
    driver = DriverOpenStack(config=cfg, imagen_id_defecto="<id-cirros>", flavor_id_defecto="<id-flavor>",
                             fallar_en_operacion=6 if args.fallo else None, espera_reintento_s=0.0)
    driver.sesion = SesionSimulada(cfg)
    sim = driver.sesion
    hosts = driver.descubrir_hosts()
    print("Hosts descubiertos:", [(h.id, h.uri_libvirt, h.capacidad_vcpu) for h in hosts])

    slc = construir_topologia("demo", "anillo-demo", "grupo2@pucp.edu.pe", TipoTopologia.ANILLO, 3)
    print("\n== DESPLIEGUE ==")
    try:
        desplegar_slice_existente(slc, hosts, driver, VLANAllocator(), EstrategiaPlacement.ROUND_ROBIN,
                                  reintentos=args.reintentos, espera_reintento_s=0.0)
    except DespliegueFallidoError as exc:
        print("Despliegue revertido:", exc)
    mostrar(sim.comandos)
    print(f"\nEstado: {slc.estado.value} | instancias={len(sim.servidores)} puertos={len(sim.puertos)} "
          f"redes={len(sim.redes) - 1} proyectos={len(sim.proyectos)}")

    if slc.estado.value == "activo":
        print("\n== ELIMINACIÓN ==")
        n = len(sim.comandos)
        eliminar_slice(slc, hosts, driver, VLANAllocator())
        mostrar(sim.comandos, n)
        print(f"\nEstado: {slc.estado.value} | instancias={len(sim.servidores)} puertos={len(sim.puertos)} "
              f"redes={len(sim.redes) - 1} proyectos={len(sim.proyectos)}")


if __name__ == "__main__":
    main()
