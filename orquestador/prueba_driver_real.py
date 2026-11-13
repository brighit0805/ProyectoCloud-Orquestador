"""
prueba_driver_real.py
----------------------
Prueba de humo del DriverOpenStack contra el cluster REAL (no simulado).

Solo hace operaciones de LECTURA (no crea, no borra nada):
  1. Autentica contra Keystone con las credenciales del .env.real
  2. Descubre los hosts reales (workers) vía Nova (os-hypervisors)
  3. Sincroniza el catalogo de imagenes consultando Glance
  4. Sincroniza el catalogo de flavors consultando Nova

Uso (en el HeadNode, dentro de orquestador/):
  . .env.real
  PYTHONPATH=. python3 prueba_driver_real.py
"""
from __future__ import annotations

import sys

from slice_orchestrator.driver_openstack import ConfigOpenStack, DriverOpenStack, ModoOpenStack


def main() -> int:
    cfg = ConfigOpenStack.desde_entorno()
    faltan = cfg.faltantes()
    if faltan:
        print(f"[ERROR] Faltan variables de entorno: {', '.join(faltan)}")
        print("        Revisa que hiciste '. .env.real' antes de correr este script.")
        return 1

    print("--- Configuracion ---")
    print(f"Keystone : {cfg.keystone}")
    print(f"Nova     : {cfg.nova}")
    print(f"Neutron  : {cfg.neutron}")
    print(f"Glance   : {cfg.glance}")
    print()

    driver = DriverOpenStack(modo=ModoOpenStack.REAL, config=cfg)

    print("--- 1) Autenticacion (token admin) ---")
    try:
        driver._admin()
        print("OK: token obtenido correctamente.")
    except Exception as exc:
        print(f"[ERROR] No se pudo autenticar: {exc}")
        return 1
    print()

    print("--- 2) Hosts descubiertos (Nova os-hypervisors) ---")
    try:
        hosts = driver.descubrir_hosts()
        if not hosts:
            print("[AVISO] descubrir_hosts() no devolvio ningun host.")
        for h in hosts:
            print(f"  {h.id:12s} zona={h.zona_disponibilidad:10s} "
                  f"vCPU={h.vcpu_usados}/{h.capacidad_vcpu}  "
                  f"RAM={h.ram_usados_mb}/{h.capacidad_ram_mb} MB")
    except Exception as exc:
        print(f"[ERROR] descubrir_hosts() fallo: {exc}")
        return 1
    print()

    print("--- 3) Sincronizando catalogo de imagenes (Glance) ---")
    try:
        mapeo_img = driver.sincronizar_catalogo_imagenes()
        print(f"  imagen_por_nombre = {mapeo_img}")
        print(f"  imagen_id_defecto = {driver.imagen_id_defecto}")
        if not mapeo_img and not driver.imagen_id_defecto:
            print("[AVISO] No hay ninguna imagen en Glance todavia.")
    except Exception as exc:
        print(f"[ERROR] sincronizar_catalogo_imagenes() fallo: {exc}")
        return 1
    print()

    print("--- 4) Sincronizando catalogo de flavors (Nova) ---")
    try:
        mapeo_flv = driver.sincronizar_catalogo_flavors()
        print(f"  flavor_por_perfil = {mapeo_flv}")
        print(f"  flavor_id_defecto = {driver.flavor_id_defecto}")
        if not mapeo_flv and not driver.flavor_id_defecto:
            print("[AVISO] No hay ningun flavor en Nova todavia.")
    except Exception as exc:
        print(f"[ERROR] sincronizar_catalogo_flavors() fallo: {exc}")
        return 1
    print()

    print("TODO OK: el driver real se conecta, descubre hosts, "
          "e identifica imagen/flavor automaticamente.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
