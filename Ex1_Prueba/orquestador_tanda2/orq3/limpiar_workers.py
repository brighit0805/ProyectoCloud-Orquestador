#!/usr/bin/env python3
"""Deja los workers en el estado inicial que pide el paso 2 de la validación:
sin procesos QEMU y sin imágenes de la EX1 en el disco.

CUIDADO: borra las VMs demo-ex1-*, sus discos, sus semillas y las imágenes base
del pool (cirros / ubuntu). El orquestador las volverá a copiar al desplegar.
Requiere --confirmar para ejecutarse; sin él solo muestra qué haría.

    python3 limpiar_workers.py              # solo muestra
    python3 limpiar_workers.py --confirmar  # borra
"""
import os
import sys

import config_cluster as cfg
from cluster_utils import ssh


def main():
    ejecutar = "--confirmar" in sys.argv
    for w in cfg.WORKERS:
        print(f"\n== {w} ==")
        rc, doms = ssh(w, "virsh -c qemu:///system list --all --name")
        for d in [x for x in doms.split() if x.startswith(cfg.NOMBRE_SLICE)]:
            print(f"  VM {d}: destroy + undefine")
            if ejecutar:
                ssh(w, f"virsh -c qemu:///system destroy {d}; virsh -c qemu:///system undefine {d}")
        rc, vols = ssh(w, f"virsh -c qemu:///system vol-list {cfg.POOL_LIBVIRT}")
        nombres = [l.split()[0] for l in vols.splitlines() if l.split()]
        for v in nombres:
            if v.startswith(cfg.NOMBRE_SLICE) or v.startswith("seed-") or v in cfg.IMAGENES_BASE:
                print(f"  volumen {v}: borrar")
                if ejecutar:
                    ssh(w, f"virsh -c qemu:///system vol-delete --pool {cfg.POOL_LIBVIRT} {v}")
    if ejecutar and os.path.exists(cfg.ARCHIVO_ESTADO_IMAGENES):
        os.remove(cfg.ARCHIVO_ESTADO_IMAGENES)
    print("\n" + ("Limpieza hecha." if ejecutar else "Simulacro: agregue --confirmar para borrar de verdad."))


if __name__ == "__main__":
    main()
