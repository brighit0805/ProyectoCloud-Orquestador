#!/usr/bin/env python3
import subprocess

vms = [
    ("VM1", "demo-ex1-vm1", "worker1", "10.0.10.1"),
    ("VM2", "demo-ex1-vm2", "worker2", "10.0.10.2"),
    ("VM3", "demo-ex1-vm3", "worker3", "10.0.10.3"),
    ("VM4", "demo-ex1-vm4", "worker1", "10.0.10.1"),
    ("VM5", "demo-ex1-vm5", "worker2", "10.0.10.2"),
    ("VM6", "demo-ex1-vm6", "worker3", "10.0.10.3"),
]

print("===== ESTADO REAL DEL SLICE EX1 =====")

for etiqueta, nombre, worker, ip in vms:
    cmd = [
        "ssh", f"ubuntu@{ip}",
        f"sudo cat /run/libvirt/qemu/{nombre}.pid"
    ]

    r = subprocess.run(cmd, capture_output=True, text=True)

    if r.returncode == 0:
        pid = r.stdout.strip()
        print(f"{etiqueta} -> {worker} ({ip}) | PID QEMU: {pid}")
    else:
        print(f"{etiqueta} -> {worker} ({ip}) | PID no disponible")
