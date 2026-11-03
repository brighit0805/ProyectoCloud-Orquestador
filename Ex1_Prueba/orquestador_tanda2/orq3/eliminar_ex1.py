#!/usr/bin/env python3
"""Elimina la EX1 de los workers con BORRADO INTELIGENTE.

Por cada VM borra el dominio, su disco derivado y su semilla. Después, en cada
worker, borra la copia de la imagen base SOLO si la copió el orquestador y ya
no queda ninguna VM que dependa de ella. Es idempotente.

    python3 eliminar_ex1.py
"""
import config_cluster as cfg
from cluster_utils import nombres_vms, worker_de
from ejecutar_ex1_real import crear_entorno


def main():
    _, driver, _ = crear_entorno()
    for vm in nombres_vms():
        w = worker_de(vm)
        print(f"Eliminando {vm} en {w}...")
        driver.eliminar_vm(cfg.uri(w), vm)
    print("\nBorrado inteligente de imágenes base:")
    for w in cfg.WORKERS:
        for imagen in cfg.IMAGENES_BASE:
            borrada = driver.liberar_imagen_si_huerfana(cfg.uri(w), imagen)
            print(f"  {w}: {imagen} -> {'BORRADA (ya sin VMs que la usen)' if borrada else 'se conserva (no la copió el orquestador o aún se usa)'}")
    print("\nListo. Compruebe con: python3 verificar_cluster.py")


if __name__ == "__main__":
    main()
