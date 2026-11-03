#!/usr/bin/env python3
"""Muestra, por cada VM de la EX1: worker, estado, PID del proceso QEMU, y el
formato QCOW2 con su backing file (qemu-img info). Es lo que pide la validación.

    python3 mostrar_vms.py            # tabla de VMs y PIDs
    python3 mostrar_vms.py --disco    # además, qemu-img info de cada disco
"""
import sys

import config_cluster as cfg
from cluster_utils import nombres_vms, ssh, worker_de


def pid_qemu(worker: str, vm: str) -> str:
    for prefijo in ("", "sudo -n "):
        rc, out = ssh(worker, f"{prefijo}cat /run/libvirt/qemu/{vm}.pid")
        if rc == 0 and out.isdigit():
            return out
    rc, out = ssh(worker, f"pgrep -f 'guest={vm},' | head -1")
    return out if out.isdigit() else "?"


def estado(worker: str, vm: str) -> str:
    rc, out = ssh(worker, f"virsh -c qemu:///system domstate {vm}")
    return out if rc == 0 else "no existe"


def info_disco(worker: str, vm: str) -> str:
    ruta = f"{cfg.CARPETA_IMAGENES}/{vm}.qcow2"
    for prefijo in ("", "sudo -n "):
        rc, out = ssh(worker, f"{prefijo}qemu-img info --force-share {ruta}")
        if rc == 0:
            return out
    return f"(no se pudo leer {ruta}: {out})"


def main():
    print(f"{'VM':<14}{'WORKER':<10}{'ESTADO':<10}{'PID QEMU':<10}")
    print("-" * 44)
    for vm in nombres_vms():
        w = worker_de(vm)
        print(f"{vm:<14}{w:<10}{estado(w, vm):<10}{pid_qemu(w, vm):<10}")
    if "--disco" in sys.argv:
        for vm in nombres_vms():
            print(f"\n--- qemu-img info {vm} ({worker_de(vm)}) ---")
            print(info_disco(worker_de(vm), vm))


if __name__ == "__main__":
    main()
